"""Budget admission policy: who may spend, how much, and when the door closes.

Every decision here is a pure function of its arguments. No clock, no database, no
network. That is deliberate and it is the same split the worldserver side settled on
after two tasks of review: a rule that lives inside a transaction is a rule the tests
reach only through a transaction, and the arithmetic is the part that has to be right.

Money is integer nano-USD throughout. Floats do not sum to a ceiling reliably, and a
budget whose enforcement depends on the last bit of a double is not enforced. The ledger
stores decimal dollars, because the deployed columns are ``DECIMAL(12, 6)``; the
conversion happens there, and :func:`quantize_storable_nano` is what keeps the two
representations describing the same amount.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

NANO = 1_000_000_000


class RequestPriority(enum.Enum):
    """Who is waiting on this request.

    ``IMMEDIATE_HUMAN`` is a person who spoke and is waiting for an answer. Everything
    else is work the server chose to do: bot-only continuation, background memory
    extraction, moderation classification. The distinction exists because a protected
    slice of the ceiling is held for the first kind, so a quiet server that spent the
    day on background work still has something left when somebody actually talks.
    """

    IMMEDIATE_HUMAN = "immediate_human"
    BACKGROUND = "background"


class RequestKind(enum.Enum):
    """What a reservation is paying for.

    The values are the deployed ``request_kind`` enumerators, character for character.
    They are not the sidecar's own vocabulary: the column belongs to the
    mod-playerbots-llm schema, and a value this enum can produce that the column
    cannot hold is a write that fails in production and nowhere else.

    ``MODERATION_CLASSIFICATION`` is produced by the roleplay assessment preflight: the
    classifier that reports what a human line is doing before the worldserver decides what
    follows. It is classification work, never generation, which is why the assessment path
    reserves under this kind rather than under a chat kind.
    """

    CHAT_RESPONSE = "chat_response"
    BACKSTORY_GENERATION = "backstory_generation"
    MEMORY_EXTRACTION = "memory_extraction"
    MODERATION_CLASSIFICATION = "moderation_classification"
    CAREER_GENERATION = "career_generation"


class AdmissionDecision(enum.Enum):
    ADMITTED = "admitted"
    DENIED_CEILING = "denied_ceiling"
    DENIED_RESERVE = "denied_reserve"
    DENIED_CIRCUIT_OPEN = "denied_circuit_open"
    DENIED_UNKNOWN_PRICING = "denied_unknown_pricing"
    DENIED_INVALID_REQUEST = "denied_invalid_request"


@dataclass(frozen=True)
class BudgetState:
    """What the ledger already owes, in nano-USD.

    ``settled_nano`` is money actually charged. ``outstanding_nano`` is the sum of live
    reservations: work admitted but not yet completed. Both count against the ceiling,
    because a reservation that is not counted is a reservation two concurrent requests
    can each spend.
    """

    settled_nano: int = 0
    outstanding_nano: int = 0
    circuit_open: bool = False

    @property
    def committed_nano(self) -> int:
        return self.settled_nano + self.outstanding_nano


class BudgetConfigurationError(ValueError):
    """The configured ceiling or reserve ratio cannot be enforced as written."""


def usd_to_nano(usd: Decimal | str | int | float) -> int:
    """Converts a dollar amount to integer nano-USD.

    A float is routed through its decimal string rather than through Decimal(float),
    because Decimal(1.1) is 1.100000000000000088817841970012523233890533447265625 and a
    ceiling built from that is not the ceiling anyone configured. The config path keeps
    the value as text end to end; this conversion exists for callers that already hold a
    float and cannot.
    """

    if isinstance(usd, float):
        if usd != usd or usd in (float("inf"), float("-inf")):
            raise BudgetConfigurationError(f"not a finite amount: {usd!r}")
        usd = str(usd)

    try:
        amount = Decimal(usd)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BudgetConfigurationError(f"not a usable amount: {usd!r}") from error

    if not amount.is_finite():
        raise BudgetConfigurationError(f"not a finite amount: {usd!r}")

    return int((amount * NANO).to_integral_value())


def nano_to_usd_string(nano: int) -> str:
    """Exact decimal rendering of nano-USD, without float artifacts (2900000 -> "0.0029").

    Built by integer division rather than by formatting a float, so a reported figure is
    the ledger's figure and not the nearest double to it.
    """

    sign = "-" if nano < 0 else ""
    whole, fraction = divmod(abs(nano), NANO)
    digits = f"{fraction:09d}".rstrip("0")
    return f"{sign}{whole}.{digits}" if digits else f"{sign}{whole}"


def validate_daily_ceiling(usd: Decimal | str | int) -> int:
    """The configured ceiling is the sole limit. Returns it in nano-USD.

    No policy maximum sits above it. An operator who configures a large budget has
    configured a large budget, and a second, lower ceiling in the code would silently
    ignore what they asked for.

    There is one hard limit, and it is physical rather than a policy: the ledger records
    money in ``DECIMAL(12, 6)`` columns, so a ceiling above what those can hold is a
    ceiling the ledger cannot enforce. Honest traffic under such a ceiling would eventually
    saturate the day's spent total, and the settle path would have to either overflow or
    report a breach that never happened. Refused LOUDLY rather than quietly clamped: an
    unenforceable budget is a configuration mistake, and silently substituting a different
    number is exactly what removing the old hard-coded cap was meant to stop.

    The limit is 999999.999999 USD in one day. It was roughly 18.4 billion while the
    sidecar kept its own ``BIGINT UNSIGNED`` nano columns; the deployed schema records
    decimal dollars instead, and this is what that shape can hold. It still constrains
    nothing a realm would configure on purpose.
    """

    ceiling = usd_to_nano(usd)
    if ceiling <= 0:
        raise BudgetConfigurationError("daily budget must be greater than zero")

    if ceiling > MAX_STORABLE_NANO:
        raise BudgetConfigurationError(
            "daily budget exceeds what the ledger can record "
            f"(limit {nano_to_usd_string(MAX_STORABLE_NANO)} USD)"
        )

    return ceiling


def validate_reserve_ratio(ratio: Decimal | str | int) -> Decimal:
    """The protected share of the ceiling, from 0 through 1 inclusive.

    Zero means no protection and one means background work never runs, and both are
    legitimate configurations rather than mistakes to reject.
    """

    try:
        value = Decimal(ratio)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BudgetConfigurationError(f"not a usable ratio: {ratio!r}") from error

    if not value.is_finite() or value < 0 or value > 1:
        raise BudgetConfigurationError("reserve ratio must be between 0 and 1 inclusive")

    return value


def reserve_floor_nano(ceiling_nano: int, reserve_ratio: Decimal) -> int:
    """How much of the ceiling background work may not touch.

    Rounded UP. Default half-even rounding lets a fractional reserve round down, which
    hands background work part of the slice being protected. The whole point of the
    reserve is that it is the amount a player is guaranteed, so the rounding error has
    to fall on the side that protects rather than the side that spends.
    """

    return int((Decimal(ceiling_nano) * reserve_ratio).to_integral_value(rounding="ROUND_CEILING"))


def token_cost_nano(
    input_tokens: int,
    output_tokens: int,
    input_usd_per_mtok: Decimal | str | int | None,
    output_usd_per_mtok: Decimal | str | int | None,
) -> int | None:
    """What a given token count costs, or None when that cannot be known.

    Unknown or nonsensical pricing returns None rather than a guess. A cost that cannot
    be computed cannot be admitted under a ceiling, and inventing a price is how a budget
    silently stops being one.
    """

    if input_tokens < 0 or output_tokens < 0:
        return None

    if input_usd_per_mtok is None or output_usd_per_mtok is None:
        return None

    try:
        input_price = Decimal(input_usd_per_mtok)
        output_price = Decimal(output_usd_per_mtok)
    except (InvalidOperation, TypeError, ValueError):
        return None

    if not input_price.is_finite() or not output_price.is_finite():
        return None

    if input_price < 0 or output_price < 0:
        return None

    per_token = (input_price * input_tokens + output_price * output_tokens) / Decimal(1_000_000)

    # Rounded UP, in both of this function's uses. For the reservation, a maximum that
    # rounds down is not a maximum. For the settlement, rounding a sub-nano fraction
    # toward the ceiling keeps the error on the side that protects the budget.
    return int((per_token * NANO).to_integral_value(rounding="ROUND_CEILING"))


def conservative_max_cost_nano(
    input_tokens: int,
    max_output_tokens: int,
    input_usd_per_mtok: Decimal | str | int | None,
    output_usd_per_mtok: Decimal | str | int | None,
) -> int | None:
    """The most this request could possibly cost, or None when that cannot be known.

    Charged up front at the maximum output length rather than estimated, because an
    estimate that is ever low is a ceiling that can be crossed. The caller denies on
    None: a request whose cost cannot be bounded cannot be admitted under a bound.
    """

    return token_cost_nano(input_tokens, max_output_tokens, input_usd_per_mtok, output_usd_per_mtok)


def admit(
    *,
    ceiling_nano: int,
    state: BudgetState,
    max_cost_nano: int | None,
    priority: RequestPriority,
    reserve_ratio: Decimal,
) -> AdmissionDecision:
    """Decides whether one request may be reserved against the ledger.

    The order matters and is deliberate:

    1. An open circuit denies everything. It opens when the provider reported a cost
       above the maximum that was reserved, which means the ceiling was already crossed
       by an amount nobody authorised, and the only safe response is to stop spending.
    2. Unknown pricing denies, because an unbounded cost cannot fit under a bound.
    3. The total ceiling denies both priorities. Human work is protected FROM background
       work; it is not exempt from the ceiling.
    4. The reserve denies background work only. This is the one rule that treats the two
       priorities differently, and it treats them differently in exactly one direction.
    """

    if state.circuit_open:
        return AdmissionDecision.DENIED_CIRCUIT_OPEN

    if max_cost_nano is None:
        return AdmissionDecision.DENIED_UNKNOWN_PRICING

    if ceiling_nano <= 0 or max_cost_nano < 0:
        return AdmissionDecision.DENIED_INVALID_REQUEST

    projected = state.committed_nano + max_cost_nano

    # The total ceiling binds every request, including a human's.
    if projected > ceiling_nano:
        return AdmissionDecision.DENIED_CEILING

    if priority is RequestPriority.BACKGROUND:
        floor = reserve_floor_nano(ceiling_nano, reserve_ratio)
        if projected > ceiling_nano - floor:
            return AdmissionDecision.DENIED_RESERVE

    return AdmissionDecision.ADMITTED


# One microdollar, in nano-USD. The deployed money columns are DECIMAL(12, 6), so six
# fractional digits are all a stored amount can carry, while the arithmetic above runs at
# nine.
STORAGE_SCALE_NANO = 1_000

# The widest value the ledger's DECIMAL(12, 6) columns can hold: 999999.999999 USD. A
# reported cost outside this cannot be stored at all, which matters because the breaker
# exists precisely for impossible reports: if storing one fails, the transaction rolls
# back and the breaker never fires for the case it was built for.
MAX_STORABLE_NANO = 999_999_999_999_000


def nano_to_fixed_usd_string(nano: int) -> str:
    """Render one exact stored microdollar amount with six fractional digits."""

    if nano < 0 or nano > MAX_STORABLE_NANO or nano % STORAGE_SCALE_NANO != 0:
        raise ValueError("cost is not an exact stored microdollar amount")

    whole, fraction = divmod(nano, NANO)
    return f"{whole}.{fraction // STORAGE_SCALE_NANO:06d}"


def quantize_storable_nano(nano: int) -> int:
    """Rounds a nano amount UP to something a DECIMAL(12, 6) column holds exactly.

    Rounded up rather than to nearest, for the same reason every other rounding in this
    module goes that way: on a reservation, a maximum that rounds down is not a maximum,
    and on a settlement, rounding toward the ceiling keeps the error on the side that
    protects the budget.

    The ledger quantizes BEFORE deciding, not on the way to the database, so the number it
    admitted against and the number the column holds are the same number. Quantizing only
    at the write would leave the running totals disagreeing with the rows they are meant
    to be the sum of, by up to 999 nano a row, forever.
    """

    if nano <= 0:
        return 0

    return -(-nano // STORAGE_SCALE_NANO) * STORAGE_SCALE_NANO


def storable_actual_cost_nano(actual_cost_nano: int) -> int:
    """Fits a reported cost into what the ledger can physically record.

    Two adjustments, in this order. Rounded UP to the microdollar the column can hold,
    then clamped to the widest value it can hold. Rounding first and clamping second is
    what keeps the result inside the bound, since the bound is itself a whole number of
    microdollars.

    Deliberately NOT a silent correction: the caller opens the circuit for the same
    value, so the clamped figure is stored alongside an open breaker and a reason rather
    than passing as an ordinary settlement. Losing the exact impossible number is worth
    it to keep the breaker able to fire, which is the difference between a recorded
    integrity incident and a rolled back transaction that leaves the reservation
    outstanding forever.
    """

    if actual_cost_nano < 0:
        return 0

    return min(quantize_storable_nano(actual_cost_nano), MAX_STORABLE_NANO)


def circuit_should_open(max_cost_nano: int, actual_cost_nano: int) -> bool:
    """Whether a completion's reported cost is impossible and must stop all spending.

    A provider reporting more than the reservation permitted means the ceiling has
    already been crossed by an amount nobody authorised. The reported figure is stored
    truthfully rather than clamped to the maximum, because clamping would make the
    ledger agree with a bound that was actually broken, and the breach is the thing
    worth knowing.
    """

    if actual_cost_nano < 0 or max_cost_nano < 0:
        return True

    return actual_cost_nano > max_cost_nano
