"""Tests for src/runtime.py — validation questions, clarifications, pricing."""

from __future__ import annotations

import pytest


class TestValidationQuestionSuggestedIndex:
    def test_clamps_suggested_option_index_to_valid_range(self):
        from src.schemas import ValidationQuestion

        q = ValidationQuestion(
            question="Q?",
            options=["a", "b", "c"],
            suggested_option_index=99,
        )
        assert q.suggested_option_index == 2

        q2 = ValidationQuestion(
            question="Q?",
            options=["only"],
            suggested_option_index=-3,
        )
        assert q2.suggested_option_index == 0

    def test_rejects_context_dependent_options(self):
        from src.schemas import ValidationQuestion

        with pytest.raises(ValueError, match="standalone"):
            ValidationQuestion(
                question="Σε ποιο επίπεδο;",
                options=[
                    "Μακρο-επίπεδο",
                    "Μικρο-επίπεδο",
                    "Συνδυασμός των παραπάνω",
                ],
            )


class TestValidationClarifications:
    def test_parse_validation_answers_maps_letter_choices(self):
        from src.runtime import parse_validation_answers
        from src.schemas import ValidationQuestion

        questions = [
            ValidationQuestion(
                question="Choose scope",
                options=["Macro", "Micro", "Mixed"],
            ),
            ValidationQuestion(
                question="Need case study",
                options=["Yes", "No"],
            ),
        ]

        clarifications = parse_validation_answers(questions, "1. b, 2. a")

        assert len(clarifications) == 2
        assert clarifications[0].answer == "Micro"
        assert clarifications[1].answer == "Yes"

    def test_parse_validation_answers_allows_single_question_freeform(self):
        from src.runtime import parse_validation_answers
        from src.schemas import ValidationQuestion

        questions = [
            ValidationQuestion(
                question="Clarify the focus",
                options=["Option A", "Option B"],
            )
        ]

        clarifications = parse_validation_answers(
            questions, "Focus on public policy implications"
        )

        assert len(clarifications) == 1
        assert clarifications[0].answer == "Focus on public policy implications"

    def test_parse_validation_answers_expands_legacy_context_dependent_choice(self):
        from src.runtime import parse_validation_answers
        from src.schemas import expand_context_dependent_option

        question = type(
            "LegacyValidationQuestion",
            (),
            {
                "question": "Choose scope",
                "options": ["Macro", "Micro", "Combination of the above"],
            },
        )()

        clarifications = parse_validation_answers([question], "1. c")

        assert len(clarifications) == 1
        assert clarifications[0].answer == "Macro / Micro"

        assert (
            expand_context_dependent_option(
                "Συνδυασμός των παραπάνω",
                ["Μακρο-επίπεδο", "Μικρο-επίπεδο", "Συνδυασμός των παραπάνω"],
                selected_index=2,
            )
            == "Μακρο-επίπεδο / Μικρο-επίπεδο"
        )


class TestPricing:
    def test_calc_cost_known_model(self):
        from src.runtime import calc_cost

        cost = calc_cost(
            "gemini-2.5-flash", input_tokens=1_000_000, output_tokens=1_000_000
        )
        assert cost > 0

    def test_calc_cost_unknown_model_returns_zero(self):
        from src.runtime import calc_cost

        cost = calc_cost("nonexistent-model-xyz", input_tokens=1000, output_tokens=100)
        assert cost == 0.0

    def test_calc_cost_includes_thinking(self):
        from src.runtime import calc_cost

        cost_no_think = calc_cost("gpt-4o", input_tokens=1000, output_tokens=100)
        cost_with_think = calc_cost(
            "gpt-4o", input_tokens=1000, output_tokens=100, thinking_tokens=500
        )
        assert cost_with_think > cost_no_think
