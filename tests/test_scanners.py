import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tmux_llm_rescue.cli import discover
from tmux_llm_rescue.config import Home
from tmux_llm_rescue.scanners import claude, codex, kimi

SID = "01914145-10cd-736e-a040-856c858f6582"
SECOND = "01914145-10cd-736e-a040-856c858f6583"
ANCHOR = 1770000000
CWD = "/work/example"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def ndjson(path, *records):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    os.utime(path, (ANCHOR, ANCHOR))


class ScannerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.warnings = []

    def test_claude_activity_uses_user_turns_before_anchor_and_deduplicates_moved_file(self):
        log = self.root / "projects" / "-work-example" / f"{SID}.jsonl"
        turn = {"type": "user", "cwd": CWD, "timestamp": iso(ANCHOR - 20),
                "message": {"role": "user", "content": [{"type": "text", "text": "First question"}]}}
        ndjson(log, turn, {"type": "assistant", "timestamp": iso(ANCHOR - 10),
                          "message": {"model": "claude-sonnet-4-6"}},
               {**turn, "timestamp": iso(ANCHOR + 300), "message": {"content": "After crash"}})
        ndjson(log.parent.parent / "old-name" / log.name, turn)
        rows = claude.scan(self.root, ANCHOR, 3600, self.warnings.append)
        self.assertEqual([(row["sid"], row["mtime"], row["title"]) for row in rows],
                         [(SID, ANCHOR - 20, "First question")] * 2)
        rows = discover([Home("claude", self.root, "claude --resume {sid}")],
                        ANCHOR, 3600, None, set())
        self.assertEqual([row["resume"] for row in rows], [f"cd {CWD} && claude --resume {SID}"])

    def test_codex_db_and_rollout_fill_wal_gap_and_skip_subagents(self):
        with sqlite3.connect(self.root / "state_5.sqlite") as conn:
            conn.execute("""CREATE TABLE threads
                          (id TEXT, cwd TEXT, updated_at INT, title TEXT, source TEXT,
                           rollout_path TEXT, created_at INT, tokens_used INT, model TEXT)""")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (SID, CWD, ANCHOR - 9, "Saved thread", "cli", "rollout.jsonl",
                          ANCHOR - 100, 10000, "gpt"))
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         ("subagent", CWD, ANCHOR - 8, "Helper", '{"subagent": {}}',
                          "child.jsonl", ANCHOR - 20, 40, "gpt"))
        day = datetime.fromtimestamp(ANCHOR).strftime("%Y/%m/%d")
        for sid, source in ((SECOND, "cli"), ("01914145-10cd-736e-a040-856c858f6584", {"subagent": {}})):
            rollout = self.root / "sessions" / day / f"rollout-2026-02-02T10-00-00-{sid}.jsonl"
            ndjson(rollout, {"type": "session_meta", "timestamp": iso(ANCHOR - 100),
                             "payload": {"id": sid, "cwd": CWD, "source": source}},
                   {"type": "response_item", "timestamp": iso(ANCHOR - 5)},
                   {"type": "response_item", "timestamp": iso(ANCHOR + 120), "text": "x" * 70000})
        rows = codex.scan(self.root, ANCHOR, 3600, self.warnings.append)
        self.assertEqual({row["sid"]: (row["cwd"], row["mtime"]) for row in rows},
                         {SID: (CWD, ANCHOR - 9), SECOND: (CWD, ANCHOR - 5)})
        self.assertIn("subagent", self.warnings[0])

    def test_kimi_hashed_state_and_work_dir_metadata(self):
        (self.root / "kimi.json").write_text(json.dumps({"work_dirs": [{"path": CWD}]}))
        directory = self.root / "sessions" / hashlib.md5(CWD.encode()).hexdigest() / SID
        ndjson(directory / "state.json", {"custom_title": "Saved Kimi session"})
        ndjson(directory / "wire.jsonl", {"timestamp": ANCHOR - 3})
        rows = kimi.scan(self.root, ANCHOR, 3600, self.warnings.append)
        self.assertEqual([(row["sid"], row["cwd"], row["mtime"], row["title"]) for row in rows],
                         [(SID, CWD, ANCHOR - 3, "Saved Kimi session")])

    def test_kimi_code_index_resolves_work_dir_without_hash(self):
        sid = f"session_{SID}"
        directory = self.root / "sessions" / sid
        ndjson(directory / "state.json", {"custom_title": "Indexed Kimi"})
        ndjson(self.root / "session_index.jsonl",
               {"sessionId": sid, "sessionDir": str(directory), "workDir": CWD})
        rows = kimi.scan(self.root, ANCHOR, 3600, self.warnings.append)
        self.assertEqual([(row["sid"], row["cwd"], row["mtime"]) for row in rows],
                         [(sid, CWD, ANCHOR)])
