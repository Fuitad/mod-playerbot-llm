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
from typing import Annotated, Literal, Self, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 4
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

# The worldserver's trusted prompt authority, carried inside the structured social context.
# Only these four spellings exist, and only authorized_roleplay may lift the ordinary player
# voice; the C++ bridge refuses to serialize anything outside them. Expansions are the game's
# own three: 0 classic, 1 burning crusade, 2 wrath.
RoleplayPromptMode = Literal[
    "ordinary",
    "decline_roleplay",
    "acknowledge_roleplay",
    "authorized_roleplay",
]
ROLEPLAY_PROMPT_MODES: tuple[str, ...] = get_args(RoleplayPromptMode)
MAX_SOCIAL_ACTIVE_EXPANSION = 2
SocialAdmissionLane = Literal["immediate_human", "background"]

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
FICTIONAL_IDENTITY_COUNTRIES = (
    "United States",
    "Canada",
    "Mexico",
    "Australia",
    "New Zealand",
    "Singapore",
    "Malaysia",
    "Thailand",
    "Indonesia",
    "Philippines",
    "Brazil",
    "Argentina",
    "Chile",
    "Colombia",
    "Peru",
    "Uruguay",
    "Ecuador",
    "Costa Rica",
    "Panama",
    "Guatemala",
    "United Kingdom",
    "Germany",
    "France",
    "Netherlands",
    "Belgium",
    "Ireland",
    "Denmark",
    "Sweden",
    "Norway",
    "Finland",
    "Iceland",
    "Spain",
    "Italy",
    "Portugal",
    "Greece",
    "Poland",
    "Austria",
    "Switzerland",
    "Czechia",
    "Hungary",
    "Romania",
    "Slovakia",
    "Ukraine",
)

# The most memories one finished conversation may yield. Matches
# PLAYERBOT_SOCIAL_MAX_EXTRACTED_MEMORIES on the worldserver. A dozen turns among a few people
# does not contain five things worth remembering, so a reply past this is a model producing
# volume rather than substance, and every extra one is a durable row replayed into later prompts.
MAX_EXTRACTED_MEMORIES = 4
MAX_SOCIAL_CONTEXT_ENTRY_BYTES = 512

"""The wire shape of a generated biography.

Here rather than beside the model that produces it, because this is what the worldserver's
assembler accepts: it is a protocol fact, and the generator is one of its two users. `claude`
asserts its own reply model against this tuple at import, so the two cannot drift apart quietly.

The bound matches PLAYERBOT_SOCIAL_BIOGRAPHY_MAX_FIELD_LENGTH on the C++ side. A field the
worldserver would refuse as FieldTooLong is refused here instead, where the request is still
identifiable.
"""
BIOGRAPHY_FIELD_NAMES = (
    "origin",
    "motivation",
    "formative_experience",
    "interests",
    "aversions",
    "preferred_topics",
    "mannerisms",
    "values",
)
MAX_BIOGRAPHY_FIELD_BYTES = 240

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

    schema_version: Literal[4]
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

    schema_version: Literal[4]
    token: str
    kind: Literal["social"]
    social_request_token: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_NAME_BYTES)]
    bot_human: Literal[0, 1]
    subject_guid: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    subject_name: Annotated[str, StringConstraints(max_length=MAX_ACTOR_NAME_BYTES)]
    subject_human: Literal[0, 1]
    admission_lane: SocialAdmissionLane
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

    Every assembled field is optional because an empty one is a legitimate answer rather than
    a gap: a reply has no starter, a bot meeting somebody for the first time has no
    relationship, and a fresh thread has no memories. The producer omits what it did not
    assemble instead of sending an empty value, so an absent key and an empty one never have
    to mean different things here. A context that does not parse as this shape is not an error
    either, it is just text, and the caller falls back to treating it as one opaque untrusted
    block rather than going silent.

    The two authority fields are the exception: `prompt_mode` and `active_expansion` are the
    worldserver's decision, required on every structured context. A context without them is a
    producer this sidecar does not know, so parsing fails and the caller keeps the ordinary
    prompt; no inferred or defaulted mode exists that could quietly become roleplay.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_mode: RoleplayPromptMode
    active_expansion: Annotated[int, Field(ge=0, le=MAX_SOCIAL_ACTIVE_EXPANSION)]
    persona: Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)] = ""
    fictional_identity_request: Literal["age", "home_country", "age_and_home_country"] | None = None
    fictional_age: Annotated[int, Field(ge=18, le=65)] | None = None
    fictional_home_country: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    relationship: Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)] = ""
    starter: Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)] = ""
    nearby: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ] = []
    thread: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ] = []
    memories: Annotated[list[SocialMemory], Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES)] = []

    @model_validator(mode="after")
    def authorized_roleplay_carries_no_fictional_identity(self) -> Self:
        """An in-character premise and ordinary fictional player facts never mix.

        The fictional age and home country exist to answer direct personal questions in the
        ordinary player voice. Inside an authorized roleplay premise the same fact would be
        reinterpreted as an Azeroth character's biography, so a context carrying both refuses
        outright: the caller falls back to the ordinary prompt rather than guessing which of
        the two framings the producer meant.
        """

        if self.prompt_mode == "authorized_roleplay" and (
            self.fictional_identity_request is not None
            or self.fictional_age is not None
            or self.fictional_home_country is not None
        ):
            raise ValueError("authorized roleplay carries no fictional player identity")

        return self

    @model_validator(mode="after")
    def fictional_identity_is_coherent(self) -> Self:
        """Facts are optional only when the matching request was withheld."""

        request = self.fictional_identity_request
        if request is None:
            if self.fictional_age is not None or self.fictional_home_country is not None:
                raise ValueError("fictional identity facts require a request marker")
            return self

        if self.fictional_age is not None and request not in {"age", "age_and_home_country"}:
            raise ValueError("fictional age does not match the request marker")

        if self.fictional_home_country is not None:
            if request not in {"home_country", "age_and_home_country"}:
                raise ValueError("fictional home country does not match the request marker")
            if self.fictional_home_country not in FICTIONAL_IDENTITY_COUNTRIES:
                raise ValueError("fictional home country is not in the approved roster")

        return self

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


RoleplayAssessmentKind = Literal[
    "ordinary",
    "roleplay_invitation",
    "roleplay_continuation",
    "practical",
    "opt_out",
    "uncertain",
]

RoleplayContentCapability = Literal[
    "classic_content",
    "outland",
    "blood_elf",
    "draenei",
    "death_knight",
    "burning_crusade_profession",
    "wrath_profession",
    "other_burning_crusade",
    "other_wrath",
    "unknown",
]

ROLEPLAY_ASSESSMENT_KINDS: tuple[str, ...] = get_args(RoleplayAssessmentKind)
ROLEPLAY_CONTENT_CAPABILITIES: tuple[str, ...] = get_args(RoleplayContentCapability)


class RoleplayAssessmentRequest(BaseModel):
    """One roleplay classification asked for by the worldserver coordinator.

    Deliberately blind: no candidates, no affinities, no GUIDs, no progression authority, and
    no prompt mode cross this seam. The classifier sees only the bounded conversation text the
    worldserver's privacy rules already admitted for prompting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4]
    token: str
    kind: Literal["roleplay_assessment"]
    roleplay_assessment_request_token: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    channel: Annotated[int, Field(ge=0, le=255)]
    thread_id: Annotated[str, StringConstraints(min_length=1, max_length=MAX_THREAD_ID_BYTES)]
    current_line: Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]
    thread_lines: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ] = []

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        return _validated_token(value)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: int) -> int:
        if value >= SOCIAL_CHANNEL_COUNT:
            raise ValueError("channel is not a known social channel")

        return value

    @field_validator("thread_id")
    @classmethod
    def _validate_thread_id(cls, value: str) -> str:
        if _byte_length(value) > MAX_THREAD_ID_BYTES:
            raise ValueError(f"thread_id must be at most {MAX_THREAD_ID_BYTES} UTF-8 bytes")

        return value

    @field_validator("current_line")
    @classmethod
    def _validate_current_line(cls, value: str) -> str:
        # StringConstraints counts characters; the frame budget is bytes.
        if _byte_length(value) > MAX_SOCIAL_CONTEXT_ENTRY_BYTES:
            raise ValueError(f"current_line must be at most {MAX_SOCIAL_CONTEXT_ENTRY_BYTES} UTF-8 bytes")

        return value

    @field_validator("thread_lines")
    @classmethod
    def _validate_thread_lines(cls, value: list[str]) -> list[str]:
        for line in value:
            if _byte_length(line) > MAX_SOCIAL_CONTEXT_ENTRY_BYTES:
                raise ValueError(
                    f"each thread line must be at most {MAX_SOCIAL_CONTEXT_ENTRY_BYTES} UTF-8 bytes"
                )

        return value


class RoleplayAssessmentCompletion(BaseModel):
    """Classifier output schema: evidence only.

    There is deliberately nowhere to put a correlation token, an expansion number, or any
    authority claim. The worldserver validates every reported capability against its own
    active progression policy; this model can only describe what the text asked for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    assessment_kind: RoleplayAssessmentKind
    capabilities: Annotated[
        list[RoleplayContentCapability], Field(max_length=len(ROLEPLAY_CONTENT_CAPABILITIES))
    ]

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        """The per kind cardinality contract, identical to the C++ side's."""

        if self.assessment_kind in {"ordinary", "practical", "opt_out"}:
            if self.capabilities:
                raise ValueError("this assessment kind carries no capabilities")
            return self

        if self.assessment_kind == "uncertain":
            if self.capabilities != ["unknown"]:
                raise ValueError("uncertain carries exactly the unknown capability")
            return self

        if not self.capabilities:
            raise ValueError("a roleplay premise names at least one capability")

        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")

        if "unknown" in self.capabilities:
            raise ValueError("unknown always refuses roleplay; report uncertain instead")

        if "classic_content" in self.capabilities and len(self.capabilities) > 1:
            raise ValueError("classic_content is valid only by itself")

        return self


def parse_roleplay_assessment_request(payload: bytes, expected_token: str) -> RoleplayAssessmentRequest:
    """Strict parser for a roleplay assessment request. Mirrors parse_social_request."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("roleplay assessment request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("roleplay assessment request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("roleplay assessment request payload is not a JSON object")

    try:
        request = RoleplayAssessmentRequest.model_validate(data)
    except ValidationError as error:
        raise ProtocolError(
            f"roleplay assessment request schema violation: {error.error_count()} error(s)"
        ) from error

    if not hmac.compare_digest(request.token.encode("utf-8"), expected_token.encode("utf-8")):
        raise TokenMismatchError("bridge token mismatch")

    return request


def encode_roleplay_assessment_response(
    roleplay_assessment_request_token: int,
    completion: RoleplayAssessmentCompletion,
    token: str,
) -> bytes:
    """Builds the exact flat payload shape the C++ assessment response parser accepts.

    The correlation fields come from the REQUEST and the authenticated bridge token, never
    from the completion: the model has nowhere to supply either, so it cannot answer a
    different question than the one it was asked.
    """

    if not 1 <= roleplay_assessment_request_token <= _UINT64_MAX:
        raise ProtocolError("roleplay_assessment_request_token out of range")

    token = _encoder_token(token)

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "kind": "roleplay_assessment",
        "roleplay_assessment_request_token": roleplay_assessment_request_token,
        "assessment_kind": completion.assessment_kind,
        "capability_count": len(completion.capabilities),
    }
    for index, capability in enumerate(completion.capabilities):
        payload[f"capability_{index}"] = capability

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class BiographyRequest(BaseModel):
    """A request for one bot's stable player-style social profile.

    Carries the identity rather than asking for it. Name, race, class and gender are
    authoritative character data, and `BiographyReply` deliberately has nowhere to put them, so
    they travel out here and are stamped back on afterwards. A generated value can then never
    become an identity, because the model is never asked for one.

    `biography_request_token` is the half of the staleness rule that the profile's own state
    cannot supply. Requiring the profile to still be Pending stops a completion replacing a
    biography that is already Ready, but it cannot say WHICH request a completion answers: after
    the pending timeout and a fresh request, a very late reply to the superseded call still finds
    the profile Pending. Only a token minted per request and echoed back closes that, so it is
    required here rather than optional.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4]
    token: str
    kind: Literal["biography"]
    biography_request_token: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    character_name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_NAME_BYTES)]
    race_id: Annotated[int, Field(ge=0, le=255)]
    class_id: Annotated[int, Field(ge=0, le=255)]
    gender_id: Annotated[int, Field(ge=0, le=255)]

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        return _validated_token(value)

    @field_validator("character_name")
    @classmethod
    def _validate_character_name(cls, value: str) -> str:
        # The same rule the social path applies, and for the same reason: this name reaches the
        # TRUSTED half of the prompt, because the bot has to be told who it is.
        if not actor_name_is_usable(value):
            raise ValueError("character name is not a usable character name")

        if _byte_length(value) > MAX_ACTOR_NAME_BYTES:
            raise ValueError(f"character name must be at most {MAX_ACTOR_NAME_BYTES} UTF-8 bytes")

        return value


def parse_biography_request(payload: bytes, expected_token: str) -> BiographyRequest:
    """Strict parser for a biography request. Mirrors parse_social_request field for field."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("biography request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("biography request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("biography request payload is not a JSON object")

    try:
        request = BiographyRequest.model_validate(data)
    except ValidationError as error:
        raise ProtocolError(f"biography request schema violation: {error.error_count()} error(s)") from error

    if not hmac.compare_digest(request.token.encode("utf-8"), expected_token.encode("utf-8")):
        raise TokenMismatchError("bridge token mismatch")

    return request


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


def encode_biography_response(
    biography_request_token: int,
    bot_guid: int,
    biography: dict[str, str],
    token: str,
) -> bytes:
    """Builds the exact payload shape the C++ biography response parser accepts.

    Carries only the generated fields. Identity is stamped on the worldserver from its own
    character tables, and a payload offering one would be refused by the assembler's whitelist,
    so the encoder never builds one.

    There is no failure variant. A generation that produces nothing returns silence, and the
    coordinator's own request timeout is what opens the retry: a failure frame would be a second
    way to reach the same transition, reachable by anything that can address the bridge.
    """

    if not 1 <= biography_request_token <= _UINT64_MAX:
        raise ProtocolError("biography_request_token out of range")

    if not 1 <= bot_guid <= _UINT64_MAX:
        raise ProtocolError("bot_guid out of range")

    token = _encoder_token(token)

    expected = set(BIOGRAPHY_FIELD_NAMES)
    if set(biography) != expected:
        # Named rather than tolerated in either direction. A missing field reaches the
        # worldserver as MissingRequiredField and burns a retry; an extra one is refused by the
        # whitelist. Both are cheaper to catch on this side, where the request id is still known.
        raise ProtocolError("biography must carry exactly the generated fields")

    for name, value in biography.items():
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError(f"biography field {name} is empty")

        if _byte_length(value) > MAX_BIOGRAPHY_FIELD_BYTES:
            raise ProtocolError(f"biography field {name} exceeds {MAX_BIOGRAPHY_FIELD_BYTES} UTF-8 bytes")

        if any(ord(character) < 0x20 for character in value):
            raise ProtocolError(f"biography field {name} must be a single line without control characters")

    # Flat, not nested under a "biography" object. The worldserver's reader is a strict parser for
    # ONE flat object and fails the parse on any nesting at all, deliberately: that narrowness is
    # most of what makes it safe to point at a payload from the network. Sending a nested document
    # would mean widening it for this one frame, which is a worse trade than eight more keys.
    # The generated names cannot collide with the protocol ones, and the exact-field check above is
    # what keeps that true.
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "kind": "biography",
        "biography_request_token": biography_request_token,
        "bot_guid": bot_guid,
    }
    payload.update(biography)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class MemorySubject(BaseModel):
    """One character a memory from this thread may be about.

    Sent explicitly rather than parsed out of the thread lines, because the coordinator has
    already filtered these for consent and presence, and a name recovered from a line is a value
    a PLAYER chose. Every guid here is one the worldserver is willing to store against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_NAME_BYTES)]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        # Same rule as every other actor name: this one is interpolated into the TRUSTED half of
        # the prompt so the model can attribute a memory, so length alone is not a bound.
        if _byte_length(value) > MAX_ACTOR_NAME_BYTES:
            raise ValueError(f"actor name must be at most {MAX_ACTOR_NAME_BYTES} UTF-8 bytes")

        if not actor_name_is_usable(value):
            raise ValueError("actor name is not a usable character name")

        return value


class MemoryRequest(BaseModel):
    """One idle conversation, offered for whatever is worth remembering about it.

    Nobody is waiting on this. It runs in the background lane beside a biography, because a
    memory extracted a second late costs nothing while a player watching for an answer is
    watching now.

    `scope` is deliberately narrower than the memory scopes the rest of the protocol carries: a
    whisper is never buffered on the worldserver, so a whisper scoped extraction cannot
    legitimately exist, and one arriving here means the producer stopped honouring that. The
    schema refuses it rather than trusting the far side, because being wrong means private
    messages inside a provider request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4]
    token: str
    kind: Literal["memory"]
    memory_request_token: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_guid: Annotated[int, Field(ge=1, le=_UINT64_MAX)]
    bot_name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ACTOR_NAME_BYTES)]
    thread_id: Annotated[str, StringConstraints(min_length=1, max_length=MAX_THREAD_ID_BYTES)]
    scope: Literal["public", "party"]
    subjects: Annotated[list[MemorySubject], Field(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRIES)]

    # The same two bounds the worldserver's buffer enforces. A request past either did not come
    # from a buffer applying them, so accepting it would let one producer bug become an
    # unbounded prompt.
    thread: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRY_BYTES)]],
        Field(min_length=1, max_length=MAX_SOCIAL_CONTEXT_ENTRIES),
    ]

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        return _validated_token(value)

    @field_validator("bot_name")
    @classmethod
    def _validate_bot_name(cls, value: str) -> str:
        if _byte_length(value) > MAX_ACTOR_NAME_BYTES:
            raise ValueError(f"actor name must be at most {MAX_ACTOR_NAME_BYTES} UTF-8 bytes")

        if not actor_name_is_usable(value):
            raise ValueError("actor name is not a usable character name")

        return value

    @field_validator("thread_id")
    @classmethod
    def _validate_thread_id(cls, value: str) -> str:
        if _byte_length(value) > MAX_THREAD_ID_BYTES:
            raise ValueError(f"thread_id must be at most {MAX_THREAD_ID_BYTES} UTF-8 bytes")

        return value

    @field_validator("thread")
    @classmethod
    def _validate_thread(cls, value: list[str]) -> list[str]:
        # Characters versus bytes again, per entry and in total. The per entry declaration above
        # is the cheap first cut; a multibyte thread passes it and still overflows the frame.
        if any(_byte_length(line) > MAX_SOCIAL_CONTEXT_ENTRY_BYTES for line in value):
            raise ValueError(f"a thread line must be at most {MAX_SOCIAL_CONTEXT_ENTRY_BYTES} UTF-8 bytes")

        if sum(_byte_length(line) for line in value) > MAX_SOCIAL_CONTEXT_BYTES:
            raise ValueError(f"the thread must be at most {MAX_SOCIAL_CONTEXT_BYTES} UTF-8 bytes")

        return value

    @model_validator(mode="after")
    def _subjects_are_distinct(self) -> Self:
        guids = [subject.guid for subject in self.subjects]
        if len(guids) != len(set(guids)):
            raise ValueError("memory subjects must be distinct")

        return self

    @property
    def subject_guids(self) -> tuple[int, ...]:
        return tuple(subject.guid for subject in self.subjects)


def parse_memory_request(payload: bytes, expected_token: str) -> MemoryRequest:
    """Strict parser for a memory extraction request. Mirrors parse_social_request field for field."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("memory request payload is not valid UTF-8") from error

    try:
        data = json.loads(text, object_pairs_hook=_object_with_unique_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("memory request payload is not valid JSON") from error

    if not isinstance(data, dict):
        raise ProtocolError("memory request payload is not a JSON object")

    try:
        request = MemoryRequest.model_validate(data)
    except ValidationError as error:
        raise ProtocolError(f"memory request schema violation: {error.error_count()} error(s)") from error

    if not hmac.compare_digest(request.token.encode("utf-8"), expected_token.encode("utf-8")):
        raise TokenMismatchError("bridge token mismatch")

    return request


def encode_memory_response(
    memory_request_token: int,
    bot_guid: int,
    thread_id: str,
    candidates: list[dict[str, object]],
    token: str,
) -> bytes:
    """Builds the payload the C++ memory reply parser accepts.

    An empty candidate list is a normal answer and is encoded like any other. Most conversations
    are not worth remembering, and the coordinator still needs a reply to close the request out;
    treating "nothing found" as silence would leave it waiting for its own timeout instead.

    The thread identity travels back so the coordinator can check the conversation is still the
    one it asked about. A thread pruned while this was in flight makes the answer stale rather
    than wrong, and that is its to decide, not this encoder's.
    """

    if not 1 <= memory_request_token <= _UINT64_MAX:
        raise ProtocolError("memory_request_token out of range")

    if not 1 <= bot_guid <= _UINT64_MAX:
        raise ProtocolError("bot_guid out of range")

    if not thread_id or _byte_length(thread_id) > MAX_THREAD_ID_BYTES:
        raise ProtocolError("thread_id out of range")

    if len(candidates) > MAX_EXTRACTED_MEMORIES:
        raise ProtocolError("memory reply carries more memories than one conversation supports")

    token = _encoder_token(token)

    for candidate in candidates:
        paraphrase = candidate.get("paraphrase")
        if not isinstance(paraphrase, str) or not paraphrase.strip():
            raise ProtocolError("memory candidate is empty")

        if _byte_length(paraphrase) > MAX_SOCIAL_CONTEXT_ENTRY_BYTES:
            raise ProtocolError(f"memory candidate exceeds {MAX_SOCIAL_CONTEXT_ENTRY_BYTES} UTF-8 bytes")

        about_guid = candidate.get("about_guid")
        # bool is a subclass of int, so True would otherwise pass as guid 1.
        if not isinstance(about_guid, int) or isinstance(about_guid, bool):
            raise ProtocolError("memory candidate names no subject")

        if not 1 <= about_guid <= _UINT64_MAX:
            raise ProtocolError("memory candidate subject out of range")

        if candidate.get("scope") not in SOCIAL_MEMORY_SCOPES:
            raise ProtocolError("memory candidate carries an unknown scope")

    # Flat, not a nested "candidates" array, for the reason encode_biography_response gives: the
    # worldserver's reader is a strict parser for ONE flat object and fails the parse on any
    # nesting at all. That narrowness is most of what makes it safe to point at a payload from the
    # network, and widening it for this one frame is a worse trade than three keys per memory.
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "kind": "memory",
        "memory_request_token": memory_request_token,
        "bot_guid": bot_guid,
        "thread_id": thread_id,
        "memory_count": len(candidates),
    }
    for index, candidate in enumerate(candidates):
        payload[f"memory_{index}_paraphrase"] = candidate["paraphrase"]
        payload[f"memory_{index}_about_guid"] = candidate["about_guid"]
        payload[f"memory_{index}_scope"] = candidate["scope"]

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
