"""Tests for strip_leading_submission_metadata."""

from src.tools.essay_sanitize import strip_leading_submission_metadata


def test_strip_greek_cover_block_before_title():
    raw = (
        "Ονοματεπώνυμο φοιτητή/τριας: [Ονοματεπώνυμο] "
        "Κωδικός μαθήματος: DLSPS511 Τίτλος μαθήματος: [Τίτλος] "
        "Τίτλος εργασίας: η εργασία\n\n"
        "# ο ρόλος της ψυχοθεραπείας\n\n"
        "Παράγραφος εισαγωγής."
    )
    out = strip_leading_submission_metadata(raw)
    assert "Ονοματεπώνυμο" not in out
    assert out.startswith("# ο ρόλος")


def test_strip_metadata_paragraph_after_h1():
    raw = (
        "# ο ρόλος της ψυχοθεραπείας\n\n"
        "Ονοματεπώνυμο: [x] Κωδικός μαθήματος: ABC Τίτλος μαθήματος: [y]\n\n"
        "Η εισαγωγή ξεκινά εδώ."
    )
    out = strip_leading_submission_metadata(raw)
    assert "Ονοματεπώνυμο" not in out
    assert "Η εισαγωγή" in out


def test_preserves_normal_intro():
    raw = "# Θέμα εργασίας\n\nΗ ψυχοθεραπεία αποτελεί κεντρικό πεδίο μελέτης.\n"
    assert strip_leading_submission_metadata(raw) == raw


def test_preserves_when_only_one_marker():
    raw = "Κωδικός μαθήματος: XYZ\n\n# Τίτλος\n\nΚείμενο.\n"
    out = strip_leading_submission_metadata(raw)
    assert "Κωδικός μαθήματος" in out


def test_english_markers():
    raw = (
        "Student name: Jane Doe Course code: CS101 Course title: Intro\n\n"
        "# My essay title\n\nBody."
    )
    out = strip_leading_submission_metadata(raw)
    assert "Student name" not in out
    assert out.startswith("# My essay")
