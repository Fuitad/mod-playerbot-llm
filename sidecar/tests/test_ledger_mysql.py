"""Budget ledger tests against a real MySQL.

Marked ``mysql`` and skipped unless ``PLAYERBOT_CLAUDE_TEST_MYSQL_DSN`` names a
disposable database. These cannot be mocked usefully: what they prove is that
``SELECT ... FOR UPDATE`` actually serializes two concurrent transactions, and a mock
that serializes them is a mock that assumes the answer.

``scripts/run_ledger_mysql_tests.sh`` starts a throwaway container, exports the DSN, and
removes it afterwards. It never touches a MySQL server that is already running.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiomysql
import pytest

from playerbot_claude import app, budget, claude, ledger, protocol
from playerbot_claude import state as state_module
from playerbot_claude.app import PlayerbotsDatabaseSettings
from playerbot_claude.budget import AdmissionDecision, RequestPriority

pytestmark = [pytest.mark.mysql, pytest.mark.asyncio]

DSN = os.environ.get("PLAYERBOT_CLAUDE_TEST_MYSQL_DSN")

if not DSN:  # pragma: no cover - the skip is the point
    pytest.skip("PLAYERBOT_CLAUDE_TEST_MYSQL_DSN is not set", allow_module_level=True)


CEILING = budget.usd_to_nano("10.00")
QUARTER = Decimal("0.25")
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# More distinct bots than there are lock buckets, so the bound is actually exercised.
CONVERSATION_LOCK_SAMPLE = 400

# Same fixture token the unit suite uses; the protocol only checks length and match.
TOKEN = "0123456789abcdef0123456789abcdef"


def _settings() -> PlayerbotsDatabaseSettings:
    # The module-level skip above already guarantees this; restating it keeps the type
    # checker honest without a cast that would also hide a real None.
    assert DSN is not None
    return PlayerbotsDatabaseSettings.parse_info(DSN)


async def _connect() -> aiomysql.Connection:
    # Spelled out rather than splatted from a dict: a **kwargs splat makes every argument
    # an untyped object, so a misspelled or wrongly typed connection setting reaches the
    # driver instead of the type checker.
    settings = _settings()
    return await aiomysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        db=settings.database,
        autocommit=False,
    )


@pytest.fixture
async def clean_ledger():
    book = ledger.BudgetLedger(CEILING, QUARTER)
    connection = await _connect()
    try:
        await book.ensure_schema(connection)
        async with connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_claude_budget_reservation")
            await cursor.execute("DELETE FROM playerbot_claude_budget_day")
        await connection.commit()
        yield book, connection
    finally:
        connection.close()


async def test_a_reservation_is_recorded_and_counted(clean_ledger) -> None:
    book, connection = clean_ledger

    decision, reservation = await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None

    state = await book.snapshot(connection, now=NOW)
    assert state.outstanding_nano == budget.usd_to_nano("1.00")
    assert state.settled_nano == 0


async def test_two_concurrent_reservations_cannot_jointly_exceed_the_ceiling(clean_ledger) -> None:
    """Definition of Done 1, against a real lock.

    Both transactions are opened before either commits, so this genuinely exercises
    SELECT ... FOR UPDATE rather than a sequence that happens to be ordered. A mock
    that serializes them would be assuming the thing under test.
    """
    book, first_connection = clean_ledger
    second_connection = await _connect()

    try:
        six = budget.usd_to_nano("6.00")

        # A barrier, because asyncio.gather alone does not guarantee contention: the
        # first coroutine can finish and commit before the second begins, which would
        # make this pass while proving only that two sequential reservations behave.
        #
        # Both connections take a real write lock on the SAME day row here, before either
        # calls reserve, so the second reserve genuinely has to wait on the first.
        barrier = asyncio.Barrier(2)

        # The day row is created first, outside the barrier, so what the two coroutines
        # actually contend on is the row lock rather than the insert.
        await first_connection.begin()
        async with first_connection.cursor() as cursor:
            await cursor.execute(
                "INSERT IGNORE INTO playerbot_claude_budget_day (usage_date, settled_nano) VALUES (%s, 0)",
                (ledger.utc_day(NOW),),
            )
        await first_connection.commit()

        async def reserve(connection, request_id):
            await barrier.wait()
            return await book.reserve(
                connection,
                request_id=request_id,
                attempt=1,
                max_cost_nano=six,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=NOW,
            )

        first, second = await asyncio.gather(reserve(first_connection, 1), reserve(second_connection, 2))
        decisions = {first[0], second[0]}

        # Six plus six is twelve and the ceiling is ten, so exactly one may win.
        assert decisions == {AdmissionDecision.ADMITTED, AdmissionDecision.DENIED_CEILING}

        state = await book.snapshot(first_connection, now=NOW)
        assert state.outstanding_nano == six
    finally:
        second_connection.close()


async def test_background_work_is_denied_at_the_reserve_while_a_human_proceeds(clean_ledger) -> None:
    """Definition of Done 2."""
    book, connection = clean_ledger

    await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=budget.usd_to_nano("7.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    one = budget.usd_to_nano("1.00")
    background, _ = await book.reserve(
        connection,
        request_id=2,
        attempt=1,
        max_cost_nano=one,
        priority=RequestPriority.BACKGROUND,
        now=NOW,
    )
    human, _ = await book.reserve(
        connection,
        request_id=3,
        attempt=1,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert background is AdmissionDecision.DENIED_RESERVE
    assert human is AdmissionDecision.ADMITTED


async def test_every_retry_gets_its_own_reservation_and_cost(clean_ledger) -> None:
    """Definition of Done 3. The unique key is on (request_id, attempt), not request_id."""
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    first_decision, first = await book.reserve(
        connection,
        request_id=42,
        attempt=1,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    second_decision, second = await book.reserve(
        connection,
        request_id=42,
        attempt=2,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    assert first_decision is AdmissionDecision.ADMITTED
    assert second_decision is AdmissionDecision.ADMITTED
    assert first.reservation_id != second.reservation_id

    await book.settle(connection, reservation=first, actual_cost_nano=1000, now=NOW)
    await book.settle(connection, reservation=second, actual_cost_nano=2000, now=NOW)

    state = await book.snapshot(connection, now=NOW)
    assert state.settled_nano == 3000
    assert state.outstanding_nano == 0


async def test_a_crash_leaves_a_reservation_that_expiry_reclaims_without_double_charging(
    clean_ledger,
) -> None:
    """Definition of Done 4.

    The reservation is never settled, as if the sidecar died mid-request. A later
    transaction reclaims it, and the completion that eventually arrives is refused
    rather than charged against a reservation that no longer exists.
    """
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    _, stranded = await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    later = NOW + ledger.RESERVATION_EXPIRY + timedelta(seconds=1)

    # A later request reclaims it: the money is back in the day's budget.
    decision, _ = await book.reserve(
        connection,
        request_id=2,
        attempt=1,
        max_cost_nano=budget.usd_to_nano("10.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=later,
    )
    assert decision is AdmissionDecision.ADMITTED

    # And the late completion for the reclaimed reservation is refused, not charged.
    assert await book.settle(connection, reservation=stranded, actual_cost_nano=one, now=later) is False

    state = await book.snapshot(connection, now=later)
    assert state.settled_nano == 0


async def test_an_impossible_reported_cost_opens_the_circuit_and_stops_admission(clean_ledger) -> None:
    """Definition of Done 7.

    The reported figure is stored truthfully rather than clamped to the maximum, because
    clamping would make the ledger agree with a bound that was actually broken.
    """
    book, connection = clean_ledger
    one = budget.usd_to_nano("1.00")

    _, reservation = await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=one,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    impossible = one * 5
    assert await book.settle(connection, reservation=reservation, actual_cost_nano=impossible, now=NOW)

    state = await book.snapshot(connection, now=NOW)
    assert state.circuit_open is True
    assert state.settled_nano == impossible  # truthful, not clamped

    for priority in RequestPriority:
        decision, _ = await book.reserve(
            connection,
            request_id=99,
            attempt=1,
            max_cost_nano=1,
            priority=priority,
            now=NOW,
        )
        assert decision is AdmissionDecision.DENIED_CIRCUIT_OPEN


async def test_a_released_reservation_returns_its_money(clean_ledger) -> None:
    book, connection = clean_ledger
    nine = budget.usd_to_nano("9.00")

    _, reservation = await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )

    # Nothing else fits while it is outstanding.
    blocked, _ = await book.reserve(
        connection,
        request_id=2,
        attempt=1,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert blocked is AdmissionDecision.DENIED_CEILING

    assert await book.release(connection, reservation=reservation) is True
    # Releasing twice is not an error and does not credit twice.
    assert await book.release(connection, reservation=reservation) is False

    freed, _ = await book.reserve(
        connection,
        request_id=3,
        attempt=1,
        max_cost_nano=nine,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert freed is AdmissionDecision.ADMITTED


async def test_the_ceiling_rolls_over_at_utc_midnight(clean_ledger) -> None:
    book, connection = clean_ledger
    ten = budget.usd_to_nano("10.00")

    _, reservation = await book.reserve(
        connection,
        request_id=1,
        attempt=1,
        max_cost_nano=ten,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    await book.settle(connection, reservation=reservation, actual_cost_nano=ten, now=NOW)

    exhausted, _ = await book.reserve(
        connection,
        request_id=2,
        attempt=1,
        max_cost_nano=1,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert exhausted is AdmissionDecision.DENIED_CEILING

    # A new UTC day is a new row and a fresh ceiling.
    tomorrow = NOW + timedelta(days=1)
    fresh, _ = await book.reserve(
        connection,
        request_id=3,
        attempt=1,
        max_cost_nano=ten,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=tomorrow,
    )
    assert fresh is AdmissionDecision.ADMITTED


# The non-budget durable state ------------------------------------------------------


@pytest.fixture
async def store(clean_ledger):
    _, connection = clean_ledger
    # Written out rather than looped over an interpolated name. A table name cannot be a
    # bound parameter, so a loop here means building SQL from a string, and a test that
    # does that teaches the pattern even when its own inputs are literals.
    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_claude_profile")
        await cursor.execute("DELETE FROM playerbot_claude_conversation_turn")
        await cursor.execute("DELETE FROM playerbot_claude_career_decision")
        await cursor.execute("DELETE FROM playerbot_claude_ambient_attempt")
    await connection.commit()
    return ledger.SidecarStore(), connection


async def test_a_profile_round_trips_and_the_latest_write_wins(store) -> None:
    book, connection = store

    assert await book.get_profile(connection, bot_guid=7) is None

    await book.record_profile(
        connection,
        bot_guid=7,
        profile_version=2,
        crafting_affinity=10,
        gathering_affinity=20,
        exploration_affinity=30,
        sociability=40,
        voice="wry",
        bot_name="Grimbold",
        now=NOW,
    )
    await book.record_profile(
        connection,
        bot_guid=7,
        profile_version=2,
        crafting_affinity=11,
        gathering_affinity=20,
        exploration_affinity=30,
        sociability=40,
        voice="earnest",
        bot_name="Grimbold",
        now=NOW,
    )

    profile = await book.get_profile(connection, bot_guid=7)
    assert profile["crafting_affinity"] == 11
    assert profile["voice"] == "earnest"


async def test_conversation_memory_is_trimmed_on_disk_not_merely_on_read(store) -> None:
    """An unbounded history is an unbounded prompt, which is an unbounded cost."""
    book, connection = store

    for index in range(ledger.CONVERSATION_TURN_LIMIT + 5):
        await book.append_turn(connection, bot_guid=7, role="user", content=f"turn {index}", now=NOW)

    turns = await book.recent_turns(connection, bot_guid=7)
    assert len(turns) == ledger.CONVERSATION_TURN_LIMIT
    # The OLDEST are the ones dropped, and order is preserved.
    assert turns[0][1] == "turn 5"
    assert turns[-1][1] == f"turn {ledger.CONVERSATION_TURN_LIMIT + 4}"

    # And the trim really happened in the table, not just in what the query returned.
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_claude_conversation_turn WHERE bot_guid = 7")
        row = await cursor.fetchone()
    assert int(row[0]) == ledger.CONVERSATION_TURN_LIMIT


async def test_one_bots_memory_does_not_trim_another(store) -> None:
    book, connection = store

    await book.append_turn(connection, bot_guid=1, role="user", content="mine", now=NOW)
    for index in range(ledger.CONVERSATION_TURN_LIMIT + 3):
        await book.append_turn(connection, bot_guid=2, role="user", content=str(index), now=NOW)

    assert len(await book.recent_turns(connection, bot_guid=1)) == 1


async def test_an_unsupported_role_is_refused_rather_than_stored(store) -> None:
    book, connection = store

    with pytest.raises(ledger.LedgerError):
        await book.append_turn(connection, bot_guid=7, role="system", content="x", now=NOW)

    # The name says "rather than stored", so the test has to check that rather than
    # settling for the exception having been raised.
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_claude_conversation_turn WHERE bot_guid = 7")
        row = await cursor.fetchone()
    assert int(row[0]) == 0


async def test_a_career_decision_round_trips_and_is_one_per_bot(store) -> None:
    book, connection = store

    assert await book.get_career_decision(connection, bot_guid=7) is None

    await book.record_career_decision(
        connection,
        bot_guid=7,
        career_version=1,
        candidate_token="career-alpha",
        spending_style="minimal",
        now=NOW,
    )
    await book.record_career_decision(
        connection,
        bot_guid=7,
        career_version=1,
        candidate_token="career-beta",
        spending_style="progression",
        now=NOW,
    )

    decision = await book.get_career_decision(connection, bot_guid=7)
    assert decision["candidate_token"] == "career-beta"
    assert decision["spending_style"] == "progression"


async def test_the_ambient_rate_holds_across_a_rolling_hour(store) -> None:
    book, connection = store

    for _ in range(3):
        assert await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW) is True

    assert await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW) is False

    # An hour later the window has rolled and the slots are back.
    later = NOW + ledger.AMBIENT_WINDOW + timedelta(seconds=1)
    assert await book.try_begin_ambient(connection, messages_per_hour=3, now=later) is True


async def test_an_out_of_range_ambient_rate_consumes_nothing(store) -> None:
    book, connection = store

    assert await book.try_begin_ambient(connection, messages_per_hour=0, now=NOW) is False
    assert (
        await book.try_begin_ambient(
            connection, messages_per_hour=ledger.MAX_AMBIENT_MESSAGES_PER_HOUR + 1, now=NOW
        )
        is False
    )

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_claude_ambient_attempt")
        row = await cursor.fetchone()
    assert int(row[0]) == 0


# Contention on the paths the earlier tests did not reach ---------------------------


async def test_the_very_first_reservation_of_a_day_succeeds_with_no_row_present(clean_ledger) -> None:
    """The path the other concurrency test skips by pre-creating the day row.

    Two transactions both taking a locking READ on a MISSING row acquire compatible gap
    locks and then both insert into the gap they are each holding, which deadlocks. That
    is the mirror of the insert-then-lock deadlock, and it only appears when nothing
    exists yet.
    """
    book, first_connection = clean_ledger
    second_connection = await _connect()

    try:
        barrier = asyncio.Barrier(2)
        one = budget.usd_to_nano("1.00")

        async def reserve(connection, request_id):
            await barrier.wait()
            return await book.reserve(
                connection,
                request_id=request_id,
                attempt=1,
                max_cost_nano=one,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=NOW,
            )

        # Nothing is pre-created: the day row does not exist when both start.
        first, second = await asyncio.gather(reserve(first_connection, 1), reserve(second_connection, 2))

        assert first[0] is AdmissionDecision.ADMITTED
        assert second[0] is AdmissionDecision.ADMITTED

        state = await book.snapshot(first_connection, now=NOW)
        assert state.outstanding_nano == one * 2
    finally:
        second_connection.close()


async def test_repeated_appends_for_one_bot_from_two_connections_settle_correctly(clean_ledger) -> None:
    """Each append inserts and then scans and deletes the same id range.

    Without per bot serialization two writers interleave those scans and deadlock. The
    lock is per bot, so this also checks the trim still landed on the right rows.
    """
    _, first_connection = clean_ledger
    second_connection = await _connect()
    book = ledger.SidecarStore()

    try:
        async with first_connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_claude_conversation_turn")
        await first_connection.commit()

        barrier = asyncio.Barrier(2)

        async def append_many(connection, label):
            await barrier.wait()
            for index in range(ledger.CONVERSATION_TURN_LIMIT):
                await book.append_turn(
                    connection, bot_guid=7, role="user", content=f"{label}{index}", now=NOW
                )

        await asyncio.gather(append_many(first_connection, "a"), append_many(second_connection, "b"))

        turns = await book.recent_turns(first_connection, bot_guid=7)
        assert len(turns) == ledger.CONVERSATION_TURN_LIMIT
    finally:
        second_connection.close()


async def test_two_ambient_attempts_for_the_last_slot_yield_exactly_one(clean_ledger) -> None:
    """A bare COUNT(*) FOR UPDATE either deadlocks or lets both callers read the same count.

    With one slot left, exactly one of two simultaneous callers may have it.
    """
    _, first_connection = clean_ledger
    second_connection = await _connect()
    book = ledger.SidecarStore()

    try:
        async with first_connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_claude_ambient_attempt")
        await first_connection.commit()

        # Two of the three slots are already gone.
        for _ in range(2):
            assert await book.try_begin_ambient(first_connection, messages_per_hour=3, now=NOW) is True

        barrier = asyncio.Barrier(2)

        async def attempt(connection):
            await barrier.wait()
            return await book.try_begin_ambient(connection, messages_per_hour=3, now=NOW)

        results = await asyncio.gather(attempt(first_connection), attempt(second_connection))

        assert sorted(results) == [False, True]
    finally:
        second_connection.close()


async def test_the_named_lock_really_blocks_a_second_holder(clean_ledger) -> None:
    """MySQL itself reports the block, rather than the test inferring it from a timeout.

    An earlier version asserted the contender had not finished within a second, which a
    slow or unscheduled task satisfies just as well as a blocked one. Setting a short
    innodb_lock_wait_timeout on the contender turns "it was blocked" into a positive
    error the database raises: with a working lock the contender times out waiting, and
    with a broken one it simply succeeds and the test fails.
    """
    _, holder = clean_ledger
    contender = await _connect()

    try:
        await holder.begin()
        async with holder.cursor() as cursor:
            await ledger.acquire_named_lock(cursor, "test-key")

        async with contender.cursor() as cursor:
            await cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")

        await contender.begin()
        with pytest.raises(aiomysql.OperationalError) as caught:
            async with contender.cursor() as cursor:
                await ledger.acquire_named_lock(cursor, "test-key")

        # 1205 is ER_LOCK_WAIT_TIMEOUT. The database is asserting the contention.
        assert caught.value.args[0] == 1205
        await contender.rollback()

        # And once the holder releases, the same acquisition goes through.
        await holder.commit()
        await contender.begin()
        async with contender.cursor() as cursor:
            await ledger.acquire_named_lock(cursor, "test-key")
        await contender.commit()
    finally:
        contender.close()


async def test_a_different_key_is_not_blocked_by_a_held_one(clean_ledger) -> None:
    """Otherwise the bucketing below would be a global lock wearing a per bot name."""
    _, holder = clean_ledger
    other = await _connect()

    try:
        await holder.begin()
        async with holder.cursor() as cursor:
            await ledger.acquire_named_lock(cursor, "key-a")

        async def take_other():
            await other.begin()
            async with other.cursor() as cursor:
                await ledger.acquire_named_lock(cursor, "key-b")
            await other.commit()

        await asyncio.wait_for(take_other(), timeout=10.0)
        await holder.commit()
    finally:
        other.close()


async def test_the_lock_key_set_is_bounded(clean_ledger) -> None:
    """A per day or per bot key adds a permanent row forever.

    Deleting them instead is worse: removing a lock row while another transaction may
    still take that key reintroduces the missing-row race the helper exists to avoid.
    """
    book, connection = clean_ledger
    store = ledger.SidecarStore()

    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_claude_lock")
        await cursor.execute("DELETE FROM playerbot_claude_conversation_turn")
    await connection.commit()

    # Many bots and several days must not produce many keys.
    for bot_guid in range(0, CONVERSATION_LOCK_SAMPLE):
        await store.append_turn(connection, bot_guid=bot_guid, role="user", content="x", now=NOW)

    for offset in range(5):
        await book.reserve(
            connection,
            request_id=1000 + offset,
            attempt=1,
            max_cost_nano=1,
            priority=RequestPriority.IMMEDIATE_HUMAN,
            now=NOW + timedelta(days=offset),
        )

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_claude_lock")
        row = await cursor.fetchone()

    # One budget key plus at most one per conversation bucket, regardless of bot count
    # or how many days were touched.
    assert int(row[0]) <= 1 + ledger.CONVERSATION_LOCK_BUCKETS
    assert int(row[0]) < CONVERSATION_LOCK_SAMPLE


async def test_retiring_superseded_locks_is_opt_in_and_leaves_live_keys_alone(clean_ledger) -> None:
    """Not run automatically, because a rolling restart makes that unsafe.

    An older sidecar still running against this database uses the per day and per bot
    keys as its live locks, so removing them mid flight strips its mutual exclusion.
    Leaving them is bounded anyway: nothing creates either shape any more.
    """
    book, connection = clean_ledger

    async with connection.cursor() as cursor:
        await cursor.execute("DELETE FROM playerbot_claude_lock")
        await cursor.executemany(
            "INSERT INTO playerbot_claude_lock (lock_key) VALUES (%s)",
            [
                ("budget_day:2026-08-01",),
                ("budget_day:2026-08-02",),
                ("conversation:9000",),
                ("conversation:5",),
                ("budget_day",),
                ("ambient",),
            ],
        )
    await connection.commit()

    # ensure_schema must NOT remove them on its own.
    await book.ensure_schema(connection)
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT COUNT(*) FROM playerbot_claude_lock")
        row = await cursor.fetchone()
    assert int(row[0]) == 6

    removed = await book.retire_superseded_locks(connection)
    assert removed == 3  # two dated budget keys and the out of range conversation key

    async with connection.cursor() as cursor:
        await cursor.execute("SELECT lock_key FROM playerbot_claude_lock ORDER BY lock_key")
        rows = await cursor.fetchall()

    # The live keys survive, and so does the low numbered conversation key, which is
    # indistinguishable from a valid bucket.
    assert [r[0] for r in rows] == ["ambient", "budget_day", "conversation:5"]


# The façade the service actually talks to ------------------------------------------


@pytest.fixture
async def mysql_state() -> AsyncIterator[tuple[state_module.MySqlSidecarState, aiomysql.Pool]]:
    """A real MySqlSidecarState over a real pool.

    The unit suite substitutes an in-memory double for this interface, which proves the
    service's ordering but proves nothing about the delegation itself. A field passed to
    the wrong parameter, or a connection returned to the pool mid-transaction, is
    invisible there and visible here.
    """
    settings = _settings()
    state, pool = await state_module.open_state(settings, ceiling_nano=CEILING, reserve_ratio=QUARTER)
    # Every table this fixture's tests touch, not just the budget ones. Leaving the
    # ambient attempts behind made the first try_begin_ambient of a later test see a
    # rolling hour that was already full, which is a test failing for another test's
    # reason. Written out rather than looped over an interpolated name: a table name
    # cannot be a bound parameter, so a loop means building SQL from a string.
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("DELETE FROM playerbot_claude_budget_reservation")
            await cursor.execute("DELETE FROM playerbot_claude_budget_day")
            await cursor.execute("DELETE FROM playerbot_claude_profile")
            await cursor.execute("DELETE FROM playerbot_claude_conversation_turn")
            await cursor.execute("DELETE FROM playerbot_claude_career_decision")
            await cursor.execute("DELETE FROM playerbot_claude_ambient_attempt")
        await connection.commit()
    try:
        yield state, pool
    finally:
        await state_module.close_pool(pool)


async def test_the_state_facade_carries_every_profile_field_to_the_right_column(mysql_state) -> None:
    """A swapped pair of affinities is a silent personality change, so read them back.

    Both the double and the real implementation take a whole request, so a mismatched
    unpacking would agree with itself everywhere except here.
    """
    state, _ = mysql_state

    request = protocol.parse_request(
        json.dumps(
            {
                "schema_version": 3,
                "token": TOKEN,
                "request_id": 1,
                "channel": "whisper",
                "bot_guid": 4242,
                "speaker_guid": 9001,
                "bot_name": "Facadebot",
                "speaker_name": "Speaker",
                "profile_version": 2,
                # Deliberately all different, so any two swapped fields fail.
                "crafting_affinity": 11,
                "gathering_affinity": 22,
                "exploration_affinity": 33,
                "sociability": 44,
                "voice": "wry",
                "event_kind": 0,
                "subject_id": 0,
                "occurrence": 0,
                "message": "hello",
            }
        ).encode(),
        TOKEN,
    )

    await state.record_profile(request, now=NOW)

    assert await state.get_profile(bot_guid=4242) == {
        "profile_version": 2,
        "crafting_affinity": 11,
        "gathering_affinity": 22,
        "exploration_affinity": 33,
        "sociability": 44,
        "voice": "wry",
        "bot_name": "Facadebot",
    }


async def test_the_state_facade_reserves_settles_and_reports(mysql_state) -> None:
    state, _ = mysql_state
    one = budget.usd_to_nano("1.00")

    decision, reservation = await state.reserve(
        request_id=501, max_cost_nano=one, priority=RequestPriority.IMMEDIATE_HUMAN, now=NOW
    )
    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None
    assert (await state.budget_state(now=NOW)).outstanding_nano == one

    assert await state.settle(reservation=reservation, actual_cost_nano=one // 4, now=NOW) is True

    after = await state.budget_state(now=NOW)
    assert after.outstanding_nano == 0
    assert after.settled_nano == one // 4


async def test_the_state_facade_never_leaks_a_transaction_back_into_the_pool(mysql_state) -> None:
    """aiomysql DISCARDS a connection released mid-transaction, so a leak is invisible.

    It shows up only as connection churn: every read silently closes a socket and opens
    a new one. Reads are the risk, because autocommit is off and even a bare SELECT
    starts a transaction that nothing then closes.
    """
    state, pool = mysql_state

    for _ in range(4):
        await state.get_profile(bot_guid=999999)
        await state.recent_turns(bot_guid=999999)
        await state.budget_state(now=NOW)

    assert pool.size <= state_module.POOL_MAX_SIZE
    assert pool.freesize == pool.size, "a connection was discarded, so one was released mid-transaction"


async def test_a_repeated_request_id_gets_its_own_attempt_rather_than_a_duplicate_key(
    mysql_state,
) -> None:
    """The worldserver's request ids restart at 1 on every process start.

    So request id 1 comes round again after a restart. With a fixed attempt number the
    second reservation violates the (request_id, attempt) unique key and the request
    fails; deriving the attempt is what makes Definition of Done 3 hold for a repeat of
    any kind.
    """
    state, pool = mysql_state
    one = budget.usd_to_nano("1.00")

    for _ in range(3):
        decision, reservation = await state.reserve(
            request_id=1, max_cost_nano=one, priority=RequestPriority.IMMEDIATE_HUMAN, now=NOW
        )
        assert decision is AdmissionDecision.ADMITTED
        assert reservation is not None

    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT attempt FROM playerbot_claude_budget_reservation "
                "WHERE request_id = 1 ORDER BY attempt"
            )
            rows = await cursor.fetchall()
        await connection.commit()

    assert [int(row[0]) for row in rows] == [1, 2, 3]


async def test_the_state_facade_carries_every_remaining_operation(mysql_state) -> None:
    """The methods the reserve and settle test does not touch.

    Each is a delegation with its own parameter list, and a delegation is exactly where a
    misrouted argument hides: the in-memory double the unit suite uses takes the same
    arguments and would agree with a wrong mapping. Read back rather than assumed.
    """
    state, pool = mysql_state

    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is True
    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is True
    assert await state.try_begin_ambient(messages_per_hour=2, now=NOW) is False

    await state.append_turn(bot_guid=7788, role="user", content="first", now=NOW)
    await state.append_turn(bot_guid=7788, role="assistant", content="second", now=NOW)
    assert await state.recent_turns(bot_guid=7788) == [("user", "first"), ("assistant", "second")]
    # A different bot's memory is its own.
    assert await state.recent_turns(bot_guid=7789) == []

    await state.record_career_decision(
        bot_guid=7788,
        career_version=1,
        candidate_token="career-def456",
        spending_style="progression",
        now=NOW,
    )
    # Read straight from the table rather than through a facade accessor. Nothing in the
    # sidecar reads a career decision back yet, and adding a method so a test can call it
    # is production surface that exists only for the test.
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT career_version, candidate_token, spending_style "
                "FROM playerbot_claude_career_decision WHERE bot_guid = %s",
                (7788,),
            )
            row = await cursor.fetchone()
        await connection.commit()
    assert row is not None
    assert (int(row[0]), row[1], row[2]) == (1, "career-def456", "progression")

    reserved = await state.reserve(
        request_id=990,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.BACKGROUND,
        now=NOW,
    )
    assert reserved[1] is not None
    assert await state.release(reservation=reserved[1]) is True
    assert (await state.budget_state(now=NOW)).outstanding_nano == 0


class _StubAdapter(claude.ClaudeAdapter):
    """Stands in for the Anthropic SDK so no HTTP request is made. Everything else is real."""

    reply = "A fine day for fishing."

    def __init__(self) -> None:
        # Deliberately no super().__init__(): the stub never builds a real client.
        pass

    def count_input_tokens(self, request, history) -> int:
        return 100

    def generate_reply(self, request, history):
        return self.reply, claude.UsageTotals(input_tokens=100, output_tokens=10)


def _service_request_payload(request_id: int, bot_guid: int) -> bytes:
    return json.dumps(
        {
            "schema_version": 3,
            "token": TOKEN,
            "request_id": request_id,
            "channel": "whisper",
            "bot_guid": bot_guid,
            "speaker_guid": 9001,
            "bot_name": "Facadebot",
            "speaker_name": "Speaker",
            "profile_version": 2,
            "crafting_affinity": 11,
            "gathering_affinity": 22,
            "exploration_affinity": 33,
            "sociability": 44,
            "voice": "wry",
            "event_kind": 0,
            "subject_id": 0,
            "occurrence": 0,
            "message": "What do you enjoy doing?",
        }
    ).encode()


async def test_a_complete_service_request_runs_against_real_mysql(mysql_state) -> None:
    """The whole pipeline through the real facade, not the in-memory double.

    Every unit test of the service substitutes a double for the state, so the wiring
    between the service and MySQL is exercised by nothing else: a facade method that
    reached the wrong ledger call, or a connection returned mid-transaction under real
    load, would pass the entire unit suite. This reserves, generates, settles, and writes
    memory through the real pool, then reads the rows back.
    """
    state, pool = mysql_state
    request_id, bot_guid = 4242, 55555

    config = app.SidecarConfig(
        enable=True,
        bridge_port=0,
        daily_budget_usd="10.00",
        human_budget_reserve_ratio="0.25",
        input_usd_per_mtok="1.00",
        output_usd_per_mtok="5.00",
    )
    service = app.SidecarService(
        config=config, token=TOKEN, adapter=_StubAdapter(), store=state, now=lambda: NOW
    )

    payload = await service.process_payload(_service_request_payload(request_id, bot_guid))
    assert payload is not None
    assert json.loads(payload)["message"] == _StubAdapter.reply

    request = protocol.parse_request(_service_request_payload(request_id, bot_guid), TOKEN)
    assert await state.recent_turns(bot_guid=bot_guid) == [
        ("user", claude.build_user_message(request)),
        ("assistant", _StubAdapter.reply),
    ]

    profile = await state.get_profile(bot_guid=bot_guid)
    assert profile is not None
    assert profile["bot_name"] == "Facadebot"
    assert profile["gathering_affinity"] == 22

    after = await state.budget_state(now=NOW)
    assert after.outstanding_nano == 0
    # 100 input at $1/Mtok plus 10 output at $5/Mtok.
    assert after.settled_nano == budget.usd_to_nano("0.00015")

    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT state, actual_cost_nano, attempt FROM playerbot_claude_budget_reservation "
                "WHERE request_id = %s",
                (request_id,),
            )
            rows = await cursor.fetchall()
        await connection.commit()

    assert len(rows) == 1
    assert rows[0][0] == "settled"
    assert int(rows[0][1]) == after.settled_nano
    assert int(rows[0][2]) == 1

    # The pool is intact: nothing was discarded for holding a transaction open.
    assert pool.freesize == pool.size


async def test_a_cost_that_would_overflow_the_day_total_still_opens_the_circuit(clean_ledger) -> None:
    """The breaker must survive the exact report it exists to catch.

    settled_nano is BIGINT UNSIGNED. Clamping only the incoming value still overflows the
    SUM once anything has been spent: MySQL rejects the statement in strict mode, the
    transaction rolls back, and the breaker stays shut while the reservation stays
    outstanding forever. That is the same failure mode as round 1's unstorable value, one
    level up, so this drives the addition rather than the addend.
    """
    book, connection = clean_ledger

    # Reserve first, while the day is empty and admission is possible.
    decision, reservation = await book.reserve(
        connection,
        request_id=7001,
        max_cost_nano=budget.usd_to_nano("1.00"),
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert decision is AdmissionDecision.ADMITTED
    assert reservation is not None

    # THEN put the day's total within a whisker of the column's ceiling. This is the
    # state a previous impossible report would have left behind, and it has to be set
    # after admission because a ledger holding that much would refuse every request.
    async with connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE playerbot_claude_budget_day SET settled_nano = %s WHERE usage_date = %s",
            (budget.MAX_STORABLE_NANO - 10, ledger.utc_day(NOW)),
        )
    await connection.commit()

    # A report far above both the reservation and the remaining headroom.
    assert (
        await book.settle(
            connection,
            reservation=reservation,
            actual_cost_nano=budget.MAX_STORABLE_NANO,
            now=NOW,
        )
        is True
    )

    state = await book.snapshot(connection, now=NOW)
    assert state.circuit_open is True, "the breaker must fire rather than the update failing"
    assert state.settled_nano == budget.MAX_STORABLE_NANO

    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT circuit_reason FROM playerbot_claude_budget_day WHERE usage_date = %s",
            (ledger.utc_day(NOW),),
        )
        row = await cursor.fetchone()
    await connection.commit()

    assert row is not None
    # The reported figure survives verbatim, and the saturation is named.
    assert str(budget.MAX_STORABLE_NANO) in row[0]
    assert "saturated" in row[0]

    # And admission is closed afterwards.
    denied, _ = await book.reserve(
        connection,
        request_id=7002,
        max_cost_nano=1,
        priority=RequestPriority.IMMEDIATE_HUMAN,
        now=NOW,
    )
    assert denied is AdmissionDecision.DENIED_CIRCUIT_OPEN
