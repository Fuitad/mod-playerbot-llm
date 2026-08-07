"""Budget admission policy tests.

Every case here is arithmetic over values, with no database and no clock, which is why
these can assert the actual rule rather than a refusal that came from an unrelated gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from playerbot_llm import budget
from playerbot_llm.budget import (
    AdmissionDecision,
    BudgetConfigurationError,
    BudgetState,
    RequestPriority,
)

CEILING = budget.usd_to_nano("10.00")
QUARTER = Decimal("0.25")


def _admit(
    max_cost_nano: int | None,
    priority: RequestPriority = RequestPriority.IMMEDIATE_HUMAN,
    state: BudgetState | None = None,
    ratio: Decimal = QUARTER,
    ceiling: int = CEILING,
) -> AdmissionDecision:
    return budget.admit(
        ceiling_nano=ceiling,
        state=state or BudgetState(),
        max_cost_nano=max_cost_nano,
        priority=priority,
        reserve_ratio=ratio,
    )


# Configuration --------------------------------------------------------------------


def test_the_configured_ceiling_is_the_only_ceiling() -> None:
    """No hard-coded maximum sits above it.

    The previous code capped the configured value at 5.00, which silently ignored what
    an operator asked for. A large configured budget is a large configured budget.
    """
    assert budget.validate_daily_ceiling("500.00") == budget.usd_to_nano("500.00")
    assert budget.validate_daily_ceiling("0.01") == 10_000_000
    assert not hasattr(budget, "MAX_DAILY_BUDGET_USD")


def test_a_ceiling_of_zero_or_less_is_refused() -> None:
    for bad in ("0", "0.00", "-1"):
        with pytest.raises(BudgetConfigurationError):
            budget.validate_daily_ceiling(bad)


def test_a_ceiling_the_ledger_cannot_record_is_refused_rather_than_clamped() -> None:
    """The one hard limit, and it is physical rather than policy.

    The ledger stores money in the deployed DECIMAL(12, 6) columns. Under a ceiling above
    what those hold, honest traffic eventually saturates the day's spent total, and the
    settle path would have to either overflow or claim a breach that never happened.
    Refusing is what keeps "saturation implies a breach" true.

    Refused rather than quietly clamped: silently substituting a different number is
    exactly what removing the old hard-coded cap was meant to stop.
    """
    at_the_limit = budget.nano_to_usd_string(budget.MAX_STORABLE_NANO)
    assert budget.validate_daily_ceiling(at_the_limit) == budget.MAX_STORABLE_NANO

    with pytest.raises(BudgetConfigurationError, match="ledger can record"):
        budget.validate_daily_ceiling(Decimal(budget.MAX_STORABLE_NANO + 1) / budget.NANO)

    # And it constrains nothing anybody would configure for a realm: just under a million
    # dollars of generation usage in a single UTC day.
    assert budget.validate_daily_ceiling("999999") > 0


def test_a_float_amount_goes_through_its_decimal_string_not_its_bits() -> None:
    """Decimal(1.1) is 1.10000000000000008881784197001252323389053344726562500.

    A ceiling built from that is not the ceiling anyone configured. The config path keeps
    the value as text end to end; this conversion exists for callers already holding a
    float, and it must agree with the text path exactly.
    """
    assert budget.usd_to_nano(1.10) == budget.usd_to_nano("1.10")
    assert budget.usd_to_nano(0.1) + budget.usd_to_nano(0.2) == budget.usd_to_nano("0.3")

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(BudgetConfigurationError):
            budget.usd_to_nano(bad)


def test_the_reserve_ratio_spans_zero_through_one_inclusive() -> None:
    # Both ends are legitimate configurations rather than mistakes: zero means no
    # protection, one means background work never runs.
    assert budget.validate_reserve_ratio("0") == Decimal(0)
    assert budget.validate_reserve_ratio("1") == Decimal(1)
    assert budget.validate_reserve_ratio("0.25") == Decimal("0.25")

    for bad in ("-0.01", "1.01", "banana"):
        with pytest.raises(BudgetConfigurationError):
            budget.validate_reserve_ratio(bad)


# Conservative maximum cost --------------------------------------------------------


def test_the_maximum_cost_is_charged_at_the_maximum_output() -> None:
    # 1000 input at $3/Mtok plus 500 output at $15/Mtok = 0.003 + 0.0075 = 0.0105
    cost = budget.conservative_max_cost_nano(1000, 500, "3", "15")
    assert cost == budget.usd_to_nano("0.0105")


def test_the_maximum_rounds_up_because_a_maximum_that_rounds_down_is_not_one() -> None:
    # One token at a price that cannot divide evenly into nano-USD.
    cost = budget.conservative_max_cost_nano(1, 0, "3", "15")
    assert cost == 3000  # 3/1_000_000 USD = 3000 nano exactly
    assert budget.conservative_max_cost_nano(1, 0, "0.0000001", "0") == 1


def test_unknown_pricing_yields_no_maximum_at_all() -> None:
    """Fail closed. A request whose cost cannot be bounded cannot be admitted."""
    assert budget.conservative_max_cost_nano(1000, 500, None, "15") is None
    assert budget.conservative_max_cost_nano(1000, 500, "3", None) is None
    assert budget.conservative_max_cost_nano(1000, 500, "not-a-price", "15") is None
    assert budget.conservative_max_cost_nano(-1, 500, "3", "15") is None
    assert budget.conservative_max_cost_nano(1000, 500, "-3", "15") is None


# Admission ------------------------------------------------------------------------


def test_a_request_that_fits_is_admitted() -> None:
    assert _admit(budget.usd_to_nano("1.00")) is AdmissionDecision.ADMITTED


def test_two_concurrent_reservations_cannot_jointly_exceed_the_ceiling() -> None:
    """Definition of Done 1.

    The first reservation is outstanding, not settled, when the second is decided. A
    ledger that counted only settled money would admit both and cross the ceiling by
    the size of whichever landed second.
    """
    half = CEILING // 2
    first = BudgetState(outstanding_nano=half)

    # The second half still fits exactly.
    assert _admit(half, state=first) is AdmissionDecision.ADMITTED

    # One nano more does not.
    assert _admit(half + 1, state=first) is AdmissionDecision.DENIED_CEILING


def test_the_ceiling_binds_a_human_request_too() -> None:
    # Human work is protected FROM background work. It is not exempt from the ceiling.
    nearly_spent = BudgetState(settled_nano=CEILING - 100)
    assert _admit(101, priority=RequestPriority.IMMEDIATE_HUMAN, state=nearly_spent) is (
        AdmissionDecision.DENIED_CEILING
    )


def test_background_work_is_denied_at_the_reserve_while_a_human_may_use_it() -> None:
    """Definition of Done 2, and the only rule that treats the priorities differently."""
    # 25% of 10.00 is protected, so background work may use at most 7.50.
    spent = BudgetState(settled_nano=budget.usd_to_nano("7.00"))
    request = budget.usd_to_nano("1.00")

    assert _admit(request, priority=RequestPriority.BACKGROUND, state=spent) is (
        AdmissionDecision.DENIED_RESERVE
    )
    assert _admit(request, priority=RequestPriority.IMMEDIATE_HUMAN, state=spent) is (
        AdmissionDecision.ADMITTED
    )


def test_background_work_may_use_everything_up_to_the_reserve() -> None:
    spent = BudgetState(settled_nano=budget.usd_to_nano("7.00"))

    # Exactly to the boundary is allowed; one nano past it is not.
    assert _admit(budget.usd_to_nano("0.50"), priority=RequestPriority.BACKGROUND, state=spent) is (
        AdmissionDecision.ADMITTED
    )
    assert (
        _admit(budget.usd_to_nano("0.50") + 1, priority=RequestPriority.BACKGROUND, state=spent)
        is AdmissionDecision.DENIED_RESERVE
    )


def test_a_reserve_of_zero_lets_background_work_use_the_whole_ceiling() -> None:
    spent = BudgetState(settled_nano=budget.usd_to_nano("9.00"))
    assert (
        _admit(
            budget.usd_to_nano("1.00"),
            priority=RequestPriority.BACKGROUND,
            state=spent,
            ratio=Decimal(0),
        )
        is AdmissionDecision.ADMITTED
    )


def test_a_reserve_of_one_stops_background_work_entirely() -> None:
    assert _admit(1, priority=RequestPriority.BACKGROUND, ratio=Decimal(1)) is (
        AdmissionDecision.DENIED_RESERVE
    )
    # And a human is unaffected by it.
    assert _admit(1, priority=RequestPriority.IMMEDIATE_HUMAN, ratio=Decimal(1)) is (
        AdmissionDecision.ADMITTED
    )


def test_unknown_pricing_denies_before_anything_else_is_considered() -> None:
    assert _admit(None) is AdmissionDecision.DENIED_UNKNOWN_PRICING


def test_an_open_circuit_denies_every_priority() -> None:
    """Definition of Done 7. The ceiling was already crossed by an unauthorised amount."""
    broken = BudgetState(circuit_open=True)

    for priority in RequestPriority:
        assert _admit(1, priority=priority, state=broken) is AdmissionDecision.DENIED_CIRCUIT_OPEN

    # Even a request that would otherwise be free.
    assert _admit(0, state=broken) is AdmissionDecision.DENIED_CIRCUIT_OPEN


def test_an_unusable_ceiling_or_cost_is_refused_rather_than_admitted() -> None:
    assert _admit(1, ceiling=0) is AdmissionDecision.DENIED_INVALID_REQUEST
    assert _admit(-1) is AdmissionDecision.DENIED_INVALID_REQUEST


# The circuit breaker ---------------------------------------------------------------


def test_a_cost_above_its_reservation_opens_the_circuit() -> None:
    assert budget.circuit_should_open(1000, 1001) is True
    assert budget.circuit_should_open(1000, 1000) is False
    assert budget.circuit_should_open(1000, 0) is False


def test_a_nonsensical_cost_opens_the_circuit_too() -> None:
    # Fail closed on values that should be impossible rather than reasoning about which
    # impossible value is safe.
    assert budget.circuit_should_open(1000, -1) is True
    assert budget.circuit_should_open(-1, 0) is True


def test_the_reserve_floor_rounds_up_so_a_fraction_never_costs_the_player() -> None:
    """Default half-even rounding hands background work part of the protected slice.

    The reserve is the amount a player is guaranteed, so the rounding error has to fall
    on the side that protects rather than the side that spends.
    """
    # A ceiling and ratio whose product is not a whole nano.
    ceiling = 1_000_000_001
    assert budget.reserve_floor_nano(ceiling, Decimal("0.25")) == 250_000_001

    # And the boundary case that half-even would have rounded down.
    assert budget.reserve_floor_nano(5, Decimal("0.5")) == 3


def test_an_unstorable_cost_is_clamped_so_the_breaker_can_still_fire() -> None:
    """The breaker exists for impossible reports, so storing one must not fail.

    A value outside BIGINT UNSIGNED would fail the SQL update, roll the transaction
    back, and leave the reservation outstanding with the breaker never firing. Clamping
    loses the exact number, which the reason string keeps, and preserves the incident.
    """
    assert budget.storable_actual_cost_nano(-5) == 0
    assert budget.storable_actual_cost_nano(budget.MAX_STORABLE_NANO + 1) == budget.MAX_STORABLE_NANO
    assert budget.storable_actual_cost_nano(1000) == 1000

    # Both still open the circuit, which is the point of clamping rather than rejecting.
    assert budget.circuit_should_open(1000, -5) is True
    assert budget.circuit_should_open(1000, budget.MAX_STORABLE_NANO + 1) is True


def test_a_stored_cost_has_one_exact_six_decimal_display() -> None:
    assert budget.nano_to_fixed_usd_string(0) == "0.000000"
    assert budget.nano_to_fixed_usd_string(budget.quantize_storable_nano(1)) == "0.000001"
    assert budget.nano_to_fixed_usd_string(budget.MAX_STORABLE_NANO) == "999999.999999"

    for invalid in (-1, 1, budget.MAX_STORABLE_NANO + budget.STORAGE_SCALE_NANO):
        with pytest.raises(ValueError, match="stored microdollar"):
            budget.nano_to_fixed_usd_string(invalid)
