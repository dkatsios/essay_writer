"""Tests for registry merge behaviour during recovery research passes."""

from __future__ import annotations

from src.tools.research_sources import _build_registry


class TestBuildRegistryMerge:
    """Verify _build_registry preserves existing entries on recovery passes."""

    def _make_hit(self, title, doi="", year=2024, authors=None, citation_count=0):
        return {
            "title": title,
            "doi": doi,
            "year": year,
            "authors": authors or ["Author"],
            "author_families": ["Author"],
            "url": f"https://example.com/{title.replace(' ', '_')}",
            "pdf_url": "",
            "citation_count": citation_count,
        }

    def test_no_existing_registry(self):
        hits = [self._make_hit("Paper A"), self._make_hit("Paper B")]
        reg = _build_registry(hits, 100)
        assert len(reg) == 2

    def test_existing_entries_preserved(self):
        existing = {
            "smith2020": {
                "title": "Existing Paper",
                "doi": "10.1000/existing",
                "authors": ["Smith"],
                "year": "2020",
                "abstract": "",
                "url": "https://example.com/existing",
            }
        }
        new_hits = [self._make_hit("New Paper", doi="10.1000/new")]
        reg = _build_registry(new_hits, 100, existing_registry=existing)
        assert "smith2020" in reg
        assert reg["smith2020"]["title"] == "Existing Paper"
        assert len(reg) == 2

    def test_duplicate_doi_skipped(self):
        existing = {
            "smith2020": {
                "title": "Existing Paper",
                "doi": "10.1000/same",
                "authors": ["Smith"],
                "year": "2020",
                "abstract": "",
                "url": "https://example.com/existing",
            }
        }
        new_hits = [self._make_hit("Different Title Same DOI", doi="10.1000/same")]
        reg = _build_registry(new_hits, 100, existing_registry=existing)
        assert len(reg) == 1
        assert reg["smith2020"]["title"] == "Existing Paper"

    def test_duplicate_title_skipped(self):
        existing = {
            "smith2020": {
                "title": "Existing Paper",
                "doi": "",
                "authors": ["Smith"],
                "year": "2020",
                "abstract": "",
                "url": "https://example.com/existing",
            }
        }
        new_hits = [self._make_hit("Existing Paper", doi="10.1000/new")]
        reg = _build_registry(new_hits, 100, existing_registry=existing)
        assert len(reg) == 1

    def test_id_collision_gets_suffix(self):
        existing = {
            "author2024": {
                "title": "First Paper",
                "doi": "10.1000/first",
                "authors": ["Author"],
                "year": "2024",
                "abstract": "",
                "url": "https://example.com/first",
            }
        }
        new_hits = [self._make_hit("Second Paper", doi="10.1000/second")]
        reg = _build_registry(new_hits, 100, existing_registry=existing)
        assert "author2024" in reg
        assert "author2024a" in reg
        assert len(reg) == 2

    def test_truly_new_entries_added(self):
        existing = {
            "smith2020": {
                "title": "Old Paper",
                "doi": "10.1000/old",
                "authors": ["Smith"],
                "year": "2020",
                "abstract": "",
                "url": "https://example.com/old",
            }
        }
        new_hits = [
            self._make_hit("Brand New A", doi="10.1000/a"),
            self._make_hit("Brand New B", doi="10.1000/b"),
        ]
        reg = _build_registry(new_hits, 100, existing_registry=existing)
        assert len(reg) == 3
        assert "smith2020" in reg
