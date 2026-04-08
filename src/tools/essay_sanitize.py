"""Strip assignment submission headers accidentally pasted into essay markdown."""

from __future__ import annotations

# Labels commonly found on Greek university cover sheets / submission forms.
_GREEK_MARKERS: tuple[str, ...] = (
    "Ονοματεπώνυμο",
    "Κωδικός μαθήματος",
    "Τίτλος μαθήματος",
    "Τίτλος εργασίας",
    "Αριθμός μητρώου",
    "Εξάμηνο",
)
# Common English equivalents.
_ENGLISH_MARKERS: tuple[str, ...] = (
    "Student name",
    "Course code",
    "Course title",
    "Assignment title",
)


def _marker_hits(text: str) -> int:
    t = text
    n = sum(1 for m in _GREEK_MARKERS if m in t)
    n += sum(1 for m in _ENGLISH_MARKERS if m in t)
    return n


def _looks_like_submission_metadata(text: str) -> bool:
    s = text.strip()
    if len(s) < 12:
        return False
    hits = _marker_hits(s)
    return hits >= 2


def strip_leading_submission_metadata(md: str) -> str:
    """Remove a leading submission/cover block before or just after the first H1.

    Models sometimes copy Ονοματεπώνυμο / Κωδικός μαθήματος / … from the brief into
    the essay body. This keeps structured fields in the brief only.
    """
    if not md:
        return md
    md = md.lstrip("\ufeff").lstrip()

    lines = md.splitlines()
    if not lines:
        return md

    first_heading = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("#")),
        None,
    )

    # Pass 1: cover lines before the first markdown heading.
    if first_heading is not None and first_heading > 0:
        preamble = "\n".join(lines[:first_heading])
        if _looks_like_submission_metadata(preamble):
            rest = "\n".join(lines[first_heading:])
            return strip_leading_submission_metadata(rest)

    # Pass 2: first line is H1; remove a metadata-only paragraph immediately after it.
    if first_heading == 0:
        stripped0 = lines[0].lstrip()
        if stripped0.startswith("#") and not stripped0.startswith("##"):
            i = 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                para_lines: list[str] = []
                j = i
                while j < len(lines) and lines[j].strip():
                    if lines[j].lstrip().startswith("#"):
                        break
                    para_lines.append(lines[j])
                    j += 1
                para = "\n".join(para_lines)
                if para_lines and _looks_like_submission_metadata(para):
                    new_lines = lines[:i] + lines[j:]
                    return "\n".join(new_lines).lstrip("\n")

    return md
