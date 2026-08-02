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
import unicodedata
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 3
MAX_FRAME_PAYLOAD_BYTES = 64 * 1024
MAX_REQUEST_MESSAGE_BYTES = 512
MAX_CAREER_MESSAGE_BYTES = 60 * 1024
MAX_RESPONSE_MESSAGE_BYTES = 240
MAX_ACTOR_NAME_BYTES = 48
MAX_SOCIAL_CONTEXT_BYTES = 4 * 1024
MAX_THREAD_ID_BYTES = 64
MAX_CAREER_TOKEN_BYTES = 64
MAX_CAREER_SUMMARY_BYTES = 160
MIN_BRIDGE_TOKEN_BYTES = 32
MAX_BRIDGE_TOKEN_BYTES = 256
AMBIENT_EVENT_KIND = 4
CAREER_EVENT_KIND = 5
AMBIENT_EVENT_MARKER = "ambient_world"

VOICES = ("reserved", "pragmatic", "earnest", "wry", "boisterous")
SPENDING_STYLES = ("none", "minimal", "progression", "completionist")

# Channels where a gesture can actually be seen, mirroring the coordinator's own rule. General
# is zone wide and its participants are nowhere near each other, and a whisper has no physical
# presence at all, so neither can carry one.
SOCIAL_EMOTE_CHANNELS = (1, 2)

"""The closed set of gestures a bot may make, as name to WoW text emote ID.

Values are `TextEmotes` from `src/server/shared/SharedDefines.h`, the family behind /wave and
/cheer, which produces visible, optionally targeted social behaviour. The other `Emote` enum is
animation only and cannot be aimed at anybody, so it is the wrong family for something the
contract describes as a gesture a nearby player has to be present to see. Recorded here because
the coordinator's `emoteId` field does not name its family, and the send path Task 10A adds has
to agree with this choice.

The model picks a NAME and never sees or emits a number, so it cannot invent one that happens to
parse. Same reasoning as the career candidate token: proving a value's shape proves nothing about
whether it was ever on the menu.
"""
SOCIAL_EMOTES = {
    "applaud": 5,
    "bow": 17,
    "cheer": 21,
    "chuckle": 23,
    "greet": 48,
    "grin": 49,
    "laugh": 60,
    "nod": 67,
    "salute": 78,
    "shrug": 83,
    "sigh": 85,
    "smile": 163,
    "thank": 97,
    "wave": 101,
    "ponder": 120,
}

SOCIAL_EMOTE_IDS = frozenset(SOCIAL_EMOTES.values())


# `MAX_PLAYER_NAME` in `src/server/game/Globals/ObjectMgr.h`, the client's own limit.
#
# Not the same bound as MAX_ACTOR_NAME_BYTES, and neither replaces the other. 48 bytes is what
# a frame can carry, and it equals twelve characters only in the worst case of a four byte
# script, so on its own it admits 48 Latin letters. Twelve is what a name can actually be.
MAX_PLAYER_NAME_CHARACTERS = 12


def actor_name_is_usable(value: str) -> bool:
    """Whether a string can be a character name.

    A participant name reaches the TRUSTED system prompt, because the bot has to be told who
    it is and who it is speaking to, so the SHAPE of this value is a security property rather
    than a formatting preference.

    The rule is the game's own. `ObjectMgr::CheckPlayerName` calls
    `isValidString(name, mask, numericOrSpace=false)`, so a real character name is letters
    only: no digits, no spaces, no punctuation, at most twelve characters. Enforcing exactly
    that closes the vector rather than narrowing it, because a name that cannot contain a
    space cannot spell a sentence. A rule that merely excluded delimiters would still have
    admitted "Ignore all previous rules", which reads as an instruction because it is one.

    Decided by Unicode category rather than by a character class: `\\w` admits digits and the
    underscore, and excludes the combining marks that legitimately appear in names.
    """

    if not value or len(value) > MAX_PLAYER_NAME_CHARACTERS:
        return False

    if unicodedata.category(value[0])[0] != "L":
        return False

    # Letters, and the marks that attach to them. Nothing else, in any script.
    return all(unicodedata.category(character)[0] in {"L", "M"} for character in value[1:])


# Valid values of PlayerbotSocialChannel. Bounded to the enum rather than to a byte, because
# every consumer indexes a four-entry table with it.
SOCIAL_CHANNEL_COUNT = 4

"""Privacy scopes a remembered fact can carry, least to most private.

The order is the lattice and it is load bearing: a memory may be drawn on only when the channel
it would be spoken over is at least as private as the scope it was learned in. Mirrors
`PlayerbotSocialPrivacyScope` on the coordinator side.
"""
SOCIAL_MEMORY_SCOPES = ("public", "party", "whisper")

# Channel index to the most private memory scope it may draw on. General and say are heard by
# anyone nearby, so they get public only; party may use what the party knows; a whisper is
# already the most private thing there is.
SOCIAL_CHANNEL_MEMORY_SCOPE = (0, 0, 1, 2)

MAX_SOCIAL_CONTEXT_ENTRIES = 12
MAX_SOCIAL_CONTEXT_ENTRY_BYTES = 512

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


def _validated_token(value: str) -> str:
    """Both bridge token bounds, in UTF-8 bytes. Shared by every request model and encoder.

    Raises ValueError, not ProtocolError, because this runs inside a pydantic field validator:
    pydantic collects a ValueError into the ValidationError that parse_request and
    parse_social_request already translate, and a ProtocolError raised here would escape that
    path and be reported by a different route than every other schema violation.

    The encoders below want a ProtocolError instead, so they translate at their own boundary.
    """

    length = _byte_length(value)
    if length < MIN_BRIDGE_TOKEN_BYTES or length > MAX_BRIDGE_TOKEN_BYTES:
        raise ValueError(
            f"bridge token must be {MIN_BRIDGE_TOKEN_BYTES} to {MAX_BRIDGE_TOKEN_BYTES} UTF-8 bytes"
        )

    return value


def _encoder_token(value: str) -> str:
    """The same bound at an encoder boundary, reported as a ProtocolError."""

    try:
        return _validated_token(value)
    except ValueError as error:
        raise ProtocolError(str(error)) from error


class CareerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: Annotated[str, StringConstraints(pattern=r"^career-[a-z0-9]+$", max_length=64)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    maximum_spending_style: Literal["none", "minimal", "progression", "completionist"]
    market_eligible: Literal[0, 1]
    engagement: Annotated[int, Field(ge=0, le=100)]

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        # StringConstraints counts characters. The budget is bytes, and a summary is free text
        # a model wrote, so it is the field most likely to be multibyte. The token is safe from
        # this by its own ASCII pattern rather than by luck.
        if _byte_length(value) > MAX_CAREER_SUMMARY_BYTES:
            raise ValueError(f"summary must be at most {MAX_CAREER_SUMMARY_BYTES} UTF-8 bytes")

        return value


class CareerRequestContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    personality_version: Literal[2]
    career_version: Literal[1]
    candidates: Annotated[list[CareerCandidate], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def _unique_tokens(self) -> Self:
        tokens = [candidate.token for candidate in self.candidates]
        if len(tokens) != len(set(tokens)):
            raise ValueError("career candidate tokens must be unique")
        return self


class ChatRequest(BaseModel):
    """One trusted request from worldserver. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[3]
    token: str
    request_id: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    channel: Literal["whisper", "party", "world", "career", "social"]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    speaker_guid: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    bot_name: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    speaker_name: Annotated[str, StringConstraints(max_length=48)]
    profile_version: Literal[2]
    crafting_affinity: Annotated[int, Field(ge=0, le=100)]
    gathering_affinity: Annotated[int, Field(ge=0, le=100)]
    exploration_affinity: Annotated[int, Field(ge=0, le=100)]
    sociability: Annotated[int, Field(ge=0, le=100)]
    voice: Literal["reserved", "pragmatic", "earnest", "wry", "boisterous"]
    event_kind: Literal[0, 1, 2, 3, 4, 5]
    subject_id: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    occurrence: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    message: str

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        # Bytes, not characters. StringConstraints would have counted characters, which is the
        # same trap every other bounded string in this protocol carries a validator to avoid.
        return _validated_token(value)

    @field_validator("bot_name", "speaker_name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        # Same character versus byte trap as the social actors. Every bound in this protocol is a
        # byte budget, and StringConstraints counts characters.
        if _byte_length(value) > MAX_ACTOR_NAME_BYTES:
            raise ValueError(f"name must be at most {MAX_ACTOR_NAME_BYTES} UTF-8 bytes")

        return value

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        if not value or _byte_length(value) > MAX_CAREER_MESSAGE_BYTES:
            raise ValueError("message must be 1 to 61440 UTF-8 bytes")

        return value

    @model_validator(mode="after")
    def _validate_ambient_fields(self) -> Self:
        if self.channel == "career" or self.event_kind == CAREER_EVENT_KIND:
            if (
                self.channel != "career"
                or self.event_kind != CAREER_EVENT_KIND
                or self.speaker_guid != 0
                or self.speaker_name
                or self.subject_id != 0
                or self.occurrence != 0
            ):
                raise ValueError("career request fields do not match the trusted contract")
            parse_career_content(self.message)
        elif self.channel == "world" or self.event_kind == AMBIENT_EVENT_KIND:
            if (
                self.channel != "world"
                or self.event_kind != AMBIENT_EVENT_KIND
                or self.speaker_guid != 0
                or self.speaker_name
                or self.subject_id != 0
                or self.message != AMBIENT_EVENT_MARKER
            ):
                raise ValueError("ambient World request fields do not match the trusted contract")
        elif self.speaker_guid == 0 or not self.speaker_name:
            raise ValueError("direct chat requires a human speaker identity")
        elif _byte_length(self.message) > MAX_REQUEST_MESSAGE_BYTES:
            raise ValueError("chat message must be at most 512 UTF-8 bytes")

        return self

    @property
    def is_ambient(self) -> bool:
        return self.channel == "world"

    @property
    def is_career(self) -> bool:
        return self.channel == "career"

    @property
    def career_content(self) -> CareerRequestContent:
        return parse_career_content(self.message)


def parse_career_content(message: str) -> CareerRequestContent:
    try:
        data = json.loads(message, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("career content is not valid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("career content must be a JSON object")
    return CareerRequestContent.model_validate(data)


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


def _object_with_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


class SocialRequest(BaseModel):
    """One social generation asked for by the worldserver coordinator.

    The bot and the subject carry the same field shape, differing only in their ``human``
    flag. Two shapes would let a prompt builder treat them differently by accident, and the
    contract is explicit that a human's priority comes from being actively engaged rather
    than from being human.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[3]
    token: str
    kind: Literal["social"]
    social_request_token: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_NAME_BYTES)]
    bot_human: Literal[0, 1]
    subject_guid: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    subject_name: Annotated[str, StringConstraints(max_length=MAX_ACTOR_NAME_BYTES)]
    subject_human: Literal[0, 1]
    speak_on_channel: Annotated[int, Field(ge=0, le=255)]
    thread_id: Annotated[str, StringConstraints(min_length=1, max_length=MAX_THREAD_ID_BYTES)]
    context: str

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        return _validated_token(value)

    @model_validator(mode="after")
    def _subject_is_present_or_absent(self) -> Self:
        # Never half described. A zero guid with a name attached is an orphan that still travels
        # and still describes a participant who is not there.
        absent = self.subject_guid == 0 and not self.subject_name and self.subject_human == 0
        present = self.subject_guid != 0 and bool(self.subject_name)
        if not absent and not present:
            raise ValueError("subject must be either fully absent or fully identified")

        return self

    @field_validator("bot_name", "subject_name")
    @classmethod
    def _validate_actor_name(cls, value: str) -> str:
        # StringConstraints(max_length=...) counts CHARACTERS. Every bound here is a byte budget,
        # so a multibyte name passes the character check and still overflows the frame. The
        # declared max_length stays as a cheap first cut; this is the one that actually holds.
        if _byte_length(value) > MAX_ACTOR_NAME_BYTES:
            raise ValueError(f"actor name must be at most {MAX_ACTOR_NAME_BYTES} UTF-8 bytes")

        # Shape, not just size. This value is interpolated into the TRUSTED system prompt, so a
        # name bounded only by length is 48 bytes of arbitrary text sitting inside the
        # instructions. An absent subject is the empty string and is allowed through here; the
        # model validator below is what decides absence is coherent.
        if value and not actor_name_is_usable(value):
            raise ValueError("actor name is not a usable character name")

        return value

    @field_validator("speak_on_channel")
    @classmethod
    def _validate_channel(cls, value: int) -> int:
        # Bounded to the enum rather than to a byte. Every consumer indexes a four entry table
        # with this, so 0 to 255 was an IndexError waiting on an authenticated request, raised
        # outside the ProtocolError the connection handler knows how to answer.
        if value >= SOCIAL_CHANNEL_COUNT:
            raise ValueError("speak_on_channel is not a known social channel")

        return value

    @field_validator("thread_id")
    @classmethod
    def _validate_thread_id(cls, value: str) -> str:
        if _byte_length(value) > MAX_THREAD_ID_BYTES:
            raise ValueError(f"thread_id must be at most {MAX_THREAD_ID_BYTES} UTF-8 bytes")

        return value

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value: str) -> str:
        if _byte_length(value) > MAX_SOCIAL_CONTEXT_BYTES:
            raise ValueError(f"context must be at most {MAX_SOCIAL_CONTEXT_BYTES} UTF-8 bytes")

        return value


class SocialMemory(BaseModel):
    """One remembered fact, and the privacy it was learned under."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]
    scope: Literal["public", "party", "whisper"]


class SocialContext(BaseModel):
    """What the coordinator assembled for one social line.

    Every field is optional, because nothing populates this yet: Task 8's transport sends an
    empty context and Task 9A is what will fill it. A context that does not parse as this
    shape is not an error either, it is just text, and the caller falls back to treating it
    as one opaque untrusted block rather than going silent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    persona: Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)] = ""
    relationship: Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)] = ""
    nearby: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ] = []
    thread: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ] = []
    memories: Annotated[list[SocialMemory], Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES)] = []

    def memories_within(self, channel: int) -> list[SocialMemory]:
        """The memories this channel may draw on, most private allowed first computed.

        A memory learned in a whisper cannot be repeated to a zone. The coordinator is meant
        to filter before sending, and this does not replace that; it is the second layer, so
        that one bug at the producer is not a bot repeating a private confidence in General.
        """

        if not 0 <= channel < len(SOCIAL_CHANNEL_MEMORY_SCOPE):
            return []

        allowed = SOCIAL_CHANNEL_MEMORY_SCOPE[channel]
        return [memory for memory in self.memories if SOCIAL_MEMORY_SCOPES.index(memory.scope) <= allowed]


def parse_social_context(context: str) -> SocialContext | None:
    """The assembled context, or None when it is not that shape.

    None is a normal answer rather than a failure. Nothing populates this field yet, and a
    producer that changes shape should not silence every bot on the realm; the caller carries
    an unparseable context through as opaque untrusted text instead.
    """

    if not context:
        return None

    try:
        data = json.loads(context, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    try:
        return SocialContext.model_validate(data)
    except ValidationError:
        return None


def declared_kind(payload: bytes) -> str | None:
    """The `kind` a payload declares, or None when it declares none.

    Read BEFORE a request model is chosen. Choosing a model first and falling back to the
    other one when it fails would let a malformed social frame be re-read as a chat frame,
    and the caller would then be told about the wrong 23 schema errors. It also means an
    unrecognized kind can be refused as unrecognized rather than mis-parsed.

    A chat request declares nothing, which is why None is a value rather than an error: it
    is what every request looked like before schema 3 added the social variant.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("request payload is not a JSON object")

    kind = data.get("kind")
    if kind is None:
        return None

    if not isinstance(kind, str):
        raise ProtocolError("request kind must be a string")

    return kind


def parse_social_request(payload: bytes, expected_token: str) -> SocialRequest:
    """Strict parser for a social request. Mirrors parse_request field for field."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("social request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("social request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("social request payload is not a JSON object")

    try:
        request = SocialRequest.model_validate(data)
    except ValidationError as error:
        raise ProtocolError(f"social request schema violation: {error.error_count()} error(s)") from error

    if not hmac.compare_digest(request.token.encode("utf-8"), expected_token.encode("utf-8")):
        raise TokenMismatchError("bridge token mismatch")

    return request


def encode_social_response(
    social_request_token: int,
    bot_guid: int,
    speak_on_channel: int,
    message: str,
    token: str,
    regenerate: bool = False,
    emote_id: int = 0,
) -> bytes:
    """Builds the exact payload shape the C++ social response parser accepts.

    Exactly one of three answers: a line, a gesture, or a regeneration. A regeneration
    carries no message, because it is the sidecar reporting that its own output was
    unusable rather than offering one. A gesture carries no message either: it is one
    answer, and a line attached to it is a second.
    """

    if not 1 <= social_request_token <= _UINT64_MAX:
        raise ProtocolError("social_request_token out of range")

    if not 1 <= bot_guid <= _UINT64_MAX:
        raise ProtocolError("bot_guid out of range")

    if not 0 <= speak_on_channel <= 255:
        raise ProtocolError("speak_on_channel out of range")

    token = _encoder_token(token)

    if regenerate:
        message = ""
        emote_id = 0
    elif emote_id:
        if emote_id not in SOCIAL_EMOTE_IDS:
            raise ProtocolError("emote_id is not a supported social gesture")

        # The coordinator drops text attached to a gesture rather than storing it. Refusing
        # to build the frame at all means the caller finds out which request was at fault,
        # instead of a line silently evaporating on the far side.
        if message:
            raise ProtocolError("a social emote carries no message")

        if speak_on_channel not in SOCIAL_EMOTE_CHANNELS:
            raise ProtocolError("an emote cannot be seen on this channel")
    else:
        _validate_response_message(message)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "kind": "social",
        "social_request_token": social_request_token,
        "bot_guid": bot_guid,
        "speak_on_channel": speak_on_channel,
        "message": message,
        "emote_id": emote_id,
        "regenerate": 1 if regenerate else 0,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_request(payload: bytes, expected_token: str) -> ChatRequest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
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

    # Explicit rather than incidental. The runtime token was validated when it was read, but an
    # encoder that only ever relies on that is one refactor away from signing a frame with
    # whatever it was handed.
    token = _encoder_token(token)

    _validate_response_message(message)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "request_id": request_id,
        "message": message,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
