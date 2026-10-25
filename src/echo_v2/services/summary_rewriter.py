"""Summary tone rewriter — a separate LLM call that warms the summary.

The WaitingForMe analyzer produces a cold, descriptive one-liner in
Hebrew (e.g. "ממתין לאישור תאריך הפגישה"). This module rewrites that
summary into a friendlier, more conversational tone (e.g. "מחכים שתאשר
את תאריך הפגישה") via a **separate** LLM call.

Keeping the tone rewrite in its own call — rather than in the analyzer's
system prompt — fully isolates it from the decision logic. The analyzer's
``next_owner`` / ``decision`` output is never affected by tone iteration,
and the rewrite can be A/B-tested or disabled without re-running the
sanity eval.

The rewriter is best-effort: on any failure (API error, invalid output,
empty response) it falls back to the original summary so the analysis
pipeline is never broken by a tone-rewrite glitch.

The OpenAI client is injected (not created per-call) so it can be wrapped
with ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from langsmith import traceable

from echo_v2.domain.ai_usage_event import AiUsageStatus, AiUsageType
from echo_v2.observability.tracing import tracing_client

if TYPE_CHECKING:
    from echo_v2.services.reporting.ai_usage_recorder import AiUsageRecorder

__all__ = [
    "SummaryRewriter",
    "SummaryRewriterProtocol",
]

_logger = logging.getLogger("echo_v2.services.summary_rewriter")

# Bump when the rewrite prompt or logic changes.
SUMMARY_REWRITER_VERSION = "2026-09-19.1"

_SYSTEM_PROMPT = """\
You rewrite short Hebrew summary sentences to sound warmer and more \
conversational — like a friendly nudge from someone helping the user, \
not a formal report.

Input: a single Hebrew sentence (max 160 characters) describing what the \
user is being waited on for.

Rules:
- Output ONLY the rewritten sentence. No quotes, no explanation, no \
preamble.
- Keep it in Hebrew.
- Keep the same meaning — do not add, remove, or invent details.
- Prefer warm, second-person phrasing. Examples of the desired tone:
  "מחכים שתאשר את תאריך הפגישה"
  "צריך לחזור אליהם עם המחיר"
  "שואלים אם תגיע מחר — כדאי לעדכן"
- Avoid stiff, bureaucratic phrasing such as:
  "ממתין לאישור תאריך הפגישה"
  "נדרשת תגובה מהמשתמש"
  "המתנה לאישור מצד המשתמש"
- No exclamation marks. No emojis.
- Max 160 characters. If the rewrite would exceed 160, shorten it.
- Do NOT include the contact's name.
"""


class SummaryRewriterProtocol(Protocol):
    """Protocol for summary tone rewriters."""

    async def rewrite(self, summary: str) -> str:
        """Rewrite a summary to a warmer tone, falling back on failure."""
        raise NotImplementedError


class SummaryRewriter:
    """Rewrites a cold summary into a warmer tone via a separate LLM call.

    The OpenAI client is injected so it can be wrapped with
    ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.

    Args:
        client: An OpenAI-compatible async client.
        model: Model name. Default ``gpt-4.1``. Reasoning models
            (``gpt-5+``, ``o*``) are supported — temperature is left at
            the default and a larger token budget is used to accommodate
            reasoning tokens.
    """

    def __init__(
        self,
        client: Any,
        model: str = "gpt-4.1",
        recorder: AiUsageRecorder | None = None,
    ) -> None:
        self._client = client
        self._model = model
        # GPT-5+ reasoning models only support the default temperature (1)
        # and consume completion tokens for reasoning, so they need a
        # larger budget than the 100 tokens that suffice for gpt-4.x.
        self._is_reasoning_model = model.startswith(("gpt-5", "gpt-6", "o"))
        self._recorder = recorder

    @traceable(
        name="wfm.summary_rewrite",
        client=tracing_client,
    )
    async def rewrite(self, summary: str) -> str:
        """Rewrite ``summary`` to a warmer tone.

        Falls back to the original ``summary`` on any failure (API error,
        empty/whitespace-only response, etc.) so the analysis pipeline is
        never broken by a tone-rewrite glitch.

        Args:
            summary: The original cold/formal summary (Hebrew, max 160
                chars).

        Returns:
            The warmed summary, or the original on failure.
        """
        if not summary or not summary.strip():
            return summary

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": summary},
            ],
        }
        if self._is_reasoning_model:
            request_kwargs["max_completion_tokens"] = 2000
        else:
            request_kwargs["temperature"] = 0
            request_kwargs["max_completion_tokens"] = 100

        invocation_id = uuid4()
        try:
            response = await self._client.chat.completions.create(
                **request_kwargs
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, fall back to original
            _logger.warning("Summary rewrite API error, using original: %s", exc)
            await self._record_usage(
                status=AiUsageStatus.ERROR,
                invocation_id=invocation_id,
                error_type=type(exc).__name__,
            )
            return summary

        usage = getattr(response, "usage", None)
        await self._record_usage(
            status=AiUsageStatus.SUCCESS,
            invocation_id=invocation_id,
            usage=usage,
        )

        rewritten = (response.choices[0].message.content or "").strip()
        if not rewritten:
            _logger.warning("Summary rewrite returned empty, using original")
            return summary

        # Enforce the 160-char contract even if the model overshoots.
        if len(rewritten) > 160:
            rewritten = rewritten[:157] + "…"

        return rewritten

    async def _record_usage(
        self,
        *,
        status: str,
        invocation_id: Any,
        usage: Any = None,
        error_type: str | None = None,
    ) -> None:
        """Record one billable provider attempt. Best-effort."""
        if self._recorder is None:
            return
        from echo_v2.services.reporting.ai_usage_recorder import (
            UsageContext,
            UsagePayload,
        )

        def _uv(field: str) -> int | None:
            value = getattr(usage, field, None) if usage is not None else None
            return value if isinstance(value, int) else None

        context = UsageContext(
            operation="summary_rewriter",
            provider="openai",
            model=self._model,
        )
        payload = UsagePayload(
            usage_type=AiUsageType.TOKENS,
            status=status,
            prompt_tokens=_uv("prompt_tokens"),
            completion_tokens=_uv("completion_tokens"),
            total_tokens=_uv("total_tokens"),
            invocation_id=invocation_id,
            error_type=error_type if status == AiUsageStatus.ERROR else None,
        )
        await self._recorder.record(context, payload)
