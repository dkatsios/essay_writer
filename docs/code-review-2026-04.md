# Code Review — April 2026

Holistic review of the essay_writer codebase. Items are independent and can be tackled in any order.

---

## 1. Thread-safety: `_num_id_counter` in docx_builder

**Severity**: Medium
**File**: `src/tools/docx_builder.py`
**Issue**: `_num_id_counter` is a module-level global incremented during document builds with no locking. Two concurrent web jobs exporting simultaneously can race on this counter.
**Fix**: Either protect with `threading.Lock`, or make it a local variable threaded through `_restart_list_numbering` → `_parse_and_add_content` → `build_document`.

- [ ] Done

---

## 2. Thread-safety: Semantic Scholar throttle is globally shared

**Severity**: Medium
**File**: `src/tools/academic_search.py`
**Issue**: `_last_request_time` and `_request_lock` are module-level. Two simultaneous pipeline runs serialize all Semantic Scholar requests behind a single 1 req/s throttle, even though rate limits are per-API-key (or per-IP).
**Fix**: Pass a throttle object per pipeline run, or accept the serialization as intentional (if you truly share one IP and no API key). If intentional, add a comment explaining why.

- [ ] Done

---

## 3. Bare `except Exception` hides bugs

**Severity**: Medium
**Files**: ~20 occurrences across `pipeline_support.py`, `pipeline_sources.py`, `pipeline_writing.py`, `web_jobs.py`, `research_sources.py`
**Issue**: Catches like `except Exception: logger.warning(...)` absorb programming errors (`TypeError`, `AttributeError`, `KeyError`) the same way they absorb expected failures (network timeouts, validation errors). A schema mismatch in batch scoring silently assigns score 0.
**Fix**: At each catch site, narrow to the expected exception types:
- Network: `httpx.HTTPError`, `httpx.RequestError`, `ConnectionError`, `TimeoutError`
- Schema: `pydantic.ValidationError`
- LLM: `instructor.exceptions.InstructorRetryException` (or whatever Instructor raises)
- JSON: `json.JSONDecodeError`

Let unexpected errors propagate so they're visible in logs/tests.

- [ ] Done

---

## 4. No upload size limits on `/submit`

**Severity**: Medium
**File**: `src/web.py` (`submit` endpoint)
**Issue**: The `/submit` endpoint accepts `files` and `sources` uploads with no size cap. The optional-PDF endpoint has `_MAX_OPTIONAL_PDF_BYTES = 30MB`, but main uploads don't. A malicious or accidental multi-GB upload consumes server memory (FastAPI reads `await file.read()` into RAM).
**Fix**: Add a size check after `await file.read()`, or configure a middleware/reverse-proxy limit. Something like:

```python
raw = await file.read()
if len(raw) > MAX_UPLOAD_BYTES:
    return JSONResponse({"error": "File too large"}, status_code=413)
destination.write_bytes(raw)
```

- [ ] Done

---

## 5. `tracker: object | None` defeats static analysis

**Severity**: Low
**Files**: `pipeline_support.py`, `pipeline_sources.py`, `pipeline_writing.py`
**Issue**: `TokenTracker` is passed as `object | None` throughout the pipeline. All `.set_sub_total()`, `.increment_sub_done()`, `.record()` calls are invisible to type checkers. A breaking API change in `TokenTracker` won't be caught statically.
**Fix**: Import and use `TokenTracker | None` (or define a `Protocol` if you want to avoid the circular import). A `TYPE_CHECKING` guard avoids runtime import overhead:

```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.runtime import TokenTracker
```

- [ ] Done

---

## 6. `ruff` is a production dependency

**Severity**: Low
**File**: `pyproject.toml`
**Issue**: `ruff>=0.15.7` is listed under `[project] dependencies` instead of `[dependency-groups] dev`. This means Docker images and production installs include the linter.
**Fix**: Move `ruff` to the dev dependency group.

- [ ] Done

---

## 7. Hardcoded Greek strings in docx_builder

**Severity**: Low
**File**: `src/tools/docx_builder.py`
**Issue**: TOC header ("ΠΙΝΑΚΑΣ ΠΕΡΙΕΧΟΜΕΝΩΝ"), bibliography heading ("Βιβλιογραφία"), footnotes heading ("Σημειώσεις"), and the TOC update placeholder are hardcoded in Greek. The brief has a `language` field, but it's not passed to `build_document()`.
**Fix**: Accept a `language` parameter in `build_document()` and use a lookup dict for localized strings. The pipeline's `do_export` already loads the brief and can pass `brief.language`.

- [ ] Done

---

## 8. No pipeline integration tests

**Severity**: Medium
**Files**: `tests/`
**Issue**: Tests cover utilities (author names, sanitization, search APIs, web endpoints) but the core pipeline orchestration — step sequencing, checkpoint/resume, short-vs-long-path branching, source recovery — has no unit/integration tests. `_structured_call`, `_text_call`, `_retry_with_backoff` have no direct tests either. The E2E tests require real API keys.
**Fix**: Add integration tests with mocked LLM clients. Instructor supports test doubles. Key scenarios:
- Short path end-to-end (mock worker/writer/reviewer → verify file outputs)
- Long path with section partitioning
- Checkpoint resume skips completed steps
- Source shortfall triggers recovery
- Validation callback round-trip

- [ ] Done

---

## 9. Documentation drift: `_REQUEST_TIMEOUT`

**Severity**: Low
**File**: `.github/copilot-instructions.md`
**Issue**: The docs reference `_REQUEST_TIMEOUT` (300-second client timeout) in the retry logic description, but this constant doesn't exist in the codebase. Either the timeout was removed/renamed or the docs are stale.
**Fix**: If the timeout is still applied (e.g., via Instructor or httpx defaults), update the docs to reflect the actual mechanism. If it was removed, remove the reference.

- [ ] Done

---

## 10. Shared HTTP client never closes

**Severity**: Low
**File**: `src/tools/_http.py`
**Issue**: `get_http_client()` creates a singleton `httpx.Client` with no shutdown hook. Connection pool is never drained on exit. In tests, the client persists across test runs.
**Fix**: Add an `atexit` handler or a `close_http_client()` function. For tests, add a fixture that closes it in teardown.

- [ ] Done

---

## 11. Silent fallback to short path on plan parse failure

**Severity**: Low
**File**: `src/pipeline.py` (`_build_execution_steps`)
**Issue**: If `_parse_sections()` returns `[]` for a long essay (target > threshold), the code falls back to the short path with only a `logger.warning`. A 10,000-word essay silently using single-shot write/review instead of section-by-section can produce worse results.
**Fix**: Consider raising an error or re-attempting the plan step, rather than silently degrading. At minimum, make the log level `ERROR` so it's impossible to miss.

- [ ] Done

---

## 12. Web mutable default arguments

**Severity**: Low
**File**: `src/web.py`
**Issue**: `files: list[UploadFile] = []` and `sources: list[UploadFile] = []` — suppressed with `# noqa: B006`. FastAPI handles this correctly, but it's a code smell.
**Fix**: Not urgent. FastAPI's dependency injection creates a fresh list per request regardless. The `noqa` is fine. Remove this item if you'd rather not touch it.

- [ ] Done
