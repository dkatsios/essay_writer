"""Author surname extraction for citations and source IDs.

Crossref provides structured ``family`` names; other APIs return a single display
string. We prefer structured data when present and fall back to a small
heuristic for plain strings.
"""

from __future__ import annotations


def surname_from_author_string(author: str) -> str:
    """Best-effort surname from a single author display string.

    - If the string contains a comma (``Last, First``), use the part before the
      first comma.
    - Otherwise assume ``First ... Last`` and use the last whitespace-separated
      token (matches common English-style metadata).

    Not reliable for all naming conventions; prefer ``author_families`` from APIs
    that provide it.
    """
    s = (author or "").strip()
    if not s:
        return ""
    if "," in s:
        return s.split(",", 1)[0].strip()
    parts = s.split()
    return parts[-1].strip() if parts else ""


def inline_surnames_from_source(source: dict) -> list[str]:
    """Surnames for APA parenthetical citations, in author order.

    Uses optional ``author_families`` (parallel to ``authors``) when each slot
    is non-empty; otherwise :func:`surname_from_author_string` on each author
    string.
    """
    authors = [a for a in source.get("authors", []) if a and str(a).strip()]
    if not authors:
        return []

    families = source.get("author_families")
    if not isinstance(families, list):
        families = []

    out: list[str] = []
    for i, a in enumerate(authors):
        fam = ""
        if i < len(families) and families[i] is not None:
            fam = str(families[i]).strip()
        if fam:
            out.append(fam)
        else:
            out.append(surname_from_author_string(str(a)))
    return out
