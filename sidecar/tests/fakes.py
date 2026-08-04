"""Shared test doubles.

Lives in its own module rather than inside a test file so both the unit and the
integration suites can use it without importing each other.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from playerbot_claude import budget, ledger, schema


class FakeState:
    """In-memory stand-in for :class:`playerbot_claude.state.SidecarState`.

    The admission arithmetic is the REAL policy from ``playerbot_claude.budget``, not a
    hand-rolled approximation. What these tests exercise is the service's ordering, that
    it reserves before generating, settles after, and gives the money back on failure,
    and a double that invented its own admission rule could pass while the service and
    the ledger quietly disagreed.

    What MySQL actually does under concurrency is proven separately and for real, in
    tests/test_ledger_mysql.py against a live server. Nothing here claims to.
    """

    def __init__(self, ceiling_nano: int, reserve_ratio: Decimal = Decimal("0.25")) -> None:
        self.ceiling_nano = ceiling_nano
        self.reserve_ratio = reserve_ratio
        self.settled_nano = 0
        self.circuit_open = False
        self.outstanding: dict[int, int] = {}
        self.profiles: dict[int, dict[str, object]] = {}
        self.turns: dict[int, list[tuple[str, str]]] = {}
        self.careers: dict[int, dict[str, object]] = {}
        self.ambient_allowance = 6
        self.ambient_taken = 0
        # Ordered names of every operation, so a test can assert that the money was
        # reserved BEFORE the provider was called rather than merely that both happened.
        self.calls: list[str] = []
        self.reservations: list[ledger.Reservation] = []
        # Which lane each reserve() was admitted under, in call order. The reservation object
        # itself does not carry the lane, so without this a test can only assert that money was
        # reserved, never that a background job stayed out of the slice held for a waiting player.
        self.reserved_priorities: list[budget.RequestPriority] = []
        self.reserved_kinds: list[budget.RequestKind] = []
        self.reserved_models: list[str] = []
        self.released: list[ledger.Reservation] = []
        self.settlements: list[tuple[ledger.Reservation, int]] = []
        self._next_id = 1

    async def try_begin_ambient(self, *, messages_per_hour: int, now: datetime) -> bool:
        self.calls.append("try_begin_ambient")
        allowed = min(messages_per_hour, self.ambient_allowance)
        if self.ambient_taken >= allowed:
            return False

        self.ambient_taken += 1
        return True

    async def record_profile(self, request, *, now: datetime) -> None:
        self.calls.append("record_profile")
        self.profiles[request.bot_guid] = {
            "profile_version": request.profile_version,
            "crafting_affinity": request.crafting_affinity,
            "gathering_affinity": request.gathering_affinity,
            "exploration_affinity": request.exploration_affinity,
            "sociability": request.sociability,
            "voice": request.voice,
            "bot_name": request.bot_name,
        }

    async def get_profile(self, *, bot_guid: int) -> dict[str, object] | None:
        return self.profiles.get(bot_guid)

    async def recent_turns(self, *, bot_guid: int) -> list[tuple[str, str]]:
        self.calls.append("recent_turns")
        return list(self.turns.get(bot_guid, []))

    async def append_turn(self, *, bot_guid: int, role: str, content: str, now: datetime) -> None:
        self.calls.append("append_turn")
        self.turns.setdefault(bot_guid, []).append((role, content))

    async def record_career_decision(
        self,
        *,
        bot_guid: int,
        career_version: int,
        candidate_token: str,
        spending_style: str,
        now: datetime,
    ) -> None:
        self.calls.append("record_career_decision")
        self.careers[bot_guid] = {
            "career_version": career_version,
            "candidate_token": candidate_token,
            "spending_style": spending_style,
        }

    async def reserve(self, *, request_kind, model, max_cost_nano, priority, now):
        self.calls.append("reserve")
        self.reserved_priorities.append(priority)
        self.reserved_kinds.append(request_kind)
        self.reserved_models.append(model)
        # The real ledger rounds up to what the DECIMAL column holds before deciding, so
        # a fake that admits against the unrounded figure admits a different amount than
        # production would.
        if max_cost_nano is not None and max_cost_nano > 0:
            max_cost_nano = budget.quantize_storable_nano(max_cost_nano)
        decision = budget.admit(
            ceiling_nano=self.ceiling_nano,
            state=self._budget_state(),
            max_cost_nano=max_cost_nano,
            priority=priority,
            reserve_ratio=self.reserve_ratio,
        )
        if decision is not budget.AdmissionDecision.ADMITTED:
            return decision, None

        reservation = ledger.Reservation(
            reservation_id=self._next_id,
            public_id=ledger.mint_public_id(),
            budget_date=schema.utc_day(now),
            max_cost_nano=int(max_cost_nano or 0),
        )
        self._next_id += 1
        self.outstanding[reservation.reservation_id] = reservation.max_cost_nano
        self.reservations.append(reservation)
        return decision, reservation

    async def settle(self, *, reservation, actual_cost_nano: int, now: datetime) -> bool:
        self.calls.append("settle")
        # Mirrors the ledger: a reservation expiry has already reclaimed is no longer in
        # the reserved state, so settling it is refused rather than charged twice.
        if self.outstanding.pop(reservation.reservation_id, None) is None:
            return False

        if budget.circuit_should_open(reservation.max_cost_nano, actual_cost_nano):
            self.circuit_open = True
        self.settled_nano += budget.storable_actual_cost_nano(actual_cost_nano)
        self.settlements.append((reservation, actual_cost_nano))
        return True

    async def release(self, *, reservation) -> bool:
        self.calls.append("release")
        if self.outstanding.pop(reservation.reservation_id, None) is None:
            return False

        self.released.append(reservation)
        return True

    async def budget_state(self, *, now: datetime) -> budget.BudgetState:
        return self._budget_state()

    def _budget_state(self) -> budget.BudgetState:
        return budget.BudgetState(
            settled_nano=self.settled_nano,
            outstanding_nano=sum(self.outstanding.values()),
            circuit_open=self.circuit_open,
        )
