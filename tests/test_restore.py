import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tmux_llm_rescue import config, restore, topology


def pane(i, cwd, sid=None, window="@1"):
    return {"pane_id": f"%{i}", "window_id": window, "session": "work", "session_id": "$4",
            "window": "review | notes", "cwd": cwd, "family": "codex", "sid": sid}


def row(sid, cwd):
    return {"sid": sid, "cwd": cwd, "family": "codex", "argv": ("codex", "resume", sid)}


class RestoreTest(unittest.TestCase):
    def test_recorded_identity_precedes_cwd_matching_and_only_skips_running(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = [row("first", directory), row("second", directory)]
            snapshot = {"ts": 100, "panes": [pane(1, directory), pane(2, directory, "first")]}
            matches = topology.match_panes(snapshot, candidates)
            self.assertEqual(matches["%2"][0], candidates[0])
            self.assertEqual(matches["%1"][0], candidates[1])
            launches, notes = restore.plan(snapshot, candidates, matches, None,
                                           {("codex", "first")}, restore.numbers("1,2"))
            self.assertEqual([launch.number for launch in launches], [2])
            self.assertEqual(notes, ["1. Already running"])
            launches, _ = restore.plan(snapshot, candidates, matches, None, set(), restore.numbers("1"))
            self.assertEqual([launch.number for launch in launches], [1])

    def test_ambiguous_and_unidentified_live_sessions_do_not_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = row("a", directory), row("b", directory)
            snapshot = {"ts": 100, "panes": [pane(1, directory)]}
            self.assertEqual(topology.match_panes(snapshot, [a, b]), {})
            matches = topology.match_panes(snapshot, [a])
            launches, notes = restore.plan(snapshot, [a], matches, {"panes": [pane(2, directory)]}, set())
            self.assertFalse(launches)
            self.assertIn("live codex pane", notes[0])

    def test_exec_creates_a_new_window_in_existing_session_and_splits_only_its_id(self):
        cwd = "/example/#{pane_id}"
        launches = [restore.Launch(i, "work", "@1", "same #{name};", cwd, ("codex", "resume", sid))
                    for i, sid in ((1, "a"), (2, "b"))]
        calls = []

        def command(*args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="$4 @9\n", stderr="")

        socket = SimpleNamespace(run=command, address="example", option="-L")
        current = {"panes": [pane(9, cwd, "other", "@5")]}
        with contextlib.redirect_stdout(io.StringIO()) as preview:
            restore.describe(launches, current, socket)
        self.assertIn("Create window 'work' / 'same #{name};'", preview.getvalue())
        self.assertIn("Split new window", preview.getvalue())
        self.assertEqual(calls, [])
        with patch.object(restore.shutil, "which", return_value="/bin/codex"):
            restore.execute(launches, current, socket)
        self.assertEqual(calls[0][:6], ("new-window", "-d", "-t", "$4:", "-n", r"same ##{name}\;"))
        self.assertEqual(calls[0][-5:], ("/example/##{pane_id}", "/usr/bin/env", "codex", "resume", "a"))
        self.assertEqual(calls[1][:4], ("split-window", "-d", "-t", "@9"))
        self.assertEqual(calls[2], ("select-layout", "-t", "@9", "tiled"))

    def test_snapshot_before_restart_is_selected_even_with_newer_record(self):
        old = {"ts": 20.8, "server": {"start": 10, "pid": 100}}
        new = {"ts": 21.4, "server": {"start": 20, "pid": 101}}
        self.assertIs(topology.select_snapshot([old, new], new), old)
        self.assertIs(topology.select_snapshot([old, new], new, as_of=22), new)

    def test_config_on_python_without_tomllib(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[[homes]] # selected profile\nfamily = "codex"\nroot = "~/.other"\n'
                            """resume_command = 'env "CODEX_HOME=/two words" codex resume {sid}'\n""")
            with patch.object(config, "tomllib", None):
                home, = config.load_homes(path)
            self.assertEqual(home.argv("abc"), ("env", "CODEX_HOME=/two words", "codex", "resume", "abc"))
