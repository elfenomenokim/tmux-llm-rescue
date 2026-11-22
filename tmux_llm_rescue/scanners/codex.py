from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import UUID_RE, timestamp


def _codex_threads_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
    except sqlite3.Error:
        return set()


# Filename: rollout-<ISO start>-<uuid>.jsonl. Filename UUID == internal payload.id
# (verified 10/10). Anchored capture of the trailing sid.
_CODEX_ROLLOUT_SID_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T[\d-]+-(" + UUID_RE.pattern + r")\.jsonl$"
)


def _codex_rollout_last_ts(path: Path, ceiling: float | None = None) -> float | None:
    """Read backward in blocks so post-restart writes cannot hide earlier activity."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            remainder = b""
            while size:
                start = max(0, size - 65536)
                stream.seek(start)
                chunk = stream.read(size - start) + remainder
                lines = chunk.split(b"\n")
                remainder = lines.pop(0) if start else b""
                for raw in reversed(lines):
                    if not raw.lstrip().startswith(b"{"):
                        continue
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    ts = timestamp(event.get("timestamp"))
                    if ts is not None and (ceiling is None or ts <= ceiling):
                        return ts
                if ceiling is None:
                    break
                size = start
    except OSError:
        pass
    return None


def scan_codex_rollout(anchor: float, lookback_sec: int,
                       sessions_dir: Path, resume_bin: str, *,
                       full_window: bool = False) -> list[dict]:
    """Fallback when state_5.sqlite is unreadable (crash left -wal mid-checkpoint).

    Parses <codex-home>/sessions/<date>/rollout-*.jsonl directly:
      - sid     : from filename (resumable; == payload.id)
      - cwd     : line-1 session_meta payload.cwd (first line only — line 2 may be
                  a parent thread's meta for forked sessions)
      - source  : subagent markers appear as thread_source == 'subagent' OR as a
                  nested payload.source dict {'subagent': {...}} (format verified on
                  existing versions); skip both to mirror the DB's
                  `source NOT LIKE '%subagent%'` filter
      - last-ts : final top-level `timestamp` (anchor-window gate)
    Scoped to the anchor date + prior day (midnight-crash safety)."""
    if not sessions_dir.exists():
        return []
    out: list[dict] = []
    lo = anchor - lookback_sec
    hi = anchor if full_window else anchor + 60
    if full_window:
        day = datetime.fromtimestamp(lo).date()
        end = datetime.fromtimestamp(hi).date()
        days = []
        while day <= end:
            days.append(day.strftime("%Y/%m/%d"))
            day += timedelta(days=1)
    else:
        days = {datetime.fromtimestamp(anchor - off).strftime("%Y/%m/%d")
                for off in (0, 86400)}
    for day in sorted(days):
        day_dir = sessions_dir / day
        if not day_dir.is_dir():
            continue
        for jf in day_dir.glob("rollout-*.jsonl"):
            m = _CODEX_ROLLOUT_SID_RE.search(jf.name)
            if not m:
                continue
            sid = m.group(1)
            cwd = None
            first_ts = None
            thread_source = None
            source_val = None
            try:
                with jf.open("r", encoding="utf-8", errors="ignore") as fh:
                    d = json.loads(fh.readline())
                payload = d.get("payload") if isinstance(d, dict) else None
                if isinstance(d, dict):
                    first_ts = timestamp(d.get("timestamp"))
                if isinstance(payload, dict):
                    if full_window and (d.get("type") != "session_meta" or payload.get("id") != sid):
                        continue
                    cwd = payload.get("cwd")
                    thread_source = payload.get("thread_source")
                    source_val = payload.get("source")
            except (OSError, json.JSONDecodeError):
                continue
            if first_ts is not None and first_ts > hi:
                continue
            if thread_source == "subagent":
                continue
            if isinstance(source_val, dict) and "subagent" in source_val:
                continue
            last_ts = _codex_rollout_last_ts(jf, hi if full_window else None)
            if last_ts is None or not (lo <= last_ts <= hi):
                continue
            out.append({
                **({"first_ts": first_ts} if full_window else {}),
                "kind": "Codex",
                "profile": None,
                "model": "gpt(rollout)",
                "sid": sid,
                "cwd": cwd,
                "mtime": last_ts,
                "user_turns": None,
                "title": "(via rollout fallback — DB unreadable)",
                "source_path": str(jf),
                "resume_bin": resume_bin,
            })
    return out


def _scan_codex_home(state_db: Path, sessions_dir: Path, resume_bin: str,
                     anchor: float, lookback_sec: int, warn) -> list[dict]:
    """Codex sessions that existed by anchor (created_at <= anchor + 1m) and were
    touched within [anchor - lookback, anchor + 1m].

    Hardened against concurrent writers:
      - read-only URI mode (no lock acquisition for SELECT)
      - busy_timeout 2s to ride out write batches
      - one retry on OperationalError before surfacing the failure
      - failure is loud (stderr + degraded candidate row), never silent

    Optionally reads the `model` column if present (Codex versions may differ);
    falls back to a placeholder rather than the misleading 'gpt-5.5' default.
    """
    if not state_db.exists():
        # No DB in this home — try rollout files directly (some homes may be
        # rollout-only), then give up quietly if that's empty too.
        return scan_codex_rollout(anchor, lookback_sec, sessions_dir, resume_bin)
    out: list[dict] = []
    lo = anchor - lookback_sec
    hi = anchor + 60

    def attempt() -> list[dict] | None:
        try:
            # immutable=1 tells SQLite the file cannot change, so it skips the
            # -wal/-shm sidecars entirely. Right after a tmux crash those sidecars
            # can be truncated/mid-checkpoint, which made a plain mode=ro raise
            # "unable to open database file". immutable reads only the committed
            # main-DB image — fine for rescue (we want sessions that existed
            # *before* the crash), and the rollout fallback covers any WAL gap.
            conn = sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=2.0)
        except sqlite3.Error as e:
            warn(f"Codex DB connect failed ({state_db}): {e}")
            return None
        try:
            try:
                conn.execute("PRAGMA busy_timeout = 2000")
            except sqlite3.Error:
                pass
            cols = _codex_threads_columns(conn)
            has_model = "model" in cols
            has_tokens = "tokens_used" in cols
            # tokens_used drives the idle-tier quality floor; select it when present.
            select_cols = "id, cwd, updated_at, title, source, rollout_path, created_at"
            select_cols += ", tokens_used" if has_tokens else ", NULL AS tokens_used"
            if has_model:
                select_cols += ", model"
            try:
                cur = conn.execute(
                    f"""
                    SELECT {select_cols}
                      FROM threads
                     WHERE (source IS NULL OR source NOT LIKE '%subagent%')
                       AND created_at <= ?
                       AND updated_at >= ?
                     ORDER BY updated_at DESC
                    """,
                    (hi, lo),
                )
                rows = cur.fetchall()
            except sqlite3.Error as e:
                warn(f"Codex DB query failed: {e}")
                return None
            # Subagent threads are excluded above because they are not separately
            # resumable — but silence about them misleads. On 2026-08-12 the
            # command that panicked the machine ran inside one, and it appeared
            # nowhere, so its absence read as a scan gap rather than as design.
            try:
                n_sub = conn.execute(
                    "SELECT count(*) FROM threads WHERE source LIKE '%subagent%'"
                    "  AND created_at <= ? AND updated_at >= ?", (hi, lo)).fetchone()[0]
            except sqlite3.Error:
                n_sub = 0
            if n_sub:
                warn(f"{n_sub} Codex subagent thread(s) in window hidden — they "
                        f"belong to their parent session, resume the parent "
                        f"({Path(state_db).parent.name})")
            result: list[dict] = []
            for row in rows:
                sid, cwd, ts, title, _src, rollout, cts, tokens = row[:8]
                model_val = row[8] if has_model else None
                # Defensive coerce: sqlite returns ts as int/float; if column is TEXT
                # ISO, convert. Avoid downstream datetime.fromtimestamp TypeError.
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        warn(f"Codex thread {sid}: non-numeric updated_at {ts!r}; skipped")
                        continue
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    warn(f"Codex thread {sid}: bad updated_at type; skipped")
                    continue
                try:
                    tokens = int(tokens) if tokens is not None else None
                except (TypeError, ValueError):
                    tokens = None
                try:
                    cts = float(cts)
                except (TypeError, ValueError):
                    cts = None
                result.append({
                    "kind": "Codex",
                    "profile": None,
                    "model": (model_val or "gpt-5.5(?)"),
                    "sid": sid,
                    "cwd": cwd,
                    "mtime": ts,
                    # Birth time was CC-only until 2026-08-12, so the post-crash
                    # guard never applied to a DB-scanned Codex thread, and the
                    # pane-birth coincidence in `_born_here` had nothing to read.
                    "first_ts": cts,
                    "user_turns": None,
                    "tokens": tokens,
                    "title": (title or "")[:120],
                    "source_path": rollout,
                    "resume_bin": resume_bin,
                })
            return result
        finally:
            conn.close()

    for retry in range(2):
        result = attempt()
        if result is not None:
            out = result
            break
        if retry == 0:
            time.sleep(0.2)  # short backoff for a busy DB
    else:
        # both DB attempts failed — fall back to parsing rollout files directly.
        warn(f"Codex DB unreadable after 2 tries ({state_db}); falling back to rollout-*.jsonl parse")
        rollout = scan_codex_rollout(anchor, lookback_sec, sessions_dir, resume_bin)
        if rollout:
            out = rollout
        else:
            # fallback also empty — surface a single visible row so the user knows
            out.append({
                "kind": "Codex", "profile": None, "model": "?",
                "sid": "(scan failed)", "cwd": str(state_db),
                "mtime": anchor, "user_turns": None,
                "title": "⚠️ Codex DB scan failed + rollout fallback empty — see stderr",
                "source_path": str(state_db), "warn": True,
                "resume_bin": resume_bin,
            })
    return out


def scan(root, anchor, lookback, warn):
    sessions = root / "sessions"
    databases = sorted(root.glob("state_*.sqlite"),
                       key=lambda path: int(path.stem[6:]) if path.stem[6:].isdigit() else -1)
    rows = _scan_codex_home(databases[-1], sessions, "codex", anchor, lookback, warn) if databases else []
    by_sid = {row["sid"]: row for row in rows if not row.get("warn")}
    # A read-only immutable connection omits recent WAL entries. The rollouts
    # cover those entries and can supply a pre-anchor timestamp after a resume.
    for row in scan_codex_rollout(anchor, lookback, sessions, "codex", full_window=True):
        existing = by_sid.get(row["sid"])
        if existing is None:
            by_sid[row["sid"]] = row
        elif not anchor - lookback <= existing["mtime"] <= anchor:
            existing["mtime"] = row["mtime"]
    for row in by_sid.values():
        row["family"] = "codex"
        if row["model"] in ("gpt-5.5(?)", "gpt(rollout)"):
            row["model"] = ""
        if row["title"] == "(via rollout fallback — DB unreadable)":
            row["title"] = ""
    return list(by_sid.values())
