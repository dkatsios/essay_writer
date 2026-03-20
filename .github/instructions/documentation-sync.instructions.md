---
description: "Use when implementing features, fixes, refactors, or behavior/config changes. Enforces synchronized documentation updates across README.md, CLAUDE.md, and .github/copilot-instructions.md."
applyTo: "**"
---
# Documentation Synchronization Policy

## File roles — each file has a distinct purpose

- **`.github/copilot-instructions.md`** — Canonical AI guidance. Contains commands, architecture overview, conventions, and invariants that an AI agent cannot reliably discover from code alone. This is the only file that should hold project-wide rules and checklists.
- **`CLAUDE.md`** — Thin pointer for Claude Code. References `.github/copilot-instructions.md` for full context and duplicates only the quick-commands block. Do not add architecture, conventions, or feature details here.
- **`README.md`** — Human onboarding. Quick start, documentation links, and a high-level feature list. No implementation details, file paths, or internal conventions.

## When to update

- On important changes (new features, architectural shifts, new commands, behavior changes), review all three files in the same commit.
- Keep the `Documentation Synchronization Policy` section semantically aligned across all three files.

## What belongs in AI guidance (copilot-instructions.md)

Only include information the AI would **get wrong or miss** without explicit guidance:
- Commands to build, test, lint, and run the project
- Pipeline/orchestration flow (agent order, handler entry points)
- Cross-cutting invariants and gotchas (e.g., math normalization boundary, ConfirmDialog rule)
- Multi-file change checklists (e.g., enum change checklist)
- Pointers to dedicated docs for complex features (e.g., "See PLAYS.md")
- Configuration entry points (which YAML files, env vars)

## What does NOT belong

- File-path inventories that the AI can find via search or imports
- Standard library/framework usage patterns (i18next examples, Pydantic basics)
- CLI flag documentation reproducible from `--help`
- Dependency lists available in `pyproject.toml` / `package.json`
- Internal implementation details of a single module (put those in code comments or a dedicated doc if needed)
- Duplicate prose across files — if it belongs in one file, do not copy it to the others
