"""Tests for author surname extraction (citations and source IDs)."""

from __future__ import annotations


class TestSurnameFromAuthorString:
    def test_last_first_comma(self):
        from src.tools.author_names import surname_from_author_string

        assert surname_from_author_string("Jin, Yinghui") == "Jin"

    def test_first_last_tokens(self):
        from src.tools.author_names import surname_from_author_string

        assert surname_from_author_string("Yinghui Jin") == "Jin"
        assert surname_from_author_string("Enola K. Proctor") == "Proctor"

    def test_empty(self):
        from src.tools.author_names import surname_from_author_string

        assert surname_from_author_string("") == ""
        assert surname_from_author_string("   ") == ""


class TestInlineSurnamesFromSource:
    def test_prefers_author_families(self):
        from src.tools.author_names import inline_surnames_from_source

        s = inline_surnames_from_source(
            {
                "authors": ["Yinghui Jin", "Vikram Patel"],
                "author_families": ["Jin", "Patel"],
            }
        )
        assert s == ["Jin", "Patel"]

    def test_falls_back_per_author(self):
        from src.tools.author_names import inline_surnames_from_source

        s = inline_surnames_from_source({"authors": ["Yinghui Jin"]})
        assert s == ["Jin"]

    def test_empty_family_slot_uses_heuristic(self):
        from src.tools.author_names import inline_surnames_from_source

        s = inline_surnames_from_source(
            {
                "authors": ["Yinghui Jin"],
                "author_families": [""],
            }
        )
        assert s == ["Jin"]


class TestFormatApaInlineSurnames:
    def test_three_authors_et_al_uses_family_list(self):
        from src.tools.docx_builder import format_apa_inline

        source = {
            "authors": ["Yinghui Jin", "Jane Doe", "Bob Smith"],
            "author_families": ["Jin", "Doe", "Smith"],
            "year": 2020,
        }
        assert format_apa_inline(source, None) == "(Jin et al., 2020)"

    def test_display_name_only_heuristic(self):
        from src.tools.docx_builder import format_apa_inline

        source = {"authors": ["Yinghui Jin"], "year": 2020}
        assert format_apa_inline(source, None) == "(Jin, 2020)"


class TestMakeSourceIdFamilies:
    def test_uses_first_family_for_id(self):
        from src.tools.research_sources import make_source_id

        sid = make_source_id(
            ["Yinghui Jin"],
            2020,
            author_families=["Jin"],
        )
        assert sid == "jin2020"

    def test_heuristic_without_families(self):
        from src.tools.research_sources import make_source_id

        sid = make_source_id(["Yinghui Jin"], 2020, author_families=None)
        assert sid == "jin2020"
