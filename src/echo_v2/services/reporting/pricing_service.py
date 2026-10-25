"""PricingService — estimates USD cost from measured AI usage.

Loads a versioned pricing configuration (YAML) and calculates estimated
cost for a single billable AI call. The configuration is the source of
truth for rates; each :class:`AiUsageEvent` stores the ``price_version``
active at event time, so historical reports don't change when rates are
updated.

Cost source semantics (see ``echo_v2.domain.ai_usage_event``):

* ``measured_usage_priced`` — provider-reported tokens × YAML rates.
  The normal case for OpenAI/Gemini token-based calls.
* ``fallback_estimate`` — derived from a configurable fallback
  assumption (e.g. Modal ``usd_per_audio_minute`` × audio duration).
* ``unknown`` — no trustworthy usage; returns ``(None, "unknown")``.

``provider_reported`` (provider-supplied dollars) is not produced here —
it is reserved for future reconciliation with provider invoices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from echo_v2.domain.ai_usage_event import (
    AiCostSource,
    AiUsageProvider,
)

__all__ = ["PricingConfig", "PricingService"]

_logger = logging.getLogger("echo_v2.services.reporting.pricing_service")

_DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parent.parent.parent / "reporting" / "pricing.yaml"
)


@dataclass(frozen=True)
class _TokenRates:
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal


@dataclass(frozen=True)
class PricingConfig:
    """Parsed pricing configuration."""

    version: str
    openai: dict[str, _TokenRates]
    google: dict[str, _TokenRates]
    modal_gpu_type: str
    modal_gpu_second: dict[str, Decimal]
    modal_cpu_core_second: Decimal
    modal_memory_gib_second: Decimal
    modal_audio_usd_per_minute: Decimal
    modal_audio_include_overhead: bool
    fallback_image_usd_per_image: Decimal
    fallback_analyzer_input_tokens_per_call: int
    fallback_analyzer_output_tokens_per_call: int
    fallback_gemini_input_tokens_per_call: int
    fallback_gemini_output_tokens_per_call: int


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _parse_token_rates(section: dict[str, Any]) -> dict[str, _TokenRates]:
    parsed: dict[str, _TokenRates] = {}
    for model, rates in section.items():
        parsed[model] = _TokenRates(
            input_per_million=_to_decimal(rates["input_per_million"]),
            cached_input_per_million=_to_decimal(rates["cached_input_per_million"]),
            output_per_million=_to_decimal(rates["output_per_million"]),
        )
    return parsed


def load_pricing_config(path: Path | str = _DEFAULT_PRICING_PATH) -> PricingConfig:
    """Load and parse the pricing YAML at ``path``."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return _parse_config(raw, source=str(path))


def _parse_config(raw: dict[str, Any], *, source: str) -> PricingConfig:
    try:
        version = str(raw["version"])
        openai = _parse_token_rates(raw["openai"])
        google = _parse_token_rates(raw["google"])
        modal = raw["modal"]
        modal_gpu = {
            gpu: _to_decimal(rate)
            for gpu, rate in modal["gpu_second_usd"].items()
        }
        audio_cost = modal["audio_cost"]
        fallbacks = raw["estimation_fallbacks"]
        return PricingConfig(
            version=version,
            openai=openai,
            google=google,
            modal_gpu_type=str(modal.get("gpu_type", "T4")),
            modal_gpu_second=modal_gpu,
            modal_cpu_core_second=_to_decimal(modal["cpu_core_second"]),
            modal_memory_gib_second=_to_decimal(modal["memory_gib_second"]),
            modal_audio_usd_per_minute=_to_decimal(
                audio_cost["estimated_usd_per_minute"]
            ),
            modal_audio_include_overhead=bool(
                audio_cost.get("include_container_overhead", True)
            ),
            fallback_image_usd_per_image=_to_decimal(
                fallbacks["image"]["usd_per_image"]
            ),
            fallback_analyzer_input_tokens_per_call=int(
                fallbacks["analyzer"]["input_tokens_per_call"]
            ),
            fallback_analyzer_output_tokens_per_call=int(
                fallbacks["analyzer"]["output_tokens_per_call"]
            ),
            fallback_gemini_input_tokens_per_call=int(
                fallbacks["gemini"]["input_tokens_per_call"]
            ),
            fallback_gemini_output_tokens_per_call=int(
                fallbacks["gemini"]["output_tokens_per_call"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid pricing configuration at {source}: {exc}") from exc


class PricingService:
    """Estimates USD cost from measured AI usage using a versioned config.

    Args:
        config: The parsed pricing configuration. When ``None``, loads the
            default bundled YAML.
    """

    def __init__(self, config: PricingConfig | None = None) -> None:
        self._config = config or self._default_config()

    @staticmethod
    @lru_cache(maxsize=4)
    def _default_config() -> PricingConfig:
        return load_pricing_config(_DEFAULT_PRICING_PATH)

    def version(self) -> str:
        return self._config.version

    def estimate_token_cost(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cached_tokens: int | None = None,
    ) -> tuple[Decimal | None, str]:
        """Estimate cost for a token-based OpenAI/Gemini call.

        Returns ``(cost, cost_source)``. When token counts are present,
        cost is calculated from the model's rates and ``cost_source`` is
        ``measured_usage_priced``. When all token counts are missing, the
        fallback per-call token assumptions are used (still
        ``measured_usage_priced``). When the model is unknown, the
        provider's ``default`` rates are used. When even the provider has
        no rates, returns ``(None, "unknown")``.
        """
        rates = self._token_rates(provider, model)
        if rates is None:
            return None, AiCostSource.UNKNOWN

        prompt = prompt_tokens or 0
        completion = completion_tokens or 0
        cached = cached_tokens or 0

        if prompt == 0 and completion == 0 and cached == 0:
            # No usage reported — use fallback per-call assumptions.
            fallback_in, fallback_out = self._fallback_token_assumptions(provider)
            if fallback_in == 0 and fallback_out == 0:
                return None, AiCostSource.UNKNOWN
            prompt = fallback_in
            completion = fallback_out
            cached = 0
            cost_source = AiCostSource.FALLBACK_ESTIMATE
        else:
            cost_source = AiCostSource.MEASURED_USAGE_PRICED

        # Non-cached input = prompt - cached (clamped at 0).
        non_cached_input = max(0, prompt - cached)
        cost = (
            (Decimal(non_cached_input) * rates.input_per_million / Decimal(1_000_000))
            + (Decimal(cached) * rates.cached_input_per_million / Decimal(1_000_000))
            + (Decimal(completion) * rates.output_per_million / Decimal(1_000_000))
        )
        return cost, cost_source

    def estimate_audio_cost(
        self,
        *,
        audio_duration_seconds: float | None,
        processing_seconds: float | None = None,
    ) -> tuple[Decimal | None, str]:
        """Estimate Modal voice cost from audio duration.

        Uses the configurable ``modal.audio_cost.estimated_usd_per_minute``
        applied to ``audio_duration_seconds``. This effective rate absorbs
        container startup, warm-idle overhead, and GPU/CPU/memory into a
        single per-minute figure calibrated from actual Modal billing
        reports. ``processing_seconds`` is stored on the event but is NOT
        priced separately — that would double-count the overhead already
        baked into the per-minute rate. GPU-second metering is deferred
        until Modal runtime billing is verified.
        """
        if audio_duration_seconds is None or audio_duration_seconds <= 0:
            return None, AiCostSource.UNKNOWN
        minutes = Decimal(str(audio_duration_seconds)) / Decimal(60)
        cost = minutes * self._config.modal_audio_usd_per_minute
        return cost, AiCostSource.FALLBACK_ESTIMATE

    def estimate_image_cost(
        self,
        *,
        image_count: int | None,
    ) -> tuple[Decimal | None, str]:
        """Estimate image cost from a per-image fallback rate."""
        if image_count is None or image_count <= 0:
            return None, AiCostSource.UNKNOWN
        cost = Decimal(image_count) * self._config.fallback_image_usd_per_image
        return cost, AiCostSource.FALLBACK_ESTIMATE

    def _token_rates(self, provider: str, model: str) -> _TokenRates | None:
        section: dict[str, _TokenRates]
        if provider == AiUsageProvider.OPENAI:
            section = self._config.openai
        elif provider == AiUsageProvider.GOOGLE:
            section = self._config.google
        else:
            return None
        return section.get(model) or section.get("default")

    def _fallback_token_assumptions(self, provider: str) -> tuple[int, int]:
        if provider == AiUsageProvider.OPENAI:
            return (
                self._config.fallback_analyzer_input_tokens_per_call,
                self._config.fallback_analyzer_output_tokens_per_call,
            )
        if provider == AiUsageProvider.GOOGLE:
            return (
                self._config.fallback_gemini_input_tokens_per_call,
                self._config.fallback_gemini_output_tokens_per_call,
            )
        return 0, 0
