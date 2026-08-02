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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from playerbot_claude import budget, claude, ledger, protocol, state
from playerbot_claude.budget import AdmissionDecision, RequestPriority

CONFIG_SECTION = "worldserver"
CONFIG_PREFIX = "PlayerbotClaude."
# Environment variable *names*, not secret values.
TOKEN_ENV_VAR = "PLAYERBOT_CLAUDE_BRIDGE_TOKEN"  # noqa: S105
API_KEY_ENV_VAR = claude.API_KEY_ENV_VAR

# There is deliberately no maximum above the configured ceiling. A second limit in the
# code silently ignores what the operator asked for, and PlayerbotClaude.DailyBudgetUsd
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
            and 1 <= self.ambient_max_messages_per_hour <= ledger.MAX_AMBIENT_MESSAGES_PER_HOUR
        )


def bridge_token_from_environment() -> str | None:
    """Both bounds, at the one place the token enters the process.

    The floor is for entropy and the ceiling is for the same reason every other string in
    this protocol has one: without it a very long token is copied, compared, and written
    into every frame before anything refuses it. Mirrors ClaudeChat::BridgeTokenIsUsable.
    """

    token = os.environ.get(TOKEN_ENV_VAR)
    if token is None:
        return None

    length = len(token.encode("utf-8"))
    if length < protocol.MIN_BRIDGE_TOKEN_BYTES or length > protocol.MAX_BRIDGE_TOKEN_BYTES:
        return None

    return token


def doctor_report(config: SidecarConfig, budget_state: budget.BudgetState | None = None) -> dict[str, object]:
    """Health summary as JSON-safe data. Never contains a secret value."""

    token_present = bridge_token_from_environment() is not None
    api_key_present = bool(os.environ.get(API_KEY_ENV_VAR))
    ok = config.enable and config.bridge_port > 0 and config.generation_allowed and token_present

    report: dict[str, object] = {
        "ok": ok,
        "enable": config.enable,
        "bridge_port": config.bridge_port,
        "daily_budget_usd": config.daily_budget_usd,
        "human_budget_reserve_ratio": config.human_budget_reserve_ratio,
        "response_deadline_ms": config.response_deadline_ms,
        "bridge_token_present": token_present,
        "anthropic_api_key_present": api_key_present,
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


def _actual_cost_nano(usage: claude.UsageTotals, prices: tuple[str, str]) -> int:
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
        adapter: claude.ClaudeAdapter | None = None,
        store: state.SidecarState | None = None,
        now=None,
    ) -> None:
        self._config = config
        self._token = token
        # The default SDK client's own timeout is capped at the response deadline so
        # a provider call cannot outlive the request that paid for it.
        self._adapter = adapter or claude.ClaudeAdapter(timeout_seconds=config.response_deadline_ms / 1000)
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._generation_lock = asyncio.Lock()

    async def process_payload(self, payload: bytes) -> bytes | None:
        """Returns the response payload, or None when the bot must stay silent.

        Raises ProtocolError (including TokenMismatchError) for malformed or
        unauthenticated payloads; the connection handler closes that connection.
        """

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
            except claude.ClaudeError as error:
                _log(f"request {request.request_id}: {type(error).__name__}: {error}")
                return None

            if input_tokens > claude.MAX_INPUT_TOKENS:
                _log(
                    f"request {request.request_id}: prompt is {input_tokens} tokens "
                    f"(limit {claude.MAX_INPUT_TOKENS}), staying silent"
                )
                return None

            max_cost_nano = budget.conservative_max_cost_nano(
                input_tokens, claude.MAX_OUTPUT_TOKENS, *input_prices
            )
            decision, reservation = await store.reserve(
                request_id=request.request_id,
                max_cost_nano=max_cost_nano,
                priority=_priority_for(request),
                now=now,
            )
            if decision is not AdmissionDecision.ADMITTED or reservation is None:
                _log(f"request {request.request_id}: {decision.value}, staying silent")
                return None

            try:
                reply, usage = await asyncio.to_thread(self._adapter.generate_reply, request, history)
            except claude.ClaudeError as error:
                # The reservation is given back rather than left to expire. The call
                # raised before any completion existed, so no tokens were billed, and
                # holding the maximum for ten minutes would deny a request that the
                # budget can in fact afford. A failure the sidecar never learns about
                # (a crash, a cancelled deadline) still falls through to expiry.
                await store.release(reservation=reservation)
                _log(f"request {request.request_id}: {type(error).__name__}: {error}")
                return None

            settled_at = self._now()
            await store.settle(
                reservation=reservation,
                actual_cost_nano=_actual_cost_nano(usage, input_prices),
                now=settled_at,
            )

            if request.is_career:
                choice = claude.CareerReply.model_validate_json(reply)
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
                    content=claude.build_user_message(request),
                    now=settled_at,
                )
                await store.append_turn(
                    bot_guid=request.bot_guid, role="assistant", content=reply, now=settled_at
                )

        _log(f"request {request.request_id}: replied ({usage.input_tokens} in, {usage.output_tokens} out)")
        return protocol.encode_response(request.request_id, reply, self._token)

    async def _history_for(self, request: protocol.ChatRequest) -> list[tuple[str, str]]:
        if self._store is None or request.is_ambient or request.is_career:
            return []

        return await self._store.recent_turns(bot_guid=request.bot_guid)

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
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
                    continue

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


async def serve(
    config: SidecarConfig,
    token: str,
    adapter: claude.ClaudeAdapter | None = None,
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

        async with server:
            await stop.wait()
    finally:
        if pool is not None:
            await state.close_pool(pool)

    _log("shutting down")


def _log(message: str) -> None:
    print(f"playerbot-claude: {message}", file=sys.stderr, flush=True)


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
    parser = argparse.ArgumentParser(prog="playerbot-claude")
    parser.add_argument("command", choices=["serve", "doctor", "profile"])
    parser.add_argument("--config", required=True, help="Path to mod_playerbot_claude.conf")
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
        try:
            budget_state = asyncio.run(
                _with_state(config, database, lambda store: store.budget_state(now=datetime.now(UTC)))
            )
        except OSError as error:
            _log(f"cannot reach the Playerbots database: {type(error).__name__}")
            budget_state = None
        report = doctor_report(config, budget_state=budget_state)
        report["database_reachable"] = budget_state is not None
        if budget_state is None:
            report["ok"] = False
        print(json.dumps(report, indent=None if arguments.json else 2))
        return 0 if report["ok"] else 1

    if arguments.command == "profile":
        if arguments.bot_guid is None:
            parser.error("profile requires --bot-guid")
        bot_guid = arguments.bot_guid
        profile = asyncio.run(
            _with_state(config, database, lambda store: store.get_profile(bot_guid=bot_guid))
        )
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

    if not os.environ.get(API_KEY_ENV_VAR):
        _log(f"{API_KEY_ENV_VAR} is not set; refusing to start")
        return 1

    if not config.enable or config.bridge_port <= 0:
        _log("disabled by configuration (PlayerbotClaude.Enable / BridgePort); refusing to start")
        return 1

    asyncio.run(serve(config, token, database=database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
