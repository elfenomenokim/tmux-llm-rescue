"""Record pane identities and select a snapshot from the requested tmux server."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from .config import state_dir
from .scanners import UUID_RE
from .scanners.kimi import KIMI_SID

_BINARIES = {"claude": "claude", "claude-code": "claude", "codex": "codex",
             "kimi": "kimi", "kimi-code": "kimi"}
_FIELDS = {
    "server_pid": "pid", "server_start": "start_time",
    "session": "session_name", "session_id": "session_id", "window_id": "window_id", "window": "window_name",
    "pane_id": "pane_id", "pane_index": "pane_index", "pane_pid": "pane_pid",
    "cwd": "pane_current_path", "command": "pane_current_command", "dead": "pane_dead",
}
_SEPARATOR = r"\037"
_HISTORY_LIMIT = 256


@dataclass(frozen=True)
class Socket:
    option: str
    address: str
    key: str

    @property
    def args(self) -> tuple[str, ...]:
        return "tmux", self.option, self.address

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.args, *args], capture_output=True, text=True,
                              errors="replace", timeout=10)

    @property
    def history(self) -> Path:
        return state_dir() / f"{self.key}.jsonl"


def socket_target(name: str | None) -> Socket:
    if name is not None:
        if not name or "/" in name or name in (".", ".."):
            raise ValueError("expected a tmux socket name")
        return Socket("-L", name, _key(name))
    caller = os.environ.get("TMUX", "").rsplit(",", 2)[0]
    if caller and Path(caller).is_absolute():
        path = Path(caller).resolve()
        default_dir = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
        key = _key(path.name if path.parent == default_dir.resolve() else str(path))
        return Socket("-S", str(path), key)
    return Socket("-L", "default", _key("default"))


def _key(value: str) -> str:
    return hashlib.sha256(os.fsencode(value)).hexdigest()[:24]


def _cli(args: str) -> tuple[str | None, str | None]:
    words = args.split()
    if not words:
        return None, None
    binary = Path(words[0]).name
    if binary in ("node", "bun", "python", "python3") and len(words) > 1:
        binary = Path(words[1]).stem
    family = _BINARIES.get(binary)
    if family is None:
        return None, None
    flags = {
        "claude": ("--resume", "-r", "--session-id"),
        "codex": ("resume",),
        "kimi": ("--resume", "-r", "--session", "-S"),
    }[family]
    pattern = KIMI_SID if family == "kimi" else UUID_RE
    for index, word in enumerate(words[1:], 1):
        flag, equals, val = word.partition("=")
        if flag not in flags:
            continue
        sid = val if equals else words[index + 1] if index + 1 < len(words) else ""
        if pattern.fullmatch(sid):
            return family, sid
    return family, None


def process_identities() -> tuple[dict, dict]:
    result = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,command="],
                            capture_output=True, text=True, errors="replace",
                            timeout=10, check=True)
    identities = {}
    children = defaultdict(list)
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent, args = fields
            identities[int(pid)] = _cli(args)
            children[int(parent)].append(int(pid))
    return identities, children


def _pane_identity(pid: int, identities: dict, children: dict) -> tuple:
    nearby = deque([pid])
    fallback = None, None
    while nearby:
        current = nearby.popleft()
        family, sid = identities.get(current, (None, None))
        if family:
            if sid:
                return family, sid
            if fallback == (None, None):
                fallback = family, None
        nearby.extend(children.get(current, ()))
    return fallback


def capture(socket: Socket, identities: dict, children: dict) -> dict | None:
    fmt = _SEPARATOR.join("#{" + name + "}" for name in _FIELDS.values())
    result = socket.run("list-panes", "-a", "-F", fmt)
    if result.returncode:
        if any(marker in result.stderr for marker in ("No such file", "no server running", "Connection refused")):
            return None
        raise RuntimeError(result.stderr.strip() or "tmux list-panes failed")
    panes = []
    seen = set()
    server = None
    for line in result.stdout.splitlines():
        values = line.split(_SEPARATOR)
        if len(values) != len(_FIELDS):
            raise ValueError("tmux returned an unsupported separator in a pane field")
        pane = dict(zip(_FIELDS, values))
        server_pid = pane.pop("server_pid")
        server_start = pane.pop("server_start")
        if not server_pid.isdigit() or not server_start.isdigit():
            raise ValueError("invalid tmux server identity")
        identity = {"pid": int(server_pid), "start": int(server_start)}
        if server is not None and identity != server:
            raise ValueError("tmux panes disagree on server identity")
        server = identity
        pid = pane.pop("pane_pid")
        dead = pane.pop("dead") == "1"
        if dead or not pid.isdigit() or pane["pane_id"] in seen:
            continue
        seen.add(pane["pane_id"])
        family, sid = _pane_identity(int(pid), identities, children)
        pane["family"] = family or _BINARIES.get(pane["command"])
        pane["sid"] = sid
        panes.append(pane)
    if server is None:
        raise RuntimeError("tmux returned no pane records")
    return {"ts": time.time(), "server": server, "panes": panes}


def read_history(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            snapshots = []
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and isinstance(row.get("ts"), (int, float))
                        and isinstance(row.get("server"), dict) and isinstance(row.get("panes"), list)):
                    snapshots.append(row)
            return snapshots
    except FileNotFoundError:
        return []


def latest_snapshot(snapshots, ceiling, exclude_start_after=None):
    """Select the newest snapshot within the time limit and outside a restarted server."""
    return max(
        (row for row in snapshots
         if isinstance(row, dict) and isinstance(row.get("ts"), (float, int))
         and row["ts"] <= ceiling
         and (exclude_start_after is None or not ((row.get("server") or {}).get("start") or 0)
              or row["server"]["start"] < exclude_start_after)),
        key=lambda row: row["ts"], default=None,
    )


def select_snapshot(history: list[dict], current: dict | None,
                    as_of: float | None = None) -> dict | None:
    if as_of is not None:
        return latest_snapshot(history, as_of)
    if current:
        server = current["server"]
        old = latest_snapshot((row for row in history if row["server"] != server), server["start"] + 1)
        if old:
            return old
    return latest_snapshot(history, time.time())


def record(socket: Socket) -> dict:
    from fcntl import flock, LOCK_EX

    path = socket.history
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".record.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        flock(lock, LOCK_EX)
        snapshot = capture(socket, *process_identities())
        if snapshot is None:
            raise RuntimeError("no tmux server to record")
        history = read_history(path)
        recent = history[-(_HISTORY_LIMIT - 1):]
        old = next((row for row in reversed(history)
                    if row["server"] != snapshot["server"]), None)
        if old and old not in recent:
            recent = [old, *recent[1:]]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                for row in [*recent, snapshot]:
                    temp.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
                temp.flush()
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
    return snapshot


def match_panes(snapshot: dict, rows: list[dict]) -> dict[str, tuple[dict, str]]:
    """Reserve explicit IDs first. Join by cwd only when both sides are unique."""
    panes = snapshot["panes"]
    matches = {}
    used = set()

    def admit(pane, candidate, evidence):
        key = candidate["family"], candidate["sid"]
        used.add(key)
        matches[pane["pane_id"]] = candidate, evidence

    for pane in panes:
        if not pane.get("sid") or (pane["family"], pane["sid"]) in used:
            continue
        options = [row for row in rows if row["family"] == pane["family"] and row["sid"] == pane["sid"]]
        if len(options) == 1:
            admit(pane, options[0], "session ID")

    groups = defaultdict(list)
    for pane in panes:
        if pane.get("family") and not pane.get("sid") and pane.get("cwd"):
            groups[(pane["family"], pane["cwd"])].append(pane)
    for (family, cwd), group in groups.items():
        if len(group) != 1:
            continue
        options = [row for row in rows
                   if (row["family"], row["sid"]) not in used
                   and row["family"] == family and row.get("cwd") == cwd
                   and (row.get("first_ts") is None or row["first_ts"] <= snapshot["ts"] + 5)]
        if len(options) == 1:
            admit(group[0], options[0], "unique CLI and directory")
    return matches
