"""Provider-neutral contract for AI edit-plan generation."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """A safe provider failure that may be shown in the CRE UI."""


@dataclass(frozen=True)
class GenerationResult:
    plan: dict[str, Any]
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)
    response_id: str | None = None


@dataclass(frozen=True)
class SpecialistResult:
    output: dict[str, Any]
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int | None] = field(default_factory=dict)
    response_id: str | None = None


class BrainProvider(ABC):
    name: str

    @abstractmethod
    def generate_plan(
        self,
        *,
        schema: dict[str, Any],
        profile: dict[str, Any],
        source_analysis: dict[str, Any],
        asset_registry: dict[str, Any],
        renderer_capabilities: dict[str, Any],
        correction: dict[str, Any] | None = None,
    ) -> GenerationResult:
        """Return untrusted planning JSON (editorial intent when requested).

        CRE resolves intent and validates the compiled Edit Plan separately.
        GenerationResult.plan is never direct renderer authorization.
        """

    @abstractmethod
    def run_specialist(
        self,
        *,
        bot_id: str,
        instructions: str,
        input_data: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> SpecialistResult:
        """Return an untrusted, schema-bounded specialist proposal."""
