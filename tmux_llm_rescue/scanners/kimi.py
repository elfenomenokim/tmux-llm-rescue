from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import UUID_RE


def _kimi_last_ts(sess_dir: Path, ceiling: float | None = None) -> float | None:
    """True last-activity from wire.jsonl's per-event float `timestamp` (epoch sec).

    state.json has no per-turn timestamp, but its sibling wire.jsonl logs every
    event with a top-level float `timestamp` whose final value matches the dir
    mtime to the second (verified). Tail-reads 64KB; returns None if the file is
    absent/unparseable so the caller falls back to state.json mtime."""
    wire = sess_dir / "wire.jsonl"
    try:
        with wire.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    last: float | None = None
    for ln in data.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        ts = d.get("timestamp")
        if isinstance(ts, (int, float)) and (ceiling is None or ts <= ceiling):
            last = float(ts)
    return last


def indexed_sessions(kimi_home, warn):
    index_path = kimi_home / "session_index.jsonl"
    if not index_path.exists():
        return
    try:
        lines = index_path.read_text(encoding='utf-8').splitlines()
    except OSError as e:
        warn(f"{index_path}: {e}")
        return
    seen: set[str] = set()
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        sid = rec.get("sessionId")
        sess_dir_str = rec.get("sessionDir")
        if (not isinstance(sid, str) or not sid or not isinstance(sess_dir_str, str)
                or not sess_dir_str or sid in seen):
            continue
        seen.add(sid)
        yield rec, sid, Path(sess_dir_str)


KIMI_SID = re.compile(r"(?:session_)?" + UUID_RE.pattern)


def state_title(state, warn):
    try:
        title = json.loads(state.read_text(encoding="utf-8")).get("custom_title", "")
        return title if isinstance(title, str) else ""
    except (OSError, ValueError, AttributeError) as exc:
        warn(f"{state}: state.json corrupted ({exc})")
        return ""


def _work_dirs(root, extra_cwds, warn):
    cwds = set(extra_cwds)
    path = root / "kimi.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        for entry in metadata.get("work_dirs", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                cwds.add(entry["path"])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        warn(f"{path}: {exc}")
    return {hashlib.md5(cwd.encode()).hexdigest(): cwd for cwd in cwds}


def _sessions(root, extra_cwds, warn):
    if (root / "session_index.jsonl").is_file():
        for record, sid, directory in indexed_sessions(root, warn):
            yield sid, directory, record.get("workDir")
        return
    index = _work_dirs(root, extra_cwds, warn)
    for state in (root / "sessions").glob("*/*/state.json"):
        directory = state.parent
        yield directory.name, directory, index.get(directory.parent.name)


def scan(root, anchor, lookback, warn, extra_cwds=()):
    rows = []
    for sid, directory, cwd in _sessions(root, extra_cwds, warn):
        if not KIMI_SID.fullmatch(sid):
            continue
        state = directory / "state.json"
        try:
            info = state.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            warn(f"{state}: {exc}")
            continue
        if info.st_mtime < anchor - lookback - 1800:
            continue
        activity = _kimi_last_ts(directory, anchor) or info.st_mtime
        if not anchor - lookback <= activity <= anchor:
            continue
        rows.append({
            "kind": "Kimi", "family": "kimi", "sid": sid,
            "cwd": cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else None,
            "mtime": activity, "title": state_title(state, warn)[:160],
            "source_path": str(state),
        })
    return rows
