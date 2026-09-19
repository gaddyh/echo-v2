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
from typing import Any, Protocol

from langsmith import traceable

from echo_v2.observability.tracing import tracing_client

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
    ) -> None:
        self._client = client
        self._model = model
        # GPT-5+ reasoning models only support the default temperature (1)
        # and consume completion tokens for reasoning, so they need a
        # larger budget than the 100 tokens that suffice for gpt-4.x.
        self._is_reasoning_model = model.startswith(("gpt-5", "gpt-6", "o"))

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

        try:
            response = await self._client.chat.completions.create(
                **request_kwargs
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, fall back to original
            _logger.warning("Summary rewrite API error, using original: %s", exc)
            return summary

        rewritten = (response.choices[0].message.content or "").strip()
        if not rewritten:
            _logger.warning("Summary rewrite returned empty, using original")
            return summary

        # Enforce the 160-char contract even if the model overshoots.
        if len(rewritten) > 160:
            rewritten = rewritten[:157] + "…"

        return rewritten
