"""Loopback socket integration tests: the real server path with a fake provider.

These tests run the actual asyncio server (real TCP on 127.0.0.1, operating system
assigned port) against an injected fake adapter, proving the cross-process contract:
framing, authentication, silence semantics, reconnection, and shutdown. No real HTTP
request is ever made.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from fakes import FakeState

from playerbot_claude import app, claude, protocol

TEST_TOKEN = "0123456789abcdef0123456789abcdef"


def request_payload(request_id: int = 7, message: str = "What do you enjoy doing?") -> bytes:
    return json.dumps(
        {
            "schema_version": protocol.SCHEMA_VERSION,
            "token": TEST_TOKEN,
            "request_id": request_id,
            "channel": "whisper",
            "bot_guid": 42,
            "speaker_guid": 9001,
            "bot_name": "Botname",
            "speaker_name": "Speaker",
            "profile_version": 2,
            "crafting_affinity": 65,
            "gathering_affinity": 37,
            "exploration_affinity": 91,
            "sociability": 82,
            "voice": "earnest",
            "event_kind": 0,
            "subject_id": 0,
            "occurrence": 0,
            "message": message,
        }
    ).encode()


class FakeAdapter(claude.ClaudeAdapter):
    def __init__(self) -> None:
        # Deliberately no super().__init__(): the fake never builds a real client.
        self.reply = "A fine day for fishing."
        self.input_tokens = 100
        self.requests: list[protocol.ChatRequest] = []
        self.histories: list[list[tuple[str, str]]] = []

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int:
        # Content-driven so concurrent server processing cannot race test state.
        if "oversized" in request.message:
            return claude.MAX_INPUT_TOKENS + 1

        return self.input_tokens

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, claude.UsageTotals]:
        self.requests.append(request)
        self.histories.append(list(history))
        return self.reply, claude.UsageTotals(input_tokens=self.input_tokens, output_tokens=10)


@dataclass
class Harness:
    server: asyncio.Server
    port: int
    store: FakeState
    adapter: FakeAdapter


@asynccontextmanager
async def running_sidecar(tmp_path, budget: str = "1.0"):
    """The real server loop over an in-memory state.

    What these tests are for is the cross-process contract: framing, authentication,
    silence, reconnection, shutdown. Pointing them at a real MySQL would make every one
    of them a database test that fails for database reasons, and the ledger's own
    behaviour is already proven against a live server in tests/test_ledger_mysql.py.
    """

    adapter = FakeAdapter()
    config = app.SidecarConfig(
        enable=True,
        bridge_port=0,
        ambient_world_enable=True,
        ambient_max_messages_per_hour=6,
        daily_budget_usd=budget,
    )
    store = FakeState(config.budget_nano, config.reserve_ratio)
    service = app.SidecarService(config=config, token=TEST_TOKEN, adapter=adapter, store=store)
    server = await asyncio.start_server(service.handle_connection, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield Harness(server=server, port=port, store=store, adapter=adapter)
    finally:
        server.close()
        await server.wait_closed()


async def round_trip(port: int, payload: bytes) -> dict[str, object]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(protocol.encode_frame(payload))
        await writer.drain()
        return json.loads(await protocol.read_frame(reader))
    finally:
        writer.close()
        await writer.wait_closed()


async def test_authenticated_request_round_trips_over_real_socket(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        response = await round_trip(harness.port, request_payload())

        assert response["schema_version"] == protocol.SCHEMA_VERSION
        assert response["request_id"] == 7
        assert response["message"] == "A fine day for fishing."
        assert response["token"] == TEST_TOKEN

        # The full pipeline ran: profile stored, memory appended, budget settled.
        assert harness.store.profiles.get(42) is not None
        assert len(harness.store.turns[42]) == 2
        assert harness.store.settled_nano > 0
        assert harness.store.outstanding == {}


async def test_wrong_token_closes_connection_and_server_keeps_accepting(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        bad = json.loads(request_payload())
        bad["token"] = "z" * 32
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        writer.write(protocol.encode_frame(json.dumps(bad).encode()))
        await writer.drain()

        # The server answers authentication failure by closing the connection.
        with pytest.raises(protocol.FrameError):
            await protocol.read_frame(reader)
        writer.close()
        await writer.wait_closed()
        assert harness.adapter.requests == []

        # A fresh, correctly authenticated connection still works.
        response = await round_trip(harness.port, request_payload(request_id=8))
        assert response["request_id"] == 8


async def test_oversized_input_is_silent_and_connection_survives(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        try:
            writer.write(protocol.encode_frame(request_payload(request_id=7, message="oversized")))
            await writer.drain()

            # The same connection stays usable; the next reply proves the first
            # request produced no response frame at all.
            writer.write(protocol.encode_frame(request_payload(request_id=8)))
            await writer.drain()

            response = json.loads(await protocol.read_frame(reader))
            assert response["request_id"] == 8
        finally:
            writer.close()
            await writer.wait_closed()

        assert len(harness.adapter.requests) == 1
        assert harness.store.outstanding == {}


async def test_exhausted_budget_is_silent(tmp_path) -> None:
    async with running_sidecar(tmp_path, budget="0.000001") as harness:
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        writer.write(protocol.encode_frame(request_payload()))
        writer.write_eof()

        # EOF arrives with zero response bytes: the request was dropped silently.
        with pytest.raises(protocol.FrameError):
            await protocol.read_frame(reader)
        writer.close()
        await writer.wait_closed()

        assert harness.adapter.requests == []
        assert harness.store.outstanding == {}


async def test_reconnect_preserves_conversation_memory(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        first = await round_trip(harness.port, request_payload(request_id=7))
        assert first["request_id"] == 7

        # A brand new connection reaches the same store: the second generation
        # sees the two turns recorded by the first exchange.
        second = await round_trip(harness.port, request_payload(request_id=8, message="Still there?"))
        assert second["request_id"] == 8
        assert len(harness.adapter.histories[0]) == 0
        assert len(harness.adapter.histories[1]) == 2


async def test_graceful_shutdown_stops_accepting_connections(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        response = await round_trip(harness.port, request_payload())
        assert response["request_id"] == 7

        harness.server.close()
        await harness.server.wait_closed()

        with pytest.raises(ConnectionError):
            await asyncio.open_connection("127.0.0.1", harness.port)
