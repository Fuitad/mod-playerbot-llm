"""Offline unit tests for the sidecar.

Every byte fixture here mirrors the C++ contract tests in tests/ClaudeChatTest.cpp so
the two implementations cannot drift silently. No test makes a real HTTP request.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from fakes import FakeState

from playerbot_claude import app, budget, claude, protocol

TEST_TOKEN = "0123456789abcdef0123456789abcdef"

# Pinned so a settlement records a known instant rather than whatever the suite ran at.
FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# Byte-for-byte copy of the C++ RequestSerializesToExactContractJson fixture.
CPP_REQUEST_FIXTURE = (
    '{"schema_version":3,'
    '"token":"0123456789abcdef0123456789abcdef",'
    '"request_id":7,'
    '"channel":"whisper",'
    '"bot_guid":42,'
    '"speaker_guid":9001,'
    '"bot_name":"Botname",'
    '"speaker_name":"Speaker",'
    '"profile_version":2,'
    '"crafting_affinity":65,'
    '"gathering_affinity":37,'
    '"exploration_affinity":91,'
    '"sociability":82,'
    '"voice":"earnest",'
    '"event_kind":0,'
    '"subject_id":0,'
    '"occurrence":0,'
    '"message":"What do you enjoy doing?"}'
)

# Byte-for-byte copy of the C++ AmbientRequestSerializesToExactContractJson fixture.
CPP_AMBIENT_REQUEST_FIXTURE = (
    '{"schema_version":3,'
    '"token":"0123456789abcdef0123456789abcdef",'
    '"request_id":8,'
    '"channel":"world",'
    '"bot_guid":42,'
    '"speaker_guid":0,'
    '"bot_name":"Botname",'
    '"speaker_name":"",'
    '"profile_version":2,'
    '"crafting_affinity":65,'
    '"gathering_affinity":37,'
    '"exploration_affinity":91,'
    '"sociability":82,'
    '"voice":"earnest",'
    '"event_kind":4,'
    '"subject_id":0,'
    '"occurrence":9,'
    '"message":"ambient_world"}'
)


def valid_request_dict() -> dict[str, object]:
    return json.loads(CPP_REQUEST_FIXTURE)


def ambient_request_dict() -> dict[str, object]:
    return json.loads(CPP_AMBIENT_REQUEST_FIXTURE)


def career_request_dict() -> dict[str, object]:
    payload = valid_request_dict()
    payload.update(
        channel="career",
        speaker_guid=0,
        speaker_name="",
        event_kind=protocol.CAREER_EVENT_KIND,
        subject_id=0,
        occurrence=0,
        message=json.dumps(
            {
                "personality_version": 2,
                "career_version": 1,
                "candidates": [
                    {
                        "token": "career-abc123",
                        "summary": "No professions",
                        "maximum_spending_style": "none",
                        "market_eligible": 0,
                        "engagement": 0,
                    },
                    {
                        "token": "career-def456",
                        "summary": "Gathering career",
                        "maximum_spending_style": "progression",
                        "market_eligible": 1,
                        "engagement": 78,
                    },
                ],
            },
            separators=(",", ":"),
        ),
    )
    return payload


def parse(payload_dict: dict[str, object], token: str = TEST_TOKEN) -> protocol.ChatRequest:
    return protocol.parse_request(json.dumps(payload_dict).encode(), token)


def make_reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


# --- Framing ---


def test_encode_frame_uses_network_order_prefix() -> None:
    frame = protocol.encode_frame(b"abc")
    assert frame == b"\x00\x00\x00\x03abc"


def test_encode_frame_rejects_oversized_payload() -> None:
    protocol.encode_frame(b"x" * protocol.MAX_FRAME_PAYLOAD_BYTES)
    with pytest.raises(protocol.FrameError):
        protocol.encode_frame(b"x" * (protocol.MAX_FRAME_PAYLOAD_BYTES + 1))


async def test_read_frame_reassembles_fragmented_reads() -> None:
    payload = b'{"hello":"world"}'
    frame = protocol.encode_frame(payload)
    # Split mid-header and mid-payload.
    reader = make_reader(frame[:2], frame[2:7], frame[7:])
    assert await protocol.read_frame(reader) == payload


async def test_read_frame_handles_consecutive_frames() -> None:
    first = protocol.encode_frame(b"one")
    second = protocol.encode_frame(b"two")
    reader = make_reader(first + second)
    assert await protocol.read_frame(reader) == b"one"
    assert await protocol.read_frame(reader) == b"two"


async def test_read_frame_rejects_oversized_length() -> None:
    oversized = (protocol.MAX_FRAME_PAYLOAD_BYTES + 1).to_bytes(4, "big") + b"x"
    reader = make_reader(oversized)
    with pytest.raises(protocol.FrameError):
        await protocol.read_frame(reader)


async def test_read_frame_rejects_truncated_stream() -> None:
    frame = protocol.encode_frame(b"full payload")
    reader = make_reader(frame[: len(frame) - 3])
    with pytest.raises(protocol.FrameError):
        await protocol.read_frame(reader)


# --- Request parsing ---


def test_accepts_exact_cpp_fixture() -> None:
    request = protocol.parse_request(CPP_REQUEST_FIXTURE.encode(), TEST_TOKEN)
    assert request.request_id == 7
    assert request.channel == "whisper"
    assert request.bot_guid == 42
    assert request.speaker_guid == 9001
    assert request.bot_name == "Botname"
    assert request.crafting_affinity == 65
    assert request.gathering_affinity == 37
    assert request.exploration_affinity == 91
    assert request.sociability == 82
    assert request.voice == "earnest"
    assert request.event_kind == 0
    assert request.message == "What do you enjoy doing?"


def test_accepts_exact_cpp_ambient_fixture() -> None:
    request = protocol.parse_request(CPP_AMBIENT_REQUEST_FIXTURE.encode(), TEST_TOKEN)
    assert request.request_id == 8
    assert request.channel == "world"
    assert request.speaker_guid == 0
    assert request.event_kind == protocol.AMBIENT_EVENT_KIND
    assert request.occurrence == 9
    assert request.message == protocol.AMBIENT_EVENT_MARKER


def test_rejects_invalid_utf8_payload() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(b'{"schema_version":3,"bad":"\xff"}', TEST_TOKEN)


def test_rejects_non_object_payload() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(b"[1,2,3]", TEST_TOKEN)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(b"not json", TEST_TOKEN)


def test_rejects_wrong_schema_version() -> None:
    payload = valid_request_dict()
    payload["schema_version"] = 1
    with pytest.raises(protocol.ProtocolError):
        parse(payload)


def test_rejects_missing_and_extra_fields() -> None:
    missing = valid_request_dict()
    del missing["message"]
    with pytest.raises(protocol.ProtocolError):
        parse(missing)

    extra = valid_request_dict()
    extra["action"] = "cast fireball"
    with pytest.raises(protocol.ProtocolError):
        parse(extra)


def test_rejects_duplicate_fields_and_type_coercion() -> None:
    duplicate = CPP_REQUEST_FIXTURE.replace('"request_id":7,', '"request_id":7,"request_id":8,')
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(duplicate.encode(), TEST_TOKEN)

    for invalid_request_id in ('"7"', "true", "7.0"):
        coerced = CPP_REQUEST_FIXTURE.replace('"request_id":7,', f'"request_id":{invalid_request_id},')
        with pytest.raises(protocol.ProtocolError):
            protocol.parse_request(coerced.encode(), TEST_TOKEN)


def test_rejects_invalid_guids() -> None:
    for field in ("bot_guid", "speaker_guid"):
        payload = valid_request_dict()
        payload[field] = 0
        with pytest.raises(protocol.ProtocolError):
            parse(payload)

        payload[field] = 2**64
        with pytest.raises(protocol.ProtocolError):
            parse(payload)


def test_rejects_out_of_bounds_profile() -> None:
    for field in ("crafting_affinity", "gathering_affinity", "exploration_affinity", "sociability"):
        payload = valid_request_dict()
        payload[field] = 101
        with pytest.raises(protocol.ProtocolError):
            parse(payload)

        payload[field] = -1
        with pytest.raises(protocol.ProtocolError):
            parse(payload)

    payload = valid_request_dict()
    payload["profile_version"] = 1
    with pytest.raises(protocol.ProtocolError):
        parse(payload)

    payload = valid_request_dict()
    payload["voice"] = "sarcastic"
    with pytest.raises(protocol.ProtocolError):
        parse(payload)


def test_rejects_unknown_event_kinds_and_channels() -> None:
    payload = valid_request_dict()
    payload["event_kind"] = 6
    with pytest.raises(protocol.ProtocolError):
        parse(payload)

    payload = valid_request_dict()
    payload["channel"] = "guild"
    with pytest.raises(protocol.ProtocolError):
        parse(payload)


def test_accepts_only_the_trusted_ambient_field_combination() -> None:
    request = parse(ambient_request_dict())
    assert request.channel == "world"
    assert request.event_kind == 4
    assert request.speaker_guid == 0
    assert request.speaker_name == ""

    for field, invalid in (
        ("channel", "party"),
        ("speaker_guid", 9001),
        ("speaker_name", "Speaker"),
        ("event_kind", 0),
        ("subject_id", 42),
        ("message", "player supplied text"),
    ):
        payload = ambient_request_dict()
        payload[field] = invalid
        with pytest.raises(protocol.ProtocolError):
            parse(payload)


def test_rejects_ambient_identity_fields_on_direct_chat() -> None:
    for field, invalid in (("speaker_guid", 0), ("speaker_name", "")):
        payload = valid_request_dict()
        payload[field] = invalid
        with pytest.raises(protocol.ProtocolError):
            parse(payload)


def test_accepts_only_opaque_bounded_career_candidates() -> None:
    request = parse(career_request_dict())
    assert request.is_career
    assert request.career_content.personality_version == 2
    assert request.career_content.career_version == 1
    assert [candidate.token for candidate in request.career_content.candidates] == [
        "career-abc123",
        "career-def456",
    ]

    for field, invalid in (
        ("channel", "party"),
        ("speaker_guid", 9001),
        ("speaker_name", "Speaker"),
        ("event_kind", 0),
        ("subject_id", 42),
        ("occurrence", 1),
    ):
        payload = career_request_dict()
        payload[field] = invalid
        with pytest.raises(protocol.ProtocolError):
            parse(payload)


def test_rejects_career_raw_ids_duplicates_and_invalid_styles() -> None:
    for mutation in ("raw_id", "duplicate", "style"):
        payload = career_request_dict()
        content = json.loads(str(payload["message"]))
        if mutation == "raw_id":
            content["candidates"][0]["skill_id"] = 164
        elif mutation == "duplicate":
            content["candidates"][1]["token"] = content["candidates"][0]["token"]
        else:
            content["candidates"][1]["maximum_spending_style"] = "unlimited"
        payload["message"] = json.dumps(content, separators=(",", ":"))
        with pytest.raises(protocol.ProtocolError):
            parse(payload)


def test_rejects_oversized_or_empty_message() -> None:
    payload = valid_request_dict()
    payload["message"] = ""
    with pytest.raises(protocol.ProtocolError):
        parse(payload)

    payload["message"] = "x" * (protocol.MAX_REQUEST_MESSAGE_BYTES + 1)
    with pytest.raises(protocol.ProtocolError):
        parse(payload)

    # Multibyte characters count in bytes, exactly like the C++ bound.
    payload["message"] = "é" * (protocol.MAX_REQUEST_MESSAGE_BYTES // 2 + 1)
    with pytest.raises(protocol.ProtocolError):
        parse(payload)


def test_wrong_token_rejected_without_leaking_expected_value() -> None:
    payload = valid_request_dict()
    payload["token"] = "z" * 32
    with pytest.raises(protocol.TokenMismatchError) as excinfo:
        parse(payload)

    assert TEST_TOKEN not in str(excinfo.value)
    assert TEST_TOKEN not in repr(excinfo.value)


# --- Response encoding ---


def test_response_payload_matches_cpp_accepted_shape() -> None:
    payload = protocol.encode_response(7, "I enjoy fishing.", TEST_TOKEN)
    # Byte-for-byte the shape the C++ ValidResponseRoundTrips fixture accepts.
    assert payload == (
        b'{"schema_version":3,"token":"0123456789abcdef0123456789abcdef",'
        b'"request_id":7,"message":"I enjoy fishing."}'
    )


# --- Claude adapter (mocked HTTP transport; no real requests) ---


def make_mock_client(handler) -> anthropic.Anthropic:
    """Anthropic client whose HTTP layer is a local httpx.MockTransport."""

    return anthropic.Anthropic(
        api_key="test-key-never-used-for-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def make_request_model(**overrides: object) -> protocol.ChatRequest:
    data = valid_request_dict()
    data.update(overrides)
    return protocol.parse_request(json.dumps(data).encode(), TEST_TOKEN)


def messages_response(message_text: str, usage: dict[str, int] | None = None) -> httpx.Response:
    body = {
        "id": "msg_test_01",
        "type": "message",
        "role": "assistant",
        "model": claude.MODEL_ID,
        "content": [{"type": "text", "text": json.dumps({"message": message_text})}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 2500,
            "output_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return httpx.Response(200, json=body)


def career_messages_response(candidate_token: str, spending_style: str) -> httpx.Response:
    body = {
        "id": "msg_career_01",
        "type": "message",
        "role": "assistant",
        "model": claude.MODEL_ID,
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "candidate_token": candidate_token,
                        "spending_style": spending_style,
                    }
                ),
            }
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 400,
            "output_tokens": 24,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return httpx.Response(200, json=body)


def test_sdk_pin_matches_contract() -> None:
    # The offline contract tests below are written against this exact SDK version.
    assert anthropic.__version__ == "0.120.2"


def test_generate_reply_conditions_on_trusted_personality() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return messages_response("I do enjoy a good fishing spot.")

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    reply, usage = adapter.generate_reply(make_request_model(), history=[])

    assert reply == "I do enjoy a good fishing spot."
    assert usage.input_tokens == 2500
    assert usage.output_tokens == 80
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0

    body = captured["body"]
    assert captured["path"] == "/v1/messages"
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["max_tokens"] == claude.MAX_OUTPUT_TOKENS == 96
    assert "tools" not in body  # the model never receives tools

    # Trusted personality lives in the system prompt.
    system_text = body["system"]
    assert "65" in system_text and "91" in system_text and "82" in system_text
    assert "earnest" in system_text
    assert "Botname" in system_text

    # Player text stays a separate, explicitly untrusted user message.
    user_message = body["messages"][-1]
    assert user_message["role"] == "user"
    assert "What do you enjoy doing?" in user_message["content"]
    assert "What do you enjoy doing?" not in system_text

    # The bridge token never reaches Anthropic.
    assert TEST_TOKEN not in json.dumps(body)


def test_generate_reply_includes_bounded_history() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return messages_response("Aye, as I said before.")

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    history = [("user", "Hello there"), ("assistant", "Well met, Speaker.")]
    adapter.generate_reply(make_request_model(), history=history)

    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_ambient_provider_payload_uses_only_bot_personality() -> None:
    captured: dict[str, Any] = {}
    private_marker = "PRIVATE-WHISPER-MARKER-7E31"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return messages_response("The road has its own kind of rhythm.")

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    ambient = protocol.parse_request(json.dumps(ambient_request_dict()).encode(), TEST_TOKEN)
    adapter.generate_reply(ambient, history=[("user", private_marker), ("assistant", "Private reply")])

    body = captured["body"]
    serialized = json.dumps(body)
    assert "Botname" in serialized
    assert "65" in serialized and "91" in serialized and "82" in serialized
    assert "earnest" in serialized
    assert private_marker not in serialized
    assert "Speaker" not in serialized
    assert "9001" not in serialized
    assert "ambient_world" not in serialized
    assert "tools" not in body
    assert len(body["messages"]) == 1
    assert "observation" in body["messages"][0]["content"]
    assert "current game facts" in body["messages"][0]["content"]


def test_career_provider_selects_only_bounded_opaque_candidate_without_history() -> None:
    captured: dict[str, Any] = {}
    private_marker = "PRIVATE-WHISPER-MARKER-7E31"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return career_messages_response("career-def456", "progression")

    career = parse(career_request_dict())
    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    reply, usage = adapter.generate_reply(
        career,
        history=[("user", private_marker), ("assistant", "Private reply")],
    )

    assert json.loads(reply) == {
        "candidate_token": "career-def456",
        "spending_style": "progression",
    }
    assert usage.input_tokens == 400
    serialized = json.dumps(captured["body"])
    assert private_marker not in serialized
    assert "career-abc123" in serialized
    assert "career-def456" in serialized
    assert '"skill_id"' not in serialized
    assert len(captured["body"]["messages"]) == 1


@pytest.mark.parametrize(
    ("candidate_token", "spending_style"),
    [
        ("career-unknown", "minimal"),
        ("career-abc123", "completionist"),
    ],
)
def test_career_provider_rejects_unknown_candidate_or_excess_spending(
    candidate_token: str,
    spending_style: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return career_messages_response(candidate_token, spending_style)

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    with pytest.raises(claude.ClaudeInvalidOutputError):
        adapter.generate_reply(parse(career_request_dict()), history=[])


def test_count_input_tokens_uses_count_tokens_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages/count_tokens"
        return httpx.Response(200, json={"input_tokens": 1234})

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    assert adapter.count_input_tokens(make_request_model(), history=[]) == 1234


def test_count_and_generate_bill_the_same_structured_output_schema() -> None:
    # The budget reservation is priced from the counted prompt, so counting must
    # bill the exact request shape generation sends, including the structured
    # output schema. A mismatch lets actual input usage exceed the reservation
    # and settlement could then cross the configured hard ceiling.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/messages/count_tokens":
            captured["count"] = body
            return httpx.Response(200, json={"input_tokens": 2500})
        captured["generate"] = body
        return messages_response("A reply.")

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    adapter.count_input_tokens(make_request_model(), history=[])
    adapter.generate_reply(make_request_model(), history=[])

    assert captured["generate"].get("output_config") is not None
    assert captured["count"].get("output_config") == captured["generate"]["output_config"]


def test_generate_reply_maps_provider_failures_to_bounded_errors() -> None:
    def auth_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
        )

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
        )

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    adapter = claude.ClaudeAdapter(client=make_mock_client(auth_error))
    with pytest.raises(claude.ClaudeAuthError):
        adapter.generate_reply(make_request_model(), history=[])

    adapter = claude.ClaudeAdapter(client=make_mock_client(rate_limited))
    with pytest.raises(claude.ClaudeRateLimitError):
        adapter.generate_reply(make_request_model(), history=[])

    adapter = claude.ClaudeAdapter(client=make_mock_client(timeout))
    with pytest.raises(claude.ClaudeTimeoutError):
        adapter.generate_reply(make_request_model(), history=[])


def test_generate_reply_rejects_malformed_or_oversized_output() -> None:
    def not_schema(request: httpx.Request) -> httpx.Response:
        body = {
            "id": "msg_test_02",
            "type": "message",
            "role": "assistant",
            "model": claude.MODEL_ID,
            "content": [{"type": "text", "text": "not json at all"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        return httpx.Response(200, json=body)

    adapter = claude.ClaudeAdapter(client=make_mock_client(not_schema))
    with pytest.raises(claude.ClaudeInvalidOutputError):
        adapter.generate_reply(make_request_model(), history=[])

    def oversized(request: httpx.Request) -> httpx.Response:
        return messages_response("a" * (protocol.MAX_RESPONSE_MESSAGE_BYTES + 1))

    adapter = claude.ClaudeAdapter(client=make_mock_client(oversized))
    with pytest.raises(claude.ClaudeInvalidOutputError):
        adapter.generate_reply(make_request_model(), history=[])

    def multiline(request: httpx.Request) -> httpx.Response:
        return messages_response("two\nlines")

    adapter = claude.ClaudeAdapter(client=make_mock_client(multiline))
    with pytest.raises(claude.ClaudeInvalidOutputError):
        adapter.generate_reply(make_request_model(), history=[])


def test_adapter_ignores_global_anthropic_api_key(monkeypatch) -> None:
    # The default client must only ever read MOD_PLAYERBOT_CLAUDE_APIKEY; a
    # machine-wide ANTHROPIC_API_KEY must never be picked up implicitly.
    monkeypatch.delenv("MOD_PLAYERBOT_CLAUDE_APIKEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-global-key")
    assert claude.ClaudeAdapter()._client.api_key == ""

    monkeypatch.setenv("MOD_PLAYERBOT_CLAUDE_APIKEY", "sk-module-key")
    assert claude.ClaudeAdapter()._client.api_key == "sk-module-key"


# --- App: configuration, doctor, and request processing ---


CONF_TEXT = """[worldserver]

PlayerbotClaude.Enable = 1
PlayerbotClaude.BridgePort = 40123
PlayerbotClaude.AmbientWorldEnable = 1
PlayerbotClaude.AmbientMaxMessagesPerHour = 6
PlayerbotClaude.DailyBudgetUsd = 5.0
PlayerbotClaude.ResponseDeadlineMs = 10000
PlayerbotClaude.QueueSize = 16
PlayerbotClaude.GroupCooldownSeconds = 120
"""


def write_conf(tmp_path, text: str = CONF_TEXT) -> str:
    conf = tmp_path / "mod_playerbot_claude.conf"
    conf.write_text(text)
    return str(conf)


def test_config_parses_worldserver_conf(tmp_path) -> None:
    config = app.SidecarConfig.load(write_conf(tmp_path))
    assert config.enable is True
    assert config.bridge_port == 40123
    assert config.ambient_world_enable is True
    assert config.ambient_max_messages_per_hour == 6
    assert config.budget_nano == budget.usd_to_nano("5")


def test_config_strips_surrounding_quotes_like_worldserver(tmp_path) -> None:
    # AzerothCore .conf convention quotes string values (the shipped .dist does);
    # worldserver's ConfigMgr strips them, so the sidecar must too. A quoted ceiling
    # that keeps its quotes parses as no budget at all, which silences every bot.
    conf = write_conf(tmp_path, '[worldserver]\nPlayerbotClaude.DailyBudgetUsd = "2.50"\n')
    config = app.SidecarConfig.load(conf)
    assert config.daily_budget_usd == "2.50"
    assert config.budget_nano == budget.usd_to_nano("2.50")


def test_an_unrecordable_ceiling_disables_generation_rather_than_being_clamped(tmp_path) -> None:
    """An unenforceable budget fails closed and says nothing was configured.

    budget_nano returns 0 for any ceiling it cannot validate, and every caller already
    treats 0 as "no budget", so a ceiling the ledger cannot record silences the bots
    instead of quietly becoming a different number.
    """
    huge = budget.nano_to_usd_string(budget.MAX_STORABLE_NANO + budget.NANO)
    config = app.SidecarConfig.load(
        write_conf(tmp_path, f"[worldserver]\nPlayerbotClaude.DailyBudgetUsd = {huge}\n")
    )
    assert config.budget_nano == 0
    assert config.generation_allowed is False


def test_config_defaults_fail_closed(tmp_path) -> None:
    config = app.SidecarConfig.load(write_conf(tmp_path, "[worldserver]\n"))
    assert config.enable is False
    assert config.bridge_port == 0
    assert config.budget_nano == 0


def test_config_replaces_lifetime_budget_and_has_no_ceiling_above_the_configured_one(tmp_path) -> None:
    """The old 5.00 cap is gone: the configured value is the sole ceiling.

    A second limit in the code silently ignores what the operator asked for, which is
    why a large configured budget must now be honoured rather than clamped.
    """
    old_only = app.SidecarConfig.load(write_conf(tmp_path, "[worldserver]\nPlayerbotClaude.BudgetUsd = 1\n"))
    assert old_only.budget_nano == 0
    assert old_only.generation_allowed is False

    for value, allowed in ((0, False), (0.5, True), (5, True), (-1, False), (5.01, True), (500, True)):
        config = app.SidecarConfig.load(
            write_conf(tmp_path, f"[worldserver]\nPlayerbotClaude.DailyBudgetUsd = {value}\n")
        )
        assert config.generation_allowed is allowed, value

    # And the ceiling is carried exactly rather than through a float.
    large = app.SidecarConfig.load(
        write_conf(tmp_path, "[worldserver]\nPlayerbotClaude.DailyBudgetUsd = 500.10\n")
    )
    assert large.budget_nano == budget.usd_to_nano("500.10")


def test_config_reserve_ratio_defaults_to_a_quarter_and_fails_closed(tmp_path) -> None:
    """An unusable ratio protects everything.

    Failing closed here means a typo silences background work rather than quietly
    removing the protection it was meant to configure.
    """
    default = app.SidecarConfig.load(
        write_conf(tmp_path, "[worldserver]\nPlayerbotClaude.DailyBudgetUsd = 5\n")
    )
    assert default.reserve_ratio == Decimal("0.25")

    for value, expected in (("0", Decimal(0)), ("1", Decimal(1)), ("0.5", Decimal("0.5"))):
        config = app.SidecarConfig.load(
            write_conf(
                tmp_path,
                "[worldserver]\nPlayerbotClaude.DailyBudgetUsd = 5\n"
                f"PlayerbotClaude.HumanBudgetReserveRatio = {value}\n",
            )
        )
        assert config.reserve_ratio == expected

    for bad in ("-0.1", "1.1", "banana"):
        config = app.SidecarConfig.load(
            write_conf(
                tmp_path,
                "[worldserver]\nPlayerbotClaude.DailyBudgetUsd = 5\n"
                f"PlayerbotClaude.HumanBudgetReserveRatio = {bad}\n",
            )
        )
        assert config.reserve_ratio == Decimal(1), bad


def test_config_bounds_ambient_rate_without_disabling_direct_chat(tmp_path) -> None:
    for rate, allowed in ((0, False), (1, True), (6, True), (7, False)):
        config = app.SidecarConfig.load(
            write_conf(
                tmp_path,
                "[worldserver]\n"
                "PlayerbotClaude.DailyBudgetUsd = 5\n"
                "PlayerbotClaude.AmbientWorldEnable = 1\n"
                f"PlayerbotClaude.AmbientMaxMessagesPerHour = {rate}\n",
            )
        )
        assert config.ambient_allowed is allowed
        assert config.generation_allowed is True


def test_doctor_reports_status_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("MOD_PLAYERBOT_CLAUDE_APIKEY", "sk-ant-super-secret")
    report = app.doctor_report(app.SidecarConfig.load(write_conf(tmp_path)))
    serialized = json.dumps(report)
    assert TEST_TOKEN not in serialized
    assert "sk-ant-super-secret" not in serialized
    assert report["bridge_token_present"] is True
    assert report["anthropic_api_key_present"] is True
    assert report["bridge_port"] == 40123


def test_doctor_ignores_global_anthropic_api_key(tmp_path, monkeypatch) -> None:
    # The module never uses a machine-wide key: only MOD_PLAYERBOT_CLAUDE_APIKEY counts.
    monkeypatch.delenv("MOD_PLAYERBOT_CLAUDE_APIKEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-global-key")
    report = app.doctor_report(app.SidecarConfig.load(write_conf(tmp_path)))
    assert report["anthropic_api_key_present"] is False


def test_doctor_flags_missing_token(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", raising=False)
    report = app.doctor_report(app.SidecarConfig.load(write_conf(tmp_path)))
    assert report["bridge_token_present"] is False
    assert report["ok"] is False


def test_no_module_in_the_package_can_open_sqlite() -> None:
    """Definition of Done 6, asserted where it cannot quietly come back.

    A test that only checks "no file appeared under tmp_path" passes for a sidecar that
    writes its SQLite file somewhere else, and passes for one that opens a database and
    deletes it. What Definition of Done 6 actually claims is that the dependency is gone,
    so the assertion is over the shipped source rather than over one run's side effects.
    """
    package = Path(app.__file__).parent
    # Importing it or calling into it, not merely naming it: a docstring explaining that
    # the store used to be SQLite is documentation, and banning the word would push a
    # future reader toward deleting the explanation rather than the dependency.
    uses_sqlite = re.compile(r"^\s*(import|from)\s+sqlite3\b|\bsqlite3\s*\.", re.MULTILINE)
    users = sorted(
        path.name for path in package.glob("*.py") if uses_sqlite.search(path.read_text(encoding="utf-8"))
    )
    assert users == []
    assert not (package / "storage.py").exists()


def test_the_configuration_no_longer_carries_a_sqlite_path(tmp_path) -> None:
    """A leftover SidecarDatabase in a deployed config is inert, not a second store.

    Operators upgrade in place, so the setting will still be sitting in the file the
    sidecar reads. It has to be ignored rather than honoured: honouring it is how a
    server ends up with budget in MySQL and budget in a file nobody is looking at.
    """
    config = app.SidecarConfig.load(
        write_conf(tmp_path, CONF_TEXT + 'PlayerbotClaude.SidecarDatabase = "leftover.sqlite"\n')
    )
    assert not hasattr(config, "database_path")


class FakeAdapter(claude.ClaudeAdapter):
    def __init__(self, reply: str = "A fine day for fishing.") -> None:
        # Deliberately no super().__init__(): the fake never builds a real client.
        self.reply = reply
        self.requests: list[protocol.ChatRequest] = []
        self.histories: list[list[tuple[str, str]]] = []
        self.input_tokens = 100
        # Set by the state double when generation happens, so a test can assert the
        # money was reserved BEFORE the provider was reached and not merely that both
        # eventually occurred.
        self.generated_at_call_index: int | None = None
        self.state: FakeState | None = None

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int:
        return self.input_tokens

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, claude.UsageTotals]:
        self.requests.append(request)
        self.histories.append(list(history))
        if self.state is not None:
            self.generated_at_call_index = len(self.state.calls)
        return self.reply, claude.UsageTotals(input_tokens=self.input_tokens, output_tokens=10)


async def test_the_service_answers_a_valid_request(tmp_path) -> None:
    service, _, _ = make_stored_service(tmp_path)
    payload = await service.process_payload(CPP_REQUEST_FIXTURE.encode())
    assert payload is not None

    response = json.loads(payload)
    assert response["schema_version"] == 3
    assert response["request_id"] == 7
    assert response["message"] == "A fine day for fishing."
    assert response["token"] == TEST_TOKEN


async def test_the_service_refuses_a_bad_token_before_touching_anything(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path)
    bad = valid_request_dict()
    bad["token"] = "z" * 32

    with pytest.raises(protocol.TokenMismatchError):
        await service.process_payload(json.dumps(bad).encode())

    assert adapter.requests == []
    assert store.calls == []


async def test_the_service_makes_no_generation_call_without_a_budget(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path, daily_budget="0")
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert adapter.requests == []
    assert store.calls == []


class RaisingAdapter(FakeAdapter):
    """Fails generation with one supplied error, after the reservation exists."""

    def __init__(self, error: claude.ClaudeError) -> None:
        super().__init__()
        self.error = error

    def generate_reply(self, request, history):
        raise self.error


def make_stored_service(
    tmp_path,
    adapter=None,
    daily_budget: str = "5.0",
    state_double: FakeState | None = None,
) -> tuple[app.SidecarService, FakeState, FakeAdapter]:
    config = app.SidecarConfig.load(write_conf(tmp_path, CONF_TEXT.replace("5.0", daily_budget)))
    fake_adapter = adapter or FakeAdapter()
    store = state_double or FakeState(config.budget_nano, config.reserve_ratio)
    fake_adapter.state = store
    service = app.SidecarService(
        config=config,
        token=TEST_TOKEN,
        adapter=fake_adapter,
        store=store,
        now=lambda: FIXED_NOW,
    )
    return service, store, fake_adapter


async def test_the_service_records_profile_memory_and_settlement(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path)

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is not None

    assert store.profiles[42]["bot_name"] == "Botname"
    assert store.profiles[42]["crafting_affinity"] == 65
    assert store.profiles[42]["gathering_affinity"] == 37
    assert store.turns[42] == [
        ("user", claude.build_user_message(adapter.requests[0])),
        ("assistant", "A fine day for fishing."),
    ]

    # 100 input at $1/Mtok plus 10 output at $5/Mtok.
    expected = budget.usd_to_nano("0.0001") + budget.usd_to_nano("0.00005")
    assert store.settled_nano == expected
    assert store.outstanding == {}


async def test_the_money_is_reserved_before_the_provider_is_ever_called(tmp_path) -> None:
    """The ordering is the whole guarantee, so it is asserted as an ordering.

    A test that only checked "a reservation exists afterwards" passes for a service that
    generates first and reserves second, which is a service whose ceiling can be crossed
    by every request in flight.
    """
    service, store, adapter = make_stored_service(tmp_path)

    await service.process_payload(CPP_REQUEST_FIXTURE.encode())

    assert store.calls.index("reserve") < store.calls.index("settle")
    assert adapter.generated_at_call_index == store.calls.index("reserve") + 1


async def test_a_denied_request_never_reaches_the_provider(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path)
    store.settled_nano = store.ceiling_nano

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert adapter.requests == []
    assert store.reservations == []


async def test_an_open_circuit_stops_the_service_dead(tmp_path) -> None:
    """Definition of Done 7 as the service sees it: no admission, no call, no reply."""
    service, store, adapter = make_stored_service(tmp_path)
    store.circuit_open = True

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert adapter.requests == []
    assert store.reservations == []


async def test_ambient_chatter_and_career_choices_draw_on_the_background_lane(tmp_path) -> None:
    """Definition of Done 2, at the point where a request is classified.

    Nobody is waiting on ambient World chatter or on a career decision, so neither may
    eat the slice held for a player who actually speaks. Getting this classification
    wrong is how a reserve that exists in the policy stops existing in practice.
    """
    assert app._priority_for(parse(valid_request_dict())) is budget.RequestPriority.IMMEDIATE_HUMAN
    assert app._priority_for(parse(ambient_request_dict())) is budget.RequestPriority.BACKGROUND
    assert app._priority_for(parse(career_request_dict())) is budget.RequestPriority.BACKGROUND


async def test_ambient_requests_never_read_or_append_conversation_history(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path)
    store.turns[42] = [("user", "earlier"), ("assistant", "reply")]

    assert await service.process_payload(json.dumps(ambient_request_dict()).encode()) is not None

    assert adapter.histories == [[]]
    assert store.turns[42] == [("user", "earlier"), ("assistant", "reply")]


async def test_a_failed_ambient_attempt_still_consumes_its_rate_slot(tmp_path) -> None:
    """The rate limit counts attempts, not successes.

    Counting only successes lets a broken provider be retried without limit, which is
    both a spend loop and a way to keep the World channel busy with nothing.
    """

    class FailingAdapter(FakeAdapter):
        def count_input_tokens(self, request, history):
            raise claude.ClaudeProviderError("provider is down")

    service, store, _ = make_stored_service(tmp_path, adapter=FailingAdapter())

    assert await service.process_payload(json.dumps(ambient_request_dict()).encode()) is None
    assert store.ambient_taken == 1
    assert store.calls[0] == "try_begin_ambient"


async def test_an_exhausted_ambient_rate_makes_no_provider_call_at_all(tmp_path) -> None:
    service, store, adapter = make_stored_service(tmp_path)
    store.ambient_taken = store.ambient_allowance

    assert await service.process_payload(json.dumps(ambient_request_dict()).encode()) is None
    assert adapter.requests == []
    assert store.reservations == []


async def test_an_oversized_prompt_is_refused_before_any_money_moves(tmp_path) -> None:
    adapter = FakeAdapter()
    adapter.input_tokens = claude.MAX_INPUT_TOKENS + 1
    service, store, _ = make_stored_service(tmp_path, adapter=adapter)

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert store.reservations == []
    assert adapter.requests == []


async def test_career_decisions_stay_out_of_chat_history(tmp_path) -> None:
    adapter = FakeAdapter(reply='{"candidate_token":"career-def456","spending_style":"progression"}')
    service, store, _ = make_stored_service(tmp_path, adapter=adapter)

    assert await service.process_payload(json.dumps(career_request_dict()).encode()) is not None

    assert adapter.histories == [[]]
    assert store.turns.get(42, []) == []
    assert store.careers[42] == {
        "career_version": 1,
        "candidate_token": "career-def456",
        "spending_style": "progression",
    }


async def test_a_service_without_durable_state_stays_silent(tmp_path) -> None:
    """No state, no reply. There is no degraded mode that spends money off the books."""
    adapter = FakeAdapter()
    service = app.SidecarService(
        config=app.SidecarConfig.load(write_conf(tmp_path)), token=TEST_TOKEN, adapter=adapter
    )

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert adapter.requests == []


def test_the_actual_cost_counts_cache_tokens_at_the_input_rate(tmp_path) -> None:
    """Nothing sends a cache_control block today, so both counts are always zero.

    Charging them at the plain input rate means that if prompt caching is ever enabled,
    the ledger over-counts cache reads rather than under-counting cache writes.
    Over-counting is the safe direction under a ceiling.
    """
    plain = claude.UsageTotals(input_tokens=1000, output_tokens=100)
    cached = claude.UsageTotals(
        input_tokens=600, output_tokens=100, cache_creation_input_tokens=300, cache_read_input_tokens=100
    )
    assert app._actual_cost_nano(plain, ("1.00", "5.00")) == app._actual_cost_nano(cached, ("1.00", "5.00"))


def test_the_doctor_reports_budget_numbers_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("MOD_PLAYERBOT_CLAUDE_APIKEY", "sk-ant-super-secret")
    config = app.SidecarConfig.load(write_conf(tmp_path))

    report = app.doctor_report(
        config,
        budget_state=budget.BudgetState(
            settled_nano=budget.usd_to_nano("1.25"), outstanding_nano=budget.usd_to_nano("0.25")
        ),
    )

    assert report["budget"] == {
        "settled_usd": "1.25",
        "outstanding_usd": "0.25",
        "remaining_usd": "3.5",
        "human_reserve_usd": "1.25",
        "circuit_open": False,
    }
    serialized = json.dumps(report)
    assert TEST_TOKEN not in serialized
    assert "sk-ant-super-secret" not in serialized


def test_the_doctor_is_not_ok_while_the_circuit_is_open(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("MOD_PLAYERBOT_CLAUDE_APIKEY", "sk-ant-super-secret")
    report = app.doctor_report(
        app.SidecarConfig.load(write_conf(tmp_path)),
        budget_state=budget.BudgetState(circuit_open=True),
    )
    assert report["ok"] is False
    reported = report["budget"]
    assert isinstance(reported, dict)
    assert reported["circuit_open"] is True


# --- Failure accounting: who gets their money back and who does not ---


async def test_a_refusal_gives_the_reservation_back(tmp_path) -> None:
    """A 401 or a 429 was rejected before generation, so the money was never spent.

    Holding its maximum for the full expiry window would deny a later request money the
    budget demonstrably still has.
    """
    for error in (claude.ClaudeAuthError("401"), claude.ClaudeRateLimitError("429")):
        service, store, _ = make_stored_service(tmp_path, adapter=RaisingAdapter(error))
        assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
        assert len(store.released) == 1, f"{type(error).__name__} should release"
        assert store.settled_nano == 0
        assert store.outstanding == {}


async def test_invalid_output_settles_at_the_cost_the_provider_reported(tmp_path) -> None:
    """The completion happened and was billed. Its exact cost is known, so charge that.

    Releasing would spend money the ledger never records. Charging the reservation's
    maximum would overcharge the realm permanently for a reply that was merely unusable.
    Neither is necessary: the usage travels with the error.
    """
    usage = claude.UsageTotals(input_tokens=100, output_tokens=10)
    error = claude.ClaudeInvalidOutputError("model message must be a single line", usage)
    service, store, _ = make_stored_service(tmp_path, adapter=RaisingAdapter(error))

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None

    assert store.released == []
    assert store.settled_nano == budget.usd_to_nano("0.00015")
    assert store.settled_nano < store.reservations[0].max_cost_nano, "the maximum is not the cost"
    assert store.outstanding == {}
    # Settling below the reservation must not trip the breaker: it exists for a provider
    # reporting MORE than was authorised.
    assert store.circuit_open is False


async def test_an_undeterminable_failure_is_left_for_expiry_rather_than_guessed_at(tmp_path) -> None:
    """A timeout or provider error carries no usage, so nothing can be concluded.

    The reservation is left alone: the ledger's expiry holds it at maximum while the
    request might still matter, then reclaims it. Settling at the maximum would
    permanently overcharge the realm for one dropped connection, and releasing would spend
    money the ledger never recorded.
    """
    undeterminable = (
        claude.ClaudeTimeoutError("timed out"),
        claude.ClaudeProviderError("provider error: InternalServerError"),
        # An output too malformed to parse at all: no completion object, so no usage.
        claude.ClaudeInvalidOutputError("model output did not match the reply schema"),
    )

    for error in undeterminable:
        service, store, _ = make_stored_service(tmp_path, adapter=RaisingAdapter(error))
        assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
        name = type(error).__name__
        assert store.released == [], f"{name} must not release"
        assert store.settlements == [], f"{name} must not settle a cost nobody knows"
        # Still held at its maximum. Expiry, not this code path, decides its fate.
        assert list(store.outstanding.values()) == [store.reservations[0].max_cost_nano]
        assert store.circuit_open is False


async def test_impossible_token_counts_never_become_a_charge(tmp_path) -> None:
    """The SDK's own Usage model accepts negative token counts.

    So a broken or hostile provider response can carry them, and this is the boundary
    where provider data becomes ledger data. Two layers keep it out: the adapter refuses
    to attach unpriceable counts to the error, and the failure path catches an unpriceable
    cost rather than letting it escape into a connection handler that only understands
    protocol errors. Either way the reservation waits for expiry.
    """
    impossible = (
        claude.UsageTotals(input_tokens=-1, output_tokens=10),
        claude.UsageTotals(input_tokens=100, output_tokens=-10),
        claude.UsageTotals(input_tokens=100, output_tokens=10, cache_creation_input_tokens=-1),
        claude.UsageTotals(input_tokens=100, output_tokens=10, cache_read_input_tokens=-1),
    )

    for usage in impossible:
        assert usage.is_priceable is False
        error = claude.ClaudeInvalidOutputError("rejected content", usage)
        service, store, _ = make_stored_service(tmp_path, adapter=RaisingAdapter(error))

        # No exception escapes, and the request is simply silent.
        assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
        assert store.settlements == []
        assert store.released == []
        assert list(store.outstanding.values()) == [store.reservations[0].max_cost_nano]


def test_the_adapter_refuses_to_report_impossible_token_counts() -> None:
    """The first of the two layers, at the point the provider is believed.

    A response whose usage cannot be priced is rejected with NO usage attached, which puts
    it in the same lane as a timeout rather than producing a charge nobody can verify.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        response = messages_response("A fine day for fishing.")
        body = json.loads(response.content)
        body["usage"]["input_tokens"] = -5
        return httpx.Response(200, json=body)

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    with pytest.raises(claude.ClaudeInvalidOutputError) as caught:
        adapter.generate_reply(make_request_model(), history=[])

    assert caught.value.usage is None
    assert "impossible token counts" in str(caught.value)


def test_a_rejected_completion_carries_the_usage_that_was_billed(tmp_path) -> None:
    """The adapter must read usage BEFORE it validates content.

    Every content check rejects a completion that was already generated and charged.
    Discovering the tokens after deciding to raise is how that charge goes missing, and
    the settle path above depends on it being there.
    """
    long_line = "x" * 300

    def handler(request: httpx.Request) -> httpx.Response:
        return messages_response(long_line)

    adapter = claude.ClaudeAdapter(client=make_mock_client(handler))
    with pytest.raises(claude.ClaudeInvalidOutputError) as caught:
        adapter.generate_reply(make_request_model(), history=[])

    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 2500
    assert caught.value.usage.output_tokens == 80


def test_the_billing_classification_covers_every_adapter_error(tmp_path) -> None:
    """A new error type defaults to billable, which is the safe direction.

    Enumerated rather than spot-checked, so adding a subclass without deciding its
    billing status cannot silently start releasing reservations.
    """

    def descendants(cls) -> set[str]:
        # Recursive: a subclass of a subclass is still an error this code must classify,
        # and a direct-children-only check would not see one.
        found = set()
        for child in cls.__subclasses__():
            found.add(child.__name__)
            found |= descendants(child)
        return found

    subclasses = descendants(claude.ClaudeError)
    assert subclasses == {
        "ClaudeTimeoutError",
        "ClaudeAuthError",
        "ClaudeRateLimitError",
        "ClaudeProviderError",
        "ClaudeInvalidOutputError",
    }
    assert claude.billing_is_impossible(claude.ClaudeError("unclassified")) is False


async def test_an_unpriceable_completion_stays_silent_rather_than_settling_free(tmp_path) -> None:
    """Settling a real completion at zero is the one outcome a ceiling cannot survive.

    The reservation is left outstanding at its maximum for the ledger's expiry to
    reclaim, and the failure is bounded rather than escaping into the connection handler,
    which only understands protocol and connection errors.
    """

    class NegativeUsageAdapter(FakeAdapter):
        def generate_reply(self, request, history):
            self.requests.append(request)
            return self.reply, claude.UsageTotals(input_tokens=-1, output_tokens=10)

    service, store, _ = make_stored_service(tmp_path, adapter=NegativeUsageAdapter())

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert store.settlements == []
    assert store.released == []
    # Still held at maximum: expiry reclaims it, nothing records it as free.
    assert list(store.outstanding.values()) == [store.reservations[0].max_cost_nano]


# --- The response deadline, which bounds the whole pipeline ---


def test_the_default_adapter_client_timeout_is_capped_at_the_deadline(tmp_path, monkeypatch) -> None:
    """A provider call must not outlive the request that paid for it."""
    captured: dict[str, object] = {}

    class RecordingAdapter(claude.ClaudeAdapter):
        def __init__(self, client=None, timeout_seconds: float = claude.REQUEST_TIMEOUT_SECONDS) -> None:
            captured["timeout_seconds"] = timeout_seconds

    monkeypatch.setattr(claude, "ClaudeAdapter", RecordingAdapter)
    config = app.SidecarConfig.load(write_conf(tmp_path))
    app.SidecarService(config=config, token=TEST_TOKEN, store=FakeState(config.budget_nano))

    assert captured["timeout_seconds"] == config.response_deadline_ms / 1000


async def test_the_response_deadline_stops_a_slow_request_without_charging_it_free(tmp_path) -> None:
    """Definition of Done: an expired request stays silent and writes no memory.

    The worldserver has already given up by then, so finishing late would only spend on a
    reply nobody can receive. The reservation deliberately stays outstanding at its
    maximum for expiry to reclaim: a request cancelled mid-generation may well have been
    billed.
    """
    started = asyncio.Event()

    class SlowAdapter(FakeAdapter):
        def generate_reply(self, request, history):
            started.set()
            time.sleep(0.5)
            return self.reply, claude.UsageTotals(input_tokens=100, output_tokens=10)

    config_text = CONF_TEXT.replace(
        "PlayerbotClaude.ResponseDeadlineMs = 10000", "PlayerbotClaude.ResponseDeadlineMs = 50"
    )
    config = app.SidecarConfig.load(write_conf(tmp_path, config_text))
    store = FakeState(config.budget_nano, config.reserve_ratio)
    service = app.SidecarService(
        config=config, token=TEST_TOKEN, adapter=SlowAdapter(), store=store, now=lambda: FIXED_NOW
    )

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None

    assert started.is_set(), "the deadline must cut a call that actually started"
    assert store.settlements == []
    assert store.turns.get(42, []) == []
    assert list(store.outstanding.values()) == [store.reservations[0].max_cost_nano]


def test_nano_amounts_render_as_exact_decimals() -> None:
    # 2500 input at $1/Mtok plus 80 output at $5/Mtok = 0.0029 exactly.
    cost = budget.token_cost_nano(2500, 80, "1.00", "5.00")
    assert cost is not None
    assert budget.nano_to_usd_string(cost) == "0.0029"
    assert budget.nano_to_usd_string(0) == "0"
    assert budget.nano_to_usd_string(-2_500_000_000) == "-2.5"


def test_response_rejects_invalid_messages() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(7, "", TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(7, "a" * (protocol.MAX_RESPONSE_MESSAGE_BYTES + 1), TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(7, "two\nlines", TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(7, "control\x01char", TEST_TOKEN)

    # 240 bytes exactly is accepted.
    protocol.encode_response(7, "a" * protocol.MAX_RESPONSE_MESSAGE_BYTES, TEST_TOKEN)


# Typed social protocol ------------------------------------------------------------------


def _social_request_payload(**overrides: object) -> bytes:
    payload = {
        "schema_version": 3,
        "token": TEST_TOKEN,
        "kind": "social",
        "social_request_token": 77,
        "bot_guid": 500,
        "bot_name": "Grimbold",
        "bot_human": 0,
        "subject_guid": 900,
        "subject_name": "Deszy",
        "subject_human": 1,
        "speak_on_channel": 2,
        "thread_id": "thr_00000000000000000000000000000001",
        "context": "party pull",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_social_request_round_trips() -> None:
    request = protocol.parse_social_request(_social_request_payload(), TEST_TOKEN)

    assert request.social_request_token == 77
    assert request.bot_guid == 500
    assert request.bot_human == 0
    assert request.subject_human == 1


def test_social_request_rejects_unknown_fields_and_bad_kind() -> None:
    # The C++ side declares the kind rather than relying on shape, so a career answer cannot
    # be read as a social one. The same holds in reverse here.
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(kind="career"), TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(extra=1), TEST_TOKEN)


def test_social_request_rejects_an_old_schema() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(schema_version=2), TEST_TOKEN)


def test_social_request_bounds_the_context_in_bytes() -> None:
    # Bounded in bytes rather than characters: a multibyte context would otherwise pass a
    # character check and overflow the frame budget.
    multibyte = "\u00e9" * (protocol.MAX_SOCIAL_CONTEXT_BYTES // 2 + 1)
    assert len(multibyte) <= protocol.MAX_SOCIAL_CONTEXT_BYTES

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(context=multibyte), TEST_TOKEN)


def test_social_request_rejects_a_mismatched_token() -> None:
    with pytest.raises(protocol.TokenMismatchError):
        protocol.parse_social_request(_social_request_payload(), "z" * 40)


def test_social_response_encodes_the_shape_the_worldserver_accepts() -> None:
    encoded = json.loads(protocol.encode_social_response(77, 500, 2, "Aye.", TEST_TOKEN))

    assert encoded == {
        "schema_version": 3,
        "token": TEST_TOKEN,
        "kind": "social",
        "social_request_token": 77,
        "bot_guid": 500,
        "speak_on_channel": 2,
        "message": "Aye.",
        "regenerate": 0,
    }


def test_a_regeneration_carries_no_message() -> None:
    encoded = json.loads(protocol.encode_social_response(77, 500, 2, "ignored", TEST_TOKEN, regenerate=True))

    assert encoded["regenerate"] == 1
    assert encoded["message"] == ""


def test_a_deliverable_social_line_is_still_held_to_the_response_rules() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_social_response(77, 500, 2, "", TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_social_response(77, 500, 2, "one\ntwo", TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_social_response(
            77, 500, 2, "a" * (protocol.MAX_RESPONSE_MESSAGE_BYTES + 1), TEST_TOKEN
        )


def test_social_request_bounds_names_and_thread_id_in_bytes() -> None:
    """StringConstraints counts characters; every bound here is a byte budget.

    A multibyte name or thread id passes the character check and still overflows the frame,
    which is the same trap the context bound was already written to avoid.
    """
    long_name = "é" * (protocol.MAX_ACTOR_NAME_BYTES // 2 + 1)
    assert len(long_name) <= protocol.MAX_ACTOR_NAME_BYTES

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(bot_name=long_name), TEST_TOKEN)

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(subject_name=long_name), TEST_TOKEN)

    long_thread = "é" * (protocol.MAX_THREAD_ID_BYTES // 2 + 1)
    assert len(long_thread) <= protocol.MAX_THREAD_ID_BYTES

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(thread_id=long_thread), TEST_TOKEN)


def test_bridge_token_is_bounded_in_bytes_at_both_ends() -> None:
    """The floor is for entropy and the ceiling is for the same reason every other string has one.

    Bytes rather than characters, because StringConstraints counts characters and a multibyte
    token would pass a character ceiling and still be copied into every frame.
    """
    at_ceiling = "k" * protocol.MAX_BRIDGE_TOKEN_BYTES
    protocol.parse_social_request(_social_request_payload(token=at_ceiling), at_ceiling)

    too_long = "k" * (protocol.MAX_BRIDGE_TOKEN_BYTES + 1)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(token=too_long), too_long)

    too_short = "k" * (protocol.MIN_BRIDGE_TOKEN_BYTES - 1)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(token=too_short), too_short)

    # A multibyte token that passes a CHARACTER ceiling but not a byte one.
    multibyte = "é" * (protocol.MAX_BRIDGE_TOKEN_BYTES // 2 + 1)
    assert len(multibyte) <= protocol.MAX_BRIDGE_TOKEN_BYTES
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_social_request(_social_request_payload(token=multibyte), multibyte)


def test_a_bad_request_token_is_reported_as_a_schema_violation() -> None:
    """Through the documented path, not a different one.

    A field validator must raise ValueError so pydantic collects it into the ValidationError
    that parse_request already translates. A ProtocolError raised inside the validator would
    escape that path and surface by a different route than every other schema violation.
    """
    too_long = "k" * (protocol.MAX_BRIDGE_TOKEN_BYTES + 1)

    with pytest.raises(protocol.ProtocolError) as caught:
        protocol.parse_social_request(_social_request_payload(token=too_long), too_long)

    assert "schema violation" in str(caught.value)

    # The encoder boundary reports the same bound as a ProtocolError directly.
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_social_response(77, 500, 2, "Aye.", too_long)

    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(1, "Aye.", too_long)


# The deployed Playerbots database settings ----------------------------------------


def test_playerbots_database_info_is_parsed_from_the_deployed_value() -> None:
    settings = app.PlayerbotsDatabaseSettings.parse_info('"127.0.0.1;3306;acore;s3cr3t;acore_playerbots"')

    assert settings.host == "127.0.0.1"
    assert settings.port == 3306
    assert settings.user == "acore"
    assert settings.password == "s3cr3t"
    assert settings.database == "acore_playerbots"


def test_the_password_never_appears_in_a_representation() -> None:
    """Definition of Done 5.

    A dataclass prints every field, and this is exactly the object most likely to reach
    a log line or a traceback. Both repr and str are overridden, because a format string
    reaches for str and logging reaches for repr.
    """
    settings = app.PlayerbotsDatabaseSettings.parse_info("127.0.0.1;3306;acore;hunter2;acore_playerbots")

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert "hunter2" not in f"{settings}"
    assert "<redacted>" in repr(settings)
    # And the value itself is still usable, since redaction is for display only.
    assert settings.password == "hunter2"


def test_a_malformed_database_info_is_refused_rather_than_guessed_at() -> None:
    for bad in (
        "127.0.0.1;3306;acore;pw",  # too few fields
        "127.0.0.1;3306;acore;pw;db;extra",  # a semicolon in the password shifts everything
        ";3306;acore;pw;db",  # no host
        "127.0.0.1;3306;;pw;db",  # no user
        "127.0.0.1;3306;acore;pw;",  # no database
        "127.0.0.1;notaport;acore;pw;db",
        "127.0.0.1;0;acore;pw;db",
        "127.0.0.1;70000;acore;pw;db",
    ):
        with pytest.raises(ValueError):
            app.PlayerbotsDatabaseSettings.parse_info(bad)


def test_database_info_is_found_in_a_realistic_config_file(tmp_path) -> None:
    # These files carry duplicate keys and section free preambles, which is why the
    # reader is line based rather than a strict parser.
    path = tmp_path / "playerbots.conf"
    path.write_text(
        "########################################\n"
        "# Some preamble with no section header\n"
        "AiPlayerbot.Enabled = 1\n"
        "#PlayerbotsDatabaseInfo = commented out and must be ignored\n"
        'PlayerbotsDatabaseInfo = "127.0.0.1;3306;acore;pw;acore_playerbots"\n'
        "AiPlayerbot.Enabled = 1\n",
        encoding="utf-8",
    )

    settings = app.PlayerbotsDatabaseSettings.load(str(path))
    assert settings.database == "acore_playerbots"

    missing = tmp_path / "empty.conf"
    missing.write_text("AiPlayerbot.Enabled = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        app.PlayerbotsDatabaseSettings.load(str(missing))
