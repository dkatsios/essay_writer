---
description: "Use when implementing features, fixing bugs, or refactoring code. Enforces low-complexity, clean, maintainable changes with simple and readable solutions."
applyTo: "src/**/*.py, src/**/*.ts, src/**/*.tsx, tests/**/*.py, scripts/**/*.py"
---
# Low-Complexity Code Guidelines

- Prefer straightforward, readable control flow over clever abstractions.
- Treat these guidelines as a strong default; allow exceptions only when required for correctness, security, or compatibility.
- Keep functions and classes focused on one responsibility.
- Reuse existing project patterns before introducing new architecture.
- Choose the smallest safe change that solves the problem end-to-end.
- Avoid premature optimization or speculative extensibility.
- Keep naming explicit and consistent with existing code conventions.
- Add brief comments only where logic is non-obvious.
- When multiple options work, choose the one that is easiest to test and maintain.
