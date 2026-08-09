"""Provider-neutral generation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from playerbot_llm import protocol


@dataclass(frozen=True)
class GenerationProviderMetadata:
    """Provider identity and the request limits enforced by the service."""

    name: str
    model: str
    max_input_tokens: int
    output_token_limits: Mapping[str, int]

    def max_output_tokens(self, operation: str) -> int:
        try:
            return self.output_token_limits[operation]
        except KeyError as error:
            raise ValueError(f"generation provider has no output limit for {operation}") from error


@dataclass(frozen=True)
class GenerationUsage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def is_priceable(self) -> bool:
        return (
            self.input_tokens >= 0
            and self.output_tokens >= 0
            and self.cache_creation_input_tokens >= 0
            and self.cache_read_input_tokens >= 0
        )


@dataclass(frozen=True)
class SocialGenerationResult:
    """One validated social proposal plus the provider usage that produced it."""

    message: str
    emote_id: int
    contribution: str
    claim_subject: str
    cited_evidence_ids: tuple[str, ...]
    usage: GenerationUsage


class GenerationBillingStatus(StrEnum):
    KNOWN = "known"
    IMPOSSIBLE = "impossible"
    INDETERMINATE = "indeterminate"


class GenerationError(Exception):
    """Base class for bounded generation failures."""

    billing_status = GenerationBillingStatus.INDETERMINATE
    retryable = False
    usage: GenerationUsage | None = None


class GenerationTimeoutError(GenerationError):
    retryable = True


class GenerationAuthError(GenerationError):
    billing_status = GenerationBillingStatus.IMPOSSIBLE


class GenerationRateLimitError(GenerationError):
    billing_status = GenerationBillingStatus.IMPOSSIBLE
    retryable = True


class GenerationProviderError(GenerationError):
    retryable = True


class GenerationInvalidOutputError(GenerationError):
    """The provider answered, but the answer is unusable."""

    def __init__(
        self,
        message: str,
        usage: GenerationUsage | None = None,
        category: object | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.category = category
        self.billing_status = (
            GenerationBillingStatus.KNOWN
            if usage is not None and usage.is_priceable
            else GenerationBillingStatus.INDETERMINATE
        )


class GenerationProvider(Protocol):
    """All generation operations consumed by the sidecar service."""

    metadata: GenerationProviderMetadata

    @property
    def configured(self) -> bool: ...

    def count_input_tokens(self, request: protocol.ChatRequest, history: list[tuple[str, str]]) -> int: ...

    def generate_reply(
        self, request: protocol.ChatRequest, history: list[tuple[str, str]]
    ) -> tuple[str, GenerationUsage]: ...

    def count_social_input_tokens(self, request: protocol.SocialRequest) -> int: ...

    def generate_social_reply(self, request: protocol.SocialRequest) -> SocialGenerationResult: ...

    def count_roleplay_assessment_input_tokens(self, request: protocol.RoleplayAssessmentRequest) -> int: ...

    def assess_roleplay(
        self, request: protocol.RoleplayAssessmentRequest
    ) -> tuple[protocol.RoleplayAssessmentCompletion, GenerationUsage]: ...

    def count_biography_input_tokens(self, request: protocol.BiographyRequest) -> int: ...

    def generate_biography(
        self, request: protocol.BiographyRequest
    ) -> tuple[dict[str, str], GenerationUsage]: ...

    def count_memory_input_tokens(self, request: protocol.MemoryRequest) -> int: ...

    def generate_memories(
        self, request: protocol.MemoryRequest
    ) -> tuple[list[dict[str, object]], GenerationUsage]: ...
