"""Tests for word-target consistency: validator + normalization."""

from __future__ import annotations

import json

from src.schemas import EssayPlan


def _plan_dict(
    sections: list[dict], total: int, title: str = "Test", thesis: str = "Thesis"
) -> dict:
    return {
        "title": title,
        "thesis": thesis,
        "research_queries": ["query"],
        "total_word_target": total,
        "sections": sections,
    }


def _sections(word_targets: list[int]) -> list[dict]:
    return [
        {
            "number": i + 1,
            "title": f"Section {i + 1}",
            "heading": f"## Section {i + 1}",
            "word_target": wt,
        }
        for i, wt in enumerate(word_targets)
    ]


# ── Fix 1: Pydantic validator rejects mismatched totals ────────────────


class TestEssayPlanWordTargetValidator:
    def test_matching_totals_accepted(self):
        plan = EssayPlan.model_validate(_plan_dict(_sections([400, 600]), total=1000))
        assert plan.total_word_target == 1000

    def test_within_5_percent_accepted(self):
        # sum=1000, total=1040 → 4% off → accepted
        plan = EssayPlan.model_validate(_plan_dict(_sections([400, 600]), total=1040))
        assert plan.total_word_target == 1040

    def test_over_5_percent_auto_corrected(self):
        # sum=1000, total=2000 → 100% off → auto-corrected
        plan = EssayPlan.model_validate(_plan_dict(_sections([400, 600]), total=2000))
        assert plan.total_word_target == 2000
        assert sum(s.word_target for s in plan.sections) == 2000
        for s in plan.sections:
            assert s.word_target % 10 == 0

    def test_real_world_double_counting_auto_corrected(self):
        """Reproduces the actual bug: 24 sections summing to ~43k vs total 24k."""
        targets = [
            2160,
            3936,
            1312,
            1312,
            1312,
            3936,
            984,
            984,
            984,
            984,
            3936,
            1312,
            1312,
            1312,
            3936,
            1312,
            1312,
            1312,
            3936,
            984,
            984,
            984,
            984,
            2160,
        ]
        assert sum(targets) == 43680
        plan = EssayPlan.model_validate(_plan_dict(_sections(targets), total=24000))
        assert sum(s.word_target for s in plan.sections) == 24000
        for s in plan.sections:
            assert s.word_target % 10 == 0
            assert s.word_target >= 10

    def test_zero_total_auto_derived(self):
        plan = EssayPlan.model_validate(_plan_dict(_sections([300, 700]), total=0))
        assert plan.total_word_target == 1000


# ── Fix 2: pipeline normalization scales and rounds to tens ────────────


class TestNormalizeSectionWordTargets:
    def _make_sections(self, word_targets: list[int]):
        from src.pipeline_support import Section

        return [
            Section(
                position=i + 1,
                number=i + 1,
                title=f"S{i + 1}",
                heading=f"## S{i + 1}",
                word_target=wt,
                key_points="pts",
            )
            for i, wt in enumerate(word_targets)
        ]

    def test_no_op_when_already_matching(self):
        from src.pipeline_support import normalize_section_word_targets

        sections = self._make_sections([300, 500, 200])
        normalize_section_word_targets(sections, 1000)
        assert [s.word_target for s in sections] == [300, 500, 200]

    def test_scales_down_and_rounds_to_tens(self):
        from src.pipeline_support import normalize_section_word_targets

        # 3936 * (24000/43680) ≈ 2162.6 → round to nearest 10 = 2160
        # 984 * (24000/43680) ≈ 540.7 → 540
        sections = self._make_sections([3936, 984, 1312])
        total = 24000
        normalize_section_word_targets(sections, total)

        for section in sections:
            assert section.word_target % 10 == 0, (
                f"section word_target {section.word_target} not rounded to tens"
            )
        assert sum(s.word_target for s in sections) == total

    def test_real_world_case_sums_to_total(self):
        from src.pipeline_support import normalize_section_word_targets

        targets = [
            2160,
            3936,
            1312,
            1312,
            1312,
            3936,
            984,
            984,
            984,
            984,
            3936,
            1312,
            1312,
            1312,
            3936,
            1312,
            1312,
            1312,
            3936,
            984,
            984,
            984,
            984,
            2160,
        ]
        sections = self._make_sections(targets)
        normalize_section_word_targets(sections, 24000)

        assert sum(s.word_target for s in sections) == 24000
        for s in sections:
            assert s.word_target % 10 == 0
            assert s.word_target >= 10

    def test_no_op_when_sum_is_zero(self):
        from src.pipeline_support import normalize_section_word_targets

        sections = self._make_sections([0, 0])
        normalize_section_word_targets(sections, 1000)
        # No crash, values unchanged
        assert [s.word_target for s in sections] == [0, 0]

    def test_parse_sections_normalizes(self, tmp_path):
        """Integration: parse_sections applies normalization even for mismatched plans."""
        from src.pipeline_support import parse_sections

        # sum(600+400)=1000 but total=500 → validator auto-corrects,
        # then parse_sections normalizes again (no-op since already fixed).
        plan = {
            "title": "Test",
            "thesis": "Thesis",
            "research_queries": ["q"],
            "sections": [
                {"number": 1, "title": "A", "heading": "A", "word_target": 600},
                {"number": 2, "title": "B", "heading": "B", "word_target": 400},
            ],
            "total_word_target": 500,
        }
        (tmp_path / "plan").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plan" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

        sections = parse_sections(tmp_path)

        assert sum(s.word_target for s in sections) == 500
        # 600 * (500/1000) = 300, 400 * (500/1000) = 200
        assert sections[0].word_target == 300
        assert sections[1].word_target == 200
