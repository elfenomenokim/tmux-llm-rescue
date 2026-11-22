"""Print an actual scan and restore preview using a fabricated local session."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
SID = "d5c23ff8-8e76-4ffe-9732-648f5669d3f3"
ANCHOR_TEXT = "2026-02-02T10:02:00+00:00"
ANCHOR = datetime.fromisoformat(ANCHOR_TEXT).timestamp()


def write(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = root / "claude"
    transcript = store / "projects" / "-tmp" / f"{SID}.jsonl"
    write(transcript, {"type": "user", "cwd": "/tmp", "message": {"content": "Summarize the migration"},
                       "timestamp": datetime.fromtimestamp(ANCHOR - 120, timezone.utc).isoformat()})
    os.utime(transcript, (ANCHOR, ANCHOR))
    config = root / "config" / "tmux-llm-rescue" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[[homes]]\nfamily = "claude"\nroot = "{store}"\n'
                      'resume_command = "claude --resume {sid}"\n', encoding="utf-8")
    state = root / "state" / "tmux-llm-rescue"
    socket = "demo"
    name = hashlib.sha256(socket.encode()).hexdigest()[:24]
    write(state / f"{name}.jsonl", {"ts": ANCHOR, "server": {"start": ANCHOR - 900, "pid": 1},
                                  "panes": [{"family": "claude", "sid": SID, "session": "work",
                                             "window": "migration", "cwd": "/tmp", "pane_id": "%1",
                                             "window_id": "@1"}]})
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "config"),
           "XDG_STATE_HOME": str(root / "state"), "TMUX_TMPDIR": directory,
           "PYTHONPATH": str(BASE), "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC"}
    for arguments in (["-L", socket], ["restore", "-L", socket, "--dry-run"]):
        print("$ tmux-llm-rescue " + " ".join(arguments), flush=True)
        subprocess.run([sys.executable, "-m", "tmux_llm_rescue.cli", *arguments], env=env, check=True)
        print()
