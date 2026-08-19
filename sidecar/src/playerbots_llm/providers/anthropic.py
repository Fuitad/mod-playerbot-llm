"""Anthropic implementation of the provider-neutral generation contract."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TypeVar, cast

import anthropic
import httpx
from anthropic.lib._parse._response import parse_response
from anthropic.lib._parse._transform import transform_schema
from anthropic.types import MessageParam as AnthropicMessageParam
from anthropic.types import ParsedMessage
from pydantic import BaseModel, ValidationError

from playerbots_llm import protocol, provider
from playerbots_llm.generation import (
    BiographyReply,
    CareerReply,
    ChatReply,
    MemoryReply,
    MessageParam,
    SocialReply,
    _biography_messages,
    _build_messages,
    _memory_messages,
    _roleplay_assessment_messages,
    _social_messages,
    _validate_career_reply,
    biography_fields_for_transport,
    build_biography_system_prompt,
    build_memory_system_prompt,
    build_roleplay_assessment_system_prompt,
    build_social_system_prompt,
    build_system_prompt,
    validate_memory_reply,
    validate_social_reply,
)

API_KEY_ENV_VAR = "MOD_PLAYERBOTS_LLM_ANTHROPIC_API_KEY"
MODEL_ID = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 96
BIOGRAPHY_MAX_OUTPUT_TOKENS = 512
MAX_INPUT_TOKENS = 4095
REQUEST_TIMEOUT_SECONDS = 30.0

_ResponseModelT = TypeVar("_ResponseModelT", bound=BaseModel)


def _messages_for_anthropic(messages: list[MessageParam]) -> list[AnthropicMessageParam]:
    return cast(list[AnthropicMessageParam], messages)


class AnthropicProvider:
    """Synchronous Anthropic SDK wrapper with bounded, typed failures."""

    metadata = provider.GenerationProviderMetadata(
        name="anthropic",
        model=MODEL_ID,
        max_input_tokens=MAX_INPUT_TOKENS,
        output_token_limits={
            "chat": MAX_OUTPUT_TOKENS,
            "career": MAX_OUTPUT_TOKENS,
            "social": MAX_OUTPUT_TOKENS,
            "biography": BIOGRAPHY_MAX_OUTPUT_TOKENS,
            "memory": MAX_OUTPUT_TOKENS,
            "roleplay_assessment": MAX_OUTPUT_TOKENS,
        },
    )

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        model_io_logger: Callable[[str], None] | None = None,
    ) -> None:
        # api_key is always passed explicitly: an empty value fails authentication
        # instead of silently falling back to the SDK's ANTHROPIC_API_KEY lookup.
        self._client = client or anthropic.Anthropic(
            api_key=os.environ.get(API_KEY_ENV_VAR, ""),
            timeout=timeout_seconds,
            max_retries=1,
        )
        self._model_io_logger = model_io_logger

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(API_KEY_ENV_VAR))

    def _trace_request(
        self,
        kind: str,
        correlation_id: int,
        system: str,
        messages: list[MessageParam],
        output_format: type[BaseModel],
        max_tokens: int,
        trace_content: bool = True,
    ) -> None:
        if self._model_io_logger is None:
            return

        if not trace_content:
            # The call is still on the record - what was said is not. Used for whisper scoped
            # extractions, whose prompt carries private player text that must never reach a
            # durable log, whatever the operator's diagnostic setting.
            self._model_io_logger(
                json.dumps(
                    {
                        "phase": "request",
                        "kind": kind,
                        "correlation_id": correlation_id,
                        "model": MODEL_ID,
                        "max_tokens": max_tokens,
                        "redacted": True,
                        "message_count": len(messages),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return

        self._model_io_logger(
            json.dumps(
                {
                    "phase": "request",
                    "kind": kind,
                    "correlation_id": correlation_id,
                    "model": MODEL_ID,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": messages,
                    "output_schema": output_format.model_json_schema(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def _trace_response(
        self, kind: str, correlation_id: int, response: BaseModel, trace_content: bool = True
    ) -> None:
        if self._model_io_logger is None:
            return

        if not trace_content:
            # A whisper extraction's reply paraphrases the same private text its prompt carried.
            self._model_io_logger(
                json.dumps(
                    {
                        "phase": "response",
                        "kind": kind,
                        "correlation_id": correlation_id,
                        "redacted": True,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return

        provider_message = response.model_dump(
            mode="json",
            exclude={"parsed_output": True, "content": {"__all__": {"parsed_output"}}},
            warnings=False,
        )
        self._model_io_logger(
            json.dumps(
                {
                    "phase": "response",
                    "kind": kind,
                    "correlation_id": correlation_id,
                    "provider_message": provider_message,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def _generate_structured(
        self,
        kind: str,
        correlation_id: int,
        system: str,
        messages: list[MessageParam],
        output_format: type[_ResponseModelT],
        max_tokens: int,
        trace_content: bool = True,
    ) -> ParsedMessage[_ResponseModelT]:
        self._trace_request(kind, correlation_id, system, messages, output_format, max_tokens, trace_content)
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=max_tokens,
            system=system,
            messages=_messages_for_anthropic(messages),
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": transform_schema(output_format),
                }
            },
        )
        self._trace_response(kind, correlation_id, response, trace_content)
        return cast(
            ParsedMessage[_ResponseModelT],
            parse_response(response=response, output_format=output_format),
        )

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int:
        output_format = CareerReply if request.is_career else ChatReply
        try:
            # output_format must match generate_reply exactly: the structured output
            # schema is billed as input, and the budget reservation is priced from
            # this count.
            result = self._client.messages.count_tokens(
                model=MODEL_ID,
                system=build_system_prompt(request),
                messages=_messages_for_anthropic(_build_messages(request, history)),
                output_format=output_format,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error

        return result.input_tokens

    def count_social_input_tokens(self, request: protocol.SocialRequest) -> int:
        try:
            # Must match generate_social_reply exactly, for the same reason the chat count
            # does: the structured output schema is billed as input and priced from here.
            result = self._client.messages.count_tokens(
                model=MODEL_ID,
                system=build_social_system_prompt(request),
                messages=_messages_for_anthropic(_social_messages(request)),
                output_format=SocialReply,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error

        return result.input_tokens

    def generate_social_reply(self, request: protocol.SocialRequest) -> provider.SocialGenerationResult:
        system = build_social_system_prompt(request)
        messages = _social_messages(request)
        try:
            response = self._generate_structured(
                "social", request.social_request_token, system, messages, SocialReply, MAX_OUTPUT_TOKENS
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error
        except ValidationError as error:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the social reply schema"
            ) from error

        # Read BEFORE validating, for the same reason as the chat path: everything below
        # rejects a completion that was already generated and billed, and the caller has to
        # settle the real cost of it.
        usage = response.usage
        totals = provider.GenerationUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )

        if not totals.is_priceable:
            raise provider.GenerationInvalidOutputError("provider reported impossible token counts")

        parsed = response.parsed_output
        if parsed is None or not isinstance(parsed, SocialReply):
            raise provider.GenerationInvalidOutputError(
                "model output did not match the social reply schema", totals
            )

        message, emote_id = validate_social_reply(parsed, request, totals)
        return provider.SocialGenerationResult(
            message=message,
            emote_id=emote_id,
            contribution=parsed.contribution,
            claim_subject=parsed.claim_subject,
            cited_evidence_ids=tuple(parsed.cited_evidence_ids),
            cited_memory_ids=tuple(parsed.cited_memory_ids),
            usage=totals,
        )

    def count_roleplay_assessment_input_tokens(self, request: protocol.RoleplayAssessmentRequest) -> int:
        try:
            # Must match assess_roleplay exactly: the structured output schema is billed as
            # input and priced from here.
            result = self._client.messages.count_tokens(
                model=MODEL_ID,
                system=build_roleplay_assessment_system_prompt(request),
                messages=_messages_for_anthropic(_roleplay_assessment_messages(request)),
                output_format=protocol.RoleplayAssessmentCompletion,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error

        return result.input_tokens

    def assess_roleplay(
        self, request: protocol.RoleplayAssessmentRequest
    ) -> tuple[protocol.RoleplayAssessmentCompletion, provider.GenerationUsage]:
        """Classifies one observed line, or raises. Never guesses from partial text.

        The completion is the strict protocol schema directly, so its per kind cardinality
        contract is enforced by parsing: output that does not satisfy it is a
        provider.GenerationInvalidOutputError, which the service answers with silence rather than with a
        fabricated ordinary result.
        """

        system = build_roleplay_assessment_system_prompt(request)
        messages = _roleplay_assessment_messages(request)
        try:
            response = self._generate_structured(
                "roleplay_assessment",
                request.roleplay_assessment_request_token,
                system,
                messages,
                protocol.RoleplayAssessmentCompletion,
                MAX_OUTPUT_TOKENS,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error
        except ValidationError as error:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the roleplay assessment schema"
            ) from error

        # Read BEFORE validating, so a rejected completion still settles what it cost.
        usage = response.usage
        totals = provider.GenerationUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )

        if not totals.is_priceable:
            raise provider.GenerationInvalidOutputError("provider reported impossible token counts")

        parsed = response.parsed_output
        if parsed is None or not isinstance(parsed, protocol.RoleplayAssessmentCompletion):
            raise provider.GenerationInvalidOutputError(
                "model output did not match the roleplay assessment schema", totals
            )

        return parsed, totals

    def count_biography_input_tokens(self, request: protocol.BiographyRequest) -> int:
        try:
            # Must match generate_biography exactly, for the same reason the other two counts
            # do: the structured output schema is billed as input and priced from here.
            result = self._client.messages.count_tokens(
                model=MODEL_ID,
                system=build_biography_system_prompt(request),
                messages=_messages_for_anthropic(_biography_messages(request)),
                output_format=BiographyReply,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error

        return result.input_tokens

    def generate_biography(
        self, request: protocol.BiographyRequest
    ) -> tuple[dict[str, str], provider.GenerationUsage]:
        system = build_biography_system_prompt(request)
        messages = _biography_messages(request)
        try:
            response = self._generate_structured(
                "biography",
                request.biography_request_token,
                system,
                messages,
                BiographyReply,
                BIOGRAPHY_MAX_OUTPUT_TOKENS,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error
        except ValidationError as error:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the biography schema"
            ) from error

        # Read BEFORE validating, for the reason the other two generators give: everything below
        # rejects a completion that was already generated and billed.
        usage = response.usage
        totals = provider.GenerationUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )

        if not totals.is_priceable:
            raise provider.GenerationInvalidOutputError("provider reported impossible token counts")

        parsed = response.parsed_output
        if parsed is None or not isinstance(parsed, BiographyReply):
            raise provider.GenerationInvalidOutputError(
                "model output did not match the biography schema", totals
            )

        return biography_fields_for_transport(parsed, request, totals), totals

    def count_memory_input_tokens(self, request: protocol.MemoryRequest) -> int:
        try:
            # Must match generate_memories exactly. The structured output schema is billed as
            # input, so a count taken without it under-prices the reservation.
            result = self._client.messages.count_tokens(
                model=MODEL_ID,
                system=build_memory_system_prompt(request),
                messages=_messages_for_anthropic(_memory_messages(request)),
                output_format=MemoryReply,
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error

        return result.input_tokens

    def generate_memories(
        self, request: protocol.MemoryRequest
    ) -> tuple[list[dict[str, object]], provider.GenerationUsage]:
        system = build_memory_system_prompt(request)
        messages = _memory_messages(request)
        try:
            response = self._generate_structured(
                "memory",
                request.memory_request_token,
                system,
                messages,
                MemoryReply,
                MAX_OUTPUT_TOKENS,
                # A whisper extraction's prompt and reply both carry private player text; the
                # model-IO diagnostic keeps the call on the record without keeping the words.
                trace_content=request.scope != "whisper",
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error
        except ValidationError as error:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the memory schema"
            ) from error

        # Read BEFORE validating. Everything below rejects a completion that was already
        # generated and billed, and the caller has to settle it either way.
        usage = response.usage
        totals = provider.GenerationUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )

        if not totals.is_priceable:
            raise provider.GenerationInvalidOutputError("provider reported impossible token counts")

        parsed = response.parsed_output
        if parsed is None or not isinstance(parsed, MemoryReply):
            raise provider.GenerationInvalidOutputError(
                "model output did not match the memory schema", totals
            )

        accepted = validate_memory_reply(parsed, request, totals)
        return accepted, totals

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, provider.GenerationUsage]:
        output_format = CareerReply if request.is_career else ChatReply
        kind = "career" if request.is_career else "chat"
        system = build_system_prompt(request)
        messages = _build_messages(request, history)
        try:
            response = self._generate_structured(
                kind, request.request_id, system, messages, output_format, MAX_OUTPUT_TOKENS
            )
        except anthropic.APIError as error:
            raise _map_api_error(error) from error
        except httpx.TimeoutException as error:
            raise provider.GenerationTimeoutError(str(error)) from error
        except ValidationError as error:
            raise provider.GenerationInvalidOutputError(
                "model output did not match the reply schema"
            ) from error

        # Read BEFORE validating the content. Everything below this line rejects a
        # completion that was generated and billed, and the caller has to settle the real
        # cost of it; discovering the tokens after deciding to raise is how that charge
        # goes missing.
        usage = response.usage
        totals = provider.GenerationUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )

        if not totals.is_priceable:
            # Impossible counts are not a cost the caller can settle, so they are not
            # handed on as one. Raised WITHOUT usage, which puts this in the same lane as
            # a timeout: the reservation stays outstanding at its maximum for expiry
            # rather than being charged a number that cannot be right or released as free.
            raise provider.GenerationInvalidOutputError("provider reported impossible token counts")

        parsed = response.parsed_output
        if parsed is None:
            raise provider.GenerationInvalidOutputError("model output did not match the reply schema", totals)

        if request.is_career:
            if not isinstance(parsed, CareerReply):
                raise provider.GenerationInvalidOutputError(
                    "model output did not match the career reply schema", totals
                )
            message = _validate_career_reply(request, parsed, totals)
        else:
            if not isinstance(parsed, ChatReply):
                raise provider.GenerationInvalidOutputError(
                    "model output did not match the chat reply schema", totals
                )
            message = parsed.message.strip()
        if not message:
            raise provider.GenerationInvalidOutputError("model returned an empty message", totals)

        if any(ord(character) < 0x20 for character in message):
            raise provider.GenerationInvalidOutputError("model message must be a single line", totals)

        if len(message.encode("utf-8")) > protocol.MAX_RESPONSE_MESSAGE_BYTES:
            raise provider.GenerationInvalidOutputError("model message exceeds 240 UTF-8 bytes", totals)

        return message, totals


def _map_api_error(error: anthropic.APIError) -> provider.GenerationError:
    if isinstance(error, anthropic.APITimeoutError):
        return provider.GenerationTimeoutError(str(error))
    if isinstance(error, anthropic.AuthenticationError):
        return provider.GenerationAuthError("authentication with the Anthropic API failed")
    if isinstance(error, anthropic.RateLimitError):
        return provider.GenerationRateLimitError("the Anthropic API rate limit was hit")

    return provider.GenerationProviderError(f"provider error: {type(error).__name__}")
