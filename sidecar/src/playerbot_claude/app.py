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
import sqlite3
import sys
from dataclasses import dataclass

from playerbot_claude import claude, protocol, storage

CONFIG_SECTION = "worldserver"
CONFIG_PREFIX = "PlayerbotClaude."
# Environment variable *names*, not secret values.
TOKEN_ENV_VAR = "PLAYERBOT_CLAUDE_BRIDGE_TOKEN"  # noqa: S105
API_KEY_ENV_VAR = claude.API_KEY_ENV_VAR
MAX_DAILY_BUDGET_USD = 5.0


@dataclass(frozen=True)
class SidecarConfig:
    enable: bool = False
    bridge_port: int = 0
    ambient_world_enable: bool = False
    ambient_max_messages_per_hour: int = 6
    daily_budget_usd: float = 0.0
    input_usd_per_mtok: float = 1.00
    output_usd_per_mtok: float = 5.00
    database_path: str = "playerbot_claude.sqlite"
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
            daily_budget_usd=float(option("DailyBudgetUsd", "0")),
            input_usd_per_mtok=float(option("InputUsdPerMTok", "1.00")),
            output_usd_per_mtok=float(option("OutputUsdPerMTok", "5.00")),
            database_path=option("SidecarDatabase", "playerbot_claude.sqlite"),
            response_deadline_ms=int(option("ResponseDeadlineMs", "10000")),
            queue_size=int(option("QueueSize", "16")),
            group_cooldown_seconds=int(option("GroupCooldownSeconds", "120")),
        )

    @property
    def prices(self) -> storage.PriceSnapshot:
        return storage.PriceSnapshot.from_usd_per_mtok(self.input_usd_per_mtok, self.output_usd_per_mtok)

    @property
    def budget_nano(self) -> int:
        return storage.usd_to_nano(self.daily_budget_usd)

    @property
    def generation_allowed(self) -> bool:
        """Positive budget and positive rates are required for any provider call."""

        return (
            0 < self.daily_budget_usd <= MAX_DAILY_BUDGET_USD
            and self.input_usd_per_mtok > 0
            and self.output_usd_per_mtok > 0
        )

    @property
    def ambient_allowed(self) -> bool:
        return (
            self.ambient_world_enable
            and 1 <= self.ambient_max_messages_per_hour <= storage.MAX_AMBIENT_MESSAGES_PER_HOUR
        )


def bridge_token_from_environment() -> str | None:
    token = os.environ.get(TOKEN_ENV_VAR)
    if token is None or len(token.encode("utf-8")) < protocol.MIN_BRIDGE_TOKEN_BYTES:
        return None

    return token


def doctor_report(config: SidecarConfig, store: storage.Storage | None = None) -> dict[str, object]:
    """Health summary as JSON-safe data. Never contains a secret value."""

    token_present = bridge_token_from_environment() is not None
    api_key_present = bool(os.environ.get(API_KEY_ENV_VAR))
    ok = config.enable and config.bridge_port > 0 and config.generation_allowed and token_present

    report: dict[str, object] = {
        "ok": ok,
        "enable": config.enable,
        "bridge_port": config.bridge_port,
        "daily_budget_usd": config.daily_budget_usd,
        "response_deadline_ms": config.response_deadline_ms,
        "bridge_token_present": token_present,
        "anthropic_api_key_present": api_key_present,
    }

    if store is not None:
        snapshot = store.rolling_budget_snapshot()
        remaining = max(0, config.budget_nano - snapshot.spent_nano - snapshot.reserved_nano)
        report["budget"] = {
            "rolling_spent_usd": storage.nano_to_usd_string(snapshot.spent_nano),
            "rolling_reserved_usd": storage.nano_to_usd_string(snapshot.reserved_nano),
            "rolling_remaining_usd": storage.nano_to_usd_string(remaining),
            "next_expiry_at": (
                snapshot.next_expiry_at.isoformat() if snapshot.next_expiry_at is not None else None
            ),
        }

    return report


class SidecarService:
    """Parses, validates, and answers one request payload at a time.

    All budget bookkeeping (count, reserve, settle, memory) is serialized with a
    single lock, so socket concurrency can never race the ledger. One residual
    overlap exists by design: when the deadline cancels a request mid-generation,
    the lock is released while the abandoned synchronous SDK call finishes in its
    worker thread (Python cannot interrupt it; the client timeout, capped at the
    same deadline, bounds it). That is safe: httpx clients are thread-safe, and a
    cancelled request can never settle or write conversation memory, so its
    reservation stays charged at maximum.
    """

    def __init__(
        self,
        config: SidecarConfig,
        token: str,
        adapter: claude.ClaudeAdapter | None = None,
        store: storage.Storage | None = None,
    ) -> None:
        self._config = config
        self._token = token
        # The default SDK client's own timeout is capped at the response deadline so
        # a provider call cannot outlive the request that paid for it.
        self._adapter = adapter or claude.ClaudeAdapter(timeout_seconds=config.response_deadline_ms / 1000)
        self._store = store
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
        async with self._generation_lock:
            if request.is_ambient:
                if self._store is None or not self._store.try_begin_ambient(
                    self._config.ambient_max_messages_per_hour
                ):
                    _log(f"request {request.request_id}: ambient rate exhausted, staying silent")
                    return None

            if self._store is not None:
                self._store.record_profile(request)
            history = self._history_for(request)

            reservation: int | None = None
            if self._store is not None:
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

                reservation = self._store.reserve(
                    request.request_id,
                    input_tokens,
                    claude.MAX_OUTPUT_TOKENS,
                    self._config.prices,
                    self._config.budget_nano,
                )
                if reservation is None:
                    _log(f"request {request.request_id}: budget exhausted, staying silent")
                    return None
                self._store.mark_submitted(reservation)

            try:
                reply, usage = await asyncio.to_thread(self._adapter.generate_reply, request, history)
            except claude.ClaudeError as error:
                # Fail closed on money: an unsettled reservation deliberately stays
                # charged at its maximum cost until independently reconciled.
                _log(f"request {request.request_id}: {type(error).__name__}: {error}")
                return None

            if self._store is not None:
                if reservation is not None:
                    self._store.settle(
                        reservation, usage.input_tokens, usage.output_tokens, self._config.prices
                    )
                if not request.is_ambient:
                    self._store.append_turn(request.bot_guid, "user", claude.build_user_message(request))
                    self._store.append_turn(request.bot_guid, "assistant", reply)

        _log(f"request {request.request_id}: replied ({usage.input_tokens} in, {usage.output_tokens} out)")
        return protocol.encode_response(request.request_id, reply, self._token)

    def _history_for(self, request: protocol.ChatRequest) -> list[tuple[str, str]]:
        if self._store is None or request.is_ambient:
            return []

        return self._store.recent_turns(request.bot_guid)

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
    store: storage.Storage | None = None,
) -> None:
    owned_store = store is None
    if store is None:
        store = storage.Storage(config.database_path)

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
        if owned_store:
            store.close()

    _log("shutting down")


def _log(message: str) -> None:
    print(f"playerbot-claude: {message}", file=sys.stderr, flush=True)


def _open_store(config: SidecarConfig) -> storage.Storage | None:
    try:
        return storage.Storage(config.database_path)
    except sqlite3.Error:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playerbot-claude")
    parser.add_argument("command", choices=["serve", "doctor", "profile"])
    parser.add_argument("--config", required=True, help="Path to mod_playerbot_claude.conf")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--bot-guid", type=int, help="Bot GUID counter (profile command)")
    arguments = parser.parse_args(argv)

    config = SidecarConfig.load(arguments.config)

    if arguments.command == "doctor":
        store = _open_store(config)
        try:
            report = doctor_report(config, store=store)
        finally:
            if store is not None:
                store.close()
        print(json.dumps(report, indent=None if arguments.json else 2))
        return 0 if report["ok"] else 1

    if arguments.command == "profile":
        if arguments.bot_guid is None:
            parser.error("profile requires --bot-guid")
        store = _open_store(config)
        profile = None
        if store is not None:
            try:
                profile = store.get_profile(arguments.bot_guid)
            finally:
                store.close()
        if profile is None:
            # A bot that has never spoken through the bridge has no trustworthy
            # observed profile to report.
            print(json.dumps({"bot_guid": arguments.bot_guid, "observed": False}))
        else:
            print(json.dumps({"bot_guid": arguments.bot_guid, "observed": True, "profile": profile}))
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

    asyncio.run(serve(config, token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
