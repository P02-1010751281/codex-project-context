import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from scripts import contextctl as ctl


class ContextCtlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ctl.ensure_layout(self.root)
        self.transcript = self.root / "transcript.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, event="PostToolUse", session="s1", **extra):
        value = {
            "hook_event_name": event,
            "session_id": session,
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
        }
        value.update(extra)
        return value

    def records(self, session="s1"):
        path = ctl.session_root(self.root, session) / "session.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_opaque_transcript_append_rewrite_and_dedupe(self):
        self.transcript.write_bytes(b"one\n")
        ctl.archive_event(self.root, self.payload())
        ctl.archive_event(self.root, self.payload())
        self.transcript.write_bytes(b"one\ntwo\n")
        ctl.archive_event(self.root, self.payload(event="PostToolUse", tool_name="shell"))
        raw = [item for item in self.records() if item["kind"] == "transcript-chunk"]
        self.assertEqual(b"one\n", base64.b64decode(raw[0]["payload_b64"]))
        self.assertEqual(b"two\n", base64.b64decode(raw[1]["payload_b64"]))
        self.assertEqual(2, len(raw))

        self.transcript.write_bytes(b"new\n")
        ctl.archive_event(self.root, self.payload(event="PostToolUse", tool_name="rewrite"))
        records = self.records()
        self.assertTrue(any(item["kind"] == "source-reset" for item in records))
        self.assertEqual(b"new\n", base64.b64decode([item for item in records if item["kind"] == "transcript-chunk"][-1]["payload_b64"]))

    def test_native_codex_import_routes_opaque_rollout_and_is_idempotent(self):
        source_root = self.root / "native"
        source_root.mkdir()
        native = source_root / "rollout.jsonl"
        native.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "session_id": "native-1",
                "cwd": str(self.root),
                "source": "vscode",
                "thread_source": "user",
            },
        }) + "\n" + ("opaque-result\n" * 10), encoding="utf-8")
        result = ctl.import_codex_sessions([source_root], [self.root])
        self.assertEqual(1, result["files_seen"])
        self.assertEqual(1, result["files_imported"])
        first = self.records("native-1")
        self.assertTrue(any(item["kind"] == "native-codex-metadata" for item in first))
        raw = [item for item in first if item["kind"] == "native-codex-session"]
        self.assertEqual(native.read_bytes(), b"".join(base64.b64decode(item["payload_b64"]) for item in raw))
        second = ctl.import_codex_sessions([source_root], [self.root])
        self.assertEqual(0, second["chunks_added"])
        self.assertEqual(len(first), len(self.records("native-1")))

    def test_native_codex_import_can_keep_unmatched_rollout_explicitly_unassigned(self):
        source_root = self.root / "native"
        source_root.mkdir()
        native = source_root / "unassigned.jsonl"
        native.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"session_id": "native-unassigned", "cwd": "/tmp/codex-test"},
        }) + "\nraw", encoding="utf-8")
        result = ctl.import_codex_sessions([source_root], [self.root], unmatched_root=self.root)
        self.assertEqual(1, result["unmatched_imported"])
        self.assertEqual(1, len([item for item in self.records("native-unassigned") if item["kind"] == "native-codex-session"]))

    def test_session_storage_ids_are_distinct_and_reversible(self):
        values = [None, "", "unknown", ".", "session-dot", "a/b", "a-b", "你好"]
        paths = {ctl.session_root(self.root, value).name for value in values}
        self.assertEqual(len(values), len(paths))
        self.assertTrue(all(path.startswith("s-") for path in paths))

    def test_partial_jsonl_tail_is_recovered(self):
        self.transcript.write_bytes(b"hello")
        session_dir = ctl.session_root(self.root, "s1")
        session_dir.mkdir(parents=True)
        (session_dir / "session.jsonl").write_bytes(b'{"id":"partial"')
        ctl.archive_event(self.root, self.payload())
        for line in (session_dir / "session.jsonl").read_text().splitlines():
            json.loads(line)

    def test_concurrent_archive_writers_do_not_duplicate_raw_delta(self):
        self.transcript.write_bytes(b"concurrent\n")
        barrier = threading.Barrier(8)

        def write_event(index):
            barrier.wait()
            ctl.archive_event(self.root, self.payload(tool_name=f"tool-{index}"))

        workers = [threading.Thread(target=write_event, args=(index,)) for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        raw = [item for item in self.records() if item["kind"] == "transcript-chunk"]
        self.assertEqual(1, len(raw))
        self.assertEqual(b"concurrent\n", base64.b64decode(raw[0]["payload_b64"]))

    def test_hook_injection_has_a_limit_and_stable_order(self):
        (ctl.memory_root(self.root) / "CONTEXT.md").write_text("context " * 100)
        (ctl.memory_root(self.root) / "MEMORY.md").write_text("memory")
        (self.root / ".agents" / "skills" / "one").mkdir(parents=True)
        (self.root / ".agents" / "skills" / "one" / "SKILL.md").write_text("skill")
        result = ctl.build_injection(self.root, 80)
        self.assertLessEqual(len(result), 80 + 120)
        self.assertIn("CONTEXT.md", result)

    def test_migration_is_file_level_and_preserves_conflicts(self):
        source = self.root / ".pi" / "MEMORY.md"
        source.parent.mkdir(parents=True)
        source.write_text("old")
        self.assertEqual(1, ctl.migrate(self.root))
        target = ctl.memory_root(self.root) / "MEMORY.md"
        self.assertEqual("old", target.read_text())
        source.write_text("new")
        ctl.migrate(self.root)
        self.assertEqual("old", target.read_text())
        self.assertTrue(list(target.parent.glob("MEMORY.md.legacy-*")))
        manifest = ctl.read_json(ctl.memory_root(self.root) / "migration-manifest.json", [])
        self.assertTrue(all("sha256" in item for item in manifest))
        self.assertFalse(any("tree" in item for item in manifest))

    def test_omp_memory_is_imported_without_deleting_source(self):
        home = self.root / "omp-home"
        encoded = self.root.as_posix().lstrip("/").replace("/", "-")
        source = home / ".omp" / "agent" / "memories" / f"---{encoded}--"
        source.mkdir(parents=True)
        (source / "MEMORY.md").write_text("omp facts")
        (source / "memory_summary.md").write_text("omp context")
        (source / "learned.md").write_text("omp learned")
        ctl.migrate(self.root, omp_home=home)
        self.assertEqual("omp facts", (ctl.memory_root(self.root) / "MEMORY.md").read_text())
        self.assertIn("omp context", (ctl.memory_root(self.root) / "CONTEXT.md").read_text())
        self.assertTrue((ctl.memory_root(self.root) / "skill-candidates" / "omp-learned.md").exists())
        self.assertTrue((source / "MEMORY.md").exists())

    def test_unreadable_transcript_is_diagnostic_but_non_blocking(self):
        payload = self.payload(transcript_path=str(self.root / "missing-transcript.jsonl"))
        ctl.archive_event(self.root, payload)
        self.assertIn("cannot archive transcript", (ctl.memory_root(self.root) / "errors.log").read_text())
        self.assertTrue((ctl.session_root(self.root, "s1") / "session.jsonl").exists())

    def test_project_layout_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.rmtree(self.root / ".agents")
        os.symlink(outside, self.root / ".agents")
        with self.assertRaises(OSError):
            ctl.ensure_layout(self.root)

    def test_session_directory_symlink_is_rejected(self):
        outside = self.root / "outside-session"
        outside.mkdir()
        target = ctl.session_root(self.root, "s1")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, target)
        with self.assertRaises(OSError):
            ctl.archive_event(self.root, self.payload())

    def test_stop_creates_one_pending_request(self):
        config = ctl.memory_root(self.root) / "project-context.json"
        config.write_text(json.dumps({"consolidateTurns": 1}))
        ctl.archive_event(self.root, self.payload("UserPromptSubmit", prompt="do work"))
        session_dir = ctl.archive_event(self.root, self.payload("Stop", turn_id="t1"))
        first = ctl.hook_response("Stop", self.root, session_dir, self.payload("Stop", turn_id="t1"))
        self.assertEqual("block", first["decision"])
        second = ctl.hook_response("Stop", self.root, session_dir, self.payload("Stop", turn_id="t1"))
        self.assertEqual({}, second)
        self.assertEqual("pending", ctl.read_json(session_dir / "maintenance-request.json")["status"])

    def test_failed_request_can_be_retried_with_a_new_attempt(self):
        session_dir = ctl.session_root(self.root, "s1")
        session_dir.mkdir(parents=True)
        request = session_dir / "maintenance-request.json"
        request.write_text(json.dumps({"status": "pending", "request_id": "s1:t1:1", "base_request_id": "s1:t1:1", "attempt": 1}))
        ctl.mark_request_failed(request, "invalid model output")
        result = ctl.retry_request(request)
        self.assertEqual("pending", result["status"])
        self.assertEqual("s1:t1:1:attempt-2", result["request_id"])

    def test_compaction_handoff_is_injected_via_session_start(self):
        self.transcript.write_bytes(b"compact\n")
        session_dir = ctl.archive_event(self.root, self.payload("PreCompact", trigger="manual"))
        self.assertIn("systemMessage", ctl.hook_response("PreCompact", self.root, session_dir, self.payload("PreCompact")))
        self.assertIn("HANDOFF.md", ctl.build_injection(self.root, 10_000))
        self.assertIn("systemMessage", ctl.hook_response("PostCompact", self.root, session_dir, self.payload("PostCompact")))

    def test_concurrent_stop_writers_create_one_block(self):
        (ctl.memory_root(self.root) / "project-context.json").write_text(json.dumps({"consolidateTurns": 1}))
        ctl.archive_event(self.root, self.payload("UserPromptSubmit", prompt="do work"))
        session_dir = ctl.session_root(self.root, "s1")
        results = []
        barrier = threading.Barrier(8)

        def stop():
            barrier.wait()
            results.append(ctl.hook_response("Stop", self.root, session_dir, self.payload("Stop", turn_id="t1")))

        workers = [threading.Thread(target=stop) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(1, sum(1 for result in results if result.get("decision") == "block"))

    def test_apply_maintenance_strict_schema_and_atomic_outputs(self):
        session = ctl.archive_event(self.root, self.payload("UserPromptSubmit", prompt="a"))
        request = session / "maintenance-request.json"
        request.write_text(json.dumps({"status": "pending", "request_id": "r1"}))
        result = self.root / "result.json"
        result.write_text(json.dumps({
            "schema": "project-context/2",
            "memory_markdown": "facts",
            "context": {"title": "T", "summary": "S", "key_points": ["K"], "open_tasks": ["O"]},
        }))
        applied = ctl.apply_maintenance(self.root, request, result)
        self.assertTrue(applied["ok"])
        self.assertIn("facts", (ctl.memory_root(self.root) / "MEMORY.md").read_text())
        self.assertIn("T", (ctl.memory_root(self.root) / "CONTEXT.md").read_text())
        self.assertEqual("consumed", ctl.read_json(request)["status"])
        result.write_text(json.dumps({"schema": "project-context/2", "memory_markdown": "x", "context": {}, "extra": 1}))
        with self.assertRaises(ValueError):
            ctl.apply_maintenance(self.root, request, result)

    def test_memory_journal_is_authoritative_and_previous_bytes_are_backed_up(self):
        memory = ctl.memory_root(self.root) / "MEMORY.md"
        memory.write_bytes(b"legacy facts\n")
        request = self.root / "request.json"
        request.write_text(json.dumps({"status": "pending", "request_id": "journal-1"}))
        result = self.root / "result.json"
        result.write_text(json.dumps({
            "schema": "project-context/2",
            "memory_markdown": "new durable facts",
            "context": {"title": "T", "summary": "S", "key_points": [], "open_tasks": []},
        }))
        ctl.apply_maintenance(self.root, request, result)

        journal = ctl.memory_journal_file(self.root)
        self.assertTrue(journal.exists())
        self.assertIn("new durable facts", ctl.load_memory_document(self.root))
        self.assertTrue(any(path.read_bytes() == b"legacy facts\n" for path in ctl.memory_root(self.root).glob("MEMORY.md.memory-backup-*")))

        # A render older than the journal is not allowed to override the journal projection.
        memory.write_text("stale render")
        old = journal.stat().st_mtime - 10
        os.utime(memory, (old, old))
        self.assertIn("new durable facts", ctl.build_injection(self.root, 10_000))

    def test_poisoned_memory_is_recovered_and_original_bytes_are_preserved(self):
        poison = {
            "memory_markdown": "# Project Memory\n\nA durable recovered fact.\n",
            "context": {"title": "old", "summary": "old", "key_points": [], "open_tasks": []},
        }
        memory = ctl.memory_root(self.root) / "MEMORY.md"
        original = (json.dumps(poison) + "\n").encode()
        memory.write_bytes(original)
        request = self.root / "request.json"
        request.write_text(json.dumps({"status": "pending", "request_id": "poison-1"}))
        result = self.root / "result.json"
        result.write_text(json.dumps({
            "schema": "project-context/2",
            "memory_markdown": "clean durable facts",
            "context": {"title": "T", "summary": "S", "key_points": [], "open_tasks": []},
        }))
        ctl.apply_maintenance(self.root, request, result)
        self.assertTrue(any(path.read_bytes() == original for path in ctl.memory_root(self.root).glob("MEMORY.md.memory-backup-*")))
        self.assertEqual("# Project Memory\n\nclean durable facts\n", memory.read_text())
        self.assertIn("poisoned MEMORY.md", (ctl.memory_root(self.root) / "errors.log").read_text())

    def test_damaged_memory_journal_fails_closed(self):
        journal = ctl.memory_journal_file(self.root)
        journal.write_text("not-json\n")
        memory = ctl.memory_root(self.root) / "MEMORY.md"
        before = memory.read_bytes()
        request = self.root / "request.json"
        request.write_text(json.dumps({"status": "pending", "request_id": "damaged-1"}))
        result = self.root / "result.json"
        result.write_text(json.dumps({
            "schema": "project-context/2",
            "memory_markdown": "must not overwrite",
            "context": {"title": "T", "summary": "S", "key_points": [], "open_tasks": []},
        }))
        with self.assertRaises(ValueError):
            ctl.apply_maintenance(self.root, request, result)
        self.assertEqual(before, memory.read_bytes())
        self.assertEqual("pending", ctl.read_json(request)["status"])

    def test_memory_journal_rotation_keeps_an_archive(self):
        old_limit = ctl.MEMORY_JOURNAL_ROTATE_BYTES
        ctl.MEMORY_JOURNAL_ROTATE_BYTES = 100
        try:
            request = self.root / "request.json"
            request.write_text(json.dumps({"status": "pending", "request_id": "rotate-1"}))
            result = self.root / "result.json"
            result.write_text(json.dumps({
                "schema": "project-context/2",
                "memory_markdown": "x" * 200,
                "context": {"title": "T", "summary": "S", "key_points": [], "open_tasks": []},
            }))
            ctl.apply_maintenance(self.root, request, result)
            self.assertTrue(list(ctl.memory_root(self.root).glob("memory-log-*.jsonl")))
            self.assertIn("x" * 100, ctl.load_memory_document(self.root))
        finally:
            ctl.MEMORY_JOURNAL_ROTATE_BYTES = old_limit

    def test_hook_wrapper_returns_json_and_writes_project_state(self):
        self.transcript.write_bytes(b"{opaque:true}\n")
        wrapper = Path(__file__).resolve().parents[1] / "hooks" / "context_hook.py"
        result = subprocess.run(
            [sys.executable, str(wrapper)],
            input=json.dumps(self.payload("SessionStart", prompt="start")),
            text=True,
            capture_output=True,
            cwd=self.root,
            env={**os.environ, "PLUGIN_ROOT": str(wrapper.parent.parent)},
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertTrue((ctl.session_root(self.root, "s1") / "session.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
