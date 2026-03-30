# Essay Writer Implementation Tracker

This file tracks the proposal backlog, what has been completed, and what remains.

## Status Legend

- [x] Completed
- [ ] Not started
- [~] In progress

## Proposal Backlog

1. [x] Make source selection control what the writer sees
   - Use `sources/selected.json` to filter `SourceNote` inputs for essay writing.
   - Fall back to all accessible notes when `selected.json` is missing or unusable.
   - Add tests that verify selected-source filtering and fallback behavior.

2. [ ] Reduce quadratic token growth in the long-essay path
   - Bound section-writing context.
   - Replace full-essay per-section review with narrower context plus an optional final pass.

3. [ ] Remove dead staging and VFS-era abstractions
   - Simplify `stage_files()` / `build_message_content()` usage.
   - Remove stale VFS and subagent references from runtime code.

4. [ ] Clean up documentation drift aggressively
   - Archive or remove deepagents-specific reference material from the active docs surface.
   - Align runtime module docstrings with the current deterministic pipeline.

5. [ ] Centralize HTTP transport, retries, and connection pooling
   - Introduce one shared `httpx` client policy.
   - Consolidate timeout, SSL, retry, and header behavior.

6. [ ] Parallelize research more intelligently across queries
   - Keep host-specific limits.
   - Add bounded query-level concurrency.

7. [ ] Make validation answers structured instead of a single free-form blob
   - Store per-question answers.
   - Preserve human-readable CLI interaction.

8. [ ] Eliminate configuration drift by wiring or removing unused settings
   - Review `word_count_tolerance`, `max_sources_per_direction`, `prefer_greek_sources`, and `search_language`.

9. [ ] Stop duplicating model pricing in code and config
   - Load pricing from `config/gemini_pricing.json`.

10. [ ] Expand tests around pipeline invariants
   - Add orchestration-focused tests for source selection, long-essay context assembly, validation persistence, and research ranking.

## Current Session

- [x] Proposal #1 implemented.
- [x] Proposal #1 tests passing: `uv run python -m pytest tests/test_refactoring.py -v`.
- [x] Proposal #1 tracker updated to completed.
