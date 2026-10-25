"""Billable AI provider call — the unit of the cost ledger.

One ``AiUsageEvent`` corresponds to one *actual* provider call (an OpenAI
``chat.completions.parse``, a Gemini ``generate_content``, a Modal HTTP
POST). A provider retry, a manual reanalysis, or a duplicate analysis of
the same chat each produces a fresh event with its own ``invocation_id``.

``invocation_id`` is the idempotency key for persistence: a recorder that
fails to persist and retries uses the same id, so ``ON CONFLICT DO NOTHING``
guarantees one row per billable attempt.

``cost_source`` distinguishes how ``estimated_cost_usd`` was derived:

* ``provider_reported``    — a monetary amount supplied by the provider
  (e.g. an invoice line). Reserved for provider-supplied dollars, not for
  token counts we price ourselves.
* ``measured_usage_priced`` — usage units (tokens, GPU seconds) reported by
  the provider, multiplied by our YAML rates. The normal case for
  OpenAI/Gemini token-based calls.
* ``fallback_estimate``    — cost derived from a configurable fallback
  assumption (e.g. Modal ``usd_per_audio_minute`` × audio duration) because
  no direct usage metering is available.
* ``unknown``              — no trustworthy usage information; cost is
  ``None``, never auto-zero.

Underlying measurements (token counts, ``audio_duration_seconds``,
``processing_seconds``) are stored even when cost is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID


class AiUsageProvider:
    OPENAI = "openai"
    GOOGLE = "google"
    MODAL = "modal"


class AiUsageType:
    TOKENS = "tokens"
    GPU_SECONDS = "gpu_seconds"
    AUDIO_SECONDS = "audio_seconds"
    IMAGE_UNITS = "image_units"


class AiCostSource:
    PROVIDER_REPORTED = "provider_reported"
    MEASURED_USAGE_PRICED = "measured_usage_priced"
    FALLBACK_ESTIMATE = "fallback_estimate"
    UNKNOWN = "unknown"


class AiUsageStatus:
    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True)
class AiUsageEvent:
    """One billable AI provider call.

    Attributes:
        invocation_id: Fresh UUID per actual provider call; the
            idempotency key for persistence retries.
        provider: ``openai`` | ``google`` | ``modal``.
        operation: ``guard_security`` | ``guard_exposure`` | ``media_image``
            | ``media_document`` | ``media_video`` | ``media_link`` |
            ``media_audio`` | ``guard_incident_match`` | ``wfm_analyzer`` |
            ``summary_rewriter`` | ``time_parser`` | ``analysis_judge`` |
            ``other``.
        model: Actual model id invoked (not a configured default).
        usage_type: ``tokens`` | ``gpu_seconds`` | ``audio_seconds`` |
            ``image_units``.
        cost_source: How ``estimated_cost_usd`` was derived.
        status: ``success`` | ``error``.
        logical_ref: Optional grouping key for debugging (e.g.
            ``guard_security:{child_id}:{chat_id}:{target_version}``). Not
            unique.
        child_user_id: The monitored child, or ``None`` for shared /
            unattributed operations. The global cost total must not exclude
            unattributed rows.
        prompt_tokens: Provider-reported prompt tokens, when available.
        completion_tokens: Provider-reported completion tokens.
        cached_tokens: Provider-reported cached input tokens.
        total_tokens: Provider-reported total tokens.
        usage_details: Provider-specific measurements (e.g.
            ``audio_duration_seconds``, ``processing_seconds``, ``gpu_type``,
            ``image_count``).
        estimated_cost_usd: Estimated cost in USD, or ``None`` when
            ``cost_source`` is ``unknown``.
        price_version: Pricing config version used for the estimate.
        provider_request_id: Provider's request id, for reconciliation.
        pipeline_event_id: Optional link to ``pipeline_events.id``.
        error_type: Exception type name when ``status`` is ``error``.
        created_at: When the provider call occurred (UTC).
    """

    invocation_id: UUID
    provider: str
    operation: str
    model: str
    usage_type: str
    cost_source: str
    status: str
    logical_ref: str | None = None
    child_user_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    usage_details: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: Decimal | None = None
    price_version: str | None = None
    provider_request_id: str | None = None
    pipeline_event_id: str | None = None
    error_type: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = [
    "AiCostSource",
    "AiUsageEvent",
    "AiUsageProvider",
    "AiUsageStatus",
    "AiUsageType",
]
