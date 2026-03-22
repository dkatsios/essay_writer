# Deepagents Framework Reference

This document captures the relevant APIs, patterns, and constraints of the `deepagents` package (v0.4.11) as they apply to our essay writer project. It is based on direct source code analysis of the installed package.

---

## 1. Core Entry Point

### `create_deep_agent()`

The main function to create a deep agent. Returns a `CompiledStateGraph` (LangGraph).

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",       # or a BaseChatModel instance
    tools=[my_tool_1, my_tool_2],               # custom tools
    system_prompt="You are an essay writer...",  # prepended to base prompt
    subagents=[...],                             # list of SubAgent dicts
    skills=["/skills/project/"],                 # skill directory paths
    memory=["/memory/AGENTS.md"],                # AGENTS.md paths
    middleware=[...],                             # additional middleware
    backend=my_backend,                          # or a factory function
    interrupt_on={"edit_file": True},            # human-in-the-loop
    checkpointer=my_checkpointer,                # for persistent state
    store=my_store,                               # for StoreBackend
    response_format=my_format,                   # structured output
    name="essay-orchestrator",
    debug=False,
)
```

**Built-in tools** (always available):
- `write_todos` — manage a todo list
- `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep` — file operations on the backend
- `execute` — run shell commands (only if backend implements `SandboxBackendProtocol`)
- `task` — spawn subagents

**Built-in middleware stack** (applied automatically):
1. `TodoListMiddleware`
2. `MemoryMiddleware` (if `memory` is provided)
3. `SkillsMiddleware` (if `skills` is provided)
4. `FilesystemMiddleware` (provides file tools)
5. `SubAgentMiddleware` (provides `task` tool)
6. `SummarizationMiddleware` (auto-compacts conversation)
7. `AnthropicPromptCachingMiddleware`
8. `PatchToolCallsMiddleware`

**Model resolution**: Uses `provider:model` format via `langchain.chat_models.init_chat_model()`. Default model is `claude-sonnet-4-6`. Supports `anthropic:`, `openai:`, `google_genai:`, etc.

---

## 2. Backends

Backends provide the storage layer for file operations. All backends implement `BackendProtocol` with these operations:
- `ls_info(path)` — list directory contents
- `read(file_path, offset, limit)` — read file with line numbers
- `write(file_path, content)` — create new file (errors if exists)
- `edit(file_path, old_string, new_string, replace_all)` — string replacement
- `grep_raw(pattern, path, glob)` — literal text search
- `glob_info(pattern, path)` — glob file matching
- `upload_files(files)` / `download_files(paths)` — batch file operations

All methods have async counterparts (`aread`, `awrite`, etc.).

### 2.1 StateBackend (Ephemeral)

```python
from deepagents.backends import StateBackend
```

- Files stored in LangGraph agent state as `state["files"]` dict.
- Ephemeral — lives within a conversation thread, not across threads.
- State is checkpointed after each agent step (if checkpointer provided).
- File data structure: `{"content": list[str], "created_at": str, "modified_at": str}`.
- `write()` returns `WriteResult` with `files_update` dict for state merging.
- **This is the default backend** when none is specified.
- Initialized via factory: `lambda rt: StateBackend(rt)` (needs runtime).

**Relevance**: This is likely our VFS. Files written by subagents via `write_file` tool are stored here and accessible by parent and sibling agents through the shared state. Good for intermediate artifacts (plans, source extracts, section drafts).

### 2.2 FilesystemBackend (Persistent)

```python
from deepagents.backends import FilesystemBackend

backend = FilesystemBackend(root_dir="/path/to/project", virtual_mode=True)
```

- Reads/writes directly to the real filesystem.
- `root_dir` — base directory for operations (defaults to cwd).
- `virtual_mode=True` — paths are treated as virtual paths anchored to `root_dir`. Blocks path traversal (`..`, `~`). Recommended.
- `virtual_mode=False` (default, deprecated) — absolute paths allowed, no security.
- Uses ripgrep for `grep` when available, falls back to Python.

**Relevance**: Good for persisting final outputs (the .docx file), source PDFs, and for inspectable VFS artifacts. Could be used via CompositeBackend to route certain paths to disk.

### 2.3 CompositeBackend (Route by Path Prefix)

```python
from deepagents.backends import CompositeBackend, StateBackend, FilesystemBackend

composite = CompositeBackend(
    default=StateBackend(runtime),
    routes={
        "/output/": FilesystemBackend(root_dir="/output", virtual_mode=True),
        "/sources/": FilesystemBackend(root_dir="/sources", virtual_mode=True),
    }
)
```

- Routes file operations to different backends based on path prefix.
- Longest prefix match wins.
- Unmatched paths go to the default backend.
- Path prefix is stripped before forwarding to the routed backend.
- `ls_info("/")` aggregates all backends + shows route directories.

**Relevance**: This is key for our architecture. We can route:
- `/output/` → FilesystemBackend (for the final .docx)
- `/sources/raw/` → FilesystemBackend (for input PDFs)
- Everything else → StateBackend (for VFS artifacts: plans, extracts, drafts)

### 2.4 StoreBackend (Persistent with LangGraph Store)

Uses LangGraph's `BaseStore` for persistent key-value storage. Requires a `store` parameter in `create_deep_agent()`.

**Relevance**: Potentially useful for caching across runs (future consideration), but StateBackend + FilesystemBackend via CompositeBackend is sufficient for v1.

---

## 3. Subagents

### 3.1 Defining Subagents

Subagents are defined as `SubAgent` TypedDicts:

```python
from deepagents import SubAgent

research_agent: SubAgent = {
    "name": "research-agent",
    "description": "Searches academic databases for credible sources on specific topics.",
    "system_prompt": "You are a research agent specialized in finding academic sources...",
    "tools": [academic_search_tool, web_fetch_tool],
    "model": "anthropic:claude-sonnet-4-6",   # optional, inherits from parent
    "skills": ["/skills/research/"],           # optional
    "middleware": [],                           # optional
}
```

Required fields: `name`, `description`, `system_prompt`.
Optional fields: `tools`, `model`, `middleware`, `interrupt_on`, `skills`.

### 3.2 How Subagents Are Invoked

The `task` tool is automatically available. The main agent calls it with:
- `description` — detailed instructions for the subagent (must be self-contained since subagent has no conversation history)
- `subagent_type` — the `name` field of the subagent to invoke

```
task(description="Search Google Scholar for...", subagent_type="research-agent")
```

### 3.3 Key Behaviors

- **Ephemeral**: Subagents are short-lived. They run, produce a result, and terminate. No follow-up messages.
- **State sharing**: Subagents receive the parent's state (excluding `messages`, `todos`, `structured_response`). This means they share the `files` dict — so **files written by the parent are readable by subagents, and files written by subagents propagate back to the parent** via Command state updates.
- **Parallel execution**: Multiple `task` calls in a single message run in parallel. This is how we parallelize research agents and source extractors.
- **Result**: The subagent's final message text is returned as a `ToolMessage` to the parent. State updates (including file changes) are merged back.
- **Independent context**: Each subagent gets a fresh context window with only the `description` as a HumanMessage. It does NOT see the parent's conversation history.
- **Default subagent**: A "general-purpose" subagent is automatically created with the same tools as the main agent, unless overridden.

### 3.4 Pre-compiled Subagents

For advanced cases, you can pass a pre-compiled LangGraph graph:

```python
from deepagents import CompiledSubAgent

custom: CompiledSubAgent = {
    "name": "custom-agent",
    "description": "Does custom things",
    "runnable": my_compiled_graph,  # must have 'messages' in state
}
```

**Relevance**: Most of our subagents (planner, research, extractor, writer, reviewer) are best defined as `SubAgent` dicts with specialized system prompts, tools, and potentially different models (cheaper model for cataloguer).

---

## 4. Skills

### 4.1 Structure

Skills are directories containing a `SKILL.md` file:

```
/skills/project/
└── essay-planning/
    ├── SKILL.md          # Required: YAML frontmatter + instructions
    └── planning_guide.md # Optional: supporting files
```

### 4.2 SKILL.md Format

```markdown
---
name: essay-planning
description: Structured approach to planning academic essays with section breakdown and word allocation
---

# Essay Planning Skill

## When to Use
- When creating a draft essay plan from an assignment brief
...

## Instructions
1. Read the assignment brief
2. ...
```

Frontmatter fields:
- `name` (required) — must match directory name, lowercase alphanumeric + hyphens, max 64 chars
- `description` (required) — max 1024 chars
- `license`, `compatibility`, `metadata`, `allowed-tools` — optional

### 4.3 Progressive Disclosure

Skills use a **progressive disclosure** pattern:
1. The agent sees skill names and descriptions in the system prompt.
2. When a skill is relevant, the agent reads the full `SKILL.md` via `read_file`.
3. The agent follows the skill's instructions.

This means skill content does NOT bloat the system prompt — only metadata is always present.

### 4.4 Skill Sources

Multiple sources can be layered (later overrides earlier):

```python
skills=[
    "/skills/base/",      # base skills
    "/skills/project/",   # project-specific overrides
]
```

### 4.5 Skills on Subagents

Subagents can have their own skill sources via the `skills` key:

```python
planner_agent: SubAgent = {
    "name": "planner",
    "skills": ["/skills/planning/"],
    ...
}
```

**Relevance**: Skills are perfect for our specialized workflows (planning, extraction, writing, reviewing). Each skill contains the detailed instructions for how that phase should work. The progressive disclosure pattern keeps system prompts lean.

---

## 5. Memory (AGENTS.md)

```python
agent = create_deep_agent(
    memory=["/memory/AGENTS.md"],
    ...
)
```

- AGENTS.md files are loaded into the system prompt at startup.
- Content is always present in context (unlike skills which are on-demand).
- Multiple sources are concatenated in order.
- The agent can update memory via `edit_file` tool.

**Relevance**: Could be used for project-level conventions (citation styles, Greek academic writing patterns) that should always be in context. However, for our use case, skills + Jinja-rendered system prompts may be more appropriate since memory content is static and verbose.

---

## 6. File Operations in Detail

The `FilesystemMiddleware` provides these tools to agents:

| Tool | Behavior |
|---|---|
| `write_file(path, content)` | Creates a new file. **Errors if file already exists** — must use `edit_file` for modifications. |
| `edit_file(path, old_string, new_string, replace_all)` | String replacement. `old_string` must be unique unless `replace_all=True`. File must be read first. |
| `read_file(path, offset, limit)` | Reads with line numbers (cat -n format). Default limit: 100 lines. |
| `ls(path)` | Lists directory contents (non-recursive). |
| `glob(pattern, path)` | Glob file matching. |
| `grep(pattern, path, glob)` | Literal text search (not regex). |

**Key constraint**: `write_file` fails on existing files. To overwrite, must `read_file` then `edit_file`. This is important for our pipeline — if a section needs rewriting, the agent must edit the existing draft, not write a new one at the same path.

---

## 7. Summarization

```python
from deepagents.middleware.summarization import create_summarization_middleware

middleware = create_summarization_middleware(model, backend)
```

- Auto-compacts conversation when token usage exceeds a configurable threshold.
- Older messages are summarized via LLM and offloaded to backend storage.
- Offloaded history stored at `/conversation_history/{thread_id}.md`.
- Applied automatically by `create_deep_agent()`.

**Relevance**: Helps keep the orchestrator's conversation manageable during long pipeline runs. No custom configuration needed for v1.

---

## 8. Human-in-the-Loop

```python
agent = create_deep_agent(
    interrupt_on={
        "write_file": True,        # pause before every write
        "task": {"subagent_type": ["research-agent"]},  # selective
    },
    checkpointer=my_checkpointer,  # required for interrupts
)
```

- Requires a checkpointer.
- Can be set at the main agent level or per subagent.
- Pauses execution for human approval before the specified tool runs.

**Relevance**: Maps directly to our human-in-the-loop checkpoints (after planning, after source assignment). We can interrupt on `task` calls or on specific file writes (e.g., writing the final plan).

---

## 9. Model Configuration

```python
# String format (auto-resolved)
model = "anthropic:claude-sonnet-4-6"
model = "anthropic:claude-haiku-4-5-20251001"
model = "openai:gpt-4o"
model = "google_genai:gemini-2.5-flash"

# Or pre-configured instance
from langchain_anthropic import ChatAnthropic
model = ChatAnthropic(model_name="claude-sonnet-4-6")
```

- Main agent and subagents can use different models.
- `model` is optional on subagents — inherits from parent if not specified.
- Default model: `claude-sonnet-4-6`.

**Relevance**: We can use cheaper models for cataloguing and extraction, and stronger models for writing and reviewing. Each subagent can specify its own model.

---

## 10. Invoking the Agent

```python
# Basic invocation
result = agent.invoke(
    {"messages": [HumanMessage(content="Write an essay about...")]}
)

# With pre-loaded files (for StateBackend)
result = agent.invoke({
    "messages": [HumanMessage(content="Write an essay...")],
    "files": {
        "/brief/assignment.md": {
            "content": ["Line 1", "Line 2"],
            "created_at": "2024-01-01T00:00:00",
            "modified_at": "2024-01-01T00:00:00",
        }
    }
})

# With checkpointer (for multi-turn / interrupts)
config = {"configurable": {"thread_id": "essay-123"}}
result = agent.invoke(
    {"messages": [HumanMessage(content="...")]},
    config=config,
)
```

**Relevance**: We can pre-load input documents into the `files` state before invoking the agent, making them immediately available via `read_file` tool.

---

## 11. Installed Dependencies

Key packages available in the project environment:

| Package | Version | Purpose |
|---|---|---|
| `deepagents` | 0.4.11 | Core framework |
| `langchain` | 1.2.13 | Agent creation, chat models |
| `langchain-anthropic` | 1.4.0 | Anthropic model integration |
| `langchain-google-genai` | 4.2.1 | Google Gemini integration |
| `langchain-core` | 1.2.20 | Base classes, tools, messages |
| `langgraph` | 1.1.3 | Graph execution, state management |
| `langgraph-checkpoint` | 4.0.1 | Checkpointing |
| `langsmith` | 0.7.22 | Tracing / observability |
| `anthropic` | 0.86.0 | Anthropic API client |
| `pydantic` | 2.12.5 | Data validation |
| `pyyaml` | 6.0.3 | YAML parsing |
| `requests` | 2.32.5 | HTTP client |
| `httpx` | 0.28.1 | Async HTTP client |

**Not installed** (will need to be added):
- `python-docx` — for .docx generation
- `jinja2` — for prompt templating
- `dynaconf` — if we go the YAML config route
- Academic search libraries / API clients (scholarly, serpapi, etc.)
- PDF parsing (pymupdf, pdfplumber, etc.)

---

## 12. Key Patterns for Our Architecture

### 12.1 VFS = Backend File System

Our "VFS" maps directly to the backend's file system. When agents call `write_file("/plan/draft.md", content)`, this writes to the backend. Other agents (parent or sibling subagents) can read it via `read_file("/plan/draft.md")`.

With `StateBackend` (default), this is all in-memory within the LangGraph state. With `FilesystemBackend` or `CompositeBackend`, it can persist to disk.

### 12.2 Subagent Parallelism

The main agent can spawn multiple subagents in parallel by making multiple `task` tool calls in a single message. The framework handles parallel execution natively:

```
# Main agent's tool calls in one message:
task(description="Extract source 1...", subagent_type="source-extractor")
task(description="Extract source 2...", subagent_type="source-extractor")
task(description="Extract source 3...", subagent_type="source-extractor")
# All three run in parallel
```

### 12.3 State Propagation

When a subagent writes files, the state update propagates back to the parent. The parent can then read those files. This is the mechanism by which extractors write per-section entries and writers read them.

Flow: extractor writes → state update → parent receives → next writer reads.

### 12.4 Different Models per Role

```python
subagents = [
    {"name": "cataloguer", "model": "google_genai:gemini-2.5-flash", ...},
    {"name": "extractor", "model": "anthropic:claude-sonnet-4-6", ...},
    {"name": "writer", "model": "anthropic:claude-sonnet-4-6", ...},
]
```

### 12.5 Skills for Workflow Instructions

Each workflow step can have a dedicated skill with detailed instructions:

```
/skills/project/
├── essay-writing/SKILL.md
├── essay-review/SKILL.md
└── docx-export/SKILL.md
```

The agent reads the relevant skill only when entering that phase — progressive disclosure keeps the system prompt lean.

### 12.6 File Write Constraint

`write_file` errors on existing files. For retry/rewrite flows (e.g., word count adjustment), the rewrite agent must use `edit_file` to modify the existing draft, or the orchestrator must manage file naming (e.g., `draft_v1.md`, `draft_v2.md`).
