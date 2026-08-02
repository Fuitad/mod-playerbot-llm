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

# Bounded per bot conversation memory, trimmed on every write. An unbounded history is
# an unbounded prompt, which is an unbounded cost, which is the thing this whole module
# exists to prevent.
CONVERSATION_TURN_LIMIT = 12

# How many named locks the per bot conversation trim spreads across. Bounded so the lock
# table cannot grow with the bot roster; wide enough that unrelated bots rarely collide.
CONVERSATION_LOCK_BUCKETS = 256

AMBIENT_WINDOW = timedelta(hours=1)
MAX_AMBIENT_MESSAGES_PER_HOUR = 6

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_lock (
        `lock_key` VARCHAR(64) NOT NULL,
        PRIMARY KEY (`lock_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Named serialization points, from a bounded key set. Never deleted.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_profile (
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `profile_version` INT UNSIGNED NOT NULL,
        `crafting_affinity` TINYINT UNSIGNED NOT NULL,
        `gathering_affinity` TINYINT UNSIGNED NOT NULL,
        `exploration_affinity` TINYINT UNSIGNED NOT NULL,
        `sociability` TINYINT UNSIGNED NOT NULL,
        `voice` VARCHAR(32) NOT NULL,
        `bot_name` VARCHAR(48) NOT NULL,
        `updated_at` DATETIME NOT NULL,
        PRIMARY KEY (`bot_guid`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Last observed personality profile per bot, written by the worldserver.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_conversation_turn (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `role` ENUM('user','assistant') NOT NULL,
        `content` TEXT NOT NULL,
        `created_at` DATETIME NOT NULL,
        PRIMARY KEY (`id`),
        KEY `ix_bot` (`bot_guid`, `id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Bounded per bot conversation memory. Trimmed on write, never unbounded.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_career_decision (
        `bot_guid` BIGINT UNSIGNED NOT NULL,
        `career_version` INT UNSIGNED NOT NULL,
        `candidate_token` VARCHAR(64) NOT NULL,
        `spending_style` VARCHAR(32) NOT NULL,
        `updated_at` DATETIME NOT NULL,
        PRIMARY KEY (`bot_guid`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='One current career decision per bot.'
    """,
    """
    CREATE TABLE IF NOT EXISTS playerbot_claude_ambient_attempt (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `created_at` DATETIME NOT NULL,
        PRIMARY KEY (`id`),
        KEY `ix_created` (`created_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='Rolling hourly ambient rate, surviving restart.'
    """,
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


async def acquire_named_lock(cursor, key: str) -> None:
    """Serializes every caller holding the same key, for the rest of the transaction.

    One statement, and it always WRITES, which is the point. A read that locks
    (``SELECT ... FOR UPDATE``) takes a gap lock when the row is missing, and two
    transactions holding compatible gap locks that then both insert into that gap
    deadlock. An upsert takes an exclusive row lock immediately instead, with no gap to
    share and no shared lock to upgrade.

    ``lock_key = lock_key`` is a deliberate no-op update: it makes the statement a write
    on an existing row without changing anything, which is what takes the lock.

    Keys come from a BOUNDED set, and rows here are never deleted. A per-day or per-bot
    key would add a permanent row for every day and every bot the server ever sees, which
    is a table that only grows. Deleting them instead is worse: removing a lock row while
    another transaction may still take that key reintroduces the missing-row race the
    helper exists to avoid. Bounded keys make the question moot.

    Nothing currently takes two named locks in one transaction. If something ever does,
    it needs a canonical acquisition order first, or two callers taking the same pair in
    opposite orders will deadlock.
    """

    await cursor.execute(
        "INSERT INTO playerbot_claude_lock (lock_key) VALUES (%s) "
        "ON DUPLICATE KEY UPDATE lock_key = lock_key",
        (key,),
    )


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

            # The retired key shapes are deliberately NOT deleted here.
            #
            # An earlier version cleaned them up automatically, which is unsafe during a
            # restart: an old sidecar still running against this database is using
            # `budget_day:<date>` and `conversation:<bot_guid>` as its live locks, and
            # removing them mid-flight strips its mutual exclusion and reintroduces the
            # missing row race for it. A cosmetic tidy is not worth that.
            #
            # Leaving them is safe and still bounded: after the upgrade nothing creates
            # a key of either shape, so the residue is a fixed historical set rather
            # than a table that keeps growing. `retire_superseded_locks` below removes
            # it when an operator knows no old process remains.
        await connection.commit()

    async def retire_superseded_locks(self, connection) -> int:
        """Removes lock rows from the retired per day and per bot key shapes.

        NOT called automatically, and that is the point. Deleting a lock row is only
        safe once nothing can still take that key, and during a rolling restart an older
        sidecar is still using these as its live locks. Run this from a maintenance step
        when no old process remains; skipping it costs a fixed residue of rows, not
        growth.

        Returns how many rows were removed.
        """

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                # No parameters, so the % is a literal rather than a placeholder.
                await cursor.execute("DELETE FROM playerbot_claude_lock WHERE lock_key LIKE 'budget_day:%'")
                removed = cursor.rowcount

                # A conversation key at or above the bucket bound cannot have come from
                # the current code. One BELOW it is indistinguishable from a live bucket
                # key and is left alone.
                await cursor.execute(
                    "DELETE FROM playerbot_claude_lock WHERE lock_key LIKE 'conversation:%%' "
                    "AND CAST(SUBSTRING(lock_key, 14) AS UNSIGNED) >= %s",
                    (CONVERSATION_LOCK_BUCKETS,),
                )
                removed += cursor.rowcount
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return removed

    async def _lock_day(self, cursor, day: date) -> tuple[int, bool]:
        """Serializes on the day, then reads its settled total and circuit state.

        The lock is taken through :func:`acquire_named_lock` rather than on the budget
        row itself. Two earlier attempts both had races that only appear when the row is
        MISSING, which is the first request of any day:

        - insert-then-``SELECT FOR UPDATE`` deadlocks on an EXISTING row, because
          ``INSERT IGNORE`` takes a shared lock on the duplicate key that both
          transactions then try to upgrade;
        - ``SELECT FOR UPDATE``-then-insert deadlocks on a MISSING row, because both
          transactions take compatible gap locks and then both try to insert into the
          gap they are each holding.

        An upsert that always writes takes an exclusive row lock in one statement, with
        no gap and no upgrade, which is why the named lock below is used for every
        serialization point in this module rather than each one improvising.
        """

        # One key for the budget, not one per day. Only one day is ever being written
        # at a time in practice, so a per-day key bought no concurrency and grew the
        # lock table by a row a day forever.
        await acquire_named_lock(cursor, "budget_day")

        await cursor.execute(
            "INSERT INTO playerbot_claude_budget_day (usage_date, settled_nano) VALUES (%s, 0) "
            "ON DUPLICATE KEY UPDATE usage_date = usage_date",
            (day,),
        )
        await cursor.execute(
            "SELECT settled_nano, circuit_open FROM playerbot_claude_budget_day WHERE usage_date = %s",
            (day,),
        )
        row = await cursor.fetchone()

        if row is None:  # pragma: no cover - the upsert above guarantees a row
            raise LedgerError("budget day row vanished between upsert and read")

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

        # Decided BEFORE anything is written, and against the value as reported. An
        # out-of-range cost would fail the SQL update, roll the transaction back, and
        # leave the reservation outstanding with the breaker never firing, which is
        # exactly the case the breaker exists for.
        breach = budget.circuit_should_open(reservation.max_cost_nano, actual_cost_nano)
        storable = budget.storable_actual_cost_nano(actual_cost_nano)

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                await self._lock_day(cursor, reservation.usage_date)

                await cursor.execute(
                    "UPDATE playerbot_claude_budget_reservation "
                    "SET state = 'settled', actual_cost_nano = %s, settled_at = %s "
                    "WHERE id = %s AND state = 'reserved'",
                    (storable, now, reservation.reservation_id),
                )
                if cursor.rowcount == 0:
                    await connection.commit()
                    return False

                await cursor.execute(
                    "UPDATE playerbot_claude_budget_day SET settled_nano = settled_nano + %s "
                    "WHERE usage_date = %s",
                    (storable, reservation.usage_date),
                )

                if breach:
                    # The REPORTED figure goes in the reason even when it could not be
                    # stored in the column, because the number is the evidence.
                    await cursor.execute(
                        "UPDATE playerbot_claude_budget_day SET circuit_open = 1, circuit_reason = %s "
                        "WHERE usage_date = %s",
                        (
                            f"reported cost {actual_cost_nano} exceeded reservation "
                            f"{reservation.max_cost_nano}"[:255],
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


class SidecarStore:
    """The non-budget durable state, on the shared Playerbots database.

    Everything here used to live in a private SQLite file. Sharing the Playerbots
    database instead removes a second thing to back up, a second thing to migrate, and a
    file whose absence or corruption was a failure mode nobody monitored.

    Like :class:`BudgetLedger`, every method takes an open connection rather than owning
    a pool, so the caller decides connection lifetime.
    """

    async def record_profile(
        self,
        connection,
        *,
        bot_guid: int,
        profile_version: int,
        crafting_affinity: int,
        gathering_affinity: int,
        exploration_affinity: int,
        sociability: int,
        voice: str,
        bot_name: str,
        now: datetime,
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO playerbot_claude_profile (bot_guid, profile_version, crafting_affinity, "
                "gathering_affinity, exploration_affinity, sociability, voice, bot_name, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE profile_version = VALUES(profile_version), "
                "crafting_affinity = VALUES(crafting_affinity), "
                "gathering_affinity = VALUES(gathering_affinity), "
                "exploration_affinity = VALUES(exploration_affinity), "
                "sociability = VALUES(sociability), voice = VALUES(voice), "
                "bot_name = VALUES(bot_name), updated_at = VALUES(updated_at)",
                (
                    bot_guid,
                    profile_version,
                    crafting_affinity,
                    gathering_affinity,
                    exploration_affinity,
                    sociability,
                    voice,
                    bot_name,
                    now,
                ),
            )
        await connection.commit()

    async def get_profile(self, connection, *, bot_guid: int) -> dict[str, object] | None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT profile_version, crafting_affinity, gathering_affinity, exploration_affinity, "
                "sociability, voice, bot_name FROM playerbot_claude_profile WHERE bot_guid = %s",
                (bot_guid,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "profile_version": int(row[0]),
            "crafting_affinity": int(row[1]),
            "gathering_affinity": int(row[2]),
            "exploration_affinity": int(row[3]),
            "sociability": int(row[4]),
            "voice": row[5],
            "bot_name": row[6],
        }

    async def append_turn(self, connection, *, bot_guid: int, role: str, content: str, now: datetime) -> None:
        """Appends one turn and trims the bot's history to the limit.

        Trimmed on write rather than on read, so the table is bounded on disk and not
        merely in what a query returns. The subquery is wrapped in a derived table
        because MySQL will not read from the table it is deleting from otherwise.
        """

        if role not in ("user", "assistant"):
            raise LedgerError(f"unsupported conversation role: {role!r}")

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                # Serialized per bot. Two concurrent appends for the same bot would each
                # insert and then scan and delete the same id range, which deadlocks. The
                # lock is per bot rather than global so unrelated bots never wait.
                # Bucketed rather than one key per bot, so the lock table is bounded by
                # CONVERSATION_LOCK_BUCKETS rather than by how many bots the server has
                # ever seen. Two bots sharing a bucket wait for each other, which costs a
                # little contention and buys a table that cannot grow.
                await acquire_named_lock(cursor, f"conversation:{bot_guid % CONVERSATION_LOCK_BUCKETS}")
                await cursor.execute(
                    "INSERT INTO playerbot_claude_conversation_turn (bot_guid, role, content, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (bot_guid, role, content, now),
                )
                await cursor.execute(
                    "DELETE FROM playerbot_claude_conversation_turn WHERE bot_guid = %s AND id NOT IN "
                    "(SELECT id FROM (SELECT id FROM playerbot_claude_conversation_turn "
                    "WHERE bot_guid = %s ORDER BY id DESC LIMIT %s) AS keep)",
                    (bot_guid, bot_guid, CONVERSATION_TURN_LIMIT),
                )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async def recent_turns(self, connection, *, bot_guid: int) -> list[tuple[str, str]]:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT role, content FROM playerbot_claude_conversation_turn "
                "WHERE bot_guid = %s ORDER BY id ASC",
                (bot_guid,),
            )
            rows = await cursor.fetchall()

        return [(row[0], row[1]) for row in rows]

    async def record_career_decision(
        self,
        connection,
        *,
        bot_guid: int,
        career_version: int,
        candidate_token: str,
        spending_style: str,
        now: datetime,
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO playerbot_claude_career_decision "
                "(bot_guid, career_version, candidate_token, spending_style, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE career_version = VALUES(career_version), "
                "candidate_token = VALUES(candidate_token), spending_style = VALUES(spending_style), "
                "updated_at = VALUES(updated_at)",
                (bot_guid, career_version, candidate_token, spending_style, now),
            )
        await connection.commit()

    async def get_career_decision(self, connection, *, bot_guid: int) -> dict[str, object] | None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT career_version, candidate_token, spending_style "
                "FROM playerbot_claude_career_decision WHERE bot_guid = %s",
                (bot_guid,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "career_version": int(row[0]),
            "candidate_token": row[1],
            "spending_style": row[2],
        }

    async def try_begin_ambient(self, connection, *, messages_per_hour: int, now: datetime) -> bool:
        """Consumes one ambient slot if the rolling hour has room.

        The whole check and insert run in one transaction with the count taken under a
        write lock, so two sidecar workers cannot both read the same count and both
        decide there was room.
        """

        if not 1 <= messages_per_hour <= MAX_AMBIENT_MESSAGES_PER_HOUR:
            return False

        cutoff = now - AMBIENT_WINDOW
        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                # One guard row rather than locking the whole attempts table. A bare
                # COUNT(*) FOR UPDATE locks every row it scans, which blocks unrelated
                # inserts and gets more expensive as the table grows, and under gap
                # locking two callers can deadlock on it.
                await acquire_named_lock(cursor, "ambient")

                await cursor.execute(
                    "DELETE FROM playerbot_claude_ambient_attempt WHERE created_at <= %s",
                    (cutoff,),
                )
                # Predicated on the indexed column, so this reads an index range rather
                # than the table.
                await cursor.execute(
                    "SELECT COUNT(*) FROM playerbot_claude_ambient_attempt WHERE created_at > %s",
                    (cutoff,),
                )
                row = await cursor.fetchone()
                if row is not None and int(row[0]) >= messages_per_hour:
                    await connection.commit()
                    return False

                await cursor.execute(
                    "INSERT INTO playerbot_claude_ambient_attempt (created_at) VALUES (%s)",
                    (now,),
                )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return True
