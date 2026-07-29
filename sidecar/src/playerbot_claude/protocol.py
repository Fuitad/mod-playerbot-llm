"""Strict framing and models mirroring the C++ ClaudeChat protocol contract.

The wire format is a 4-byte network-order length prefix followed by one UTF-8 JSON
object of at most 64 KiB. Requests come from worldserver; responses go back. Both
directions carry the shared bridge token, compared in constant time and never included
in anything sent to Claude or written to logs.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import struct
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

SCHEMA_VERSION = 1
MAX_FRAME_PAYLOAD_BYTES = 64 * 1024
MAX_REQUEST_MESSAGE_BYTES = 512
MAX_RESPONSE_MESSAGE_BYTES = 240
MIN_BRIDGE_TOKEN_BYTES = 32

VOICES = ("reserved", "pragmatic", "earnest", "wry", "boisterous")

_UINT64_MAX = 2**64 - 1
_FRAME_HEADER = struct.Struct("!I")


class FrameError(Exception):
    """Frame-level violation: oversized length or truncated stream."""


class ProtocolError(Exception):
    """Payload-level violation: invalid UTF-8, JSON, schema, or bounds."""


class TokenMismatchError(ProtocolError):
    """Bridge token comparison failed. Never carries the expected value."""


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


class ChatRequest(BaseModel):
    """One trusted request from worldserver. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    token: str
    request_id: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    channel: Literal["whisper", "party"]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    speaker_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_name: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    speaker_name: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    profile_version: Literal[1]
    crafting_affinity: Annotated[int, Field(ge=0, le=100)]
    exploration_affinity: Annotated[int, Field(ge=0, le=100)]
    sociability: Annotated[int, Field(ge=0, le=100)]
    voice: Literal["reserved", "pragmatic", "earnest", "wry", "boisterous"]
    event_kind: Literal[0, 1, 2, 3]
    subject_id: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    occurrence: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    message: str

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        if not value or _byte_length(value) > MAX_REQUEST_MESSAGE_BYTES:
            raise ValueError("message must be 1 to 512 UTF-8 bytes")

        return value


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_PAYLOAD_BYTES:
        raise FrameError("frame payload exceeds 64 KiB")

    return _FRAME_HEADER.pack(len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    try:
        header = await reader.readexactly(_FRAME_HEADER.size)
    except asyncio.IncompleteReadError as error:
        raise FrameError("stream ended before a complete frame header") from error

    (length,) = _FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_PAYLOAD_BYTES:
        raise FrameError("frame length exceeds 64 KiB")

    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise FrameError("stream ended before the complete frame payload") from error


def parse_request(payload: bytes, expected_token: str) -> ChatRequest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("request payload is not valid UTF-8") from error

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError("request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("request payload is not a JSON object")

    try:
        request = ChatRequest.model_validate(data)
    except ValidationError as error:
        raise ProtocolError(f"request schema violation: {error.error_count()} error(s)") from error

    if not hmac.compare_digest(request.token.encode("utf-8"), expected_token.encode("utf-8")):
        raise TokenMismatchError("bridge token mismatch")

    return request


def _validate_response_message(message: str) -> None:
    if not message:
        raise ProtocolError("response message is empty")

    if _byte_length(message) > MAX_RESPONSE_MESSAGE_BYTES:
        raise ProtocolError("response message exceeds 240 UTF-8 bytes")

    if any(ord(character) < 0x20 for character in message):
        raise ProtocolError("response message must be a single line without control characters")


def encode_response(request_id: int, message: str, token: str) -> bytes:
    """Builds the exact payload shape the C++ response parser accepts."""

    if not 1 <= request_id <= _UINT64_MAX:
        raise ProtocolError("request_id out of range")

    _validate_response_message(message)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "request_id": request_id,
        "message": message,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
