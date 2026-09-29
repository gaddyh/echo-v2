"""LLM-based Guard analyzer (stub).

Takes a slice of a child's conversation and asks the LLM to assess the
safety level at that point in time. Returns a :class:`GuardResult` with
one of four decisions — ``none``, ``watch``, ``concerning``, ``urgent`` —
plus the signals and categories that justify it.

This is a minimal stub: a single LLM call with a straightforward system
prompt. It exists so the Guard eval harness can run end-to-end. The real
analyzer will likely add prompt versioning, tracing, a summary rewriter,
and an alert policy — but the output contract (decision + signals +
categories) is expected to stay stable.

The OpenAI client is injected (not created per-call) so it can be wrapped
with ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "AnalysisError",
    "ChatCompletionClient",
    "GuardAnalysisInput",
    "GuardAnalyzer",
    "GuardResult",
    "LLMGuardAnalyzer",
]

_logger = logging.getLogger("echo_v2.services.guard_analyzer")

# Bump when the prompt or output contract changes.
GUARD_PROMPT_VERSION = "v0-stub"

# Bump when the analysis pipeline changes.
GUARD_ANALYZER_VERSION = "2026-09-29.0"

Decision = Literal["none", "watch", "concerning", "urgent"]

_VALID_DECISIONS: frozenset[str] = frozenset({"none", "watch", "concerning", "urgent"})


@dataclass(frozen=True)
class GuardAnalysisInput:
    """Stable, serializable representation of a chat slice for Guard analysis.

    Attributes:
        child_id: Identifier for the child whose conversation is monitored.
        chat_id: Identifier for the chat.
        messages: List of (sender, text) tuples. ``sender`` is a role label
            such as ``"child"``, ``"other"``, ``"other_2"``.
        context: Optional non-message facts the analyzer may know
            (e.g. child age, known contacts).
    """

    child_id: str
    chat_id: str
    messages: list[tuple[str, str]] = field(default_factory=list)
    context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardResult:
    """Result of a Guard analysis.

    Attributes:
        decision: One of ``none``, ``watch``, ``concerning``, ``urgent``.
        signals: Detected risk signals (e.g. ``"secrecy"``,
            ``"location_request"``, ``"bullying"``).
        categories: High-level categories (e.g. ``"suspicious_contact"``,
            ``"bullying"``).
        should_alert: Whether the AlertPolicy should fire an alert now.
            Independent of ``decision`` — e.g. ``concerning`` may not
            alert if the policy wants to see a pattern over time.
        confidence: Model confidence 0.0–1.0.
        reason: One short sentence explaining the decision.
        model: Model name used.
        prompt_version: Prompt version used.
        analyzer_version: Analyzer version used.
    """

    decision: Decision
    signals: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    should_alert: bool = False
    confidence: float = 0.0
    reason: str = ""
    model: str = ""
    prompt_version: str = ""
    analyzer_version: str = ""


class AnalysisError(Exception):
    """Raised when the LLM analysis fails (API error or invalid output)."""


class ChatCompletionClient(Protocol):
    """Narrow protocol for an OpenAI-compatible chat completion client."""

    chat: Any


class GuardAnalyzer(Protocol):
    """Protocol for Guard analyzers."""

    async def analyze(self, conversation: GuardAnalysisInput) -> GuardResult:
        raise NotImplementedError


_SYSTEM_PROMPT = """\
You are a child-safety conversation analyzer. You receive a WhatsApp \
conversation between a child and one or more other participants. Your \
job is to assess the safety level of the conversation AT THIS POINT \
IN TIME — i.e. given everything said so far.

Answer with exactly one of four decisions:

- "none": No concern. Normal friendly chat, banter, or harmless exchange.
- "watch": Mild concern worth monitoring. Something slightly off but not \
yet clearly dangerous. E.g. an unknown contact mentioning offline \
knowledge of the child.
- "concerning": Clear concern. A pattern of risky behavior is forming. \
E.g. an unknown contact asking about the child's location/routine, \
repeated harassment, or pressure for secrecy.
- "urgent": Immediate danger. E.g. a stranger requesting a secret \
in-person meeting, explicit threats, or grooming at an advanced stage.

Also output:
- "signals": A list of detected risk signals. Common signals include: \
"offline_knowledge", "location_request", "routine_probing", "secrecy", \
"meeting_request", "repeated_harassment", "exclusion", "bullying", \
"threat", "grooming". You may use other signal names if needed.
- "categories": High-level categories this conversation falls into, if \
any. Common categories: "suspicious_contact", "bullying", \
"sexual_harassment", "threats". May be empty for "none" decisions.
- "should_alert": A boolean. Whether an alert should be sent to the \
parent NOW based on this conversation. This is an independent decision \
from "decision" — "watch" usually does NOT alert, and even \
"concerning" may not alert if the concern is not yet actionable. \
Reserve "should_alert": true for cases where a parent genuinely needs \
to be notified right now.
- "confidence": 0.0–1.0.
- "reason": One short sentence (in Hebrew or English) explaining the \
decision based on the conversation so far.

Rules:
- Base your decision on the FULL conversation up to this point, not just \
the last message.
- Do NOT alert on a plain "hey" — the whole point is to catch \
escalation, not to flag every unknown contact.
- Friendly banter with mutual engagement (emojis, reciprocal teasing) \
is "none", not "bullying". Look for power imbalance, distress, or \
exclusion.
- "should_alert" is independent of "decision". A "concerning" decision \
does not automatically mean "should_alert": true — only alert when the \
situation is actionable enough that a parent should see it now.
- Return ONLY a JSON object, no explanation outside the JSON.

Output format (JSON only):
{"decision": "<none|watch|concerning|urgent>", \
"signals": ["..."], "categories": ["..."], \
"should_alert": <true|false>, \
"confidence": <0.0-1.0>, "reason": "<one short sentence>"}
"""


class LLMGuardAnalyzer:
    """LLM-based Guard analyzer using the OpenAI API.

    Args:
        client: An OpenAI-compatible async client.
        model: Model name. Default ``gpt-4.1``.
        prompt_version: Reserved for future prompt versioning. Currently
            unused — there is only one prompt.
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        model: str = "gpt-4.1",
        prompt_version: str = GUARD_PROMPT_VERSION,
    ) -> None:
        self._client = client
        self._model = model
        self._prompt_version = prompt_version
        # GPT-5+ reasoning models only support the default temperature.
        self._is_reasoning_model = model.startswith(("gpt-5", "gpt-6", "o"))

    async def analyze(self, conversation: GuardAnalysisInput) -> GuardResult:
        """Run analysis and return a :class:`GuardResult`."""
        result, _raw = await self._analyze_core(conversation)
        return result

    async def analyze_with_raw(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardResult, str]:
        """Run analysis and return ``(result, raw_llm_response)``.

        Intended for evaluation harnesses that need to persist full traces.
        """
        return await self._analyze_core(conversation)

    async def _analyze_core(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardResult, str]:
        if not conversation.messages:
            return (
                GuardResult(
                    decision="none",
                    confidence=1.0,
                    reason="No messages to analyze.",
                    model=self._model,
                    prompt_version=self._prompt_version,
                    analyzer_version=GUARD_ANALYZER_VERSION,
                ),
                "",
            )

        user_msg = _build_user_message(conversation)

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        }
        if self._is_reasoning_model:
            request_kwargs["max_completion_tokens"] = 4000
        else:
            request_kwargs["temperature"] = 0
            request_kwargs["max_completion_tokens"] = 400

        try:
            response = await self._client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            _logger.warning("Guard analyzer API error: %s", exc)
            raise AnalysisError(f"LLM request failed: {exc}") from exc

        raw_output = response.choices[0].message.content or ""
        result = _parse_llm_output(raw_output)
        result = GuardResult(
            decision=result.decision,
            signals=result.signals,
            categories=result.categories,
            should_alert=result.should_alert,
            confidence=result.confidence,
            reason=result.reason,
            model=self._model,
            prompt_version=self._prompt_version,
            analyzer_version=GUARD_ANALYZER_VERSION,
        )
        return result, raw_output


def _build_user_message(conversation: GuardAnalysisInput) -> str:
    """Render the conversation as a readable transcript for the LLM."""
    lines: list[str] = []
    for sender, text in conversation.messages:
        lines.append(f"{sender}: {text}")
    return "Conversation:\n" + "\n".join(lines)


def _parse_llm_output(raw: str) -> GuardResult:
    """Parse and validate the LLM's JSON output."""
    raw = raw.strip()
    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.removesuffix("```").strip()

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AnalysisError(f"Invalid JSON from LLM: {exc}") from exc

    decision_raw = data.get("decision", "")
    if decision_raw not in _VALID_DECISIONS:
        raise AnalysisError(
            f"Invalid decision '{decision_raw}'. "
            f"Expected one of {sorted(_VALID_DECISIONS)}."
        )
    decision: Decision = decision_raw

    signals = tuple(str(s) for s in data.get("signals", []))
    categories = tuple(str(c) for c in data.get("categories", []))
    should_alert = bool(data.get("should_alert", False))
    confidence = float(data.get("confidence", 0.0))
    reason = str(data.get("reason", ""))

    return GuardResult(
        decision=decision,
        signals=signals,
        categories=categories,
        should_alert=should_alert,
        confidence=confidence,
        reason=reason,
    )
