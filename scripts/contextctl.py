#!/usr/bin/env python3
"""Deterministic storage and lifecycle adapter for Codex project context.

The module deliberately treats Codex transcripts as opaque bytes.  It only
uses the hook contract and never depends on an undocumented transcript JSON
schema.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator


SCHEMA = "project-context/2"
MAX_MEMORY_CHARS = 32_000
MIN_MEMORY_CHARS = 4_000
MAX_MEMORY_CHARS_LIMIT = 200_000
MEMORY_TRUNCATION_LINE = re.compile(r"^_\[memory truncated at \d+ characters: \d+ dropped\]_$")
MEMORY_JOURNAL_ROTATE_BYTES = 512 * 1024
MEMORY_BACKUPS_KEPT = 5
MEMORY_BACKUPS_MAX = 20
MEMORY_BACKUP_MIN_AGE_SECONDS = 60 * 60
MAX_ERROR_LOG_BYTES = 1_000_000
KEEP_ERROR_LOG_BYTES = 64_000
DEFAULTS: dict[str, Any] = {
    "archiveEnabled": True,
    "autoConsolidate": True,
    "autoLearn": True,
    "handoffEnabled": True,
    "autolearnAt": 0,
    "autolearnTurns": 20,
    "autolearnIntervalMs": 1_800_000,
    "consolidateTurns": 6,
    "consolidateIntervalMs": 300_000,
    "forceDedupeMs": 15_000,
    "maxTokens": 8192,
    "provider": "",
    "model": "",
    "codexHookEnabled": True,
    "maintenanceMode": "stop-continuation",
    "maxInjectedChars": 12_000,
    "maxMemoryChars": MAX_MEMORY_CHARS,
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, length: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as handle:
        while remaining is None or remaining > 0:
            block_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def read_file_slice(path: Path, start: int, end: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(max(0, end - start))


def safe_id(value: str | None, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-")
    if cleaned in {".", ".."}:
        return fallback
    return cleaned[:160] or fallback


def session_storage_id(value: str | None) -> str:
    """Encode an opaque Codex session id without lossy or colliding normalization."""
    raw = value if value is not None else "<codex-missing-session-id>"
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return f"s-{encoded or 'empty'}"


def project_root(cwd: str | None) -> Path:
    candidate = Path(cwd or os.getcwd()).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return candidate


def memory_root(root: Path) -> Path:
    return root / ".agents" / "memory"


def memory_file(root: Path) -> Path:
    return memory_root(root) / "MEMORY.md"


def memory_journal_file(root: Path) -> Path:
    return memory_root(root) / "memory.jsonl"


def session_root(root: Path, session_id: str) -> Path:
    return memory_root(root) / "session-logs" / session_storage_id(session_id)


def load_config(root: Path) -> dict[str, Any]:
    path = memory_root(root) / "project-context.json"
    result = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
                    result[key] = value
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return result


def memory_limit(root: Path) -> int:
    value = load_config(root).get("maxMemoryChars", MAX_MEMORY_CHARS)
    if isinstance(value, bool) or not isinstance(value, int):
        return MAX_MEMORY_CHARS
    return max(MIN_MEMORY_CHARS, min(MAX_MEMORY_CHARS_LIMIT, value))


def ensure_memory_gitignore(root: Path) -> None:
    path = memory_root(root) / ".gitignore"
    lines = [
        "# project-context: local memory artifacts",
        "*.memory-backup-*",
        "memory.jsonl",
        "memory-log-*.jsonl",
        "errors.log",
        "*.lock",
        "*.steal",
        "*.tmp",
        "*.broken-*",
        ".maintenance-transaction.json",
    ]
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        present = {line.strip() for line in existing.splitlines()}
        missing = [line for line in lines if line not in present]
        if missing:
            prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
            atomic_write(path, prefix + "\n".join(missing) + "\n")
    except OSError:
        # Local housekeeping must never prevent context persistence.
        pass


def ensure_layout(root: Path) -> None:
    assert_project_layout_safe(root)
    recover_maintenance_transaction(root)
    (root / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
    (memory_root(root) / "session-logs").mkdir(parents=True, exist_ok=True)
    (memory_root(root) / "skill-candidates").mkdir(parents=True, exist_ok=True)
    for path in (memory_root(root) / "MEMORY.md", memory_root(root) / "CONTEXT.md"):
        if not path.exists():
            atomic_write(path, "")
    index = memory_root(root) / "session-logs" / "INDEX.md"
    if not index.exists():
        atomic_write(index, "# Session index\n\n")
    logs_ignore = memory_root(root) / "session-logs" / ".gitignore"
    if not logs_ignore.exists():
        atomic_write(logs_ignore, "*/\n")
    ensure_memory_gitignore(root)


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def atomic_write_many(entries: list[tuple[Path, bytes | str]], journal: Path | None = None) -> None:
    staged: list[tuple[Path, Path]] = []
    originals: dict[Path, bytes | None] = {}
    backups: dict[Path, Path | None] = {}
    applied: list[Path] = []
    try:
        for path, data in entries:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = data.encode("utf-8") if isinstance(data, str) else data
            originals[path] = path.read_bytes() if path.exists() and not path.is_symlink() else None
            backup = None
            if originals[path] is not None:
                fd, name = tempfile.mkstemp(prefix=f".{path.name}.backup-", dir=str(path.parent))
                backup = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(originals[path])
                    handle.flush()
                    os.fsync(handle.fileno())
            backups[path] = backup
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            temp = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((temp, path))
        if journal:
            entries_json = [
                {"target": str(path), "temp": str(temp), "backup": str(backups[path]) if backups[path] else None, "had_original": originals[path] is not None}
                for temp, path in staged
            ]
            atomic_write(journal, json.dumps({"phase": "committing", "entries": entries_json}, ensure_ascii=False))
        for temp, path in staged:
            os.replace(temp, path)
            applied.append(path)
        for parent in {path.parent for _, path in staged}:
            try:
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        if journal:
            atomic_write(journal, json.dumps({"phase": "committed", "entries": [
                {"target": str(path), "temp": str(temp), "backup": str(backups[path]) if backups[path] else None, "had_original": originals[path] is not None}
                for temp, path in staged
            ]}, ensure_ascii=False))
            journal.unlink(missing_ok=True)
            fsync_directory(journal.parent)
    except Exception:
        for path in reversed(applied):
            backup = backups.get(path)
            if backup and backup.exists():
                os.replace(backup, path)
            elif originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                try:
                    atomic_write(path, originals[path])
                except OSError:
                    pass
        if journal:
            journal.unlink(missing_ok=True)
        raise
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        for backup in backups.values():
            if backup:
                backup.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def memory_truncation_marker(dropped: int, limit: int) -> str:
    return f"_[memory truncated at {limit} characters: {dropped} dropped]_"


def _clip_to_line_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: max(0, limit)]
    cut = head.rfind("\n")
    return head[:cut] if cut > 0 else head


def normalize_memory_document(value: str, limit: int = MAX_MEMORY_CHARS) -> str:
    body = value.strip()
    body = re.sub(r"^```(?:markdown|md)?\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*```$", "", body).strip()
    body = re.sub(r"^#\s*Project Memory\s*", "", body, count=1, flags=re.IGNORECASE).strip()
    lines = body.splitlines()
    previous = next((line.strip() for line in lines if MEMORY_TRUNCATION_LINE.fullmatch(line.strip())), None)
    body = "\n".join(line for line in lines if not MEMORY_TRUNCATION_LINE.fullmatch(line.strip())).strip()
    document = f"# Project Memory\n\n{body}"
    if len(document) <= limit:
        suffix = f"\n\n{previous}" if previous else ""
        return f"{document}{suffix}".rstrip() + "\n"
    kept = _clip_to_line_boundary(document, limit).rstrip()
    return f"{kept}\n\n{memory_truncation_marker(len(document) - len(kept), limit)}\n"


def _poison_prefix_is_safe(prefix: str) -> bool:
    value = prefix.strip()
    if not value or value in {"json", "JSON", "[", "```", "```json", "```JSON", "```markdown"}:
        return True
    if "\n" in value or len(value) > 40 or not re.search(r"[:：]$", value):
        return False
    return not re.search(r"[*_#`>|\-\[\]]", value[:-1])


def decode_poisoned_memory(value: str, limit: int = MAX_MEMORY_CHARS) -> str | None:
    """Recover a prior consolidation reply accidentally stored as MEMORY.md."""
    body = re.sub(r"^#\s*Project Memory\s*", "", value.strip(), count=1, flags=re.IGNORECASE)
    object_at = body.find("{")
    if object_at < 0 or not _poison_prefix_is_safe(body[:object_at]):
        return None
    try:
        parsed, end = json.JSONDecoder().raw_decode(body[object_at:])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("memory_markdown"), str) or "context" not in parsed:
        return None
    trailing = body[object_at + end :].strip()
    if trailing not in {"", "```", "]"}:
        return None
    decoded = parsed["memory_markdown"].strip()
    if not decoded:
        return None
    canonical_prefix = body[:object_at].strip() in {"", "json", "JSON", "[", "```", "```json", "```JSON", "```markdown"}
    if not re.match(r"^#\s*Project Memory\b", decoded, flags=re.IGNORECASE) and (
        not canonical_prefix or len(decoded) < 120 or "\n" not in decoded
    ):
        return None
    return normalize_memory_document(decoded, limit)


def _memory_record(op: str, text: str) -> bytes:
    return json_bytes({"ts": now(), "op": op, "text": text}) + b"\n"


def _append_memory_record(raw: bytes, op: str, text: str) -> bytes:
    return raw + (b"\n" if raw and not raw.endswith(b"\n") else b"") + _memory_record(op, text)


def read_memory_journal(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"exists": False, "raw": b"", "entries": [], "damaged": 0, "unreadable": False}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return state
    except OSError:
        state["exists"] = True
        state["unreadable"] = True
        return state
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        state["exists"] = True
        state["raw"] = raw
        state["unreadable"] = True
        return state
    entries: list[dict[str, str]] = []
    damaged = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            damaged += 1
            continue
        if isinstance(item, dict) and item.get("op") in {"replace", "append"} and isinstance(item.get("text"), str):
            entries.append({"op": item["op"], "text": item["text"]})
        else:
            damaged += 1
    state.update({"exists": True, "raw": raw, "entries": entries, "damaged": damaged})
    return state


def fold_memory_journal(entries: list[dict[str, str]], limit: int = MAX_MEMORY_CHARS) -> str:
    document = ""
    for entry in entries:
        text = entry["text"].strip()
        if not text:
            continue
        document = text if entry["op"] == "replace" else (f"{document}\n\n{text}" if document else text)
    return normalize_memory_document(document, limit) if document else ""


def _read_memory_render(root: Path) -> bytes:
    try:
        return memory_file(root).read_bytes()
    except FileNotFoundError:
        return b""


def _decode_memory_render(raw: bytes, limit: int = MAX_MEMORY_CHARS) -> str:
    if not raw.strip():
        return ""
    text = raw.decode("utf-8")
    return decode_poisoned_memory(text, limit) or normalize_memory_document(text, limit)


def load_memory_document(root: Path, limit: int | None = None) -> str:
    """Read the journal projection, falling back to legacy MEMORY.md when needed."""
    limit = memory_limit(root) if limit is None else limit
    try:
        assert_safe_project_path(root, memory_file(root))
        assert_safe_project_path(root, memory_journal_file(root))
    except OSError:
        return ""
    journal = memory_journal_file(root)
    state = read_memory_journal(journal)
    if state["unreadable"] or (state["damaged"] and not state["entries"]):
        return ""
    if state["entries"]:
        folded = fold_memory_journal(state["entries"], limit)
        try:
            render = _read_memory_render(root)
            if render.strip() and journal.exists() and memory_file(root).stat().st_mtime > journal.stat().st_mtime:
                external = _decode_memory_render(render, limit)
                if external and external != folded:
                    return external
        except OSError:
            pass
        return folded
    try:
        return _decode_memory_render(_read_memory_render(root), limit)
    except (OSError, UnicodeDecodeError):
        return ""


def _artifact_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def _prune_memory_backups(root: Path) -> None:
    directory = memory_root(root)
    pattern = re.compile(r"^MEMORY\.md\.memory-backup-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z-[0-9a-f]{8}$")
    try:
        candidates = [
            (entry, entry.stat().st_mtime)
            for entry in directory.iterdir()
            if entry.is_file() and pattern.fullmatch(entry.name)
        ]
    except OSError:
        return
    candidates.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - MEMORY_BACKUP_MIN_AGE_SECONDS
    for index, (entry, mtime) in enumerate(candidates[MEMORY_BACKUPS_KEPT:], MEMORY_BACKUPS_KEPT):
        if mtime > cutoff and len(candidates) - index <= MEMORY_BACKUPS_MAX:
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def backup_memory_before_write(root: Path, limit: int = MAX_MEMORY_CHARS) -> tuple[Path | None, bool]:
    """Keep the exact current MEMORY.md bytes; an unreadable file fails closed."""
    path = memory_file(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, False
    if not raw:
        return None, False
    # Decode before replacing so a binary/unreadable MEMORY.md is never overwritten.
    poisoned = bool(decode_poisoned_memory(raw.decode("utf-8"), limit))
    ensure_memory_gitignore(root)
    backup = memory_root(root) / f"MEMORY.md.memory-backup-{_artifact_stamp()}-{uuid.uuid4().hex[:8]}"
    atomic_write(backup, raw)
    _prune_memory_backups(root)
    return backup, poisoned


def prepare_memory_write(root: Path, text: str, limit: int | None = None) -> tuple[str, bytes, Path | None, bool, int]:
    limit = memory_limit(root) if limit is None else limit
    normalized = normalize_memory_document(text, limit)
    journal = memory_journal_file(root)
    state = read_memory_journal(journal)
    if state["unreadable"] or (state["damaged"] and not state["entries"]):
        raise ValueError(f"memory journal exists but has no usable records: {journal}")
    render = _read_memory_render(root)
    backup, poisoned = backup_memory_before_write(root, limit)
    journal_raw = state["raw"] if state["exists"] else b""
    if state["entries"]:
        folded = fold_memory_journal(state["entries"], limit)
        if render.strip():
            external = _decode_memory_render(render, limit)
            try:
                is_newer = memory_file(root).stat().st_mtime > journal.stat().st_mtime
            except OSError:
                is_newer = False
            if external and external != folded and is_newer:
                journal_raw = _append_memory_record(journal_raw, "replace", external)
    elif render.strip():
        journal_raw = _append_memory_record(journal_raw, "replace", _decode_memory_render(render, limit))
    journal_raw = _append_memory_record(journal_raw, "replace", normalized)
    return normalized, journal_raw, backup, poisoned, int(state["damaged"])


def rotate_memory_journal(root: Path) -> None:
    path = memory_journal_file(root)
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if len(raw) <= MEMORY_JOURNAL_ROTATE_BYTES:
        return
    state = read_memory_journal(path)
    folded = fold_memory_journal(state["entries"], memory_limit(root)) if state["entries"] else ""
    if state["unreadable"] or not folded:
        return
    archive = memory_root(root) / f"memory-log-{_artifact_stamp()}-{uuid.uuid4().hex[:8]}.jsonl"
    try:
        atomic_write(archive, raw)
        atomic_write(path, _memory_record("replace", folded))
    except OSError:
        return
    pattern = re.compile(r"^memory-log-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z-[0-9a-f]{8}\.jsonl$")
    try:
        archives = sorted(
            (entry for entry in memory_root(root).iterdir() if entry.is_file() and pattern.fullmatch(entry.name)),
            key=lambda entry: (entry.stat().st_mtime, entry.name),
            reverse=True,
        )
        for entry in archives[5:]:
            if entry.stat().st_mtime < dt.datetime.now(dt.timezone.utc).timestamp() - MEMORY_BACKUP_MIN_AGE_SECONDS:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def seed_memory_journal(root: Path) -> None:
    journal = memory_journal_file(root)
    if journal.exists():
        return
    try:
        assert_safe_project_path(root, memory_file(root))
        assert_safe_project_path(root, journal)
        raw = _read_memory_render(root)
        if not raw.strip():
            return
        text = _decode_memory_render(raw, memory_limit(root))
        ensure_memory_gitignore(root)
        atomic_write(journal, _memory_record("replace", text))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        append_error(root, error)


def assert_safe_project_path(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    candidate = path if path.is_absolute() else root / path
    lexical = candidate.absolute()
    try:
        lexical_relative = lexical.relative_to(root.absolute())
    except ValueError as error:
        raise OSError(f"project path escapes root: {candidate}") from error
    current = root.absolute()
    for part in lexical_relative.parts:
        current /= part
        if current.is_symlink():
            raise OSError(f"symlink project path is not allowed: {current}")
    try:
        relative = candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as error:
        raise OSError(f"project path escapes root: {candidate}") from error
    current = root_resolved
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise OSError(f"symlink project path is not allowed: {current}")


def assert_project_layout_safe(root: Path) -> None:
    for relative in (
        ".agents",
        ".agents/skills",
        ".agents/memory",
        ".agents/memory/session-logs",
        ".agents/memory/skill-candidates",
    ):
        assert_safe_project_path(root, root / relative)


def recover_maintenance_transaction(root: Path) -> None:
    journal = memory_root(root) / ".maintenance-transaction.json"
    record = read_json(journal, None)
    if not isinstance(record, dict):
        return
    entries = record.get("entries")
    if not isinstance(entries, list):
        journal.unlink(missing_ok=True)
        return
    try:
        for entry in entries:
            assert_safe_project_path(root, Path(str(entry.get("target", ""))))
            for key in ("temp", "backup"):
                if entry.get(key):
                    assert_safe_project_path(root, Path(str(entry[key])))
    except OSError as error:
        append_error(root, error)
        return
    if record.get("phase") == "committed":
        for entry in entries:
            for key in ("temp", "backup"):
                if isinstance(entry.get(key), str):
                    Path(entry[key]).unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
        return
    for entry in reversed(entries):
        target = Path(str(entry.get("target", "")))
        backup = Path(str(entry.get("backup", ""))) if entry.get("backup") else None
        assert_safe_project_path(root, target)
        if backup and backup.exists():
            os.replace(backup, target)
        elif not entry.get("had_original"):
            target.unlink(missing_ok=True)
        if isinstance(entry.get("temp"), str):
            Path(entry["temp"]).unlink(missing_ok=True)
    journal.unlink(missing_ok=True)


def repair_jsonl(path: Path) -> None:
    if not path.exists():
        return
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n") + 1
    with path.open("r+b") as handle:
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())


def existing_ids(path: Path) -> set[str]:
    result: set[str] = set()
    try:
        with path.open("rb") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        result.add(item["id"])
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    except FileNotFoundError:
        pass
    return result


def append_envelopes(path: Path, envelopes: list[dict[str, Any]]) -> None:
    if not envelopes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for envelope in envelopes:
            handle.write(json_bytes(envelope) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def raw_envelope(session_id: str, source_path: str, identity: str, start: int, end: int, payload: bytes) -> dict[str, Any]:
    payload_digest = sha256_bytes(payload)
    return {
        "version": 1,
        "id": f"chunk:{identity}:{start}:{end}:{payload_digest}",
        "kind": "transcript-chunk",
        "session_id": session_id,
        "event": "",
        "source_path": source_path,
        "source_identity": identity,
        "from": start,
        "to": end,
        "payload_sha256": payload_digest,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "timestamp": now(),
    }


def native_import_event(session_id: str, source_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Record provenance for an imported native Codex rollout file."""
    payload = {
        "hook_event_name": "CodexNativeImport",
        "source_path": str(source_path),
        "native_session_id": str(metadata.get("session_id") or metadata.get("id") or ""),
        "cwd": str(metadata.get("cwd") or ""),
        "originator": metadata.get("originator"),
        "source": metadata.get("source"),
        "thread_source": metadata.get("thread_source"),
        "forked_from_id": metadata.get("forked_from_id"),
        "imported_as_opaque_bytes": True,
    }
    event = event_envelope(payload, session_id)
    event["kind"] = "native-codex-metadata"
    event["source_path"] = str(source_path)
    return event


def native_session_metadata(path: Path) -> dict[str, Any] | None:
    """Read only the native rollout header needed for project routing."""
    try:
        with path.open("rb") as handle:
            for raw in handle:
                try:
                    item = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                    return item["payload"]
                payload = item.get("payload")
                if isinstance(payload, dict) and payload.get("cwd"):
                    return payload
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def import_codex_sessions(source_roots: list[Path], project_roots: list[Path], unmatched_root: Path | None = None) -> dict[str, Any]:
    """Import native Codex rollout JSONL files into matching project archives.

    Native files are treated as opaque bytes and split into bounded chunks.  The
    importer never parses message/tool schemas, and chunk IDs make reruns safe.
    """
    chunk_size = 4 * 1024 * 1024
    normalized_projects = [(root.resolve(), root) for root in project_roots]
    stats: dict[str, Any] = {"files_seen": 0, "files_imported": 0, "chunks_added": 0, "bytes_imported": 0, "unmatched": 0, "unmatched_imported": 0, "errors": []}
    touched: set[Path] = set()
    for source_root in source_roots:
        source_root = source_root.expanduser().resolve()
        if not source_root.exists():
            stats["errors"].append(f"missing source root: {source_root}")
            continue
        for source_path in sorted(source_root.rglob("*.jsonl")):
            if source_path.is_symlink() or not source_path.is_file():
                continue
            stats["files_seen"] += 1
            metadata = native_session_metadata(source_path)
            cwd_value = str((metadata or {}).get("cwd") or "")
            cwd = Path(cwd_value).expanduser().resolve() if cwd_value else None
            candidates = [(root, original) for root, original in normalized_projects if cwd and path_is_under(cwd, root)]
            is_unmatched = not metadata or not candidates
            if is_unmatched and unmatched_root is None:
                stats["unmatched"] += 1
                continue
            target_root = (unmatched_root if is_unmatched else max(candidates, key=lambda item: len(item[0].parts))[1]).resolve()
            session_value = str((metadata or {}).get("session_id") or (metadata or {}).get("id") or source_path.stem)
            target = session_root(target_root, session_value)
            assert_safe_project_path(target_root, target)
            target.mkdir(parents=True, exist_ok=True)
            log = target / "session.jsonl"
            with file_lock(target / ".lock"):
                repair_jsonl(log)
                ids = existing_ids(log)
                metadata_event = native_import_event(session_value, source_path, metadata)
                if metadata_event["id"] not in ids:
                    append_envelopes(log, [metadata_event])
                    ids.add(metadata_event["id"])
                identity_stat = source_path.stat()
                identity = f"{source_path}|{identity_stat.st_dev}|{identity_stat.st_ino}"
                imported_chunks = 0
                imported_bytes = 0
                offset = 0
                with source_path.open("rb") as handle:
                    while True:
                        payload = handle.read(chunk_size)
                        if not payload:
                            break
                        envelope = raw_envelope(session_value, str(source_path), identity, offset, offset + len(payload), payload)
                        envelope["kind"] = "native-codex-session"
                        envelope["native_cwd"] = cwd_value
                        envelope["native_session_id"] = session_value
                        if envelope["id"] not in ids:
                            append_envelopes(log, [envelope])
                            ids.add(envelope["id"])
                            imported_chunks += 1
                            imported_bytes += len(payload)
                        offset += len(payload)
                final_stat = source_path.stat()
                if final_stat.st_size != offset or (final_stat.st_dev, final_stat.st_ino) != (identity_stat.st_dev, identity_stat.st_ino):
                    raise OSError(f"native rollout changed while importing: {source_path}")
                _render_session_unlocked(target)
            touched.add(target_root)
            stats["files_imported"] += 1
            if is_unmatched:
                stats["unmatched_imported"] += 1
            stats["chunks_added"] += imported_chunks
            stats["bytes_imported"] += imported_bytes
    for root in sorted(touched):
        rebuild_index(root)
    stats["projects_touched"] = [str(root) for root in sorted(touched)]
    return stats


def event_envelope(payload: dict[str, Any], session_id: str) -> dict[str, Any]:
    raw = json_bytes(payload)
    digest = sha256_bytes(raw)
    event = str(payload.get("hook_event_name") or payload.get("event") or "unknown")
    return {
        "version": 1,
        "id": f"event:{event}:{digest}",
        "kind": "hook-event",
        "session_id": session_id,
        "event": event,
        "source_path": str(payload.get("transcript_path") or ""),
        "source_identity": "",
        "from": None,
        "to": None,
        "payload_sha256": digest,
        "payload_b64": base64.b64encode(raw).decode("ascii"),
        "timestamp": now(),
    }


def reset_envelope(session_id: str, source_path: str, identity: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    payload = json_bytes({
        "reason": "transcript-rewritten-or-replaced",
        "old": {key: old.get(key) for key in ("path", "dev", "ino", "source_size", "source_sha256")},
        "new": {key: new.get(key) for key in ("path", "dev", "ino", "source_size", "source_sha256")},
    })
    digest = sha256_bytes(payload)
    return {
        "version": 1,
        "id": f"reset:{identity}:{digest}",
        "kind": "source-reset",
        "session_id": session_id,
        "event": "",
        "source_path": source_path,
        "source_identity": identity,
        "from": None,
        "to": None,
        "payload_sha256": digest,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "timestamp": now(),
    }


def validate_transcript_path(value: str) -> Path:
    transcript = Path(value).expanduser()
    file_stat = transcript.lstat()
    if transcript.is_symlink() or not stat_module.S_ISREG(file_stat.st_mode):
        raise OSError(f"transcript_path is not a regular non-symlink file: {transcript}")
    return transcript


def transcript_delta(transcript: Path, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    file_stat = transcript.stat()
    size = file_stat.st_size
    identity = f"{transcript.resolve()}|{file_stat.st_dev}|{file_stat.st_ino}"
    old = state if isinstance(state, dict) else {}
    old_size = int(old.get("source_size") or old.get("offset") or 0)
    old_prefix = str(old.get("prefix_sha256") or "")
    old_digest = str(old.get("source_sha256") or "")
    old_identity = str(old.get("source_identity") or "")
    unchanged = bool(old) and old_identity == identity and old_size == size and int(old.get("mtime_ns") or 0) == file_stat.st_mtime_ns
    full_digest = old_digest if unchanged else sha256_file(transcript)
    after_hash_stat = transcript.stat()
    if (after_hash_stat.st_dev, after_hash_stat.st_ino, after_hash_stat.st_size, after_hash_stat.st_mtime_ns) != (
        file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns
    ):
        file_stat = after_hash_stat
        size = file_stat.st_size
        identity = f"{transcript.resolve()}|{file_stat.st_dev}|{file_stat.st_ino}"
        full_digest = sha256_file(transcript)
        final_stat = transcript.stat()
        if (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns) != (
            file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns
        ):
            raise OSError("transcript changed continuously; retry on the next hook")
    reset = bool(old) and (
        old_identity != identity
        or size < old_size
        or (size == old_size and old_digest and old_digest != full_digest)
        or (size > old_size and old_prefix and sha256_file(transcript, old_size) != old_prefix)
    )

    envelopes: list[dict[str, Any]] = []
    offset = 0 if reset else min(old_size, size)
    if reset:
        new_state = {
            "path": str(transcript.resolve()),
            "source_identity": identity,
            "dev": file_stat.st_dev,
            "ino": file_stat.st_ino,
            "source_size": size,
            "source_sha256": full_digest,
        }
        envelopes.append(reset_envelope("", str(transcript), identity, old, new_state))
    if size > offset:
        payload = read_file_slice(transcript, offset, size)
        if payload:
            envelopes.append(raw_envelope("", str(transcript), identity, offset, offset + len(payload), payload))
    final_read_stat = transcript.stat()
    if (final_read_stat.st_dev, final_read_stat.st_ino, final_read_stat.st_size, final_read_stat.st_mtime_ns) != (
        file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns
    ):
        raise OSError("transcript changed while reading; retry on the next hook")

    new_state = {
        "path": str(transcript.resolve()),
        "source_identity": identity,
        "dev": file_stat.st_dev,
        "ino": file_stat.st_ino,
        "source_size": size,
        "source_sha256": full_digest,
        "prefix_sha256": full_digest,
        "offset": size,
        "mtime_ns": file_stat.st_mtime_ns,
        "last_updated": now(),
    }
    return envelopes, new_state


def render_session(session_dir: Path) -> None:
    with file_lock(session_dir / ".lock"):
        repair_jsonl(session_dir / "session.jsonl")
        _render_session_unlocked(session_dir)


def _render_session_unlocked(session_dir: Path) -> None:
    log = session_dir / "session.jsonl"
    lines = [f"# Session {session_dir.name}", "", f"Raw archive: `{log.name}`", ""]
    try:
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        records = []
    for item in records:
        kind = item.get("kind", "unknown")
        timestamp = item.get("timestamp", "")
        if kind in {"transcript-chunk", "native-codex-session"}:
            try:
                size = len(base64.b64decode(item.get("payload_b64", ""), validate=True))
            except (ValueError, TypeError):
                size = 0
            label = "native Codex bytes" if kind == "native-codex-session" else "transcript bytes"
            lines.append(f"- `{timestamp}` {label} `{item.get('from')}`–`{item.get('to')}` ({size} bytes)")
        elif kind == "native-codex-metadata":
            try:
                payload = json.loads(base64.b64decode(item.get("payload_b64", "")))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            lines.append(f"- `{timestamp}` imported native Codex rollout `{Path(str(payload.get('source_path') or item.get('source_path') or '')).name}`")
        elif kind == "source-reset":
            lines.append(f"- `{timestamp}` transcript source reset")
        elif kind == "hook-event":
            try:
                payload = json.loads(base64.b64decode(item.get("payload_b64", "")))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            event = item.get("event", "hook")
            detail = payload.get("prompt") or payload.get("tool_name") or payload.get("reason") or ""
            detail = str(detail).replace("\n", " ")[:240]
            lines.append(f"- `{timestamp}` `{event}` {detail}".rstrip())
    atomic_write(session_dir / "session.md", "\n".join(lines) + "\n")


def rebuild_index(root: Path) -> None:
    with file_lock(memory_root(root) / ".index.lock"):
        _rebuild_index_locked(root)


def _rebuild_index_locked(root: Path) -> None:
    base = memory_root(root) / "session-logs"
    rows: list[tuple[float, str, str]] = []
    for directory in base.iterdir() if base.exists() else []:
        if directory.is_symlink() or not directory.is_dir() or directory.name.startswith("."):
            continue
        title = ""
        try:
            for line in (directory / "session.md").read_text(encoding="utf-8").splitlines():
                if line.startswith("- "):
                    title = line[2:]
                    break
        except OSError:
            pass
        rows.append((directory.stat().st_mtime, directory.name, title or "(no event summary)"))
    rows.sort(key=lambda item: (-item[0], item[1]))
    lines = ["# Session index", "", "| Session | Summary |", "|---|---|"]
    for _, session_id, title in rows:
        lines.append(f"| `{session_id}` | {title.replace('|', '\\|')} |" )
    atomic_write(base / "INDEX.md", "\n".join(lines) + "\n")


def archive_event(root: Path, payload: dict[str, Any]) -> Path:
    session_value = payload.get("session_id")
    session_id = session_value if isinstance(session_value, str) else None
    target = session_root(root, session_id)
    assert_safe_project_path(root, target)
    target.mkdir(parents=True, exist_ok=True)
    assert_safe_project_path(root, target)
    log = target / "session.jsonl"
    state_file = target / "session-state.json"
    lock = target / ".lock"
    with file_lock(lock):
        repair_jsonl(log)
        ids = existing_ids(log)
        state = read_json(state_file, {}) or {}
        envelopes: list[dict[str, Any]] = []
        transcript_name = payload.get("transcript_path")
        if transcript_name:
            try:
                transcript_path = validate_transcript_path(str(transcript_name))
                delta, new_state = transcript_delta(transcript_path, state)
            except (FileNotFoundError, PermissionError, OSError) as error:
                append_error(root, OSError(f"cannot archive transcript {transcript_name}: {error}"))
                delta, new_state = [], state
            for item in delta:
                item["session_id"] = session_id
                if item["id"] not in ids:
                    envelopes.append(item)
                    ids.add(item["id"])
            state = new_state
        event = event_envelope(payload, session_id)
        if event["id"] not in ids:
            envelopes.append(event)
            ids.add(event["id"])
        append_envelopes(log, envelopes)
        state["session_id"] = session_id
        state["last_event"] = payload.get("hook_event_name")
        state["last_event_at"] = now()
        atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        _render_session_unlocked(target)
    rebuild_index(root)
    return target


def read_event_records(session_dir: Path) -> list[dict[str, Any]]:
    result = []
    try:
        for line in (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("kind") == "hook-event":
                    item["payload"] = json.loads(base64.b64decode(item.get("payload_b64", "")))
                result.append(item)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return result


def prompt_count(session_dir: Path) -> int:
    return sum(1 for item in read_event_records(session_dir) if item.get("event") == "UserPromptSubmit")


def build_injection(root: Path, limit: int) -> str:
    ensure_layout(root)
    pieces: list[str] = []
    files = [memory_root(root) / "CONTEXT.md", memory_root(root) / "MEMORY.md", memory_root(root) / "HANDOFF.md"]
    memory_document = load_memory_document(root)
    skills = sorted((root / ".agents" / "skills").glob("*/SKILL.md"))
    for path in files:
        if path.is_symlink():
            continue
        if path == memory_file(root):
            text = memory_document.strip()
        else:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
        if text:
            pieces.append(f"## {path.relative_to(root)}\n{text}")
    if skills:
        skill_lines = ["## Active project skills"]
        for path in skills:
            if path.is_symlink():
                continue
            skill_lines.append(f"- `{path.relative_to(root)}`")
        pieces.append("\n".join(skill_lines))
    combined = "\n\n".join(pieces)
    if len(combined) <= limit:
        return combined
    clipped = combined[: max(0, limit - 120)].rstrip()
    return clipped + "\n\n[project context clipped; full sources: .agents/memory/CONTEXT.md, .agents/memory/MEMORY.md, .agents/memory/HANDOFF.md, .agents/skills/*/SKILL.md]"


def migrate_one(source: Path, target: Path, manifest: list[dict[str, Any]]) -> None:
    if source.is_symlink():
        return
    if target.is_symlink():
        manifest.append({"source_path": str(source), "target_path": str(target), "sha256": "", "status": "target-symlink-skipped"})
        return
    if source.is_file():
        content = source.read_bytes()
        digest = sha256_bytes(content)
        if target.exists() and target.is_file() and target.read_bytes() == content:
            status = "same"
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            status = "copied"
        elif target.is_file() and target.read_bytes() == b"":
            shutil.copyfile(source, target)
            status = "replaced-empty-default"
        elif target.is_dir():
            conflict = Path(str(target) + f".conflict-{digest[:12]}")
            if not conflict.exists():
                shutil.copyfile(source, conflict)
            status = "type-conflict"
        else:
            legacy = Path(str(target) + f".legacy-{digest[:12]}")
            if legacy.exists() and legacy.read_bytes() == content:
                status = "legacy-same"
            else:
                legacy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, legacy)
                status = "conflict-preserved"
        manifest.append({"source_path": str(source), "target_path": str(target), "sha256": digest, "status": status})
        return
    if source.is_dir():
        if target.exists() and not target.is_dir():
            conflict = Path(str(target) + ".dir-conflict")
            conflict.mkdir(parents=True, exist_ok=True)
            for child in sorted(source.iterdir(), key=lambda item: item.name):
                migrate_one(child, conflict / child.name, manifest)
            return
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            migrate_one(child, target / child.name, manifest)


def omp_memory_dir(root: Path, home: Path | None = None) -> Path | None:
    base = (home or Path.home()) / ".omp" / "agent" / "memories"
    encoded = root.as_posix().lstrip("/").replace("/", "-")
    candidates = [base / f"---{encoded}--", base / f"--{encoded}--", base / f"-{encoded}", base / encoded]
    return next((candidate for candidate in candidates if not candidate.is_symlink() and candidate.is_dir()), None)


def migrate(root: Path, omp_home: Path | None = None) -> int:
    ensure_layout(root)
    with file_lock(memory_root(root) / ".migration.lock"):
        migrated = _migrate_locked(root, omp_home)
        seed_memory_journal(root)
        return migrated


def _migrate_locked(root: Path, omp_home: Path | None = None) -> int:
    sources = [
        (root / ".pi" / "MEMORY.md", memory_root(root) / "MEMORY.md"),
        (root / ".pi" / "CONTEXT.md", memory_root(root) / "CONTEXT.md"),
        (root / ".pi" / "session-logs", memory_root(root) / "session-logs"),
        (root / ".pi" / "skills", root / ".agents" / "skills"),
        (root / ".agents" / "memory" / "skills", root / ".agents" / "skills"),
        (root / ".agents" / "memory" / "session-index.md", memory_root(root) / "session-logs" / "INDEX.md"),
    ]
    omp_dir = omp_memory_dir(root, omp_home)
    if omp_dir:
        sources.extend([
            (omp_dir / "MEMORY.md", memory_root(root) / "MEMORY.md"),
            (omp_dir / "memory_summary.md", memory_root(root) / "CONTEXT.md"),
            (omp_dir / "learned.md", memory_root(root) / "skill-candidates" / "omp-learned.md"),
            (omp_dir / "raw_memories.md", memory_root(root) / "skill-candidates" / "omp-raw-memories.md"),
            (omp_dir / "skills", root / ".agents" / "skills"),
        ])
    manifest_path = memory_root(root) / "migration-manifest.json"
    manifest = read_json(manifest_path, [])
    if not isinstance(manifest, list):
        manifest = []
    new_entries: list[dict[str, Any]] = []
    for source, target in sources:
        if source.exists():
            migrate_one(source, target, new_entries)
    if new_entries:
        known = {(item.get("source_path"), item.get("target_path"), item.get("sha256")) for item in manifest if isinstance(item, dict)}
        for entry in new_entries:
            key = (entry["source_path"], entry["target_path"], entry["sha256"])
            if key not in known:
                manifest.append(entry)
                known.add(key)
        atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return len(new_entries)


def write_handoff(root: Path, payload: dict[str, Any], session_dir: Path) -> None:
    if not load_config(root).get("handoffEnabled", True):
        return
    assert_project_layout_safe(root)
    prompt = str(payload.get("prompt") or payload.get("last_assistant_message") or "").strip()
    context_path = memory_root(root) / "CONTEXT.md"
    open_tasks: list[str] = []
    try:
        in_tasks = False
        for line in context_path.read_text(encoding="utf-8").splitlines():
            if line.strip().lower() in {"## open tasks", "## open tasks:"}:
                in_tasks = True
                continue
            if in_tasks and line.startswith("## "):
                break
            if in_tasks and line.startswith("- "):
                open_tasks.append(line[2:].strip())
    except OSError:
        pass
    session_state = read_json(session_dir / "session-state.json", {}) or {}
    display_session_id = session_state.get("session_id")
    if not isinstance(display_session_id, str):
        display_session_id = "<codex-missing-session-id>"
    body = [
        "# Handoff",
        "",
        f"- Generated: {now()}",
        f"- Project: `{root}`",
        f"- Session: `{display_session_id}`",
        f"- Raw archive: `{session_dir.relative_to(root) / 'session.jsonl'}`",
        "- Index: `.agents/memory/session-logs/INDEX.md`",
        "",
        "## Resume checklist",
        "",
        "- Read `.agents/memory/CONTEXT.md` and `.agents/memory/MEMORY.md`.",
        f"- Inspect `{session_dir.relative_to(root) / 'session.md'}` for the latest event summary.",
        "- Continue open tasks from the project context; do not infer hidden Codex state.",
    ]
    body.extend(["", "## Open tasks", ""])
    body.extend(f"- {task}" for task in (open_tasks or ["Review CONTEXT.md for current open tasks."]))
    if prompt:
        body.extend(["", "## Latest hook text", "", prompt[:4000]])
    atomic_write(memory_root(root) / "HANDOFF.md", "\n".join(body) + "\n")


def should_maintain(root: Path, session_dir: Path, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    config = load_config(root)
    if not config.get("autoConsolidate", True) or config.get("maintenanceMode") != "stop-continuation":
        return False, None
    if payload.get("stop_hook_active"):
        return False, None
    request_path = session_dir / "maintenance-request.json"
    with file_lock(session_dir / ".lock"):
        count = prompt_count(session_dir)
        turns = int(config.get("consolidateTurns") or 0)
        due = turns > 0 and count > 0 and count % turns == 0
        if not due:
            return False, None
        current = read_json(request_path, {}) or {}
        if current.get("status") in {"pending", "failed"}:
            return False, current
        if current.get("status") == "consumed" and int(current.get("prompt_count") or -1) == count:
            return False, current
        state = read_json(session_dir / "session-state.json", {}) or {}
        turn_id = str(payload.get("turn_id") or payload.get("turn") or count)
        base_request_id = f"{safe_id(str(payload.get('session_id') or session_dir.name))}:{safe_id(turn_id)}:{int(state.get('offset') or 0)}"
        request = {
            "schema": SCHEMA,
            "request_id": base_request_id,
            "base_request_id": base_request_id,
            "attempt": 1,
            "session_id": session_dir.name,
            "project_root": str(root),
            "session_dir": str(session_dir),
            "turn_id": turn_id,
            "prompt_count": count,
            "created_at": now(),
            "status": "pending",
            "reason": "consolidate-turn-threshold",
            "sources": [str(session_dir / "session.jsonl"), str(memory_root(root) / "CONTEXT.md"), str(memory_root(root) / "MEMORY.md")],
        }
        atomic_write(request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
        return True, request


def hook_response(event: str, root: Path, session_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if event in {"SessionStart", "UserPromptSubmit"}:
        text = build_injection(root, int(load_config(root).get("maxInjectedChars") or 12_000))
        if text:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if event == "PreCompact":
        write_handoff(root, payload, session_dir)
        return {"systemMessage": "Project handoff written to .agents/memory/HANDOFF.md"}
    if event == "PostCompact":
        return {"systemMessage": "Project context is available from SessionStart compact injection and .agents/memory/HANDOFF.md."}
    if event == "Stop":
        created, request = should_maintain(root, session_dir, payload)
        if created and request:
            return {"decision": "block", "reason": f"Run project-context-maintenance for request {request['request_id']} once, then continue."}
    return {}


def redact_secrets(text: str) -> str:
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[redacted-jwt]", text)
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}", "Bearer [redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b", "[redacted-key]", value)
    return re.sub(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{8,}\b", "[redacted-token]", value)


def _rotate_error_log(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_ERROR_LOG_BYTES:
            return
        text = path.read_text(encoding="utf-8")
        atomic_write(path, "[...truncated; newest entries kept...]\n" + text[-KEEP_ERROR_LOG_BYTES:])
    except OSError:
        pass


def append_error(root: Path, error: BaseException) -> None:
    try:
        path = memory_root(root) / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        ensure_memory_gitignore(root)
        _rotate_error_log(path)
        detail = redact_secrets(str(error))[:8_000]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{now()} {type(error).__name__}: {detail}\n")
    except OSError:
        pass


def main_hook() -> None:
    raw = sys.stdin.read()
    root_hint = os.getcwd()
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            payload = {}
        root = project_root(str(payload.get("cwd") or root_hint))
        ensure_layout(root)
        config = load_config(root)
        if not config.get("codexHookEnabled", True):
            print("{}")
            return
        migrate(root) if payload.get("hook_event_name") == "SessionStart" else None
        session_value = payload.get("session_id")
        session_id = session_value if isinstance(session_value, str) else None
        session_dir = session_root(root, session_id)
        if config.get("archiveEnabled", True):
            session_dir = archive_event(root, payload)
        response = hook_response(str(payload.get("hook_event_name") or ""), root, session_dir, payload)
        print(json.dumps(response, ensure_ascii=False))
    except Exception as error:  # hooks must not break the user's Codex operation
        try:
            root = project_root(str(payload.get("cwd") if isinstance(payload, dict) else root_hint))
        except Exception:
            root = Path(root_hint).resolve()
        append_error(root, error)
        print("{}")


def valid_slug(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)) and len(value) <= 80


def session_ids(root: Path) -> set[str]:
    base = memory_root(root) / "session-logs"
    result: set[str] = set()
    if not base.exists():
        return result
    for item in base.iterdir():
        try:
            log = item / "session.jsonl"
            if not item.is_symlink() and item.is_dir() and log.is_file() and log.stat().st_size > 0:
                with log.open("rb") as handle:
                    first = json.loads(handle.readline())
                if isinstance(first, dict) and isinstance(first.get("id"), str):
                    state = read_json(item / "session-state.json", {}) or {}
                    result.add(item.name)
                    if isinstance(state.get("session_id"), str):
                        result.add(state["session_id"])
        except OSError:
            continue
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return result


def validate_maintenance(value: Any, root: Path) -> str:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("unsupported maintenance schema")
    active = value.get("active_skills")
    candidates = value.get("skill_candidates")
    if active is not None or candidates is not None:
        if set(value) != {"schema", "active_skills", "skill_candidates"}:
            raise ValueError("unknown autolearn field")
        if not isinstance(active, list) or not isinstance(candidates, list):
            raise ValueError("skill lists must be arrays")
        known = session_ids(root)
        for item, minimum in [(item, 2) for item in active] + [(item, 1) for item in candidates]:
            if not isinstance(item, dict) or set(item) != {"name", "markdown", "evidence_session_ids"}:
                raise ValueError("invalid skill item")
            if not valid_slug(item["name"]) or not isinstance(item["markdown"], str) or len(item["markdown"]) > 24_000:
                raise ValueError("invalid skill content")
            evidence = item["evidence_session_ids"]
            normalized_evidence = {session_storage_id(s) for s in evidence if isinstance(s, str)} if isinstance(evidence, list) else set()
            if not isinstance(evidence, list) or len(normalized_evidence) < minimum or any(not isinstance(s, str) or session_storage_id(s) not in known for s in evidence):
                raise ValueError("insufficient or unknown skill evidence")
        return "autolearn"
    if set(value) != {"schema", "memory_markdown", "context"}:
        raise ValueError("unknown consolidation field")
    context = value.get("context")
    if not isinstance(value.get("memory_markdown"), str) or len(value["memory_markdown"]) > MAX_MEMORY_CHARS_LIMIT:
        raise ValueError("invalid memory_markdown")
    if not isinstance(context, dict) or set(context) != {"title", "summary", "key_points", "open_tasks"}:
        raise ValueError("invalid context object")
    if not isinstance(context["title"], str) or len(context["title"]) > 160:
        raise ValueError("invalid context title")
    if not isinstance(context["summary"], str) or len(context["summary"]) > 6_000:
        raise ValueError("invalid context summary")
    for key in ("key_points", "open_tasks"):
        if not isinstance(context[key], list) or len(context[key]) > 50 or any(not isinstance(item, str) for item in context[key]):
            raise ValueError("invalid context list")
    return "consolidation"


def apply_maintenance(root: Path, request_path: Path, input_path: Path) -> dict[str, Any]:
    with file_lock(request_path.parent / ".lock"):
        with file_lock(memory_root(root) / ".memory.lock"):
            return _apply_maintenance_locked(root, request_path, input_path)


def _apply_maintenance_locked(root: Path, request_path: Path, input_path: Path) -> dict[str, Any]:
    request = read_json(request_path, {}) or {}
    if request.get("status") not in {"pending", "failed"}:
        raise ValueError("request is not pending")
    value = read_json(input_path, None)
    kind = validate_maintenance(value, root)
    if kind == "consolidation":
        context = value["context"]
        assert_safe_project_path(root, memory_file(root))
        assert_safe_project_path(root, memory_journal_file(root))
        assert_safe_project_path(root, memory_root(root) / "CONTEXT.md")
        context_text = "# Context\n\n"
        context_text += f"## {context['title']}\n\n{context['summary']}\n\n"
        context_text += "## Key points\n\n" + "\n".join(f"- {item}" for item in context["key_points"]) + "\n\n"
        context_text += "## Open tasks\n\n" + "\n".join(f"- {item}" for item in context["open_tasks"]) + "\n"
        memory_text, journal_text, backup, poisoned, damaged = prepare_memory_write(
            root, value["memory_markdown"], memory_limit(root)
        )
        outputs = [
            (memory_journal_file(root), journal_text),
            (memory_file(root), memory_text),
            (memory_root(root) / "CONTEXT.md", context_text),
        ]
    else:
        outputs = []
        for item in value["active_skills"]:
            assert_safe_project_path(root, root / ".agents" / "skills" / item["name"] / "SKILL.md")
            outputs.append((root / ".agents" / "skills" / item["name"] / "SKILL.md", item["markdown"].rstrip() + "\n"))
        for item in value["skill_candidates"]:
            assert_safe_project_path(root, memory_root(root) / "skill-candidates" / f"{item['name']}.md")
            outputs.append((memory_root(root) / "skill-candidates" / f"{item['name']}.md", item["markdown"].rstrip() + "\n"))
    atomic_write_many(outputs, journal=memory_root(root) / ".maintenance-transaction.json")
    if kind == "consolidation":
        rotate_memory_journal(root)
        if damaged:
            append_error(root, ValueError(f"memory journal has {damaged} damaged record(s); valid records were preserved"))
        if poisoned:
            append_error(root, ValueError(f"replaced poisoned MEMORY.md; backup kept at {backup}"))
    request["status"] = "consumed"
    request["consumed_at"] = now()
    request["result_kind"] = kind
    atomic_write(request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "request_id": request.get("request_id"), "kind": kind}


def mark_request_failed(request_path: Path, reason: str) -> dict[str, Any]:
    with file_lock(request_path.parent / ".lock"):
        request = read_json(request_path, {}) or {}
        if request.get("status") not in {"pending", "failed"}:
            raise ValueError("request is not pending or failed")
        request["status"] = "failed"
        request["failed_at"] = now()
        request["failure_reason"] = reason[:4000]
        atomic_write(request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
        return {"ok": True, "request_id": request.get("request_id"), "status": "failed"}


def retry_request(request_path: Path) -> dict[str, Any]:
    with file_lock(request_path.parent / ".lock"):
        request = read_json(request_path, {}) or {}
        if request.get("status") != "failed":
            raise ValueError("only failed requests can be retried")
        attempt = int(request.get("attempt") or 1) + 1
        base_id = str(request.get("base_request_id") or request.get("request_id") or "request")
        request["attempt"] = attempt
        request["request_id"] = f"{base_id}:attempt-{attempt}"
        request["status"] = "pending"
        request["retried_at"] = now()
        atomic_write(request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
        return {"ok": True, "request_id": request["request_id"], "status": "pending"}


def cli() -> int:
    parser = argparse.ArgumentParser(description="Codex Project Context storage utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "render"):
        child = sub.add_parser(command)
        child.add_argument("--project-root", default=os.getcwd())
    apply = sub.add_parser("apply-maintenance")
    apply.add_argument("--project-root", default=os.getcwd())
    apply.add_argument("--request", required=True)
    apply.add_argument("--input", required=True)
    imported = sub.add_parser("import-codex")
    imported.add_argument("--source-root", action="append", required=True)
    imported.add_argument("--project-root", dest="project_roots", action="append", required=True)
    imported.add_argument("--unmatched-project-root", default=None)
    failed = sub.add_parser("mark-failed")
    failed.add_argument("--request", required=True)
    failed.add_argument("--reason", required=True)
    retry = sub.add_parser("retry-maintenance")
    retry.add_argument("--request", required=True)
    args = parser.parse_args()
    if args.command == "import-codex":
        result = import_codex_sessions(
            [Path(value) for value in args.source_root],
            [Path(value) for value in args.project_roots],
            Path(args.unmatched_project_root) if args.unmatched_project_root else None,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not result["errors"] else 1
    root = project_root(args.project_root)
    ensure_layout(root)
    if args.command == "migrate":
        print(json.dumps({"migrated": migrate(root)}))
        return 0
    if args.command == "render":
        for item in (memory_root(root) / "session-logs").iterdir():
            if item.is_dir():
                if not item.is_symlink():
                    render_session(item)
        rebuild_index(root)
        print(json.dumps({"ok": True}))
        return 0
    try:
        if args.command == "apply-maintenance":
            result = apply_maintenance(root, Path(args.request), Path(args.input))
        elif args.command == "mark-failed":
            result = mark_request_failed(Path(args.request), args.reason)
        else:
            result = retry_request(Path(args.request))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        append_error(root, error)
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
