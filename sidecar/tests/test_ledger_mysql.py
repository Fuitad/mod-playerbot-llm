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

        async def reserve(connection, request_id):
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
