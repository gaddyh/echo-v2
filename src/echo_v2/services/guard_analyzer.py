"""LLM-based Guard analyzer (stub).

Takes a slice of a child's conversation and asks the LLM to assess what
is happening and how serious it is. Returns a :class:`GuardAnalysis` with
one of four decisions — ``none``, ``watch``, ``concerning``, ``urgent`` —
plus the signals and categories that justify it.

This is an initial analyzer: a single LLM call with a strict structured
output contract. It exists so the Guard eval harness can run end-to-end.
The analyzer will likely add prompt versioning, tracing, a summary rewriter,
and a separate alert policy, but the semantic output contract is expected
to stay stable.

The OpenAI client is injected (not created per-call) so it can be wrapped
with ``langsmith.wrappers.wrap_openai`` for automatic LLM tracing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
)

__all__ = [
    "AnalysisError",
    "ChatCompletionClient",
    "GuardAnalysis",
    "GuardAnalysisInput",
    "GuardAnalyzer",
    "GuardCategory",
    "GuardLLMOutput",
    "GuardSignal",
    "LLMGuardAnalyzer",
]

_logger = logging.getLogger("echo_v2.services.guard_analyzer")

# Bump when the prompt or output contract changes.
GUARD_PROMPT_VERSION = "v0.2-cumulative-signals-alert-threshold"

# Bump when the analysis pipeline changes.
GUARD_ANALYZER_VERSION = "2026-09-29.0"

Decision = Literal["none", "watch", "concerning", "urgent"]


class GuardSignal(str, Enum):
    """Canonical risk signals emitted by the Guard analyzer."""

    OFFLINE_KNOWLEDGE = "offline_knowledge"
    LOCATION_REQUEST = "location_request"
    ROUTINE_PROBING = "routine_probing"
    SECRECY = "secrecy"
    MEETING_REQUEST = "meeting_request"
    REPEATED_HARASSMENT = "repeated_harassment"
    EXCLUSION = "exclusion"
    BULLYING = "bullying"
    THREAT = "threat"
    GROOMING = "grooming"


class GuardCategory(str, Enum):
    """Canonical high-level risk categories emitted by Guard."""

    SUSPICIOUS_CONTACT = "suspicious_contact"
    BULLYING = "bullying"
    SEXUAL_HARASSMENT = "sexual_harassment"
    THREATS = "threats"


class GuardLLMOutput(BaseModel):
    """Strict, structured output contract returned by the Guard LLM."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    signals: list[GuardSignal]
    categories: list[GuardCategory]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: StrictStr


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
class GuardAnalysis:
    """Detection-only result produced by the Guard analyzer.

    Alerting is deliberately not part of this result. A separate
    :class:`AlertPolicy` evaluates this analysis against product context.
    """

    decision: Decision
    signals: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
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

    async def analyze(self, conversation: GuardAnalysisInput) -> GuardAnalysis:
        raise NotImplementedError


_SYSTEM_PROMPT = """\
You are a child-safety conversation analyzer. You receive a WhatsApp \
conversation between a child and one or more other participants. Your \
job is to assess the safety level of the conversation AT THIS POINT \
IN TIME — i.e. given everything said so far.

Answer with exactly one of four decisions:

- "none": No meaningful concern. Normal friendly chat, banter, or harmless \
exchange.
- "watch": One early or mild signal. Monitor only; the evidence is not yet \
a meaningful pattern.
- "concerning": A meaningful pattern exists. The conversation warrants \
attention, but severity is distinct from any notification policy.
- "urgent": An immediate or imminent safety concern.

Also output:
- "signals": A list of detected risk signals. Common signals include: \
"offline_knowledge", "location_request", "routine_probing", "secrecy", \
"meeting_request", "repeated_harassment", "exclusion", "bullying", \
"threat", "grooming". Use only these canonical signal names.
- "categories": High-level categories this conversation falls into, if \
any. The canonical categories are: "suspicious_contact", "bullying", \
"sexual_harassment", "threats". May be empty for "none" decisions.
- "confidence": 0.0–1.0.
- "reason": One short sentence (in Hebrew or English) explaining what \
is happening and why the decision is appropriate.

Rules:
- Base your decision on the FULL conversation up to this point, not just \
the last message.
- Signals are cumulative across the entire conversation prefix. Once a \
signal is established, retain it in later outputs unless later messages \
clearly disprove it. Add newly established signals; do not replace prior \
signals with only the newest signal.
- Keep this analysis detection-only: describe what is happening and how \
serious it is. Do not decide whether a parent should be notified.
- Friendly banter with mutual engagement (emojis, reciprocal teasing) \
is "none", not "bullying". Look for power imbalance, distress, or \
exclusion.
- Return ONLY a JSON object, no explanation outside the JSON.

Output format (JSON only):
{"decision": "<none|watch|concerning|urgent>", \
"signals": ["..."], "categories": ["..."], \
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

    async def analyze(self, conversation: GuardAnalysisInput) -> GuardAnalysis:
        """Run analysis and return a :class:`GuardAnalysis`."""
        result, _raw = await self._analyze_core(conversation)
        return result

    async def analyze_with_raw(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardAnalysis, str]:
        """Run analysis and return ``(result, raw_llm_response)``.

        Intended for evaluation harnesses that need to persist full traces.
        """
        return await self._analyze_core(conversation)

    async def _analyze_core(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardAnalysis, str]:
        if not conversation.messages:
            return (
                GuardAnalysis(
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
            "response_format": GuardLLMOutput,
        }
        if self._is_reasoning_model:
            request_kwargs["max_completion_tokens"] = 4000
        else:
            request_kwargs["temperature"] = 0
            request_kwargs["max_completion_tokens"] = 400

        try:
            response = await self._client.chat.completions.parse(**request_kwargs)
        except Exception as exc:
            _logger.warning("Guard analyzer API error: %s", exc)
            raise AnalysisError(f"LLM request failed: {exc}") from exc

        message = response.choices[0].message
        raw_output = message.content or ""
        parsed = message.parsed
        if parsed is None:
            raise AnalysisError("Guard LLM returned no parsed analysis")
        if not isinstance(parsed, GuardLLMOutput):
            raise AnalysisError("Guard LLM returned an unexpected parsed output")
        result = _analysis_from_output(parsed, self._model, self._prompt_version)
        return result, raw_output


def _build_user_message(conversation: GuardAnalysisInput) -> str:
    """Render the conversation as a readable transcript for the LLM."""
    lines: list[str] = []
    for sender, text in conversation.messages:
        lines.append(f"{sender}: {text}")
    return "Conversation:\n" + "\n".join(lines)


def _parse_llm_output(raw: str) -> GuardAnalysis:
    """Parse and strictly validate the LLM's structured JSON output."""
    raw = raw.strip()
    # Keep compatibility with providers that wrap JSON in markdown fences.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.removesuffix("```").strip()

    try:
        output = GuardLLMOutput.model_validate_json(raw, strict=True)
    except (ValueError, ValidationError) as exc:
        raise AnalysisError(f"Invalid Guard output: {exc}") from exc

    return _analysis_from_output(output)


def _analysis_from_output(
    output: GuardLLMOutput,
    model: str = "",
    prompt_version: str = "",
) -> GuardAnalysis:
    """Convert validated structured output into the application result."""
    return GuardAnalysis(
        decision=output.decision,
        signals=tuple(dict.fromkeys(signal.value for signal in output.signals)),
        categories=tuple(dict.fromkeys(category.value for category in output.categories)),
        confidence=output.confidence,
        reason=output.reason,
        model=model,
        prompt_version=prompt_version,
        analyzer_version=GUARD_ANALYZER_VERSION,
    )
