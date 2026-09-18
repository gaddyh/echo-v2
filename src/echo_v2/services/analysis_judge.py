"""LLM-as-judge evaluator for WaitingForMe analysis results.

After each analysis run, an LLM judge evaluates whether the decision was
correct given the conversation. The score is stored as LangSmith feedback
on the ``wfm.analysis`` run, visible in the dashboard.

The judge uses a cheaper model (gpt-4.1-mini by default) and runs as a
child trace under ``wfm.analysis`` so it appears in the trace tree.

Score semantics:
- ``1.0`` = decision is correct
- ``0.0`` = decision is wrong
- ``0.5`` = uncertain / hard to judge

The judge also returns a short explanation stored as the feedback comment.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from langsmith import traceable

from echo_v2.domain.waiting_for_me import WaitingForMeResult
from echo_v2.observability.tracing import tracing_client

if TYPE_CHECKING:
    from echo_v2.services.chat_analysis_worker import ConversationInput

__all__ = ["AnalysisJudge", "ChatCompletionClient", "JudgeResult"]

_logger = logging.getLogger("echo_v2.services.analysis_judge")


class ChatCompletionClient(Protocol):
    chat: Any


_JUDGE_SYSTEM_PROMPT = """\
You are an expert judge evaluating whether a conversation analysis was correct.

You will receive:
1. A WhatsApp conversation between "me" (the user) and "them" (the other person).
2. The analyzer's decision: waiting_for_me, not_waiting_for_me, or uncertain.
3. The analyzer's confidence and reason.

Your job: Was the analyzer's decision correct?

Definitions:
- "waiting_for_me": The next step is expected from the user. The ball is in \
the user's court. This includes:
  * A direct request, question, or expectation from "them" that "me" hasn't \
addressed.
  * A commitment "me" made ("I'll send it tomorrow", "I'll come soon") that \
hasn't been fulfilled yet.
  * Any open obligation where "me" needs to act next.
- "not_waiting_for_me": No open expectation. The ball is NOT in the user's \
court. The conversation is closed, or the other person needs to act next.
- "uncertain": Not enough information to decide confidently.

Evaluate based on the full conversation, not just the last message. \
Consider:
- Is there an unanswered question from "them"?
- Did "me" make a commitment that hasn't been fulfilled or acknowledged?
- Did "me" already respond to the last request from "them"?
- Is there a closing acknowledgment ("thanks", "got it") that resolves the thread?
- If all messages are from "me", is there a self-commitment that's still open?
- Is the decision consistent with the conversation flow?

Return ONLY a JSON object:
{"score": <0.0, 0.5, or 1.0>, "explanation": "<one short sentence>"}

Scoring:
- 1.0 = The decision is clearly correct.
- 0.5 = The decision is debatable or the conversation is genuinely ambiguous.
- 0.0 = The decision is clearly wrong.
"""


@dataclass
class JudgeResult:
    """The judge's evaluation of an analysis result."""

    score: float
    explanation: str
    run_id: str | None = None


class AnalysisJudge:
    """LLM-as-judge that evaluates WaitingForMe analysis decisions.

    Args:
        client: An OpenAI-compatible chat completion client.
        model: The model to use for judging. Default ``gpt-4.1-mini``.
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        model: str = "gpt-5.4",
    ) -> None:
        self._client = client
        self._model = model

    @traceable(
        name="wfm.judge",
        client=tracing_client,
    )
    async def judge(
        self,
        conversation: ConversationInput,
        result: WaitingForMeResult,
    ) -> JudgeResult:
        """Evaluate whether the analysis decision was correct.

        Returns a :class:`JudgeResult` with a score (0-1) and explanation.
        """
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        run_id = str(run_tree.id) if run_tree is not None else None

        transcript = _build_transcript(conversation)
        user_msg = (
            f"Conversation:\n{transcript}\n\n"
            f"Analyzer decision: {result.decision.value}\n"
            f"Analyzer confidence: {result.confidence}\n"
            f"Analyzer reason: {result.reason or '(none)'}\n\n"
            f"Was this decision correct? Return JSON only."
        )

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_completion_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001 - judge errors must not crash the worker
            _logger.warning("judge LLM call failed: %s", exc)
            return JudgeResult(score=0.5, explanation=f"judge error: {exc}", run_id=run_id)

        raw = response.choices[0].message.content or ""
        parsed = _parse_judge_output(raw)
        parsed.run_id = run_id
        return parsed


def _build_transcript(conversation: ConversationInput) -> str:
    """Render the conversation as a readable transcript for the judge."""
    lines: list[str] = []
    for direction, text, timestamp in conversation.messages:
        label = "them" if direction == "inbound" else "me"
        ts = timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {label}: {text}")
    return "\n".join(lines)


def _parse_judge_output(raw: str) -> JudgeResult:
    """Parse the judge's JSON output."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.removesuffix("```").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("judge output not valid JSON: %r", raw)
        return JudgeResult(score=0.5, explanation="judge output parse error")

    score = data.get("score")
    if score is None:
        return JudgeResult(score=0.5, explanation="judge output missing score")

    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.5

    # Clamp to allowed values.
    if score >= 0.75:
        score = 1.0
    elif score <= 0.25:
        score = 0.0
    else:
        score = 0.5

    explanation = str(data.get("explanation", ""))
    return JudgeResult(score=score, explanation=explanation)
