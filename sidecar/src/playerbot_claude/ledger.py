"""The MySQL budget ledger: the durable half of admission.

The decisions live in :mod:`playerbot_claude.budget` and are pure. This module owns
only the parts that genuinely need a database: reading committed totals under a lock,
inserting a reservation, and settling or releasing one. That split is deliberate. A
rule embedded in a transaction is a rule the tests can reach only through a
transaction, and the arithmetic is the part that has to be right.

Concurrency rests on one row. Every reservation for a given UTC day serializes on
``SELECT ... FOR UPDATE`` of that day's row, so two requests deciding at the same
instant cannot both read the same remaining budget and both spend it. The day row is
created on demand and never deleted, because a row that can vanish is a lock that can
be skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from playerbot_claude import budget
from playerbot_claude.budget import AdmissionDecision, BudgetState, RequestPriority

# How long a reservation may sit unsettled before another transaction may reclaim it.
#
# This is the crash recovery window. A sidecar that dies between reserving and settling
# leaves money committed that will never be spent, and without expiry that money is lost
# from the day's budget until midnight. Long enough that a slow provider call is never
# reclaimed underneath itself.
RESERVATION_EXPIRY = timedelta(minutes=10)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_budget_day (
        `usage_date` DATE NOT NULL,
        `settled_nano` BIGINT UNSIGNED NOT NULL DEFAULT 0,
        `circuit_open` TINYINT UNSIGNED NOT NULL DEFAULT 0,
        `circuit_reason` VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (`usage_date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='One row per UTC day. Every reservation serializes on it.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_budget_reservation (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `usage_date` DATE NOT NULL,
        `request_id` BIGINT UNSIGNED NOT NULL,
        `attempt` INT UNSIGNED NOT NULL DEFAULT 1,
        `priority` ENUM('immediate_human','background') NOT NULL,
        `max_cost_nano` BIGINT UNSIGNED NOT NULL,
        `actual_cost_nano` BIGINT UNSIGNED NULL,
        `state` ENUM('reserved','settled','released') NOT NULL DEFAULT 'reserved',
        `created_at` DATETIME NOT NULL,
        `settled_at` DATETIME NULL,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_request_attempt` (`request_id`, `attempt`),
        KEY `ix_open` (`usage_date`, `state`, `created_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Every retry gets its own row, so each has its own reservation and cost.'
    """,
)


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    usage_date: date
    max_cost_nano: int


class LedgerError(RuntimeError):
    """The ledger could not complete an operation it was asked to perform."""


def utc_day(now: datetime) -> date:
    """The UTC calendar day a moment belongs to.

    UTC rather than local time, so the ceiling rolls over at one instant regardless of
    where the server is and regardless of daylight saving, which would otherwise give
    one day of the year 23 hours of budget and another 25.
    """

    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).date()

    return now.astimezone(UTC).date()


class BudgetLedger:
    """Transactional budget admission over the shared Playerbots database.

    Every method takes an open aiomysql connection rather than owning a pool, so the
    caller decides connection lifetime and this stays testable against any connection.
    """

    def __init__(self, ceiling_nano: int, reserve_ratio: Decimal) -> None:
        self._ceiling_nano = ceiling_nano
        self._reserve_ratio = reserve_ratio

    async def ensure_schema(self, connection) -> None:
        async with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                await cursor.execute(statement)
        await connection.commit()

    async def _lock_day(self, cursor, day: date) -> tuple[int, bool]:
        """Takes the day row's write lock and returns its settled total and circuit state.

        Inserted on demand with ``INSERT IGNORE`` before the lock is taken, because
        ``SELECT ... FOR UPDATE`` on a row that does not exist locks nothing and two
        concurrent first-requests of the day would both proceed.
        """

        await cursor.execute(
            "INSERT IGNORE INTO playerbot_claude_budget_day (usage_date, settled_nano) VALUES (%s, 0)",
            (day,),
        )
        await cursor.execute(
            "SELECT settled_nano, circuit_open FROM playerbot_claude_budget_day "
            "WHERE usage_date = %s FOR UPDATE",
            (day,),
        )
        row = await cursor.fetchone()
        if row is None:  # pragma: no cover - the insert above guarantees a row
            raise LedgerError("budget day row vanished between insert and lock")

        return int(row[0]), bool(row[1])

    async def _outstanding_nano(self, cursor, day: date, now: datetime) -> int:
        """Live reservations only. Expired ones are reclaimed rather than counted.

        A sidecar that died between reserving and settling would otherwise hold that
        money against the ceiling until midnight. Reclaiming inside the same locked
        transaction is what makes the recovery safe: the settle path below only accepts
        a reservation still in the reserved state, so a late completion for a reclaimed
        row is refused rather than charged twice.
        """

        cutoff = now - RESERVATION_EXPIRY
        await cursor.execute(
            "UPDATE playerbot_claude_budget_reservation SET state = 'released' "
            "WHERE usage_date = %s AND state = 'reserved' AND created_at < %s",
            (day, cutoff),
        )
        await cursor.execute(
            "SELECT COALESCE(SUM(max_cost_nano), 0) FROM playerbot_claude_budget_reservation "
            "WHERE usage_date = %s AND state = 'reserved'",
            (day,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def reserve(
        self,
        connection,
        *,
        request_id: int,
        attempt: int,
        max_cost_nano: int | None,
        priority: RequestPriority,
        now: datetime,
    ) -> tuple[AdmissionDecision, Reservation | None]:
        """Admits and records one reservation, or returns why it was refused.

        The whole decision happens inside the day row's lock. Reading the totals,
        applying the policy, and inserting the row are one atomic step, which is what
        makes Definition of Done 1 hold: two concurrent callers cannot both see the same
        remaining budget.
        """

        day = utc_day(now)
        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                settled, circuit_open = await self._lock_day(cursor, day)
                outstanding = await self._outstanding_nano(cursor, day, now)

                decision = budget.admit(
                    ceiling_nano=self._ceiling_nano,
                    state=BudgetState(
                        settled_nano=settled,
                        outstanding_nano=outstanding,
                        circuit_open=circuit_open,
                    ),
                    max_cost_nano=max_cost_nano,
                    priority=priority,
                    reserve_ratio=self._reserve_ratio,
                )

                if decision is not AdmissionDecision.ADMITTED:
                    await connection.commit()
                    return decision, None

                await cursor.execute(
                    "INSERT INTO playerbot_claude_budget_reservation "
                    "(usage_date, request_id, attempt, priority, max_cost_nano, state, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'reserved', %s)",
                    (day, request_id, attempt, priority.value, max_cost_nano, now),
                )
                reservation_id = cursor.lastrowid

            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return AdmissionDecision.ADMITTED, Reservation(
            reservation_id=reservation_id, usage_date=day, max_cost_nano=int(max_cost_nano or 0)
        )

    async def settle(
        self, connection, *, reservation: Reservation, actual_cost_nano: int, now: datetime
    ) -> bool:
        """Charges the real cost and releases the rest of the reservation.

        Returns False when the reservation was not in the reserved state, which is how a
        completion arriving after expiry recovery is refused rather than charged a second
        time. Definition of Done 4 rests on that check.

        An actual cost above the reservation opens the circuit and is stored truthfully
        rather than clamped: clamping would make the ledger agree with a bound that was
        actually broken, and the breach is the thing worth knowing.
        """

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                await self._lock_day(cursor, reservation.usage_date)

                await cursor.execute(
                    "UPDATE playerbot_claude_budget_reservation "
                    "SET state = 'settled', actual_cost_nano = %s, settled_at = %s "
                    "WHERE id = %s AND state = 'reserved'",
                    (actual_cost_nano, now, reservation.reservation_id),
                )
                if cursor.rowcount == 0:
                    await connection.commit()
                    return False

                await cursor.execute(
                    "UPDATE playerbot_claude_budget_day SET settled_nano = settled_nano + %s "
                    "WHERE usage_date = %s",
                    (actual_cost_nano, reservation.usage_date),
                )

                if budget.circuit_should_open(reservation.max_cost_nano, actual_cost_nano):
                    await cursor.execute(
                        "UPDATE playerbot_claude_budget_day SET circuit_open = 1, circuit_reason = %s "
                        "WHERE usage_date = %s",
                        (
                            f"reported cost {actual_cost_nano} exceeded reservation "
                            f"{reservation.max_cost_nano}",
                            reservation.usage_date,
                        ),
                    )

            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return True

    async def release(self, connection, *, reservation: Reservation) -> bool:
        """Gives back an unused reservation, for a request that failed before spending."""

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE playerbot_claude_budget_reservation SET state = 'released' "
                    "WHERE id = %s AND state = 'reserved'",
                    (reservation.reservation_id,),
                )
                released = cursor.rowcount > 0
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return released

    async def snapshot(self, connection, *, now: datetime) -> BudgetState:
        """What the ledger currently owes, without taking a write lock."""

        day = utc_day(now)
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT settled_nano, circuit_open FROM playerbot_claude_budget_day WHERE usage_date = %s",
                (day,),
            )
            row = await cursor.fetchone()
            settled, circuit_open = (int(row[0]), bool(row[1])) if row else (0, False)

            await cursor.execute(
                "SELECT COALESCE(SUM(max_cost_nano), 0) FROM playerbot_claude_budget_reservation "
                "WHERE usage_date = %s AND state = 'reserved' AND created_at >= %s",
                (day, now - RESERVATION_EXPIRY),
            )
            outstanding_row = await cursor.fetchone()

        return BudgetState(
            settled_nano=settled,
            outstanding_nano=int(outstanding_row[0]) if outstanding_row else 0,
            circuit_open=circuit_open,
        )
