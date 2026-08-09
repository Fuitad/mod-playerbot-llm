"""Loopback sidecar: configuration, request processing, server lifecycle, and CLI.

Fail-closed philosophy throughout: a request that cannot be served correctly is
answered with silence (the worldserver side expires it), never with fabricated text.
The bridge token and the Anthropic API key never appear in output, logs, or JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from playerbot_llm import budget, generation, ledger, protocol, provider, state
from playerbot_llm import store as store_module
from playerbot_llm.budget import AdmissionDecision, RequestKind, RequestPriority
from playerbot_llm.providers.anthropic import (
    AnthropicProvider,
)

CONFIG_SECTION = "worldserver"
CONFIG_PREFIX = "PlayerbotLLM."
# Environment variable *names*, not secret values.
TOKEN_ENV_VAR = "PLAYERBOT_LLM_BRIDGE_TOKEN"  # noqa: S105
BOT_PURGE_POLL_SECONDS = 5.0

# There is deliberately no maximum above the configured ceiling. A second limit in the
# code silently ignores what the operator asked for, and PlayerbotLLM.DailyBudgetUsd
# is documented as the sole ceiling.


@dataclass(frozen=True)
class PlayerbotsDatabaseSettings:
    """Connection settings read from the deployed worldserver configuration.

    Read rather than duplicated on purpose. A second copy of the credentials in the
    sidecar's own config is a second thing to rotate, and the one that gets missed is
    the one that keeps working with the old password until it does not.

    ``__repr__`` is overridden because a dataclass prints every field, and this one is
    exactly the object most likely to end up in a log line or a traceback.
    """

    host: str
    port: int
    user: str
    password: str
    database: str

    def __repr__(self) -> str:
        return (
            f"PlayerbotsDatabaseSettings(host={self.host!r}, port={self.port!r}, "
            f"user={self.user!r}, password=<redacted>, database={self.database!r})"
        )

    def __str__(self) -> str:
        return repr(self)

    @classmethod
    def parse_info(cls, raw: str) -> PlayerbotsDatabaseSettings:
        """Parses one AzerothCore ``host;port;user;password;database`` value.

        The password is the only field that may legitimately contain almost anything,
        and it sits in the middle, so the split is bounded to five parts rather than
        greedy: a password containing a semicolon would otherwise shift the database
        name out of position and connect somewhere nobody named.
        """

        value = raw.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]

        parts = value.split(";")
        if len(parts) != 5:
            raise ValueError("PlayerbotsDatabaseInfo must have exactly five semicolon separated fields")

        host, port_text, user, password, database = parts
        if not host or not user or not database:
            raise ValueError("PlayerbotsDatabaseInfo host, user, and database must all be present")

        try:
            port = int(port_text)
        except ValueError as error:
            raise ValueError("PlayerbotsDatabaseInfo port must be an integer") from error

        if not 1 <= port <= 65535:
            raise ValueError("PlayerbotsDatabaseInfo port must be a usable TCP port")

        return cls(host=host, port=port, user=user, password=password, database=database)

    @classmethod
    def load(cls, path: str) -> PlayerbotsDatabaseSettings:
        """Finds PlayerbotsDatabaseInfo in a deployed worldserver or module config.

        The file is read line by line rather than through configparser, because these
        files carry duplicate keys and section-free preambles that a strict parser
        rejects, and this only needs one setting out of a thousand.
        """

        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue

                key, _, value = stripped.partition("=")
                if key.strip() == "PlayerbotsDatabaseInfo":
                    return cls.parse_info(value)

        raise ValueError(f"no PlayerbotsDatabaseInfo setting found in {path}")


@dataclass(frozen=True)
class SidecarConfig:
    enable: bool = False
    bridge_port: int = 0
    ambient_world_enable: bool = False
    ambient_max_messages_per_hour: int = 6
    # Kept as text through parsing and converted with Decimal, never float: a ceiling
    # read from a config file is text, and going through float bakes in the rounding the
    # integer nano-USD arithmetic exists to avoid.
    daily_budget_usd: str = "0"
    human_budget_reserve_ratio: str = "0.25"
    input_usd_per_mtok: str = "1.00"
    output_usd_per_mtok: str = "5.00"
    response_deadline_ms: int = 10000
    log_model_io: bool = False
    queue_size: int = 16
    group_cooldown_seconds: int = 120

    @classmethod
    def load(cls, path: str) -> SidecarConfig:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)

        def option(name: str, fallback: str) -> str:
            value = parser.get(CONFIG_SECTION, CONFIG_PREFIX + name, fallback=fallback).strip()
            # AzerothCore .conf convention quotes string values; worldserver's
            # ConfigMgr strips the surrounding quotes, so the sidecar must too.
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value

        return cls(
            enable=option("Enable", "0") == "1",
            bridge_port=int(option("BridgePort", "0")),
            ambient_world_enable=option("AmbientWorldEnable", "0") == "1",
            ambient_max_messages_per_hour=int(option("AmbientMaxMessagesPerHour", "6")),
            daily_budget_usd=option("DailyBudgetUsd", "0"),
            human_budget_reserve_ratio=option("HumanBudgetReserveRatio", "0.25"),
            input_usd_per_mtok=option("InputUsdPerMTok", "1.00"),
            output_usd_per_mtok=option("OutputUsdPerMTok", "5.00"),
            response_deadline_ms=int(option("ResponseDeadlineMs", "10000")),
            log_model_io=option("LogModelIO", "0") == "1",
            queue_size=int(option("QueueSize", "16")),
            group_cooldown_seconds=int(option("GroupCooldownSeconds", "120")),
        )

    @property
    def price_texts(self) -> tuple[str, str]:
        """Input and output price per million tokens, as configured text.

        Text end to end into the Decimal arithmetic. A price that has been through a
        float is a price whose last digits are whatever the binary representation
        happened to land on, and a ceiling enforced against that is not the ceiling
        anyone configured.
        """

        return self.input_usd_per_mtok, self.output_usd_per_mtok

    @property
    def budget_nano(self) -> int:
        """The configured ceiling in nano-USD, or 0 when it is unusable.

        Zero reads as "no budget configured", which every caller already treats as a
        reason to stay silent, so an unparseable ceiling fails closed rather than
        raising out of a property.
        """

        try:
            return budget.validate_daily_ceiling(self.daily_budget_usd)
        except budget.BudgetConfigurationError:
            return 0

    @property
    def reserve_ratio(self) -> Decimal:
        """The protected share of the ceiling. An unusable value protects everything.

        Failing closed here means a typo silences background work rather than quietly
        removing the protection it was meant to configure.
        """

        try:
            return budget.validate_reserve_ratio(self.human_budget_reserve_ratio)
        except budget.BudgetConfigurationError:
            return Decimal(1)

    @property
    def generation_allowed(self) -> bool:
        """A usable ceiling and positive rates are required for any provider call."""

        if self.budget_nano <= 0:
            return False

        try:
            return Decimal(self.input_usd_per_mtok) > 0 and Decimal(self.output_usd_per_mtok) > 0
        except (InvalidOperation, TypeError, ValueError):
            return False

    @property
    def ambient_allowed(self) -> bool:
        return (
            self.ambient_world_enable
            and 1 <= self.ambient_max_messages_per_hour <= store_module.MAX_AMBIENT_MESSAGES_PER_HOUR
        )


def bridge_token_from_environment() -> str | None:
    """Both bounds, at the one place the token enters the process.

    The floor is for entropy and the ceiling is for the same reason every other string in
    this protocol has one: without it a very long token is copied, compared, and written
    into every frame before anything refuses it. Mirrors PlayerbotLLM::BridgeTokenIsUsable.
    """

    token = os.environ.get(TOKEN_ENV_VAR)
    if token is None:
        return None

    length = len(token.encode("utf-8"))
    if length < protocol.MIN_BRIDGE_TOKEN_BYTES or length > protocol.MAX_BRIDGE_TOKEN_BYTES:
        return None

    return token


def doctor_report(
    config: SidecarConfig,
    generation_provider: provider.GenerationProvider,
    budget_state: budget.BudgetState | None = None,
) -> dict[str, object]:
    """Health summary as JSON-safe data. Never contains a secret value."""

    token_present = bridge_token_from_environment() is not None
    ok = config.enable and config.bridge_port > 0 and config.generation_allowed and token_present

    report: dict[str, object] = {
        "ok": ok,
        "enable": config.enable,
        "bridge_port": config.bridge_port,
        "daily_budget_usd": config.daily_budget_usd,
        "human_budget_reserve_ratio": config.human_budget_reserve_ratio,
        "response_deadline_ms": config.response_deadline_ms,
        "bridge_token_present": token_present,
        "provider_name": generation_provider.metadata.name,
        "provider_configured": generation_provider.configured,
    }

    if budget_state is not None:
        remaining = max(0, config.budget_nano - budget_state.committed_nano)
        reserve_floor = budget.reserve_floor_nano(config.budget_nano, config.reserve_ratio)
        report["budget"] = {
            "settled_usd": budget.nano_to_usd_string(budget_state.settled_nano),
            "outstanding_usd": budget.nano_to_usd_string(budget_state.outstanding_nano),
            "remaining_usd": budget.nano_to_usd_string(remaining),
            # What background work may not touch, so an operator can see why a bot went
            # quiet while the headline remaining figure still looks healthy.
            "human_reserve_usd": budget.nano_to_usd_string(reserve_floor),
            "circuit_open": budget_state.circuit_open,
        }
        if budget_state.circuit_open:
            report["ok"] = False

    return report


def _priority_for(request: protocol.ChatRequest) -> RequestPriority:
    """Which budget lane one request belongs in.

    A whisper, a party line, or a social reply has somebody standing there waiting for
    an answer, so it draws on the protected reserve. Ambient World chatter and career
    selection are work the server decided to do on its own: nobody is waiting, and a
    quiet realm that spent its whole day on them would have nothing left the moment a
    player finally spoke. That is the entire reason the reserve exists.
    """

    if request.is_ambient or request.is_career:
        return RequestPriority.BACKGROUND

    return RequestPriority.IMMEDIATE_HUMAN


def _social_priority_for(request: protocol.SocialRequest) -> RequestPriority:
    """Map the worldserver's validated social lane without inferring from content."""

    if request.admission_lane == "immediate_human":
        return RequestPriority.IMMEDIATE_HUMAN
    if request.admission_lane == "background":
        return RequestPriority.BACKGROUND

    raise AssertionError("unvalidated social admission lane")


def _request_kind_for(request: protocol.ChatRequest) -> RequestKind:
    """What the ledger row says this request was paying for.

    Separate from :func:`_priority_for`, and not derivable from it. Career selection and
    ambient chatter share the background lane but are different kinds of work, and a
    career decision is the one the operator is most likely to go looking for by name.
    """

    return RequestKind.CAREER_GENERATION if request.is_career else RequestKind.CHAT_RESPONSE


def _actual_cost_nano(usage: provider.GenerationUsage, prices: tuple[str, str]) -> int:
    """What a completed call really cost, from the provider's own token counts.

    Cache tokens are counted at the plain input rate. Nothing in this sidecar sends a
    cache_control block, so both counts are always zero today; charging them at the
    ordinary rate means that if prompt caching is ever turned on, the ledger over-counts
    cache reads rather than under-counting cache writes. Over-counting is the safe
    direction under a ceiling. Real cache pricing needs its own configured rates.

    An unpriceable completion raises rather than settling. Returning zero would record
    real spending as free, which is the one outcome a ceiling cannot survive; raising
    leaves the reservation charged at its maximum for the ledger's expiry to reclaim.
    """

    # Checked per FIELD, before they are summed. token_cost_nano refuses a negative
    # total, but summing hides a negative component: 100 input with -1 cache tokens adds
    # to 99, which prices cheerfully and charges for a count that cannot exist.
    if not usage.is_priceable:
        raise ValueError("provider reported impossible token counts")

    input_tokens = usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    cost = budget.token_cost_nano(input_tokens, usage.output_tokens, *prices)
    if cost is None:
        # Unreachable while generation_allowed gates the request path on usable prices,
        # and deliberately not a silent zero if that ever stops being true.
        raise ValueError("completed a request whose cost cannot be priced")

    return cost


class SidecarService:
    """Parses, validates, and answers one request payload at a time.

    All budget bookkeeping (count, reserve, settle, memory) is serialized with a
    single lock, so socket concurrency can never race the ledger. One residual
    overlap exists by design: when the deadline cancels a request mid-generation,
    the lock is released while the abandoned synchronous SDK call finishes in its
    worker thread (Python cannot interrupt it; the client timeout, capped at the
    same deadline, bounds it). That is safe: httpx clients are thread-safe, and a
    cancelled request can never settle or write conversation memory, so its
    reservation stays charged at maximum until the ledger's expiry reclaims it.

    The lock is a courtesy rather than the guarantee. The ledger's own day-row lock is
    what actually makes the ceiling hold, and it holds across processes, which this
    lock cannot: two sidecars against one database are correct, and were not before.
    """

    def __init__(
        self,
        config: SidecarConfig,
        token: str,
        adapter: provider.GenerationProvider | None = None,
        store: state.SidecarState | None = None,
        now=None,
    ) -> None:
        self._config = config
        self._token = token
        # The default SDK client's own timeout is capped at the response deadline so
        # a provider call cannot outlive the request that paid for it.
        self._adapter = adapter or _default_provider(config)
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._generation_lock = asyncio.Lock()

    async def purge_pending_bot_data(self) -> int:
        """Drain durable bot purge intents without overlapping state writes."""

        if self._store is None:
            return 0

        async with self._generation_lock:
            return await self._store.purge_pending_bot_data()

    async def process_payload(self, payload: bytes) -> bytes | None:
        """Returns the response payload, or None when the bot must stay silent.

        Raises ProtocolError (including TokenMismatchError) for malformed or
        unauthenticated payloads; the connection handler closes that connection.
        """

        # The declared kind is read before a request model is chosen. A social frame carries
        # fields ChatRequest forbids, so parsing optimistically and falling back would report
        # a social frame as twenty three chat schema errors, and the connection handler would
        # close a healthy bridge over it.
        kind = protocol.declared_kind(payload)
        if kind == "social":
            return await self._process_social_payload(payload)
        if kind == "biography":
            return await self._process_biography_payload(payload)
        if kind == "memory":
            return await self._process_memory_payload(payload)
        if kind == "roleplay_assessment":
            return await self._process_roleplay_assessment_payload(payload)
        if kind is not None:
            # Fail closed. A kind nobody here recognizes is a newer worldserver talking to an
            # older sidecar, and guessing which handler it meant is how the wrong one runs.
            raise protocol.ProtocolError(f"unrecognized request kind {kind!r}")

        request = protocol.parse_request(payload, self._token)

        if not self._config.generation_allowed:
            _log(f"request {request.request_id}: no budget configured, staying silent")
            return None
        if request.is_ambient and not self._config.ambient_allowed:
            _log(f"request {request.request_id}: ambient World chat is disabled, staying silent")
            return None

        # ResponseDeadlineMs bounds the whole pipeline, queueing included: the
        # worldserver side has already expired this request by then, so finishing
        # late would only spend budget on a reply nobody can receive. A reservation
        # made before the cut stays charged at maximum (fail closed on money).
        try:
            async with asyncio.timeout(self._config.response_deadline_ms / 1000):
                return await self._process_within_deadline(request)
        except TimeoutError:
            _log(f"request {request.request_id}: response deadline exceeded, staying silent")
            return None

    async def _process_social_payload(self, payload: bytes) -> bytes | None:
        """One social line, from a coordinator that is waiting for it.

        Silence and a regeneration are different answers and both are useful, so this
        returns None only when there is genuinely nothing to say back. An output the
        deterministic gate rejected returns a REGENERATION instead: the coordinator's
        transport owns the retry budget and spends at most one, which is where Definition
        of Done 2's "at most one" is actually enforced.
        """

        request = protocol.parse_social_request(payload, self._token)
        token = request.social_request_token

        if not self._config.generation_allowed:
            _log(f"social {token}: no budget configured, staying silent")
            return None

        try:
            async with asyncio.timeout(self._config.response_deadline_ms / 1000):
                return await self._process_social_within_deadline(request)
        except TimeoutError:
            _log(f"social {token}: response deadline exceeded, staying silent")
            return None

    async def _process_biography_payload(self, payload: bytes) -> bytes | None:
        """One bot's player profile, which nobody is waiting on.

        Silence is the only failure answer. A biography is generated once and kept, so there is
        nothing to regenerate against and no conversation to fall out of; the coordinator's own
        request timeout opens the retry, and it is the only thing that does.
        """

        request = protocol.parse_biography_request(payload, self._token)
        token = request.biography_request_token

        if not self._config.generation_allowed:
            _log(f"biography {token}: no budget configured, staying silent")
            return None

        try:
            async with asyncio.timeout(self._config.response_deadline_ms / 1000):
                return await self._process_biography_within_deadline(request)
        except TimeoutError:
            _log(f"biography {token}: response deadline exceeded, staying silent")
            return None

    async def _process_memory_payload(self, payload: bytes) -> bytes | None:
        """One finished conversation, read for whatever is worth remembering.

        Nobody is waiting on this either, so silence is the only failure answer and there is
        nothing to regenerate against: a conversation that produced an unusable answer is not
        more likely to produce a good one on a second read of the same text.
        """

        request = protocol.parse_memory_request(payload, self._token)
        token = request.memory_request_token

        if not self._config.generation_allowed:
            _log(f"memory {token}: no budget configured, staying silent")
            return None

        try:
            async with asyncio.timeout(self._config.response_deadline_ms / 1000):
                return await self._process_memory_within_deadline(request)
        except TimeoutError:
            _log(f"memory {token}: response deadline exceeded, staying silent")
            return None

    async def _process_roleplay_assessment_payload(self, payload: bytes) -> bytes | None:
        """One roleplay classification, from a coordinator that is waiting on it.

        Silence is the only failure answer: the C++ side treats an unanswered assessment as
        its ordinary fallback, and synthesizing an "ordinary" result here would dress up a
        failure as a decision the model never made.
        """

        request = protocol.parse_roleplay_assessment_request(payload, self._token)
        token = request.roleplay_assessment_request_token

        if not self._config.generation_allowed:
            _log(f"assessment {token}: no budget configured, staying silent")
            return None

        try:
            async with asyncio.timeout(self._config.response_deadline_ms / 1000):
                return await self._process_roleplay_assessment_within_deadline(request)
        except TimeoutError:
            _log(f"assessment {token}: response deadline exceeded, staying silent")
            return None

    async def _process_roleplay_assessment_within_deadline(
        self, request: protocol.RoleplayAssessmentRequest
    ) -> bytes | None:
        token = request.roleplay_assessment_request_token
        if self._store is None:
            _log(f"assessment {token}: no durable state is open, staying silent")
            return None

        store = self._store
        input_prices = self._config.price_texts

        async with self._generation_lock:
            now = self._now()

            try:
                input_tokens = await asyncio.to_thread(
                    self._adapter.count_roleplay_assessment_input_tokens, request
                )
            except provider.GenerationError as error:
                _log(f"assessment {token}: {type(error).__name__}: {error}")
                return None

            if input_tokens > self._adapter.metadata.max_input_tokens:
                _log(
                    f"assessment {token}: prompt is {input_tokens} tokens "
                    f"(limit {self._adapter.metadata.max_input_tokens}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens, self._adapter.metadata.max_output_tokens("roleplay_assessment"), *input_prices
            )
            # Human social work: a player just spoke and the coordinator is holding their reply
            # opportunity on this answer. The kind is its own enumerator so an assessment is
            # visible as an additional model call rather than hidden inside generation cost.
            decision, reservation = await store.reserve(
                request_kind=RequestKind.MODERATION_CLASSIFICATION,
                model=self._adapter.metadata.model,
                max_cost_nano=max_cost_nano,
                priority=RequestPriority.IMMEDIATE_HUMAN,
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"assessment {token}: {decision.value}, staying silent")
                return None

            try:
                completion, usage = await asyncio.to_thread(self._adapter.assess_roleplay, request)
            except provider.GenerationError as error:
                # Malformed output and provider failure land together: billed if the model ran,
                # settled either way, and never replaced by a guessed category.
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"assessment {token}: {type(error).__name__}: {error} ({outcome})")
                return None

            settled_at = self._now()
            try:
                actual_cost_nano = _actual_cost_nano(usage, input_prices)
            except ValueError as error:
                _log(f"assessment {token}: cannot price the completion: {error}")
                return None

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=settled_at,
            )
            if not settlement.completed:
                _log(f"assessment {token}: settlement refused, the reservation had already expired")
                return None

        # The framed response is built HERE, from the validated completion plus the request's
        # correlation token and the authenticated bridge token. The completion has no field
        # that could have supplied either.
        return protocol.encode_roleplay_assessment_response(token, completion, self._token)

    async def _process_memory_within_deadline(self, request: protocol.MemoryRequest) -> bytes | None:
        token = request.memory_request_token
        if self._store is None:
            _log(f"memory {token}: no durable state is open, staying silent")
            return None

        store = self._store
        input_prices = self._config.price_texts

        async with self._generation_lock:
            now = self._now()

            try:
                input_tokens = await asyncio.to_thread(self._adapter.count_memory_input_tokens, request)
            except provider.GenerationError as error:
                _log(f"memory {token}: {type(error).__name__}: {error}")
                return None

            if input_tokens > self._adapter.metadata.max_input_tokens:
                _log(
                    f"memory {token}: prompt is {input_tokens} tokens "
                    f"(limit {self._adapter.metadata.max_input_tokens}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens, self._adapter.metadata.max_output_tokens("memory"), *input_prices
            )
            # The background lane, for the same reason a biography uses it: the conversation has
            # already ended, so this must never take the slice held for a player who just spoke
            # and is watching for an answer.
            decision, reservation = await store.reserve(
                request_kind=RequestKind.MEMORY_EXTRACTION,
                model=self._adapter.metadata.model,
                max_cost_nano=max_cost_nano,
                priority=RequestPriority.BACKGROUND,
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"memory {token}: {decision.value}, staying silent")
                return None

            try:
                candidates, usage = await asyncio.to_thread(self._adapter.generate_memories, request)
            except provider.GenerationError as error:
                # A refused candidate lands here alongside a provider failure. Both were billed
                # if the model ran, both are settled, and neither is retried: the gate refused
                # this reading of this conversation, and the text is already gone on the far side.
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"memory {token}: {type(error).__name__}: {error} ({outcome})")
                return None

            settled_at = self._now()
            try:
                actual_cost_nano = _actual_cost_nano(usage, input_prices)
            except ValueError as error:
                _log(f"memory {token}: cannot price the completion: {error}")
                return None

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=settled_at,
            )
            if not settlement.completed:
                _log(f"memory {token}: settlement refused, the reservation had already expired")
                return None

        # An empty list is encoded and sent like any other answer. Most conversations support
        # nothing, and the coordinator needs the reply to close the request rather than waiting
        # out its own timeout on a question that was answered correctly.
        return protocol.encode_memory_response(
            memory_request_token=token,
            bot_guid=request.bot_guid,
            thread_id=request.thread_id,
            candidates=candidates,
            token=self._token,
        )

    async def _process_biography_within_deadline(self, request: protocol.BiographyRequest) -> bytes | None:
        token = request.biography_request_token
        if self._store is None:
            _log(f"biography {token}: no durable state is open, staying silent")
            return None

        store = self._store
        input_prices = self._config.price_texts

        async with self._generation_lock:
            now = self._now()

            try:
                input_tokens = await asyncio.to_thread(self._adapter.count_biography_input_tokens, request)
            except provider.GenerationError as error:
                _log(f"biography {token}: {type(error).__name__}: {error}")
                return None

            if input_tokens > self._adapter.metadata.max_input_tokens:
                _log(
                    f"biography {token}: prompt is {input_tokens} tokens "
                    f"(limit {self._adapter.metadata.max_input_tokens}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens, self._adapter.metadata.max_output_tokens("biography"), *input_prices
            )
            # The background lane, deliberately. Nobody is waiting on a player profile, so it must
            # never spend the slice held for a player who just said something and is watching
            # for an answer. This is Key Decision 2 expressed where it is actually enforced.
            decision, reservation = await store.reserve(
                request_kind=RequestKind.BACKSTORY_GENERATION,
                model=self._adapter.metadata.model,
                max_cost_nano=max_cost_nano,
                priority=RequestPriority.BACKGROUND,
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"biography {token}: {decision.value}, staying silent")
                return None

            try:
                fields, usage = await asyncio.to_thread(self._adapter.generate_biography, request)
            except provider.GenerationError as error:
                # Covers a refused player profile as well as a provider failure. Both were billed if
                # the model ran, both are settled, and both leave the coordinator to time the
                # request out rather than being told to try again immediately: a bot that just
                # produced a forbidden claim is not more likely to behave on the next attempt.
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"biography {token}: {type(error).__name__}: {error} ({outcome})")
                return None

            settled_at = self._now()
            try:
                actual_cost_nano = _actual_cost_nano(usage, input_prices)
            except ValueError as error:
                _log(f"biography {token}: cannot price the completion: {error}")
                return None

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=settled_at,
            )
            if not settlement.completed:
                _log(f"biography {token}: settlement refused, the reservation had already expired")
                return None

        return protocol.encode_biography_response(
            biography_request_token=token,
            bot_guid=request.bot_guid,
            biography=fields,
            token=self._token,
        )

    async def _process_social_within_deadline(self, request: protocol.SocialRequest) -> bytes | None:
        token = request.social_request_token
        if self._store is None:
            _log(f"social {token}: no durable state is open, staying silent")
            return None

        store = self._store
        input_prices = self._config.price_texts

        async with self._generation_lock:
            now = self._now()

            try:
                input_tokens = await asyncio.to_thread(self._adapter.count_social_input_tokens, request)
            except provider.GenerationError as error:
                _log(f"social {token}: {type(error).__name__}: {error}")
                return None

            if input_tokens > self._adapter.metadata.max_input_tokens:
                _log(
                    f"social {token}: prompt is {input_tokens} tokens "
                    f"(limit {self._adapter.metadata.max_input_tokens}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens, self._adapter.metadata.max_output_tokens("social"), *input_prices
            )
            decision, reservation = await store.reserve(
                request_kind=RequestKind.CHAT_RESPONSE,
                model=self._adapter.metadata.model,
                max_cost_nano=max_cost_nano,
                priority=_social_priority_for(request),
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"social {token}: {decision.value}, staying silent")
                return None

            try:
                provider_started_at = time.monotonic_ns()
                generated = await asyncio.to_thread(self._adapter.generate_social_reply, request)
                provider_latency_ms = (time.monotonic_ns() - provider_started_at) // 1_000_000
            except provider.GenerationInvalidOutputError as error:
                # Generated, billed, and refused by the gate. The money is settled from the
                # usage the provider reported when it is available, exactly as a rejected
                # chat completion is, and the coordinator is asked for one more attempt.
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"social {token}: rejected output: {error} ({outcome}), asking for a regeneration")
                return protocol.encode_social_response(
                    social_request_token=token,
                    bot_guid=request.bot_guid,
                    speak_on_channel=request.speak_on_channel,
                    message="",
                    token=self._token,
                    regenerate=True,
                )
            except provider.GenerationError as error:
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"social {token}: {type(error).__name__}: {error} ({outcome})")
                return None

            settled_at = self._now()
            try:
                actual_cost_nano = _actual_cost_nano(generated.usage, input_prices)
            except ValueError as error:
                _log(f"social {token}: cannot price the completion: {error}")
                return None

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=settled_at,
            )
            if not settlement.completed:
                # Expiry already reclaimed it, so the ledger declined to charge this line.
                # Speaking it anyway would spend outside the ceiling.
                _log(f"social {token}: settlement refused, the reservation had already expired")
                return None
            if settlement.breach or settlement.saturated:
                _log(f"social {token}: unsafe settlement opened the budget circuit, staying silent")
                return None
            if settlement.stored_cost_nano is None:
                _log(f"social {token}: completed settlement returned no stored cost, staying silent")
                return None

            try:
                metadata = protocol.SocialCallMetadata(
                    model=self._adapter.metadata.model,
                    provider_latency_ms=provider_latency_ms,
                    input_tokens=generated.usage.input_tokens,
                    output_tokens=generated.usage.output_tokens,
                    cache_creation_input_tokens=generated.usage.cache_creation_input_tokens,
                    cache_read_input_tokens=generated.usage.cache_read_input_tokens,
                    cost_usd=budget.nano_to_fixed_usd_string(settlement.stored_cost_nano),
                )
            except ValueError:
                _log(f"social {token}: invalid provider call metadata, staying silent")
                return None

        return protocol.encode_social_response(
            social_request_token=token,
            bot_guid=request.bot_guid,
            speak_on_channel=request.speak_on_channel,
            message=generated.message,
            token=self._token,
            emote_id=generated.emote_id,
            metadata=metadata,
            contribution=generated.contribution,
            claim_subject=generated.claim_subject,
            cited_evidence_ids=generated.cited_evidence_ids,
        )

    async def _process_within_deadline(self, request: protocol.ChatRequest) -> bytes | None:
        if self._store is None:
            _log(f"request {request.request_id}: no durable state is open, staying silent")
            return None

        store = self._store
        input_prices = self._config.price_texts

        async with self._generation_lock:
            now = self._now()

            if request.is_ambient and not await store.try_begin_ambient(
                messages_per_hour=self._config.ambient_max_messages_per_hour, now=now
            ):
                _log(f"request {request.request_id}: ambient rate exhausted, staying silent")
                return None

            await store.record_profile(request, now=now)
            history = await self._history_for(request)

            try:
                input_tokens = await asyncio.to_thread(self._adapter.count_input_tokens, request, history)
            except provider.GenerationError as error:
                _log(f"request {request.request_id}: {type(error).__name__}: {error}")
                return None

            if input_tokens > self._adapter.metadata.max_input_tokens:
                _log(
                    f"request {request.request_id}: prompt is {input_tokens} tokens "
                    f"(limit {self._adapter.metadata.max_input_tokens}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens,
                self._adapter.metadata.max_output_tokens("career" if request.is_career else "chat"),
                *input_prices,
            )
            decision, reservation = await store.reserve(
                request_kind=_request_kind_for(request),
                model=self._adapter.metadata.model,
                max_cost_nano=max_cost_nano,
                priority=_priority_for(request),
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"request {request.request_id}: {decision.value}, staying silent")
                return None

            try:
                reply, usage = await asyncio.to_thread(self._adapter.generate_reply, request, history)
            except provider.GenerationError as error:
                outcome = await self._account_for_failure(store, reservation, error)
                _log(f"request {request.request_id}: {type(error).__name__}: {error} ({outcome})")
                return None

            settled_at = self._now()
            try:
                actual_cost_nano = _actual_cost_nano(usage, input_prices)
            except ValueError as error:
                # A completion that cannot be priced must not settle as free. Left
                # outstanding at its maximum for the ledger's expiry to reclaim, and
                # reported as a bounded failure rather than escaping into the connection
                # handler, which only understands protocol and connection errors.
                _log(f"request {request.request_id}: cannot price the completion: {error}")
                return None

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=settled_at,
            )
            if not settlement.completed:
                # The reservation was no longer reserved, which means expiry already
                # reclaimed it: this request took longer than the expiry window and the
                # ledger has refused to charge it rather than charge it twice.
                #
                # The reply is dropped rather than delivered. Delivering it would speak a
                # line whose cost the ledger declined to record, which is spending outside
                # the ceiling, and the worldserver's own deadline has long since expired
                # the request anyway. Nothing is written to memory for the same reason.
                _log(
                    f"request {request.request_id}: settlement refused, the reservation had "
                    "already expired; dropping the reply"
                )
                return None

            if request.is_career:
                choice = generation.CareerReply.model_validate_json(reply)
                await store.record_career_decision(
                    bot_guid=request.bot_guid,
                    career_version=request.career_content.career_version,
                    candidate_token=choice.candidate_token,
                    spending_style=choice.spending_style,
                    now=settled_at,
                )
            elif not request.is_ambient:
                await store.append_turn(
                    bot_guid=request.bot_guid,
                    role="user",
                    content=generation.build_user_message(request),
                    now=settled_at,
                )
                await store.append_turn(
                    bot_guid=request.bot_guid, role="assistant", content=reply, now=settled_at
                )

        _log(f"request {request.request_id}: replied ({usage.input_tokens} in, {usage.output_tokens} out)")
        return protocol.encode_response(request.request_id, reply, self._token)

    async def _account_for_failure(
        self, store: state.SidecarState, reservation: ledger.Reservation, error: provider.GenerationError
    ) -> str:
        """Decides what a failed generation owes, and returns what was done for the log.

        Three outcomes, and which one applies is a question of what is actually known
        rather than a preference:

        1. A refusal generated nothing, so the reservation is released. Holding its
           maximum for the full expiry window would deny a later request money the budget
           demonstrably has.
        2. Invalid output arrives with the provider's own usage attached, so the exact
           cost is known and settled. This is the case that makes the whole split worth
           having: the tokens were billed, and neither releasing them nor charging the
           maximum would be true.
        3. A timeout or provider error carries no usage and nothing can be concluded, so
           the reservation is left alone. The ledger's expiry holds it at maximum while
           the request might still matter, then reclaims it. Settling an unknown cost at
           the maximum would permanently overcharge the realm for one dropped connection;
           releasing it would spend money the ledger never recorded.
        """

        if error.billing_status is provider.GenerationBillingStatus.IMPOSSIBLE:
            await store.release(reservation=reservation)
            return "reservation released, nothing was generated"

        usage = getattr(error, "usage", None)
        if usage is not None:
            try:
                actual_cost_nano = _actual_cost_nano(usage, self._config.price_texts)
            except ValueError:
                # Second layer. The adapter already refuses to attach unpriceable counts,
                # so nothing should reach here; if a future change lets something through,
                # it must fall into the expiry lane rather than escape this except block
                # into a connection handler that only understands protocol errors.
                return "reservation left outstanding for expiry, the reported cost was unusable"

            settlement = await store.settle(
                reservation=reservation,
                actual_cost_nano=actual_cost_nano,
                now=self._now(),
            )
            if not settlement.completed:
                return "settlement refused, the reservation had already expired"

            return "settled at the reported cost, the completion was billed"

        return "reservation left outstanding for expiry, billing could not be determined"

    async def _history_for(self, request: protocol.ChatRequest) -> list[tuple[str, str]]:
        if self._store is None or request.is_ambient or request.is_career:
            return []

        return await self._store.recent_turns(bot_guid=request.bot_guid)

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            try:
                await self.purge_pending_bot_data()
            except state.DatabaseUnavailable as error:
                _log(f"connection {peer}: bot purge deferred after {type(error).__name__}, closing")
                return

            while True:
                try:
                    payload = await protocol.read_frame(reader)
                except protocol.FrameError:
                    break

                try:
                    response = await self.process_payload(payload)
                except protocol.ProtocolError as error:
                    _log(f"connection {peer}: {type(error).__name__}, closing")
                    break

                if response is None:
                    # The bridge is synchronously waiting for this frame before it can dequeue
                    # another request. Closing is the protocol's silent answer and releases that
                    # read immediately; keeping the socket open can strand the whole FIFO.
                    break

                writer.write(protocol.encode_frame(response))
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def run_bot_purge_worker(
    service: SidecarService,
    stop: asyncio.Event,
    *,
    poll_seconds: float = BOT_PURGE_POLL_SECONDS,
) -> None:
    """Retry durable bot purge intents while the bridge is otherwise idle."""

    while not stop.is_set():
        try:
            purged = await service.purge_pending_bot_data()
            if purged:
                _log(f"purged sidecar data for {purged} deleted bots")
        except state.DatabaseUnavailable as error:
            _log(f"bot purge deferred after {type(error).__name__}")

        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


async def serve(
    config: SidecarConfig,
    token: str,
    adapter: provider.GenerationProvider | None = None,
    store: state.SidecarState | None = None,
    database: PlayerbotsDatabaseSettings | None = None,
) -> None:
    """Runs the loopback server until SIGINT or SIGTERM.

    Opens the pool only when the caller did not supply a state, so a test can drive the
    real server loop without a database while the deployed path always has one.
    """

    pool = None
    if store is None:
        if database is None:
            raise ValueError("serve needs either an open state or the Playerbots database settings")

        store, pool = await state.open_state(
            database, ceiling_nano=config.budget_nano, reserve_ratio=config.reserve_ratio
        )

    try:
        service = SidecarService(config=config, token=token, adapter=adapter, store=store)
        server = await asyncio.start_server(
            service.handle_connection, host="127.0.0.1", port=config.bridge_port
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)

        address = server.sockets[0].getsockname() if server.sockets else ("127.0.0.1", 0)
        _log(f"listening on 127.0.0.1:{address[1]}")

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(run_bot_purge_worker(service, stop))
            async with server:
                await stop.wait()
    finally:
        if pool is not None:
            await state.close_pool(pool)

    _log("shutting down")


def _log(message: str) -> None:
    print(f"playerbot-llm: {message}", file=sys.stderr, flush=True)


def _default_provider(config: SidecarConfig) -> provider.GenerationProvider:
    return AnthropicProvider(
        timeout_seconds=config.response_deadline_ms / 1000,
        model_io_logger=_log if config.log_model_io else None,
    )


async def _with_state(config: SidecarConfig, database: PlayerbotsDatabaseSettings, work):
    """Runs one coroutine against an open state, and always closes the pool."""

    store, pool = await state.open_state(
        database, ceiling_nano=config.budget_nano, reserve_ratio=config.reserve_ratio
    )
    try:
        return await work(store)
    finally:
        await state.close_pool(pool)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playerbot-llm")
    parser.add_argument("command", choices=["serve", "doctor", "profile"])
    parser.add_argument("--config", required=True, help="Path to mod_playerbot_llm.conf")
    parser.add_argument(
        "--playerbots-config",
        required=True,
        help=(
            "Path to the deployed playerbots.conf holding PlayerbotsDatabaseInfo. "
            "Read rather than duplicated, so there is only one copy of the credentials."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--bot-guid", type=int, help="Bot GUID counter (profile command)")
    arguments = parser.parse_args(argv)

    config = SidecarConfig.load(arguments.config)

    try:
        database = PlayerbotsDatabaseSettings.load(arguments.playerbots_config)
    except (OSError, ValueError) as error:
        # str(error) is the parser's own message, which names the setting and the file
        # but never the value it failed to parse.
        _log(f"cannot read PlayerbotsDatabaseInfo: {error}")
        return 1

    if arguments.command == "doctor":
        generation_provider = _default_provider(config)
        try:
            budget_state = asyncio.run(
                _with_state(config, database, lambda store: store.budget_state(now=datetime.now(UTC)))
            )
        except state.DatabaseUnavailable as error:
            # Only the exception TYPE is logged. A driver message can carry the host,
            # port, and user it failed to authenticate, and doctor's whole purpose is
            # being safe to paste into a bug report.
            _log(f"cannot reach the Playerbots database: {type(error).__name__}")
            budget_state = None
        report = doctor_report(config, generation_provider, budget_state=budget_state)
        report["database_reachable"] = budget_state is not None
        if budget_state is None:
            report["ok"] = False
        print(json.dumps(report, indent=None if arguments.json else 2))
        return 0 if report["ok"] else 1

    if arguments.command == "profile":
        if arguments.bot_guid is None:
            parser.error("profile requires --bot-guid")
        bot_guid = arguments.bot_guid
        try:
            profile = asyncio.run(
                _with_state(config, database, lambda store: store.get_profile(bot_guid=bot_guid))
            )
        except state.DatabaseUnavailable as error:
            _log(f"cannot reach the Playerbots database: {type(error).__name__}")
            return 1
        if profile is None:
            # A bot that has never spoken through the bridge has no trustworthy
            # observed profile to report.
            print(json.dumps({"bot_guid": bot_guid, "observed": False}))
        else:
            print(json.dumps({"bot_guid": bot_guid, "observed": True, "profile": profile}))
        return 0

    token = bridge_token_from_environment()
    if token is None:
        _log(
            f"{TOKEN_ENV_VAR} is missing or shorter than "
            f"{protocol.MIN_BRIDGE_TOKEN_BYTES} bytes; refusing to start"
        )
        return 1

    generation_provider = _default_provider(config)
    if not generation_provider.configured:
        _log("generation provider is not configured; refusing to start")
        return 1

    if not config.enable or config.bridge_port <= 0:
        _log("disabled by configuration (PlayerbotLLM.Enable / BridgePort); refusing to start")
        return 1

    try:
        asyncio.run(serve(config, token, adapter=generation_provider, database=database))
    except state.DatabaseUnavailable as error:
        # Refusing to start is the point. A sidecar that came up without its ledger would
        # answer requests with no way to record what they cost.
        _log(f"cannot reach the Playerbots database: {type(error).__name__}; refusing to start")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
