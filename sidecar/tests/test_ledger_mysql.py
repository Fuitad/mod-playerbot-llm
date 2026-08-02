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
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from playerbot_claude import budget, ledger
from playerbot_claude.budget import AdmissionDecision, RequestPriority

pytestmark = [pytest.mark.mysql, pytest.mark.asyncio]

DSN = os.environ.get("PLAYERBOT_CLAUDE_TEST_MYSQL_DSN")

if not DSN:  # pragma: no cover - the skip is the point
    pytest.skip("PLAYERBOT_CLAUDE_TEST_MYSQL_DSN is not set", allow_module_level=True)

aiomysql = pytest.importorskip("aiomysql")

CEILING = budget.usd_to_nano("10.00")
QUARTER = Decimal("0.25")
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# More distinct bots than there are lock buckets, so the bound is actually exercised.
CONVERSATION_LOCK_SAMPLE = 400


def _dsn_parts() -> dict[str, object]:
    settings = _settings()
    return {
        "host": settings.host,
        "port": settings.port,
        "user": settings.user,
        "password": settings.password,
        "db": settings.database,
        "autocommit": False,
    }


def _settings():
    from playerbot_claude.app import PlayerbotsDatabaseSettings

    return PlayerbotsDatabaseSettings.parse_info(DSN)


async def _connect():
    return await aiomysql.connect(**_dsn_parts())


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
    """The primitive, proven deterministically rather than by hoping two tasks overlap.

    Every barrier in this file synchronizes BEFORE a transaction begins, which does not
    guarantee the two transactions are ever inside the lock at the same time: a
    sequential schedule satisfies the barrier and passes. That mistake has now been made
    twice, so this test does not rely on scheduling at all.

    One connection takes the lock and holds it open. A second tries to take the same key
    and must NOT complete while the first holds it. Releasing the first lets the second
    through, which is what mutual exclusion means.
    """
    _, holder = clean_ledger
    contender = await _connect()

    try:
        await holder.begin()
        async with holder.cursor() as cursor:
            await ledger.acquire_named_lock(cursor, "test-key")

        async def take_it():
            await contender.begin()
            async with contender.cursor() as cursor:
                await ledger.acquire_named_lock(cursor, "test-key")
            await contender.commit()

        blocked = asyncio.create_task(take_it())

        # It must still be waiting while the first transaction holds the key.
        done, _ = await asyncio.wait({blocked}, timeout=1.0)
        assert not done, "second holder acquired the lock while the first still held it"

        await holder.commit()

        # And it must go through once the holder releases.
        await asyncio.wait_for(blocked, timeout=10.0)
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
