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
from typing import TYPE_CHECKING, Any, Protocol

from langsmith import traceable

from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult

if TYPE_CHECKING:
    from echo_v2.services.chat_analysis_worker import ConversationInput

__all__ = [
    "AnalysisError",
    "ChatCompletionClient",
    "LLMWaitingForMeAnalyzer",
    "WaitingForMeAnalyzer",
]

_logger = logging.getLogger("echo_v2.services.waiting_for_me_analyzer")

# Bump when the prompt or output contract changes. Used in trace metadata.
WFM_PROMPT_VERSION = "wfm-v1"


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


class WaitingForMeAnalyzer:
    """Protocol for WaitingForMe analyzers."""

    async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
        ...


def safe_analysis_inputs(inputs: dict) -> dict:
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


def safe_analysis_output(output: WaitingForMeResult) -> dict:
    """Sanitize analyzer output for LangSmith trace metadata.

    The analyzer does not yet know ``result_id`` — that ID only exists
    after persistence. Only safe result fields are included.
    """
    return {
        "decision": output.decision.value,
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
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        model: str = "gpt-4.1",
    ) -> None:
        self._client = client
        self._model = model

    @traceable(
        name="wfm.llm_analyze",
        process_inputs=safe_analysis_inputs,
        process_outputs=safe_analysis_output,
    )
    async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
        if not conversation.messages:
            return WaitingForMeResult(
                decision=WaitingForMeDecision.UNCERTAIN,
                confidence=1.0,
                reason="No messages to analyze.",
                target_version=conversation.target_version,
            )

        user_msg = _build_user_message(conversation)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_completion_tokens=200,
            )
        except Exception as exc:
            _logger.warning("WaitingForMe analyzer API error: %s", exc)
            raise AnalysisError(f"LLM request failed: {exc}") from exc

        raw_output = response.choices[0].message.content or ""
        return _parse_llm_output(raw_output, conversation.target_version)


def _build_user_message(conversation: ConversationInput) -> str:
    """Render the conversation as a readable transcript for the LLM."""
    lines: list[str] = []
    for direction, text, timestamp in conversation.messages:
        label = _DIR_LABELS.get(direction, direction)
        ts = timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {label}: {text}")
    return "Conversation:\n" + "\n".join(lines)


def _parse_llm_output(raw: str, target_version: int) -> WaitingForMeResult:
    """Parse and validate the LLM's JSON output."""
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

    if not isinstance(data, dict) or "decision" not in data:
        raise AnalysisError(f"LLM output missing 'decision' key: {raw!r}")

    decision_str = str(data["decision"]).strip().lower()
    try:
        decision = WaitingForMeDecision(decision_str)
    except ValueError as exc:
        raise AnalysisError(
            f"LLM returned unknown decision {decision_str!r}"
        ) from exc

    confidence = data.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = None

    reason = data.get("reason")
    if reason is not None:
        reason = str(reason)

    summary = data.get("summary")
    if summary is not None:
        summary = str(summary).strip()
        if not summary:
            summary = None
        elif len(summary) > 160:
            summary = summary[:157] + "…"

    return WaitingForMeResult(
        decision=decision,
        confidence=confidence,
        reason=reason,
        summary=summary,
        target_version=target_version,
    )
