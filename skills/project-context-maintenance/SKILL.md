---
name: project-context-maintenance
description: Maintain project CONTEXT.md, MEMORY.md, active skills, candidates, and handoff from the local Codex session archive.
---

# Project Context Maintenance

Use this skill when a Codex hook creates `.agents/memory/session-logs/<session-id>/maintenance-request.json`, or when the user asks to consolidate project context.

1. Read the pending request and its `session.jsonl`. Treat `session.jsonl` as the authoritative source; `session.md` and `INDEX.md` are derived views.
2. Inspect the existing `.agents/memory/CONTEXT.md`, `MEMORY.md`, active `.agents/skills/*/SKILL.md`, and candidate skills before changing anything.
3. Preserve useful facts and open work. Do not invent facts, and do not copy secrets into long-term memory.
4. Write a strict JSON result to a temporary file, then apply it with:

```bash
python3 "$PLUGIN_ROOT/scripts/contextctl.py" apply-maintenance \
  --project-root "$PWD" \
  --request ".agents/memory/session-logs/<session-id>/maintenance-request.json" \
  --input /path/to/result.json
```

The result must use `schema: "project-context/2"`.

`MEMORY.md` is a bounded projection, not the session archive. Its four sections and their headings
are owned by the plugin, so send the entries and let it render:

`Project` (purpose, stack, structure), `Invariants` (standing decisions, conventions, hard
constraints, user preferences), `Pitfalls` (operational traps and the lessons behind them), `Index`
(pointers to docs, source files, and commands).

For consolidation:

```json
{
  "schema": "project-context/2",
  "memory": {
    "project": ["one durable fact per entry"],
    "invariants": ["..."],
    "pitfalls": ["..."],
    "index": ["path - what it owns"]
  },
  "context": {
    "title": "...",
    "summary": "...",
    "key_points": ["..."],
    "open_tasks": ["..."]
  }
}
```

Rules for the entries:

- Carry forward every entry that is still true; merge duplicates and tighten wording instead of
  deleting. An entry is dropped only when something else supersedes it or already covers it.
- Write only this project's own durable facts. Never carry another repository's state or measurements
  (commit distance, file sizes, key counts) into memory or context.
- No secrets, no command transcripts, no inline tables or formulas. `Index` entries are pointers, not
  summaries.
- `context` is working state: what this session is about, what it found, what is still open. Facts
  that will outlive the session belong in `memory` instead.
- The per-section sizes are targets, not caps: the plugin pools what one section leaves unused for the
  ones that need more, and only the document cap itself drops an entry. Keep `maxMemoryChars` in mind
  but do not drop a true entry to fit a section.

`memory_markdown` is still accepted for compatibility and is written as a free-form document; prefer
the structured `memory` shape above so the plugin can render and budget it. Either way the plugin
cuts at line boundaries, keeps the head and the tail, and leaves an explicit marker when it has to
clip. The raw session log remains the source for details.

For learning:

```json
{
  "schema": "project-context/2",
  "active_skills": [
    {"name": "lowercase-kebab-case", "markdown": "...", "evidence_session_ids": ["s1", "s2"]}
  ],
  "skill_candidates": [
    {"name": "lowercase-kebab-case", "markdown": "...", "evidence_session_ids": ["s1"]}
  ]
}
```

An active skill needs evidence from at least two archived sessions; a candidate needs at least one.
Invalid schemas are rejected without changing long-term files. Candidates are written below
`.agents/memory/skill-candidates/` for later review; active skills are written below
`.agents/skills/`.

Each `markdown` value is a complete `SKILL.md` (frontmatter with `name` and `description`, then the
body). The plugin stamps its provenance marker into the body, and a later pass may only supersede a
skill that carries that marker: a hand-written skill with the same name is refused, not overwritten.
Write a new name instead, or leave the skill alone.

If a maintenance attempt must be abandoned, record it and create a new attempt instead of editing the request by hand:

```bash
python3 "$PLUGIN_ROOT/scripts/contextctl.py" mark-failed \
  --request ".agents/memory/session-logs/<session-id>/maintenance-request.json" \
  --reason "why this attempt was abandoned"
python3 "$PLUGIN_ROOT/scripts/contextctl.py" retry-maintenance \
  --request ".agents/memory/session-logs/<session-id>/maintenance-request.json"
```

After compaction, the next `SessionStart` with `source: compact` supplies the bounded context; `PostCompact` only reports the handoff path because Codex does not treat that event as an additional-context injection point.
