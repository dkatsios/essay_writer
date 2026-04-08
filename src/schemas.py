"""Pydantic models for structured pipeline artifacts.

These models define the JSON schemas for pipeline intermediates:
brief, validation, plan, and source notes. Essays remain markdown.
"""

from __future__ import annotations

from pydantic import BaseModel


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


class EssayPlan(BaseModel):
    """Structured essay plan — /plan/plan.json."""

    title: str
    thesis: str
    sections: list[PlanSection]
    research_queries: list[str]
    total_word_target: int


# -- Source notes ----------------------------------------------------------


class SourceNote(BaseModel):
    """Reader notes for a single source — /sources/notes/{id}.json."""

    source_id: str
    is_accessible: bool
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

    @property
    def content_word_count(self) -> int:
        """Approximate word count of substantive content."""
        count = len(self.summary.split())
        for extract in self.relevant_extracts:
            count += len(extract.split())
        return count
