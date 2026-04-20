"""Pydantic models for structured pipeline artifacts.

These models define the JSON schemas for pipeline intermediates:
brief, validation, plan, and source notes. Essays remain markdown.
"""

from __future__ import annotations

import ast
import json
import re

from pydantic import BaseModel, Field, field_validator, model_validator


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


_GREEK_CHAR_RE = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")
_CONTEXT_DEPENDENT_OPTION_PATTERNS = (
    re.compile(
        r"^(?:all|both|either|same|combination|mix|mixed|hybrid)(?:\s+of)?(?:\s+the)?\s+(?:above|previous|these|those)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:all|both)\b.*\b(?:above|previous)\b", re.IGNORECASE),
    re.compile(
        r"^(?:όλα|ολα|και\s+τα\s+δύο|και\s+τα\s+δυο|συνδυασμός|συνδυασμοσ|μίξη|μιξη|μεικτό|μεικτο|μεικτή|μεικτη)\b.*\b(?:παραπάνω|παραπανω|ανωτέρω|ανωτερω|προηγούμενα|προηγουμενα|προηγούμενο|προηγουμενο)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:και\s+τα\s+δύο|και\s+τα\s+δυο)$", re.IGNORECASE),
)


def _is_context_dependent_option(text: str) -> bool:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return False
    return any(
        pattern.search(normalized) for pattern in _CONTEXT_DEPENDENT_OPTION_PATTERNS
    )


def _expand_context_dependent_option(
    answer: str,
    options: list[str],
    *,
    selected_index: int | None = None,
) -> str:
    normalized_answer = (answer or "").strip()
    if not normalized_answer or not _is_context_dependent_option(normalized_answer):
        return normalized_answer

    if selected_index is None:
        answer_key = " ".join(normalized_answer.casefold().split())
        for index, option in enumerate(options):
            option_key = " ".join((option or "").strip().casefold().split())
            if option_key == answer_key:
                selected_index = index
                break

    if selected_index is None or selected_index <= 0:
        return normalized_answer

    prior_options = [
        (option or "").strip()
        for option in options[:selected_index]
        if (option or "").strip() and not _is_context_dependent_option(option)
    ]
    if len(prior_options) < 2:
        return normalized_answer

    separator = " / "
    if any(_GREEK_CHAR_RE.search(option) for option in prior_options):
        separator = " / "
    return separator.join(prior_options)


# -- Brief -----------------------------------------------------------------


class Clarification(BaseModel):
    """A Q&A pair appended after interactive validation."""

    question: str
    answer: str


class AssignmentBrief(BaseModel):
    """Structured assignment brief — /brief/assignment.json."""

    topic: str = Field(description="Main essay topic or title.")
    word_count: str | None = Field(
        default=None,
        description="Target word count as a string, or null if not specified.",
    )
    academic_level: str | None = Field(
        default=None,
        description='One of "undergraduate", "postgraduate", or null.',
    )
    language: str = Field(
        default="Greek (Δημοτική)",
        description="Language the essay should be written in. Detect from assignment documents.",
    )
    course: str | None = None
    professor: str | None = None
    student: str | None = None
    institution: str | None = None
    description: str = Field(
        description="Comprehensive prose description of the assignment task. Do not copy cover-sheet metadata here."
    )
    special_instructions: str | None = Field(
        default=None,
        description="Extra requirements including any explicit structure, headings, or section order.",
    )
    min_sources: int | None = Field(
        default=None,
        description="Minimum number of academic sources as an integer, or null if not specified.",
    )
    clarifications: list[Clarification] | None = None


# -- Validation ------------------------------------------------------------


class ValidationQuestion(BaseModel):
    """A single validation question with answer options."""

    question: str
    options: list[str] = Field(description="2–4 answer options.")
    suggested_option_index: int = Field(
        default=0,
        description="0-based index into options for the recommended default.",
    )

    @field_validator("options")
    @classmethod
    def _validate_options_are_standalone(cls, options: list[str]) -> list[str]:
        for option in options:
            if _is_context_dependent_option(option):
                raise ValueError(
                    "validation options must be standalone; avoid references like 'all of the above' or 'Συνδυασμός των παραπάνω'"
                )
        return options

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

    is_pass: bool = Field(
        description="True if the brief is complete enough; false if questions are needed."
    )
    questions: list[ValidationQuestion] | None = Field(
        default=None, description="Clarification questions when is_pass is false."
    )


# -- Plan ------------------------------------------------------------------


class PlanSection(BaseModel):
    """A section entry in the essay plan."""

    number: int = Field(description="Unique sequential section number for display.")
    title: str = Field(description="Section title in the essay language.")
    heading: str = Field(
        description="Exact markdown heading text to use (e.g. '## 1. Εισαγωγή')."
    )
    word_target: int = Field(description="Target word count for this section.")
    key_points: str = Field(
        default="", description="Key arguments or sub-topics to develop."
    )
    content_outline: str = Field(
        default="",
        description="Detailed outline: arguments, evidence types, examples, relation to thesis.",
    )
    requires_full_context: bool = Field(
        default=False,
        description="True for sections (intro, conclusion, synthesis) that should be drafted after body sections.",
    )
    deferred_order: int | None = Field(
        default=None,
        description="Writing order among deferred sections (lower = earlier). Required when requires_full_context is true.",
    )

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

    title: str = Field(
        description="Specific, descriptive essay title in the essay language."
    )
    thesis: str = Field(
        description="Clear thesis statement (1–2 sentences) in the essay language."
    )
    sections: list[PlanSection] = []
    research_queries: list[str] = Field(
        default=[],
        description="6–8 targeted search queries in both the essay language and English.",
    )
    total_word_target: int = Field(
        default=0, description="Sum of all section word_target values."
    )

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


# -- Source registry -------------------------------------------------------


class RegistryEntry(BaseModel):
    """A single entry in sources/registry.json."""

    authors: list[str] = []
    author_families: list[str] | None = None
    year: str = ""
    title: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    pdf_url: str = ""
    source_type: str = ""
    citation_count: int = 0
    user_provided: bool = False
    content_path: str | None = None

    @field_validator("authors", mode="before")
    @classmethod
    def _parse_stringified_authors(cls, v: object) -> object:
        return _parse_stringified_list_value(v)


# -- Source notes ----------------------------------------------------------


class SourceNote(BaseModel):
    """Reader notes for a single source — /sources/notes/{id}.json."""

    source_id: str = Field(description="Exact source_id from the input metadata.")
    is_accessible: bool = Field(
        description="True if any useful content could be extracted."
    )
    # Usable notes may still be abstract-only; fetched_fulltext means we had substantive body text.
    fetched_fulltext: bool = False
    title: str = ""
    authors: list[str] = []
    author_families: list[str] | None = None
    year: str | None = None
    doi: str | None = None
    source_type: str | None = None
    summary: str = Field(
        default="",
        description="Concise summary of the source and its relevance to the essay topic.",
    )
    relevant_extracts: list[str] = Field(
        default=[],
        description="Key quotes, data points, and arguments (200–500 words total).",
    )
    relevance_score: int = Field(
        default=0, description="Topic-fit score 1–5 (5 = core source)."
    )
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


# -- Source batch scoring ---------------------------------------------------


class SourceScoreItem(BaseModel):
    """Relevance score for a single source — used in batch scoring."""

    source_id: str = Field(description="Exact source_id from the input list.")
    relevance_score: int = Field(
        description="Integer 1–5 (5 = directly addresses the essay topic)."
    )


class SourceScoreBatch(BaseModel):
    """Batch scoring result — list of per-source relevance scores."""

    scores: list[SourceScoreItem]

    @field_validator("scores", mode="before")
    @classmethod
    def _parse_stringified_scores(cls, v: object) -> object:
        return _parse_stringified_list_value(v)


# -- Source assignment (long-essay path) -----------------------------------


class SectionSourceAssignment(BaseModel):
    """Maps a section position (plan order) to its assigned source IDs."""

    section_position: int = Field(
        description="Section position from the essay plan (plan order, not display number)."
    )
    source_ids: list[str] = Field(description="Source IDs assigned to this section.")


class SourceAssignmentPlan(BaseModel):
    """Worker output mapping sources to sections — /plan/source_assignments.json."""

    assignments: list[SectionSourceAssignment]


# -- Reconciliation (long-essay path) --------------------------------------


class ReconciliationInstruction(BaseModel):
    """A single section-scoped revision instruction from the reconciliation pass."""

    category: str = Field(
        description="Short label: transition, overlap, scope, source_balance, intro_alignment, conclusion_alignment, or phrase_frequency."
    )
    priority: str = Field(default="medium", description="high, medium, or low.")
    instruction: str = Field(
        description="Concrete instruction telling the reviewer exactly what to change."
    )
    related_section_positions: list[int] = Field(
        default=[], description="Positions of other sections involved, if any."
    )
    target_anchor: str | None = Field(
        default=None,
        description="Short phrase from the section text to locate the fix, if helpful.",
    )

    @field_validator("related_section_positions", mode="before")
    @classmethod
    def _parse_related_section_positions(cls, v: object) -> object:
        return _parse_stringified_list_value(v)


class SectionReconciliationNotes(BaseModel):
    """Reconciliation notes for one section — keyed by internal section position."""

    section_position: int = Field(
        description="Section position from the drafted sections list."
    )
    title: str
    instructions: list[ReconciliationInstruction] = Field(
        default=[],
        description="Targeted revision instructions. Empty list if no changes needed.",
    )

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
