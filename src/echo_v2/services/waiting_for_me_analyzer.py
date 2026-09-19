"""LLM-based WaitingForMe analyzer.

Takes a :class:`ConversationInput` (a slice of messages from a chat) and
asks the LLM the single question:

    Is there an open request, question, or commitment in this conversation
    for which the next step is expected from the user?

Returns a :class:`WaitingForMeResult` with one of three decisions:
``WAITING_FOR_ME``, ``NOT_WAITING_FOR_ME``, or ``UNCERTAIN``.

The prompt is designed to be narrow — it does not ask the LLM to decide
importance, urgency, timing, or what to notify. Only: where is the ball?

The OpenAI client is injected (not created per-call) so it can be wrapped
with ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from langsmith import traceable

from echo_v2.domain.waiting_for_me import (
    NextOwner,
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.observability.tracing import tracing_client
from echo_v2.services.waiting_for_me_prompts import (
    DEFAULT_PROMPT_VERSION,
    get_prompt,
)

if TYPE_CHECKING:
    from echo_v2.services.chat_analysis_worker import ConversationInput
    from echo_v2.services.summary_rewriter import SummaryRewriterProtocol

__all__ = [
    "AnalysisError",
    "ChatCompletionClient",
    "LLMWaitingForMeAnalyzer",
    "WaitingForMeAnalyzer",
    "WaitingForMeResult",
]

_logger = logging.getLogger("echo_v2.services.waiting_for_me_analyzer")

# Bump when the prompt or output contract changes. Used in trace metadata.
WFM_PROMPT_VERSION = DEFAULT_PROMPT_VERSION

# Bump when the analysis pipeline changes — preprocessing, window, rules,
# schema, thresholds, post-processing — not just model+prompt. Stored on
# every result so feedback can be correlated with the exact algorithm.
WFM_ANALYZER_VERSION = "2026-09-18.1"

# Deterministic mapping from the v3 next_owner contract to the product
# decision. The model never emits `waiting_for_me` directly in v3; the
# derivation happens here so the label can never be inverted by the model.
_NEXT_OWNER_TO_DECISION: dict[NextOwner, WaitingForMeDecision] = {
    NextOwner.USER: WaitingForMeDecision.WAITING_FOR_ME,
    NextOwner.OTHER: WaitingForMeDecision.NOT_WAITING_FOR_ME,
    NextOwner.NONE: WaitingForMeDecision.NOT_WAITING_FOR_ME,
    NextOwner.UNCERTAIN: WaitingForMeDecision.UNCERTAIN,
}


# Direction labels as the LLM sees them.
# The "user" is the person whose WhatsApp account we monitor.
# inbound = received from the other person; outbound = sent by the user.
_DIR_LABELS = {
    "inbound": "them",
    "outbound": "me",
}


_SYSTEM_PROMPT = """\
You are a conversation analyzer. You receive a WhatsApp conversation between \
"me" (the user) and "them" (the other person). Your job is to determine \
whether the next step in the conversation is expected from "me" (the user).

The single question:
  Is there an open request, question, or commitment in this conversation \
for which the next step is expected from the user?

Answer with exactly one of three labels:

- "waiting_for_me": There is an open request, question, or expectation \
directed at the user. The ball is in the user's court. Examples: a direct \
question, a request to do something, a request for confirmation or decision, \
a follow-up on a commitment the user made, or an explicit statement that \
the other person is waiting.

- "not_waiting_for_me": No open expectation. The ball is not in the user's \
court. Examples: a closing ("thanks", "got it"), a status update ("I'll \
update you"), an informational message, or a message where the other person \
is the one who needs to act next.

- "uncertain": Not enough information to decide confidently. Examples: \
media-only messages without accessible text, very ambiguous phrasing, \
missing context, or unclear who is being addressed.

Rules:
- Base your decision on the full conversation window, not just the last \
message. An earlier open question may still be unanswered.
- "thanks" or "got it" after a request means the request was fulfilled — \
the conversation is closed, not waiting.
- If the last message is from "me" (outbound), the ball is typically with \
"them" — but only answer "not_waiting_for_me" if there is no earlier open \
request from "them" that "me" hasn't addressed.
- Return ONLY a JSON object, no explanation outside the JSON.

Output format (JSON only):
{"decision": "<waiting_for_me|not_waiting_for_me|uncertain>", \
"confidence": <0.0-1.0>, "reason": "<one short sentence>", \
"summary": "<one sentence in Hebrew, max 160 chars>"}

The "summary" field:
- One sentence in Hebrew describing the situation and what is being \
waited for, from the user's perspective.
- Do NOT include the contact's name (it is shown separately).
- Do NOT invent details not present in the conversation.
- Max 160 characters.
- Only for "waiting_for_me" decisions; use null or empty string otherwise.
"""


class AnalysisError(Exception):
    """Raised when the LLM analysis fails (API error or invalid output)."""


class ChatCompletionClient(Protocol):
    """Narrow protocol for an OpenAI-compatible chat completion client.

    Both ``AsyncOpenAI`` and ``wrap_openai(AsyncOpenAI(...))`` satisfy this.
    """

    chat: Any


class WaitingForMeAnalyzer(Protocol):
    """Protocol for WaitingForMe analyzers."""

    async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
        raise NotImplementedError


def safe_analysis_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Sanitize analyzer inputs for LangSmith trace metadata.

    Removes ``self`` and the full conversation. Keeps only safe correlation
    fields: hashed IDs, version, message count, prompt version.
    """
    from echo_v2.observability.privacy import correlation_id

    conversation = inputs.get("conversation")
    if conversation is None:
        return {"prompt_version": WFM_PROMPT_VERSION}
    return {
        "user_id_hash": correlation_id(conversation.user_id),
        "chat_id_hash": correlation_id(conversation.chat_id),
        "target_version": conversation.target_version,
        "message_count": len(conversation.messages),
        "prompt_version": WFM_PROMPT_VERSION,
    }


def safe_analysis_output(output: WaitingForMeResult) -> dict[str, Any]:
    """Sanitize analyzer output for LangSmith trace metadata.

    The analyzer does not yet know ``result_id`` — that ID only exists
    after persistence. Only safe result fields are included.
    """
    return {
        "decision": output.decision.value,
        "next_owner": output.next_owner.value if output.next_owner else None,
        "confidence": output.confidence,
        "target_version": output.target_version,
    }


class LLMWaitingForMeAnalyzer:
    """LLM-based WaitingForMe analyzer using the OpenAI API.

    The OpenAI client is injected so it can be wrapped with
    ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.

    Args:
        client: An OpenAI-compatible async client (e.g.
            ``wrap_openai(AsyncOpenAI(...))``).
        model: Model name. Default ``gpt-4.1``.
        prompt_version: System prompt version (``"v0"``, ``"v1"``, ``"v2"``).
            Default ``"v0"`` (original baseline prompt).
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        model: str = "gpt-4.1",
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        summary_rewriter: SummaryRewriterProtocol | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._system_prompt = get_prompt(prompt_version)
        self._prompt_version = prompt_version
        self._summary_rewriter = summary_rewriter
        # GPT-5+ reasoning models only support the default temperature (1)
        # and consume completion tokens for reasoning, so they need a larger
        # budget than the 200 tokens that suffice for gpt-4.x.
        self._is_reasoning_model = model.startswith(("gpt-5", "gpt-6", "o"))

    @traceable(
        name="wfm.llm_analyze",
        client=tracing_client,
        process_inputs=safe_analysis_inputs,
        process_outputs=safe_analysis_output,
    )
    async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
        result, _raw = await self._analyze_core(conversation)
        return result

    async def analyze_with_raw(
        self, conversation: ConversationInput
    ) -> tuple[WaitingForMeResult, str]:
        """Run analysis and return ``(result, raw_llm_response)``.

        Same as :meth:`analyze` but also returns the raw LLM output text.
        Intended for evaluation harnesses that need to persist full traces.
        """
        return await self._analyze_core(conversation)

    async def _analyze_core(
        self, conversation: ConversationInput
    ) -> tuple[WaitingForMeResult, str]:
        if not conversation.messages:
            return (
                WaitingForMeResult(
                    decision=WaitingForMeDecision.UNCERTAIN,
                    confidence=1.0,
                    reason="No messages to analyze.",
                    target_version=conversation.target_version,
                    model=self._model,
                    prompt_version=self._prompt_version,
                    analyzer_version=WFM_ANALYZER_VERSION,
                ),
                "",
            )

        user_msg = _build_user_message(conversation)

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_msg},
            ],
        }
        if self._is_reasoning_model:
            # Default temperature only; leave room for reasoning tokens.
            request_kwargs["max_completion_tokens"] = 4000
        else:
            request_kwargs["temperature"] = 0
            request_kwargs["max_completion_tokens"] = 200

        try:
            response = await self._client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            _logger.warning("WaitingForMe analyzer API error: %s", exc)
            raise AnalysisError(f"LLM request failed: {exc}") from exc

        raw_output = response.choices[0].message.content or ""
        result = _parse_llm_output(raw_output, conversation.target_version)
        # Attach analyzer metadata so feedback can be correlated with the
        # exact model/prompt/analyzer version that produced this result.
        result = replace(
            result,
            model=self._model,
            prompt_version=self._prompt_version,
            analyzer_version=WFM_ANALYZER_VERSION,
        )
        # Warm the summary tone via a separate LLM call. This is fully
        # isolated from the decision logic — the rewriter only touches the
        # ``summary`` field, never ``decision`` or ``next_owner``. Falls
        # back to the original summary on any failure.
        if (
            self._summary_rewriter is not None
            and result.summary
            and result.decision is WaitingForMeDecision.WAITING_FOR_ME
        ):
            warmed = await self._summary_rewriter.rewrite(result.summary)
            result = replace(result, summary=warmed)
        return result, raw_output


def _build_user_message(conversation: ConversationInput) -> str:
    """Render the conversation as a readable transcript for the LLM."""
    lines: list[str] = []
    for direction, text, timestamp in conversation.messages:
        label = _DIR_LABELS.get(direction, direction)
        ts = timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {label}: {text}")
    return "Conversation:\n" + "\n".join(lines)


def _parse_llm_output(raw: str, target_version: int) -> WaitingForMeResult:
    """Parse and validate the LLM's JSON output.

    Supports two output contracts:
    - **v3+**: ``next_owner`` (user|other|none|uncertain) + ``open_obligation``.
      The product ``decision`` is derived deterministically from
      ``next_owner`` so the model can never invert the label.
    - **v0–v2**: ``decision`` (waiting_for_me|not_waiting_for_me|uncertain).
      Parsed directly as today; ``next_owner`` stays ``None``.

    The two contracts are distinguished by which key is present. If both are
    present, ``next_owner`` wins (v3 contract).
    """
    raw = raw.strip()
    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.removesuffix("```")
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"LLM output is not valid JSON: {raw!r}") from exc

    if not isinstance(data, dict):
        raise AnalysisError(f"LLM output is not a JSON object: {raw!r}")

    # --- v3 contract: next_owner -------------------------------------------
    if "next_owner" in data:
        owner_str = str(data["next_owner"]).strip().lower()
        try:
            next_owner = NextOwner(owner_str)
        except ValueError as exc:
            raise AnalysisError(
                f"LLM returned unknown next_owner {owner_str!r}"
            ) from exc
        decision = _NEXT_OWNER_TO_DECISION[next_owner]

        open_obligation = data.get("open_obligation")
        if open_obligation is not None:
            open_obligation = str(open_obligation).strip() or None

        confidence = _parse_confidence(data.get("confidence"))
        reason = _parse_reason(data.get("reason"))
        summary = _parse_summary(data.get("summary"))

        return WaitingForMeResult(
            decision=decision,
            next_owner=next_owner,
            open_obligation=open_obligation,
            confidence=confidence,
            reason=reason,
            summary=summary,
            target_version=target_version,
        )

    # --- v0–v2 contract: decision ------------------------------------------
    if "decision" not in data:
        raise AnalysisError(
            f"LLM output missing 'next_owner' or 'decision' key: {raw!r}"
        )

    decision_str = str(data["decision"]).strip().lower()
    try:
        decision = WaitingForMeDecision(decision_str)
    except ValueError as exc:
        raise AnalysisError(
            f"LLM returned unknown decision {decision_str!r}"
        ) from exc

    confidence = _parse_confidence(data.get("confidence"))
    reason = _parse_reason(data.get("reason"))
    summary = _parse_summary(data.get("summary"))

    return WaitingForMeResult(
        decision=decision,
        next_owner=None,
        open_obligation=None,
        confidence=confidence,
        reason=reason,
        summary=summary,
        target_version=target_version,
    )


def _parse_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _parse_reason(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_summary(value: Any) -> str | None:
    if value is None:
        return None
    summary = str(value).strip()
    if not summary:
        return None
    if len(summary) > 160:
        return summary[:157] + "…"
    return summary
