"""Pydantic models for structured pipeline artifacts.

These models define the JSON schemas for pipeline intermediates:
brief, validation, plan, and source notes. Essays remain markdown.
"""

from __future__ import annotations

import ast
import json

from pydantic import BaseModel, field_validator, model_validator


def _parse_stringified_list_value(v: object) -> object:
    """Best-effort parsing for providers that serialize arrays as strings."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(v)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return v


# -- Brief -----------------------------------------------------------------


class Clarification(BaseModel):
    """A Q&A pair appended after interactive validation."""

    question: str
    answer: str


class AssignmentBrief(BaseModel):
    """Structured assignment brief — /brief/assignment.json."""

    topic: str
    word_count: str | None = None
    academic_level: str | None = None
    language: str = "Greek (Δημοτική)"
    course: str | None = None
    professor: str | None = None
    student: str | None = None
    institution: str | None = None
    description: str
    special_instructions: str | None = None
    min_sources: int | None = None
    clarifications: list[Clarification] | None = None


# -- Validation ------------------------------------------------------------


class ValidationQuestion(BaseModel):
    """A single validation question with answer options."""

    question: str
    options: list[str]
    suggested_option_index: int = 0
    """0-based index into ``options`` for the recommended default if the user does not change it."""

    @model_validator(mode="after")
    def _clamp_suggested_option_index(self) -> ValidationQuestion:
        if not self.options:
            object.__setattr__(self, "suggested_option_index", 0)
            return self
        n = len(self.options)
        idx = self.suggested_option_index
        if idx < 0 or idx >= n:
            object.__setattr__(self, "suggested_option_index", max(0, min(idx, n - 1)))
        return self


class ValidationResult(BaseModel):
    """Validation output — /brief/validation.json."""

    is_pass: bool
    questions: list[ValidationQuestion] | None = None


# -- Plan ------------------------------------------------------------------


class PlanSection(BaseModel):
    """A section entry in the essay plan."""

    number: int
    title: str
    heading: str
    word_target: int
    key_points: str = ""
    content_outline: str = ""
    requires_full_context: bool = False
    deferred_order: int | None = None

    @model_validator(mode="after")
    def _validate_deferred_order(self) -> PlanSection:
        if self.requires_full_context and self.deferred_order is None:
            raise ValueError(
                f"section {self.number!r} has requires_full_context=true "
                "but deferred_order is missing"
            )
        if not self.requires_full_context:
            self.deferred_order = None
        return self


class EssayPlan(BaseModel):
    """Structured essay plan — /plan/plan.json."""

    title: str
    thesis: str
    sections: list[PlanSection] = []
    research_queries: list[str] = []
    total_word_target: int = 0

    @field_validator("sections", "research_queries", mode="before")
    @classmethod
    def _parse_stringified_plan_lists(cls, v: object) -> object:
        return _parse_stringified_list_value(v)

    @model_validator(mode="after")
    def _derive_totals(self) -> EssayPlan:
        """Normalize totals and reject incomplete essay plans."""
        if not self.total_word_target and self.sections:
            self.total_word_target = sum(s.word_target for s in self.sections)
        issues: list[str] = []
        if not self.sections:
            issues.append("sections must be a non-empty array of section objects")
        if not self.research_queries:
            issues.append("research_queries must be a non-empty array of strings")
        if self.total_word_target <= 0:
            issues.append("total_word_target must be a positive integer")
        if any(section.word_target <= 0 for section in self.sections):
            issues.append("each section.word_target must be a positive integer")
        if issues:
            raise ValueError("; ".join(issues))
        return self


# -- Source notes ----------------------------------------------------------


class SourceNote(BaseModel):
    """Reader notes for a single source — /sources/notes/{id}.json."""

    source_id: str
    is_accessible: bool
    # Usable notes may still be abstract-only; fetched_fulltext means we had substantive body text.
    fetched_fulltext: bool = False
    title: str = ""
    authors: list[str] = []
    author_families: list[str] | None = None
    year: str | None = None
    doi: str | None = None
    source_type: str | None = None
    summary: str = ""
    relevant_extracts: list[str] = []
    relevance_score: int = 0
    inaccessible_reason: str | None = None
    url: str | None = None

    @field_validator("authors", "relevant_extracts", mode="before")
    @classmethod
    def _parse_stringified_list(cls, v: object) -> object:
        """LLM structured output sometimes serialises lists as JSON strings.

        Anthropic (and occasionally other providers) return ``["a", "b"]`` as a
        plain string instead of a real JSON array.  ``json.loads`` handles clean
        cases, but the model often embeds unescaped inner quotes or odd escaping
        that breaks strict JSON parsing.  ``ast.literal_eval`` is more lenient
        with quote styles.  As a last resort, wrap the string in a single-element
        list so we never lose data.
        """
        parsed = _parse_stringified_list_value(v)
        if parsed is not v:
            return parsed
        if isinstance(v, str):
            # 3. Last resort: treat entire string as one item
            if v.strip():
                return [v]
        return v

    @property
    def content_word_count(self) -> int:
        """Approximate word count of substantive content."""
        count = len(self.summary.split())
        for extract in self.relevant_extracts:
            count += len(extract.split())
        return count


# -- Source assignment (long-essay path) -----------------------------------


class SectionSourceAssignment(BaseModel):
    """Maps a section number to its assigned source IDs."""

    section_number: int
    source_ids: list[str]


class SourceAssignmentPlan(BaseModel):
    """Worker output mapping sources to sections — /plan/source_assignments.json."""

    assignments: list[SectionSourceAssignment]


# -- Reconciliation (long-essay path) --------------------------------------


class ReconciliationInstruction(BaseModel):
    """A single section-scoped revision instruction from the reconciliation pass."""

    category: str
    priority: str = "medium"
    instruction: str
    related_section_positions: list[int] = []
    target_anchor: str | None = None

    @field_validator("related_section_positions", mode="before")
    @classmethod
    def _parse_related_section_positions(cls, v: object) -> object:
        return _parse_stringified_list_value(v)


class SectionReconciliationNotes(BaseModel):
    """Reconciliation notes for one section — keyed by internal section position."""

    section_position: int
    section_number: int
    title: str
    instructions: list[ReconciliationInstruction] = []

    @field_validator("instructions", mode="before")
    @classmethod
    def _parse_instructions(cls, v: object) -> object:
        return _parse_stringified_list_value(v)


class EssayReconciliationPlan(BaseModel):
    """Cross-section reconciliation output — /essay/reconciliation.json."""

    global_notes: list[str] = []
    sections: list[SectionReconciliationNotes] = []

    @field_validator("global_notes", "sections", mode="before")
    @classmethod
    def _parse_reconciliation_lists(cls, v: object) -> object:
        return _parse_stringified_list_value(v)

    @model_validator(mode="after")
    def _validate_section_positions(self) -> EssayReconciliationPlan:
        positions = [section.section_position for section in self.sections]
        if len(positions) != len(set(positions)):
            raise ValueError(
                "reconciliation sections must have unique section_position values"
            )
        return self
