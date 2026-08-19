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

from playerbots_llm import app, protocol, provider
from playerbots_llm.providers import anthropic as anthropic_provider

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


def social_request_payload(schema_version: int = protocol.SOCIAL_SCHEMA_VERSION) -> bytes:
    return json.dumps(
        {
            "schema_version": schema_version,
            "token": TEST_TOKEN,
            "kind": "social",
            "social_request_token": 77,
            "bot_guid": 500,
            "bot_name": "Grimbold",
            "bot_human": 0,
            "bot_level": 6,
            "subject_guid": 900,
            "subject_name": "Deszy",
            "subject_human": 1,
            "admission_lane": "immediate_human",
            "speak_on_channel": 2,
            "thread_id": "thr_00000000000000000000000000000001",
            "context": "party pull",
            "evidence": [
                {
                    "id": "g1",
                    "subject": "candidate_bot",
                    "fact": "name",
                    "value": "Grimbold",
                    "provenance": "current_world",
                    "scope": "public",
                    "observed_at": 1000,
                }
            ],
            "transcript_event_ids": ["evt_00000000000000000000000000000001"],
            "profile_load_state": "loaded",
            "memory_input_state": "loaded",
            "active_content_expansion": 0,
            "expects_answer": 0,
            "addressed_to_bot": 0,
            "bot_race_id": 3,
            "bot_class_id": 1,
            "bot_zone": "Dun Morogh",
        }
    ).encode()


class FakeAdapter(anthropic_provider.AnthropicProvider):
    def __init__(self) -> None:
        # Deliberately no super().__init__(): the fake never builds a real client.
        self.reply = "A fine day for fishing."
        self.input_tokens = 100
        self.requests: list[protocol.ChatRequest] = []
        self.histories: list[list[tuple[str, str]]] = []
        self.social_requests: list[protocol.SocialRequest] = []

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int:
        # Content-driven so concurrent server processing cannot race test state.
        if "oversized" in request.message:
            return anthropic_provider.MAX_INPUT_TOKENS + 1

        return self.input_tokens

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, provider.GenerationUsage]:
        self.requests.append(request)
        self.histories.append(list(history))
        return self.reply, provider.GenerationUsage(input_tokens=self.input_tokens, output_tokens=10)

    def count_social_input_tokens(self, request: protocol.SocialRequest) -> int:
        return self.input_tokens

    def generate_social_reply(self, request: protocol.SocialRequest) -> provider.SocialGenerationResult:
        self.social_requests.append(request)
        return provider.SocialGenerationResult(
            message=self.reply,
            emote_id=0,
            contribution="fact_free_banter",
            claim_subject="none",
            cited_evidence_ids=(),
            cited_memory_ids=(),
            usage=provider.GenerationUsage(input_tokens=self.input_tokens, output_tokens=10),
        )


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


async def test_social_v7_round_trips_while_social_v6_fails_closed(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        response = await round_trip(harness.port, social_request_payload())

        assert response["schema_version"] == protocol.SOCIAL_SCHEMA_VERSION
        assert response["kind"] == "social"
        assert response["model"] == harness.adapter.metadata.model
        assert response["input_tokens"] == 100
        assert response["output_tokens"] == 10
        assert response["cache_creation_input_tokens"] == 0
        assert response["cache_read_input_tokens"] == 0
        assert response["cost_usd"] == "0.000150"

        await silent_round_trip(
            harness.port,
            social_request_payload(schema_version=protocol.SOCIAL_SCHEMA_VERSION - 1),
        )


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


async def test_oversized_input_closes_the_connection_and_server_keeps_accepting(tmp_path) -> None:
    async with running_sidecar(tmp_path) as harness:
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        try:
            writer.write(protocol.encode_frame(request_payload(request_id=7, message="oversized")))
            await writer.drain()

            # A silent answer has no frame. Closing is what releases the bridge's synchronous
            # read so it can dequeue another request instead of stranding the shared FIFO.
            with pytest.raises(protocol.FrameError):
                await asyncio.wait_for(protocol.read_frame(reader), timeout=0.5)
        finally:
            writer.close()
            await writer.wait_closed()

        response = await round_trip(harness.port, request_payload(request_id=8))
        assert response["request_id"] == 8
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


# Roleplay assessment ------------------------------------------------------------------------------


def assessment_payload(request_token: int = 91, current_line: str = "care to share a tale?") -> bytes:
    return json.dumps(
        {
            "schema_version": protocol.SCHEMA_VERSION,
            "token": TEST_TOKEN,
            "kind": "roleplay_assessment",
            "roleplay_assessment_request_token": request_token,
            "channel": 2,
            "thread_id": "thr_00000000000000000000000000000001",
            "current_line": current_line,
            "thread_lines": ["Elyse: well met"],
        }
    ).encode()


class AssessingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.assessments: list[protocol.RoleplayAssessmentRequest] = []
        self.completion = protocol.RoleplayAssessmentCompletion(
            assessment_kind="roleplay_invitation", capabilities=["classic_content"]
        )

    def count_roleplay_assessment_input_tokens(self, request: protocol.RoleplayAssessmentRequest) -> int:
        return self.input_tokens

    def assess_roleplay(
        self, request: protocol.RoleplayAssessmentRequest
    ) -> tuple[protocol.RoleplayAssessmentCompletion, provider.GenerationUsage]:
        if "explode" in request.current_line:
            raise provider.GenerationProviderError("provider unavailable")
        if "malformed" in request.current_line:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the roleplay assessment schema",
                provider.GenerationUsage(input_tokens=self.input_tokens, output_tokens=4),
            )
        if "slow" in request.current_line:
            import time

            time.sleep(0.4)

        self.assessments.append(request)
        return self.completion, provider.GenerationUsage(input_tokens=self.input_tokens, output_tokens=8)


@dataclass
class AssessmentHarness:
    server: asyncio.Server
    port: int
    store: FakeState
    adapter: AssessingAdapter


@asynccontextmanager
async def running_assessing_sidecar(budget: str = "1.0", deadline_ms: int = 10000):
    adapter = AssessingAdapter()
    config = app.SidecarConfig(
        enable=True,
        bridge_port=0,
        daily_budget_usd=budget,
        response_deadline_ms=deadline_ms,
    )
    store = FakeState(config.budget_nano, config.reserve_ratio)
    service = app.SidecarService(config=config, token=TEST_TOKEN, adapter=adapter, store=store)
    server = await asyncio.start_server(service.handle_connection, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield AssessmentHarness(server=server, port=port, store=store, adapter=adapter)
    finally:
        server.close()
        await server.wait_closed()


async def silent_round_trip(port: int, payload: bytes) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(protocol.encode_frame(payload))
    writer.write_eof()
    with pytest.raises(protocol.FrameError):
        await protocol.read_frame(reader)
    writer.close()
    await writer.wait_closed()


async def test_assessment_round_trips_with_authenticated_correlation() -> None:
    async with running_assessing_sidecar() as harness:
        response = await round_trip(harness.port, assessment_payload())

        # Correlation comes from the request and the shared secret, never from the model:
        # the completion schema has no field that could have supplied either.
        assert response["schema_version"] == protocol.SCHEMA_VERSION
        assert response["token"] == TEST_TOKEN
        assert response["kind"] == "roleplay_assessment"
        assert response["roleplay_assessment_request_token"] == 91
        assert response["assessment_kind"] == "roleplay_invitation"
        assert response["capability_count"] == 1
        assert response["capability_0"] == "classic_content"

        # The classifier saw the bounded conversation and nothing else.
        assert len(harness.adapter.assessments) == 1
        assert harness.adapter.assessments[0].current_line == "care to share a tale?"

        # Admitted as human social work, and its cost reported exactly once, as its own call.
        from playerbots_llm import budget as budget_module

        assert harness.store.reserved_priorities == [budget_module.RequestPriority.IMMEDIATE_HUMAN]
        assert harness.store.reserved_kinds == [budget_module.RequestKind.MODERATION_CLASSIFICATION]
        assert len(harness.store.settlements) == 1
        assert harness.store.settled_nano > 0
        assert harness.store.outstanding == {}


async def test_assessment_provider_failure_is_silent_and_fails_closed_on_money() -> None:
    async with running_assessing_sidecar() as harness:
        await silent_round_trip(harness.port, assessment_payload(current_line="explode now"))

        # No completion was produced and nothing was synthesized in its place.
        assert harness.adapter.assessments == []
        assert harness.store.settlements == []

        # Billing could not be determined, so the reservation is left outstanding for expiry
        # rather than refunded: the same fail-closed-on-money contract every other lane has.
        assert len(harness.store.reservations) == 1
        assert harness.store.outstanding != {}


async def test_assessment_malformed_output_is_silent_never_synthesized() -> None:
    async with running_assessing_sidecar() as harness:
        await silent_round_trip(harness.port, assessment_payload(current_line="malformed output"))

        # No fabricated "ordinary": the C++ side treats silence as its ordinary fallback.
        assert harness.store.outstanding == {}
        assert harness.store.settled_nano > 0


async def test_assessment_budget_refusal_is_silent() -> None:
    async with running_assessing_sidecar(budget="0.000001") as harness:
        await silent_round_trip(harness.port, assessment_payload())

        assert harness.adapter.assessments == []
        assert harness.store.outstanding == {}


async def test_assessment_circuit_breaker_refusal_is_silent() -> None:
    async with running_assessing_sidecar() as harness:
        harness.store.circuit_open = True
        await silent_round_trip(harness.port, assessment_payload())

        assert harness.adapter.assessments == []
        assert harness.store.outstanding == {}


async def test_assessment_deadline_is_silent() -> None:
    async with running_assessing_sidecar(deadline_ms=50) as harness:
        await silent_round_trip(harness.port, assessment_payload(current_line="slow answer please"))

        # Nothing was settled or delivered. A reservation made before the cut may stay charged
        # at maximum until expiry, which is the documented fail-closed-on-money behavior.
        assert harness.store.settlements == []
