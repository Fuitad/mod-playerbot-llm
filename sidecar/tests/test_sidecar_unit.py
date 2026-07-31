"""Offline unit tests for the sidecar.

Every byte fixture here mirrors the C++ contract tests in tests/ClaudeChatTest.cpp so
the two implementations cannot drift silently. No test makes a real HTTP request.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import anthropic
import httpx
import pytest

from playerbot_claude import app, claude, protocol, storage

TEST_TOKEN = "0123456789abcdef0123456789abcdef"

# Byte-for-byte copy of the C++ RequestSerializesToExactContractJson fixture.
CPP_REQUEST_FIXTURE = (
    '{"schema_version":2,'
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
    '{"schema_version":2,'
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
        protocol.parse_request(b'{"schema_version":2,"bad":"\xff"}', TEST_TOKEN)


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
        b'{"schema_version":2,"token":"0123456789abcdef0123456789abcdef",'
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
    assert config.daily_budget_usd == 5.0


def test_config_strips_surrounding_quotes_like_worldserver(tmp_path) -> None:
    # AzerothCore .conf convention quotes string values (the shipped .dist does);
    # worldserver's ConfigMgr strips them, so the sidecar must too.
    quoted = str(tmp_path / "quoted path.sqlite")
    conf = write_conf(
        tmp_path,
        CONF_TEXT + f'PlayerbotClaude.SidecarDatabase = "{quoted}"\n',
    )
    assert app.SidecarConfig.load(conf).database_path == quoted


def test_config_defaults_fail_closed(tmp_path) -> None:
    config = app.SidecarConfig.load(write_conf(tmp_path, "[worldserver]\n"))
    assert config.enable is False
    assert config.bridge_port == 0
    assert config.daily_budget_usd == 0.0


def test_config_replaces_lifetime_budget_and_enforces_hard_ceiling(tmp_path) -> None:
    old_only = app.SidecarConfig.load(write_conf(tmp_path, "[worldserver]\nPlayerbotClaude.BudgetUsd = 1\n"))
    assert old_only.daily_budget_usd == 0.0
    assert old_only.generation_allowed is False

    for value, allowed in ((0, False), (0.5, True), (5, True), (-1, False), (5.01, False)):
        config = app.SidecarConfig.load(
            write_conf(tmp_path, f"[worldserver]\nPlayerbotClaude.DailyBudgetUsd = {value}\n")
        )
        assert config.generation_allowed is allowed


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


class FakeAdapter(claude.ClaudeAdapter):
    def __init__(self, reply: str = "A fine day for fishing.") -> None:
        # Deliberately no super().__init__(): the fake never builds a real client.
        self.reply = reply
        self.requests: list[protocol.ChatRequest] = []
        self.histories: list[list[tuple[str, str]]] = []
        self.input_tokens = 100

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int:
        return self.input_tokens

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, claude.UsageTotals]:
        self.requests.append(request)
        self.histories.append(list(history))
        return self.reply, claude.UsageTotals(input_tokens=self.input_tokens, output_tokens=10)


def make_service(tmp_path, adapter=None, budget: float = 1.0) -> app.SidecarService:
    conf = write_conf(
        tmp_path,
        CONF_TEXT.replace("5.0", str(budget)),
    )
    return app.SidecarService(
        config=app.SidecarConfig.load(conf),
        token=TEST_TOKEN,
        adapter=adapter or FakeAdapter(),
    )


async def test_service_processes_valid_request(tmp_path) -> None:
    service = make_service(tmp_path)
    payload = await service.process_payload(CPP_REQUEST_FIXTURE.encode())
    assert payload is not None

    response = json.loads(payload)
    assert response["schema_version"] == 2
    assert response["request_id"] == 7
    assert response["message"] == "A fine day for fishing."
    assert response["token"] == TEST_TOKEN


async def test_service_stays_silent_on_bad_token(tmp_path) -> None:
    adapter = FakeAdapter()
    service = make_service(tmp_path, adapter=adapter)
    bad = valid_request_dict()
    bad["token"] = "z" * 32
    with pytest.raises(protocol.TokenMismatchError):
        await service.process_payload(json.dumps(bad).encode())
    assert adapter.requests == []


async def test_service_stays_silent_when_generation_fails(tmp_path) -> None:
    class FailingAdapter(FakeAdapter):
        def generate_reply(self, request, history):
            raise claude.ClaudeTimeoutError("provider timed out")

    service = make_service(tmp_path, adapter=FailingAdapter())
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None


async def test_service_makes_no_generation_call_without_budget(tmp_path) -> None:
    adapter = FakeAdapter()
    service = make_service(tmp_path, adapter=adapter, budget=0.0)
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert adapter.requests == []


async def test_service_keeps_career_decisions_out_of_chat_history(tmp_path) -> None:
    adapter = FakeAdapter(reply='{"candidate_token":"career-def456","spending_style":"progression"}')
    store = make_storage(tmp_path)
    service = app.SidecarService(
        config=app.SidecarConfig.load(write_conf(tmp_path)),
        token=TEST_TOKEN,
        adapter=adapter,
        store=store,
    )

    payload = await service.process_payload(json.dumps(career_request_dict()).encode())
    assert payload is not None
    assert adapter.histories == [[]]
    assert store.recent_turns(42) == []
    decision = store.get_career_decision(42)
    assert decision is not None
    assert decision == {
        "career_version": 1,
        "candidate_token": "career-def456",
        "spending_style": "progression",
        "updated_at": decision["updated_at"],
    }


# --- Storage: profiles, bounded memory, crash-safe budget ---


HAIKU_PRICES = storage.PriceSnapshot.from_usd_per_mtok(1.00, 5.00)


def make_storage(tmp_path) -> storage.Storage:
    return storage.Storage(str(tmp_path / "sidecar.sqlite"))


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def test_prices_convert_exactly() -> None:
    assert HAIKU_PRICES.input_nano_per_token == 1000
    assert HAIKU_PRICES.output_nano_per_token == 5000


def test_documented_cost_examples_are_exact() -> None:
    # Literal examples from the approved plan: 2,500 + 80 tokens cost 0.0029 USD and
    # 4,095 + 96 tokens cost 0.004575 USD.
    assert HAIKU_PRICES.cost_nano(2500, 80) == 2_900_000
    assert storage.nano_to_usd_string(HAIKU_PRICES.cost_nano(2500, 80)) == "0.0029"
    assert HAIKU_PRICES.cost_nano(4095, 96) == 4_575_000
    assert storage.nano_to_usd_string(HAIKU_PRICES.cost_nano(4095, 96)) == "0.004575"


def test_profile_persists_across_reopen(tmp_path) -> None:
    store = make_storage(tmp_path)
    store.record_profile(make_request_model())
    store.close()

    reopened = make_storage(tmp_path)
    profile = reopened.get_profile(42)
    assert profile is not None
    assert profile["profile_version"] == 2
    assert profile["crafting_affinity"] == 65
    assert profile["gathering_affinity"] == 37
    assert profile["exploration_affinity"] == 91
    assert profile["sociability"] == 82
    assert profile["voice"] == "earnest"
    assert reopened.get_profile(9999) is None


def test_profile_schema_migrates_existing_database_without_erasing_history(tmp_path) -> None:
    path = tmp_path / "sidecar.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE profiles (
            bot_guid INTEGER PRIMARY KEY,
            profile_version INTEGER NOT NULL,
            crafting_affinity INTEGER NOT NULL,
            exploration_affinity INTEGER NOT NULL,
            sociability INTEGER NOT NULL,
            voice TEXT NOT NULL,
            bot_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO profiles VALUES (42, 1, 65, 91, 82, 'earnest', 'Botname', 'old')")
    connection.commit()
    connection.close()

    store = storage.Storage(str(path))
    profile = store.get_profile(42)
    assert profile is not None
    assert profile["profile_version"] == 1
    assert profile["gathering_affinity"] == 0


def test_conversation_memory_is_bounded_to_20_turns(tmp_path) -> None:
    store = make_storage(tmp_path)
    for index in range(25):
        role = "user" if index % 2 == 0 else "assistant"
        store.append_turn(42, role, f"turn {index}")

    turns = store.recent_turns(42)
    assert len(turns) == 20
    assert turns[0] == ("user" if 5 % 2 == 0 else "assistant", "turn 5")
    assert turns[-1] == ("user", "turn 24")

    # Other bots are unaffected and isolated.
    assert store.recent_turns(7) == []


def test_reservation_settlement_produces_exact_spend(tmp_path) -> None:
    store = make_storage(tmp_path)
    budget_nano = storage.usd_to_nano(5)

    reservation = store.reserve(7, 4095, 96, HAIKU_PRICES, budget_nano)
    assert reservation is not None
    assert store.outstanding_nano() == 4_575_000
    assert store.spent_nano() == 0

    store.mark_submitted(reservation)
    store.settle(reservation, 2500, 80, HAIKU_PRICES)
    assert store.outstanding_nano() == 0
    assert store.spent_nano() == 2_900_000


def test_reservation_rejected_when_budget_cannot_fit(tmp_path) -> None:
    store = make_storage(tmp_path)
    budget_nano = storage.usd_to_nano(0.0046)  # 4,600,000 nano

    first = store.reserve(1, 4095, 96, HAIKU_PRICES, budget_nano)  # max 4,575,000
    assert first is not None

    # Outstanding reservation blocks a second one.
    assert store.reserve(2, 10, 96, HAIKU_PRICES, budget_nano) is None

    store.mark_submitted(first)
    store.settle(first, 2500, 80, HAIKU_PRICES)  # actual spend 2,900,000

    # A small follow-up fits (2,900,000 + 1,000*10 + 5,000*96 = 3,390,000).
    assert store.reserve(3, 10, 96, HAIKU_PRICES, budget_nano) is not None
    # A full-size one does not (2,900,000 + 4,575,000 > 4,600,000). The small
    # reservation above is still outstanding, which blocks it further.
    assert store.reserve(4, 4095, 96, HAIKU_PRICES, budget_nano) is None


def test_crash_before_reservation_charges_nothing(tmp_path) -> None:
    store = make_storage(tmp_path)
    store.close()
    reopened = make_storage(tmp_path)
    assert reopened.spent_nano() == 0
    assert reopened.outstanding_nano() == 0


def test_crash_after_reservation_remains_charged_at_maximum(tmp_path) -> None:
    store = make_storage(tmp_path)
    reservation = store.reserve(7, 4095, 96, HAIKU_PRICES, storage.usd_to_nano(5))
    assert reservation is not None
    store.close()  # crash before mark_submitted

    reopened = make_storage(tmp_path)
    assert reopened.outstanding_nano() == 4_575_000


def test_crash_after_submission_remains_charged_at_maximum(tmp_path) -> None:
    store = make_storage(tmp_path)
    reservation = store.reserve(7, 4095, 96, HAIKU_PRICES, storage.usd_to_nano(5))
    assert reservation is not None
    store.mark_submitted(reservation)
    store.close()  # crash before settlement

    reopened = make_storage(tmp_path)
    assert reopened.outstanding_nano() == 4_575_000
    assert reopened.spent_nano() == 0


def test_settlement_survives_restart(tmp_path) -> None:
    store = make_storage(tmp_path)
    reservation = store.reserve(7, 2500, 96, HAIKU_PRICES, storage.usd_to_nano(5))
    assert reservation is not None
    store.mark_submitted(reservation)
    store.settle(reservation, 2500, 80, HAIKU_PRICES)
    store.close()

    reopened = make_storage(tmp_path)
    assert reopened.spent_nano() == 2_900_000
    assert reopened.outstanding_nano() == 0


# --- Service integration with storage ---


def make_stored_service(
    tmp_path, adapter=None, budget: float = 1.0, input_tokens: int = 100
) -> tuple[app.SidecarService, storage.Storage, FakeAdapter]:
    fake = adapter or FakeAdapter()
    fake.input_tokens = input_tokens
    store = make_storage(tmp_path)
    conf = write_conf(tmp_path, CONF_TEXT.replace("5.0", str(budget)))
    service = app.SidecarService(
        config=app.SidecarConfig.load(conf),
        token=TEST_TOKEN,
        adapter=fake,
        store=store,
    )
    return service, store, fake


async def test_service_records_profile_memory_and_settlement(tmp_path) -> None:
    service, store, fake = make_stored_service(tmp_path)

    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is not None

    profile = store.get_profile(42)
    assert profile is not None and profile["voice"] == "earnest"

    turns = store.recent_turns(42)
    assert len(turns) == 2
    assert turns[0][0] == "user"
    assert turns[1] == ("assistant", "A fine day for fishing.")

    assert store.spent_nano() == HAIKU_PRICES.cost_nano(100, 10)
    assert store.outstanding_nano() == 0

    # The next request carries the stored history to the adapter.
    second = valid_request_dict()
    second["request_id"] = 8
    assert await service.process_payload(json.dumps(second).encode()) is not None
    assert len(fake.histories[-1]) == 2


async def test_ambient_service_never_reads_or_appends_conversation_history(tmp_path) -> None:
    service, store, fake = make_stored_service(tmp_path)
    private_marker = "PRIVATE-WHISPER-MARKER-7E31"
    store.append_turn(42, "user", private_marker)
    before = store.recent_turns(42)

    payload = json.dumps(ambient_request_dict()).encode()
    assert await service.process_payload(payload) is not None

    assert fake.histories[-1] == []
    assert store.recent_turns(42) == before


async def test_service_rejects_oversized_prompt_without_generation(tmp_path) -> None:
    service, store, fake = make_stored_service(tmp_path, input_tokens=claude.MAX_INPUT_TOKENS + 1)
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert fake.requests == []
    assert store.outstanding_nano() == 0


async def test_service_makes_no_call_when_reservation_cannot_fit(tmp_path) -> None:
    service, _store, fake = make_stored_service(tmp_path, budget=0.000001)
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    assert fake.requests == []


async def test_generation_failure_leaves_reservation_charged(tmp_path) -> None:
    class FailingAdapter(FakeAdapter):
        def generate_reply(self, request, history):
            raise claude.ClaudeTimeoutError("provider timed out")

    service, store, _fake = make_stored_service(tmp_path, adapter=FailingAdapter())
    assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
    # Fail closed: the reservation stays charged at maximum until reconciled.
    assert store.outstanding_nano() == HAIKU_PRICES.cost_nano(100, claude.MAX_OUTPUT_TOKENS)


async def test_failed_ambient_attempt_consumes_rate_before_token_counting(tmp_path) -> None:
    class FailingCountAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.count_calls = 0

        def count_input_tokens(self, request, history):
            self.count_calls += 1
            raise claude.ClaudeProviderError("count failed")

    adapter = FailingCountAdapter()
    store = make_storage(tmp_path)
    service = app.SidecarService(
        config=app.SidecarConfig(
            enable=True,
            ambient_world_enable=True,
            ambient_max_messages_per_hour=1,
            daily_budget_usd=1,
        ),
        token=TEST_TOKEN,
        adapter=adapter,
        store=store,
    )

    first = ambient_request_dict()
    assert await service.process_payload(json.dumps(first).encode()) is None
    second = ambient_request_dict()
    second["request_id"] = 9
    assert await service.process_payload(json.dumps(second).encode()) is None
    assert adapter.count_calls == 1
    store.close()


def test_default_adapter_client_timeout_is_capped_at_deadline(tmp_path, monkeypatch) -> None:
    # The default SDK client's own timeout must track ResponseDeadlineMs so an
    # abandoned provider call cannot outlive the deadline by more than one request.
    monkeypatch.delenv("MOD_PLAYERBOT_CLAUDE_APIKEY", raising=False)
    conf = write_conf(tmp_path, CONF_TEXT.replace("10000", "2500"))
    service = app.SidecarService(config=app.SidecarConfig.load(conf), token=TEST_TOKEN)
    assert service._adapter._client.timeout == 2.5


async def test_process_payload_enforces_response_deadline(tmp_path) -> None:
    # ResponseDeadlineMs bounds the whole sidecar pipeline. A provider call that
    # outlives it must not produce a response, settle as delivered conversation,
    # or block later requests for its full provider timeout.
    release = threading.Event()

    class BlockingAdapter(FakeAdapter):
        def generate_reply(self, request, history):
            release.wait(timeout=30)
            return super().generate_reply(request, history)

    store = make_storage(tmp_path)
    conf = write_conf(tmp_path, CONF_TEXT.replace("10000", "200"))
    service = app.SidecarService(
        config=app.SidecarConfig.load(conf),
        token=TEST_TOKEN,
        adapter=BlockingAdapter(),
        store=store,
    )

    started = time.monotonic()
    try:
        assert await service.process_payload(CPP_REQUEST_FIXTURE.encode()) is None
        # Returned near the 0.2s deadline, nowhere near the 30s block.
        assert time.monotonic() - started < 5.0
        # Money fails closed: the reservation stays charged at maximum, and the
        # dead exchange never enters conversation memory.
        assert store.outstanding_nano() == HAIKU_PRICES.cost_nano(100, claude.MAX_OUTPUT_TOKENS)
        assert store.recent_turns(42) == []
    finally:
        release.set()


def test_doctor_reports_budget_numbers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYERBOT_CLAUDE_BRIDGE_TOKEN", TEST_TOKEN)
    store = make_storage(tmp_path)
    reservation = store.reserve(7, 2500, 96, HAIKU_PRICES, storage.usd_to_nano(5))
    assert reservation is not None
    store.mark_submitted(reservation)
    store.settle(reservation, 2500, 80, HAIKU_PRICES)

    report = app.doctor_report(app.SidecarConfig.load(write_conf(tmp_path)), store=store)
    budget = report["budget"]
    assert isinstance(budget, dict)
    assert budget["rolling_spent_usd"] == "0.0029"
    assert budget["rolling_reserved_usd"] == "0"
    assert budget["rolling_remaining_usd"] == "4.9971"
    assert budget["next_expiry_at"] is not None


def test_ambient_rate_gate_is_rolling_persistent_and_exact_at_boundary(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
    database = str(tmp_path / "rate.sqlite")
    store = storage.Storage(database, now=clock)

    for _ in range(6):
        assert store.try_begin_ambient(6) is True
    assert store.try_begin_ambient(6) is False
    store.close()

    reopened = storage.Storage(database, now=clock)
    assert reopened.try_begin_ambient(6) is False
    clock.advance(timedelta(seconds=3599, milliseconds=999))
    assert reopened.try_begin_ambient(6) is False
    clock.advance(timedelta(milliseconds=1))
    assert reopened.try_begin_ambient(6) is True


def test_ambient_rate_one_uses_the_same_persistent_gate(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
    database = str(tmp_path / "rate-one.sqlite")
    store = storage.Storage(database, now=clock)
    assert store.try_begin_ambient(1) is True
    assert store.try_begin_ambient(1) is False
    store.close()

    reopened = storage.Storage(database, now=clock)
    assert reopened.try_begin_ambient(1) is False
    assert reopened.try_begin_ambient(0) is False
    assert reopened.try_begin_ambient(7) is False


def test_rolling_budget_combines_all_request_types_and_ages_by_reservation_time(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
    store = storage.Storage(str(tmp_path / "rolling.sqlite"), now=clock)
    budget = storage.usd_to_nano(5)

    reservations = [
        store.reserve(request_id, 100, 10, HAIKU_PRICES, budget) for request_id in (101, 102, 103, 104)
    ]
    assert all(reservation is not None for reservation in reservations)
    first = reservations[0]
    assert first is not None
    store.mark_submitted(first)
    store.settle(first, 80, 5, HAIKU_PRICES)

    snapshot = store.rolling_budget_snapshot()
    assert snapshot.spent_nano == HAIKU_PRICES.cost_nano(80, 5)
    assert snapshot.reserved_nano == 3 * HAIKU_PRICES.cost_nano(100, 10)
    assert snapshot.next_expiry_at == datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    clock.advance(timedelta(hours=24))
    expired = store.rolling_budget_snapshot()
    assert expired.spent_nano == 0
    assert expired.reserved_nano == 0
    assert expired.next_expiry_at is None
    assert store.usage_count() == 1
    assert store.outstanding_nano() == 3 * HAIKU_PRICES.cost_nano(100, 10)


def test_concurrent_reservations_cannot_cross_rolling_budget(tmp_path) -> None:
    database = str(tmp_path / "concurrent-budget.sqlite")
    clock_value = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    storage.Storage(database, now=lambda: clock_value).close()
    one_request_budget = HAIKU_PRICES.cost_nano(100, 10)
    barrier = threading.Barrier(2)

    def reserve(request_id: int) -> int | None:
        store = storage.Storage(database, now=lambda: clock_value)
        try:
            barrier.wait()
            return store.reserve(request_id, 100, 10, HAIKU_PRICES, one_request_budget)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (201, 202)))

    assert sum(result is not None for result in results) == 1


def test_concurrent_ambient_attempts_cannot_cross_rate_limit(tmp_path) -> None:
    database = str(tmp_path / "concurrent-rate.sqlite")
    clock_value = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    storage.Storage(database, now=lambda: clock_value).close()
    barrier = threading.Barrier(2)

    def begin() -> bool:
        store = storage.Storage(database, now=lambda: clock_value)
        try:
            barrier.wait()
            return store.try_begin_ambient(1)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: begin(), range(2)))

    assert results.count(True) == 1


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
