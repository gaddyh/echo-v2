"""Domain contracts for group-level Exposure Guard shadow analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ExposureCategory(str, Enum):
    VIOLENCE = "violence"
    BULLYING = "bullying"
    SEXUAL_HARM = "sexual_harm"
    COERCION = "coercion"


@dataclass(frozen=True)
class ExposureAssessment:
    """A group-level event assessment, independent of Child Guard semantics."""

    significant_exposure: bool | None
    category: ExposureCategory | None
    summary: str
    evidence_message_ids: tuple[str, ...]
    reason: str
    input_message_ids: tuple[str, ...] = ()
    input_content_hash: str | None = None
    child_user_id: str = ""
    child_name: str = ""
    connection_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    chat_name: str = ""
    timezone: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    target_version: int | None = None
    truncated: bool = False
    omitted_message_count: int = 0
    model: str = ""
    prompt_version: str = ""
    analyzer_version: str = ""
    created_at: datetime | None = None
    id: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


__all__ = ["ExposureAssessment", "ExposureCategory"]
