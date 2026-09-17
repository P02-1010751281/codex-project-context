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

For consolidation:

```json
{
  "schema": "project-context/2",
  "memory_markdown": "...",
  "context": {
    "title": "...",
    "summary": "...",
    "key_points": ["..."],
    "open_tasks": ["..."]
  }
}
```

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

An active skill needs evidence from at least two archived sessions; a candidate needs at least one. Invalid schemas are rejected without changing long-term files. Candidates are written below `.agents/memory/skill-candidates/` for later review; active skills are written below `.agents/skills/`.

If a maintenance attempt must be abandoned, record it and create a new attempt instead of editing the request by hand:

```bash
python3 "$PLUGIN_ROOT/scripts/contextctl.py" mark-failed \
  --request ".agents/memory/session-logs/<session-id>/maintenance-request.json" \
  --reason "why this attempt was abandoned"
python3 "$PLUGIN_ROOT/scripts/contextctl.py" retry-maintenance \
  --request ".agents/memory/session-logs/<session-id>/maintenance-request.json"
```

After compaction, the next `SessionStart` with `source: compact` supplies the bounded context; `PostCompact` only reports the handoff path because Codex does not treat that event as an additional-context injection point.
