"""Crossref metadata normalisation."""

from src.tools.crossref_search import _strip_inline_markup


def test_strip_inline_markup_removes_jats_scp() -> None:
    raw = (
        "Canadian Network (<scp>CANMAT</scp>) and International (<scp>ISBD</scp>) "
        "2018 guidelines"
    )
    assert _strip_inline_markup(raw) == (
        "Canadian Network (CANMAT) and International (ISBD) 2018 guidelines"
    )


def test_strip_inline_markup_plain_text_unchanged() -> None:
    assert _strip_inline_markup("Plain title") == "Plain title"


def test_strip_inline_markup_empty() -> None:
    assert _strip_inline_markup("") == ""
