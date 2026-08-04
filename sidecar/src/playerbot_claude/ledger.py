"""The MySQL budget ledger: the durable half of admission.

The decisions live in :mod:`playerbot_claude.budget` and are pure. This module owns
only the parts that genuinely need a database: reading committed totals under a lock,
inserting a reservation, and settling or releasing one. That split is deliberate. A
rule embedded in a transaction is a rule the tests can reach only through a
transaction, and the arithmetic is the part that has to be right.

Concurrency rests on one named lock. Every reservation takes ``budget_day`` through
:func:`acquire_named_lock` before reading anything, so two requests deciding at the same
instant cannot both see the same remaining budget and both spend it. One global key
rather than one per date: only one day is ever being written at a time in practice, so a
per-date key bought no concurrency and grew the lock table by a row a day forever.

The lock is an upsert rather than a locking read. Both of the obvious alternatives raced,
in opposite directions, and each one's test covered only the case the other broke. That
history is written out at :meth:`BudgetLedger._lock_day`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from playerbot_claude import budget
from playerbot_claude.budget import AdmissionDecision, BudgetState, RequestKind, RequestPriority

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
)

# The budget tables are NOT in the list above, deliberately.
#
# `playerbot_claude_daily_budget`, `playerbot_claude_budget_reservation`, and the
# `playerbot_social_runtime_control` row that carries the circuit breaker belong to
# mod-playerbots and are created by its SQL revisions. The sidecar used to carry a second
# definition of the first two here, with different column names, integer nano money
# instead of decimal dollars, and a breaker on the daily row. Both definitions used
# CREATE TABLE IF NOT EXISTS, so whichever ran first won and the other silently did
# nothing: on the deployed database that was the module, and every write this file made to
# columns only its own DDL had would have failed at runtime and nowhere else.
#
# One owner, and it is the module. This process verifies the tables are there and refuses
# to start if they are not, rather than papering over a database the schema revisions
# never reached.
REQUIRED_MODULE_TABLES = (
    "playerbot_claude_daily_budget",
    "playerbot_claude_budget_reservation",
    "playerbot_social_runtime_control",
)

# The other half of that split: the tables SCHEMA_STATEMENTS above creates. Named here so
# the ownership boundary is one list rather than a property of statement order, and so a
# test can assert that a refused start left none of them behind.
SIDECAR_OWNED_TABLES = (
    "playerbot_claude_lock",
    "playerbot_claude_profile",
    "playerbot_claude_conversation_turn",
    "playerbot_claude_career_decision",
    "playerbot_claude_ambient_attempt",
)

# The singleton primary key of `playerbot_social_runtime_control`, enforced by a CHECK
# constraint on the table itself.
RUNTIME_CONTROL_ID = 1

# Width of `budget_circuit_reason`. An over-long reason would fail the update, roll the
# transaction back, and leave the breaker shut for the exact case it exists to catch, so
# the reason is sliced to fit rather than trusted to.
CIRCUIT_REASON_LIMIT = 128

# Width of the reservation's `model` column.
MODEL_NAME_LIMIT = 64

# What the sidecar writes into `priority_lane`.
#
# The lane is decided on the worldserver side, in PlayerbotSocialAdmissionLane, collapsed
# to a queue priority, and then discarded before the request is encoded. It never crosses
# the bridge, so this process cannot know it. `unspecified` is not a guess about which lane
# a request was in; it is an accurate statement that the row's producer does not know, and
# the truthful lane arrives when the separate telemetry task gives this column a producer.
#
# `model` beside it is different, and is written truthfully: the sidecar chooses the model,
# so it is a fact this process actually holds.
PRIORITY_LANE_UNSPECIFIED = "unspecified"

# The opaque public identity of a reservation: a kind prefix and 32 lowercase hex.
PUBLIC_ID_PREFIX = "req_"
PUBLIC_ID_BODY_BYTES = 16


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    public_id: str
    budget_date: date
    max_cost_nano: int


class LedgerError(RuntimeError):
    """The ledger could not complete an operation it was asked to perform."""


def mint_public_id() -> str:
    """A fresh opaque identity for one reservation attempt.

    Random rather than derived, and that is what makes it durable. The previous key was
    the worldserver's request id paired with an attempt number, and the worldserver's
    request ids come from a per-process counter that restarts at 1: after a restart,
    request id 1 comes round again and collides with the row the previous run left behind.
    The old code worked around that by deriving the attempt number from a MAX over the
    table, which made every reservation unique but also meant the key deduplicated
    nothing. Minting an identity here says the same thing without the lookup, and says it
    across restarts.
    """

    return PUBLIC_ID_PREFIX + secrets.token_hex(PUBLIC_ID_BODY_BYTES)


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
        # The guard runs FIRST, before any DDL. `CREATE TABLE` commits itself in MySQL,
        # so a guard placed after the loop cannot be undone by refusing afterwards: the
        # sidecar's five tables would already be sitting in a database its own message
        # says is not ready. Refusing to touch a database that has not been migrated
        # means not touching it.
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN (%s, %s, %s)",
                REQUIRED_MODULE_TABLES,
            )
            present = {row[0] for row in await cursor.fetchall()}
        await connection.commit()

        missing = [name for name in REQUIRED_MODULE_TABLES if name not in present]
        if missing:
            # Refused rather than created. These tables belong to the mod-playerbots SQL
            # revisions, and a sidecar that creates its own version of them is how the two
            # definitions diverged in the first place. A missing table here means the
            # revisions have not been applied to this database, which is an operator
            # problem with an operator fix.
            raise LedgerError(
                "the mod-playerbots social schema is missing from this database "
                f"({', '.join(missing)}); apply data/sql/playerbots/updates before starting"
            )

        async with connection.cursor() as cursor:
            # Every start after the first would otherwise print one "table already
            # exists" note per table. The driver surfaces those as Python warnings on
            # stderr, which buries a real message in seven fake ones and, for the CLI
            # commands, prints ahead of the JSON somebody is trying to read. Silenced at
            # the server for this connection only; genuine errors still raise.
            await cursor.execute("SET sql_notes = 0")
            try:
                for statement in SCHEMA_STATEMENTS:
                    await cursor.execute(statement)
            finally:
                await cursor.execute("SET sql_notes = 1")

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

    async def _circuit_open(self, cursor) -> bool:
        """Whether the breaker is currently stopping all spending.

        Read from the social runtime control row rather than from the day. The breaker
        opens because a provider reported a cost nobody authorised, which is not a fact
        about one calendar day, and a per-day breaker silently reopens at UTC midnight
        with the underlying problem untouched.

        A missing singleton row reads as closed. The row is created lazily, by the breaker
        itself, so its absence means nothing has ever tripped it rather than that the
        state is unknown.
        """

        await cursor.execute(
            "SELECT budget_circuit_open FROM playerbot_social_runtime_control WHERE id = %s",
            (RUNTIME_CONTROL_ID,),
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

    async def _open_circuit(self, cursor, reason: str, now: datetime) -> None:
        """Stops all spending, and records why and when.

        An upsert rather than an update, because nothing seeds the singleton and a
        breaker that fails to record itself because a row was missing is not a breaker.
        The other control columns take their schema defaults, which are the permissive
        ones: this creates the row, it does not decide the operator's other settings.
        """

        trimmed = reason[:CIRCUIT_REASON_LIMIT]
        await cursor.execute(
            "INSERT INTO playerbot_social_runtime_control "
            "(id, budget_circuit_open, budget_circuit_reason, budget_circuit_opened_at) "
            "VALUES (%s, 1, %s, %s) "
            "ON DUPLICATE KEY UPDATE budget_circuit_open = 1, "
            "budget_circuit_reason = %s, budget_circuit_opened_at = %s",
            (RUNTIME_CONTROL_ID, trimmed, now, trimmed, now),
        )

    async def _refresh_reserved_usd(self, cursor, day: date) -> None:
        """Rewrites the day's reserved total from the reservations themselves.

        Derived rather than incremented, so it cannot drift. An expiry sweep, a
        settlement, and a release all change what is outstanding, and three separate
        increments and decrements that must each be right is three chances to be wrong.
        Recomputing costs one indexed aggregate over a table bounded by the day.

        Always called inside the day lock, because a running total written outside it is
        a total two transactions can each compute before either writes.
        """

        await cursor.execute(
            "UPDATE playerbot_claude_daily_budget SET reserved_usd = COALESCE("
            "(SELECT SUM(max_cost_usd) FROM playerbot_claude_budget_reservation "
            "WHERE budget_date = %s AND state = 'reserved'), 0) WHERE budget_date = %s",
            (day, day),
        )

    async def _lock_day(self, cursor, day: date) -> tuple[int, bool]:
        """Serializes on the day, then reads its spent total and circuit state.

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
            "INSERT INTO playerbot_claude_daily_budget (budget_date) VALUES (%s) "
            "ON DUPLICATE KEY UPDATE budget_date = budget_date",
            (day,),
        )
        await cursor.execute(
            "SELECT spent_usd FROM playerbot_claude_daily_budget WHERE budget_date = %s",
            (day,),
        )
        row = await cursor.fetchone()

        if row is None:  # pragma: no cover - the upsert above guarantees a row
            raise LedgerError("budget day row vanished between upsert and read")

        return budget.usd_to_nano(row[0]), await self._circuit_open(cursor)

    async def _outstanding_nano(self, cursor, day: date, now: datetime) -> int:
        """Live reservations only. Expired ones are reclaimed rather than counted.

        A sidecar that died between reserving and settling would otherwise hold that
        money against the ceiling until midnight. Reclaiming inside the same locked
        transaction is what makes the recovery safe: the settle path below only accepts
        a reservation still in the reserved state, so a late completion for a reclaimed
        row is refused rather than charged twice.

        Reclaimed rows move to ``expired`` rather than ``released``. Both stop counting
        against the ceiling, but they are different events with different causes, and a
        row that says released when nothing released it sends whoever reads it after the
        wrong thing. The deployed schema has a state for each; the sidecar's own DDL had
        only one, which is why this used to say released.

        Scoped to one day, so a reservation stranded either side of a UTC midnight is
        reclaimed by the day it belongs to rather than by whichever day happens to be
        current. Nothing reads a past day's totals, so nothing waits on that sweep.
        """

        await cursor.execute(
            "UPDATE playerbot_claude_budget_reservation SET state = 'expired' "
            "WHERE budget_date = %s AND state = 'reserved' AND expires_at <= %s",
            (day, now),
        )
        await cursor.execute(
            "SELECT COALESCE(SUM(max_cost_usd), 0) FROM playerbot_claude_budget_reservation "
            "WHERE budget_date = %s AND state = 'reserved'",
            (day,),
        )
        row = await cursor.fetchone()
        return budget.usd_to_nano(row[0]) if row else 0

    async def reserve(
        self,
        connection,
        *,
        request_kind: RequestKind,
        model: str,
        max_cost_nano: int | None,
        priority: RequestPriority,
        now: datetime,
    ) -> tuple[AdmissionDecision, Reservation | None]:
        """Admits and records one reservation, or returns why it was refused.

        The whole decision happens inside the day row's lock. Reading the totals,
        applying the policy, and inserting the row are one atomic step, which is what
        makes Definition of Done 1 hold: two concurrent callers cannot both see the same
        remaining budget.

        Every reservation gets its own row under its own freshly minted ``public_id``, so
        a repeat of any kind, a genuine retry or a worldserver counter that wrapped back
        over a restart, is its own reservation with its own cost record rather than a
        duplicate key error. That is Definition of Done 3, and it no longer depends on a
        per-process request id being unique, which it never was.

        The maximum is rounded up to the microdollar BEFORE the decision, so the amount
        admitted against the ceiling is the amount the row will hold.
        """

        if not model or len(model) > MODEL_NAME_LIMIT:
            # Checked rather than truncated. A model name silently cut to 64 characters
            # records a model that was never called, which is worse than no row at all.
            raise LedgerError(f"model name is empty or too long for the ledger: {model!r}")

        if max_cost_nano is not None and max_cost_nano > 0:
            max_cost_nano = budget.quantize_storable_nano(max_cost_nano)

        day = utc_day(now)
        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                spent, circuit_open = await self._lock_day(cursor, day)
                outstanding = await self._outstanding_nano(cursor, day, now)

                decision = budget.admit(
                    ceiling_nano=self._ceiling_nano,
                    state=BudgetState(
                        settled_nano=spent,
                        outstanding_nano=outstanding,
                        circuit_open=circuit_open,
                    ),
                    max_cost_nano=max_cost_nano,
                    priority=priority,
                    reserve_ratio=self._reserve_ratio,
                )

                if decision is not AdmissionDecision.ADMITTED:
                    # The sweep above may have reclaimed rows even though nothing was
                    # admitted, so the day's reserved figure still has to be rewritten.
                    await self._refresh_reserved_usd(cursor, day)
                    await connection.commit()
                    return decision, None

                public_id = mint_public_id()
                await cursor.execute(
                    "INSERT INTO playerbot_claude_budget_reservation "
                    "(public_id, budget_date, request_kind, priority_lane, model, max_cost_usd, "
                    "state, expires_at) VALUES (%s, %s, %s, %s, %s, %s, 'reserved', %s)",
                    (
                        public_id,
                        day,
                        request_kind.value,
                        PRIORITY_LANE_UNSPECIFIED,
                        model,
                        budget.nano_to_usd_string(max_cost_nano or 0),
                        now + RESERVATION_EXPIRY,
                    ),
                )
                reservation_id = cursor.lastrowid
                await self._refresh_reserved_usd(cursor, day)

            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return AdmissionDecision.ADMITTED, Reservation(
            reservation_id=reservation_id,
            public_id=public_id,
            budget_date=day,
            max_cost_nano=int(max_cost_nano or 0),
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
                spent_before, _ = await self._lock_day(cursor, reservation.budget_date)

                await cursor.execute(
                    "UPDATE playerbot_claude_budget_reservation "
                    "SET state = 'completed', actual_cost_usd = %s, settled_at = %s "
                    "WHERE id = %s AND state = 'reserved'",
                    (budget.nano_to_usd_string(storable), now, reservation.reservation_id),
                )
                if cursor.rowcount == 0:
                    await connection.commit()
                    return False

                # The SUM is clamped, not just the value. Clamping only the addend still
                # overflows DECIMAL(12, 6) once anything has been spent, and MySQL then
                # rejects the statement, rolls the transaction back, and leaves the
                # breaker shut for the exact report it exists to catch. The headroom comes
                # from the total this transaction already read under the lock, so the
                # arithmetic happens in Python where it cannot overflow.
                headroom = budget.MAX_STORABLE_NANO - spent_before
                added = max(0, min(storable, headroom))
                saturated = added < storable
                if saturated:
                    # A total that is no longer the sum of what was charged is an
                    # integrity failure whether or not the provider overran its
                    # reservation, so it stops spending on its own account. The
                    # configured ceiling is refused above MAX_STORABLE_NANO precisely so
                    # honest traffic can never arrive here.
                    breach = True

                await cursor.execute(
                    "UPDATE playerbot_claude_daily_budget SET spent_usd = spent_usd + %s "
                    "WHERE budget_date = %s",
                    (budget.nano_to_usd_string(added), reservation.budget_date),
                )
                # This reservation has left the reserved state, so the day owes less.
                await self._refresh_reserved_usd(cursor, reservation.budget_date)

                if breach:
                    # The REPORTED figure goes in the reason even when it could not be
                    # stored in the column, because the number is the evidence.
                    #
                    # Named for what actually happened. Saturation and an overrun are
                    # different incidents with different causes, and a reason that
                    # reports an overrun when the cost was within its reservation sends
                    # whoever reads it after the wrong thing.
                    causes = []
                    if budget.circuit_should_open(reservation.max_cost_nano, actual_cost_nano):
                        causes.append(
                            f"cost {budget.nano_to_usd_string(actual_cost_nano)} over reservation "
                            f"{budget.nano_to_usd_string(reservation.max_cost_nano)}"
                        )
                    if saturated:
                        causes.append(
                            f"day saturated at {budget.nano_to_usd_string(budget.MAX_STORABLE_NANO)}, "
                            f"discarding {budget.nano_to_usd_string(storable - added)}"
                        )
                    await self._open_circuit(cursor, "; ".join(causes), now)

            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        return True

    async def release(self, connection, *, reservation: Reservation) -> bool:
        """Gives back an unused reservation, for a request that failed before spending.

        Takes the day lock even though it writes one row, because it also rewrites the
        day's reserved total, and an aggregate recomputed outside the lock is one two
        transactions can each read before either writes.
        """

        try:
            await connection.begin()
            async with connection.cursor() as cursor:
                await self._lock_day(cursor, reservation.budget_date)
                await cursor.execute(
                    "UPDATE playerbot_claude_budget_reservation SET state = 'released' "
                    "WHERE id = %s AND state = 'reserved'",
                    (reservation.reservation_id,),
                )
                released = cursor.rowcount > 0
                if released:
                    await self._refresh_reserved_usd(cursor, reservation.budget_date)
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
                "SELECT spent_usd FROM playerbot_claude_daily_budget WHERE budget_date = %s",
                (day,),
            )
            row = await cursor.fetchone()
            spent = budget.usd_to_nano(row[0]) if row else 0

            # Reads the reservations rather than the day's `reserved_usd`, so an expiry
            # that has not been swept yet is excluded here too. The stored figure is
            # rewritten under the lock and would otherwise still count a reservation this
            # read can already see is dead.
            await cursor.execute(
                "SELECT COALESCE(SUM(max_cost_usd), 0) FROM playerbot_claude_budget_reservation "
                "WHERE budget_date = %s AND state = 'reserved' AND expires_at > %s",
                (day, now),
            )
            outstanding_row = await cursor.fetchone()
            circuit_open = await self._circuit_open(cursor)

        return BudgetState(
            settled_nano=spent,
            outstanding_nano=budget.usd_to_nano(outstanding_row[0]) if outstanding_row else 0,
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
