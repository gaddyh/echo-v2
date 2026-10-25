"""AiUsageRecorder — builds and persists billable AI usage events.

The recorder is the single entry point for cost capture. Each AI call
site (analyzer, media summarizer, transcriber) calls the recorder with a
:class:`UsageContext` and the raw provider usage payload; the recorder
generates the ``invocation_id`` (or accepts one from the call site for
per-attempt granularity), asks :class:`PricingService` for the
``(cost, cost_source)``, builds the :class:`AiUsageEvent`, and persists
it via the :class:`AiUsageEventRepository`.

Recording is **best-effort**: a persistence failure is logged and
swallowed (same pattern as ``MediaEnrichmentService._record_event``),
never breaking the analysis path. The failure is visible operationally
via the ``error_count`` counter and structured log — not silently
treated as zero cost.

``recorder=None`` means disabled (tests / opt-out). There is no
log-only fallback recorder — silent log substitution produces
misleading totals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from echo_v2.domain.ai_usage_event import (
    AiCostSource,
    AiUsageEvent,
    AiUsageType,
)
from echo_v2.persistence.ai_usage_events import AiUsageEventRepository
from echo_v2.services.reporting.pricing_service import PricingService

__all__ = ["AiUsageRecorder", "UsageContext", "UsagePayload"]

_logger = logging.getLogger("echo_v2.services.reporting.ai_usage_recorder")


@dataclass(frozen=True)
class UsageContext:
    """Lightweight context passed to the recorder for each AI operation.

    Attributes:
        operation: ``guard_security`` | ``guard_exposure`` | ``media_image``
            | ``media_document`` | ``media_video`` | ``media_link`` |
            ``media_audio`` | ``guard_incident_match`` | ``wfm_analyzer`` |
            ``summary_rewriter`` | ``time_parser`` | ``analysis_judge`` |
            ``other``.
        provider: ``openai`` | ``google`` | ``modal``.
        model: Actual model id invoked.
        child_user_id: The monitored child, or ``None`` for shared /
            unattributed operations.
        logical_ref: Optional grouping key for debugging (e.g.
            ``guard_security:{child_id}:{chat_id}:{target_version}``).
        pipeline_event_id: Optional link to ``pipeline_events.id``.
    """

    operation: str
    provider: str
    model: str
    child_user_id: str | None = None
    logical_ref: str | None = None
    pipeline_event_id: str | None = None


@dataclass
class UsagePayload:
    """Raw provider usage + call outcome, normalized by the call site.

    Attributes:
        usage_type: ``tokens`` | ``gpu_seconds`` | ``audio_seconds`` |
            ``image_units``.
        status: ``success`` | ``error``.
        prompt_tokens: Provider-reported prompt tokens.
        completion_tokens: Provider-reported completion tokens.
        cached_tokens: Provider-reported cached input tokens.
        total_tokens: Provider-reported total tokens.
        usage_details: Provider-specific measurements (e.g.
            ``audio_duration_seconds``, ``processing_seconds``, ``gpu_type``,
            ``image_count``).
        provider_request_id: Provider's request id.
        error_type: Exception type name when ``status`` is ``error``.
        invocation_id: Fresh UUID per actual provider call. When ``None``,
            the recorder generates one.
    """

    usage_type: str
    status: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    usage_details: dict[str, Any] = field(default_factory=dict)
    provider_request_id: str | None = None
    error_type: str | None = None
    invocation_id: UUID | None = None


class AiUsageRecorder:
    """Builds and persists :class:`AiUsageEvent` rows.

    Args:
        repo: The :class:`AiUsageEventRepository` for persistence.
        pricing: The :class:`PricingService` for cost estimation.
    """

    def __init__(
        self,
        repo: AiUsageEventRepository,
        pricing: PricingService,
    ) -> None:
        self._repo = repo
        self._pricing = pricing
        self.error_count = 0
        self.recorded_count = 0

    async def record(
        self,
        context: UsageContext,
        payload: UsagePayload,
    ) -> None:
        """Build and persist one usage event. Best-effort.

        A persistence failure is logged, increments ``error_count``, and
        is swallowed — the calling analysis path is never broken. The
        failure is visible operationally via ``error_count`` and the
        structured log, not silently treated as zero cost.
        """
        invocation_id = payload.invocation_id or uuid4()
        cost, cost_source = self._estimate(context, payload)
        event = AiUsageEvent(
            invocation_id=invocation_id,
            provider=context.provider,
            operation=context.operation,
            model=context.model,
            usage_type=payload.usage_type,
            cost_source=cost_source,
            status=payload.status,
            logical_ref=context.logical_ref,
            child_user_id=context.child_user_id,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            cached_tokens=payload.cached_tokens,
            total_tokens=payload.total_tokens,
            usage_details=payload.usage_details,
            estimated_cost_usd=cost,
            price_version=self._pricing.version(),
            provider_request_id=payload.provider_request_id,
            pipeline_event_id=context.pipeline_event_id,
            error_type=payload.error_type,
            created_at=datetime.now(timezone.utc),
        )
        try:
            await self._repo.record(event)
            self.recorded_count += 1
        except Exception:
            self.error_count += 1
            _logger.exception(
                "ai usage persistence failed invocation_id=%s operation=%s "
                "provider=%s model=%s",
                invocation_id,
                context.operation,
                context.provider,
                context.model,
            )

    def _estimate(
        self,
        context: UsageContext,
        payload: UsagePayload,
    ) -> tuple[Decimal | None, str]:
        """Calculate ``(cost, cost_source)`` for the payload."""
        if payload.usage_type == AiUsageType.TOKENS:
            return self._pricing.estimate_token_cost(
                provider=context.provider,
                model=context.model,
                prompt_tokens=payload.prompt_tokens,
                completion_tokens=payload.completion_tokens,
                cached_tokens=payload.cached_tokens,
            )
        if payload.usage_type == AiUsageType.AUDIO_SECONDS:
            return self._pricing.estimate_audio_cost(
                audio_duration_seconds=payload.usage_details.get(
                    "audio_duration_seconds"
                ),
                processing_seconds=payload.usage_details.get("processing_seconds"),
            )
        if payload.usage_type == AiUsageType.IMAGE_UNITS:
            return self._pricing.estimate_image_cost(
                image_count=payload.usage_details.get("image_count"),
            )
        # gpu_seconds or unknown usage types — not priced yet.
        return None, AiCostSource.UNKNOWN
