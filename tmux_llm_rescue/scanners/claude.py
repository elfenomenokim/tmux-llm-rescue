from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import UUID_RE


def _strip_model_suffix(m: str | None) -> str | None:
    if not m:
        return m
    return re.sub(r"\[[^\]]+\]$", "", m).strip()


def _extract_cc_meta(jsonl: Path, anchor: float, lookback_sec: int, forward_buffer: int = 60) -> dict:
    """Walk jsonl to find model, cwd, last activity ts near anchor.
    Also count user turns whose timestamp is in [anchor-lookback, anchor+buffer].

    Counts decode/parse errors so the caller can emit a 'partial/corrupt'
    warning instead of silently dropping a session that *was* live but whose
    timestamps are unreadable."""
    model: str | None = None
    cwd: str | None = None
    user_turns_total = 0
    user_turns_near_anchor = 0
    user_turns_unparseable_ts = 0
    decode_errors = 0
    first_user: str | None = None
    last_user_near: str | None = None
    last_ts_near_anchor: float | None = None
    first_ts: float | None = None      # earliest turn — "did this exist at the crash?"
    read_error: str | None = None
    lo = anchor - lookback_sec
    hi = anchor + forward_buffer

    def parse_ts(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, TypeError):
            return None

    try:
        with jsonl.open("r", encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except json.JSONDecodeError:
                    decode_errors += 1
                    continue
                if not isinstance(d, dict):
                    continue
                if not cwd and isinstance(d.get("cwd"), str):
                    cwd = d["cwd"]
                msg = d.get("message")
                if isinstance(msg, dict) and not model:
                    m = msg.get("model")
                    if isinstance(m, str):
                        model = m
                ts_str = d.get("timestamp")
                ts_epoch = parse_ts(ts_str) if isinstance(ts_str, str) else None
                if ts_epoch is not None and (first_ts is None or ts_epoch < first_ts):
                    first_ts = ts_epoch
                if d.get("type") == "user" and isinstance(msg, dict):
                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text = c.get("text", "")
                                break
                    elif isinstance(content, str):
                        text = content
                    if isinstance(text, str) and text and not text.startswith("<") and "tool_use_id" not in str(content):
                        user_turns_total += 1
                        if first_user is None:
                            first_user = text[:120]
                        if ts_epoch is None:
                            user_turns_unparseable_ts += 1
                        elif lo <= ts_epoch <= hi:
                            user_turns_near_anchor += 1
                            last_user_near = text[:120]
                            last_ts_near_anchor = ts_epoch
    except PermissionError as e:
        read_error = f"permission denied: {e}"
    except OSError as e:
        read_error = f"OSError: {e}"
    return {
        "model_raw": model,
        "model": _strip_model_suffix(model),
        "cwd": cwd,
        "user_turns": user_turns_total,
        "user_turns_near_anchor": user_turns_near_anchor,
        "user_turns_unparseable_ts": user_turns_unparseable_ts,
        "decode_errors": decode_errors,
        "first_user": first_user,
        "last_user_near": last_user_near,
        "last_ts_near_anchor": last_ts_near_anchor,
        "first_ts": first_ts,
        "read_error": read_error,
    }


def _scan_cc_home(projects_root: Path, resume_bin: str, anchor: float,
                  lookback_sec: int, prefilter_lo: float,
                  classify, warn, forward_buffer=60) -> list[dict]:
    out: list[dict] = []
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            if not UUID_RE.fullmatch(jsonl.stem):
                continue
            try:
                mtime = jsonl.stat().st_mtime
            except PermissionError as e:
                warn(f"{jsonl}: permission denied ({e})")
                continue
            except OSError:
                continue
            # mtime is the cheap prefilter only — final decision is "did any user
            # turn timestamp fall inside the anchor window?" computed by _extract_cc_meta.
            # ctime/birthtime are unreliable on macOS APFS (bumped on every append).
            if mtime < prefilter_lo:
                continue
            meta = _extract_cc_meta(jsonl, anchor, lookback_sec, forward_buffer)
            if meta["read_error"]:
                warn(f"{jsonl}: {meta['read_error']}")
                # surface in table so the user investigates
                out.append({
                    "kind": "??", "profile": None, "model": meta["model"] or "",
                    "sid": jsonl.stem, "cwd": meta["cwd"],
                    "mtime": mtime, "user_turns": meta["user_turns"],
                    "title": f"⚠️ unreadable: {meta['read_error']}",
                    "source_path": str(jsonl), "warn": True,
                    "resume_bin": resume_bin,
                })
                continue
            # No in-window activity by timestamp — but if there are unparseable
            # timestamps OR many decode errors AND total turns > 0, surface as
            # a degraded candidate the user can inspect.
            if meta["user_turns_near_anchor"] == 0:
                if (meta["user_turns_unparseable_ts"] > 0 or meta["decode_errors"] > 0) and meta["user_turns"] > 0:
                    out.append({
                        "kind": "CC?" if (meta["model"] or "").startswith("claude") else "CCS?",
                        "profile": None,
                        "model": meta["model"] or "",
                        "sid": jsonl.stem,
                        "cwd": meta["cwd"],
                        "mtime": mtime,
                        "user_turns": meta["user_turns"],
                        "title": (
                            f"⚠️ ts={meta['user_turns_unparseable_ts']}/decode={meta['decode_errors']}"
                            f" near-anchor=0 total={meta['user_turns']} — manual review"
                        ),
                        "source_path": str(jsonl), "warn": True,
                        "resume_bin": resume_bin,
                    })
                continue
            model = meta["model"] or ""
            kind, profile = classify(meta, jsonl.stem)
            assert meta["last_ts_near_anchor"] is not None  # guaranteed by filter above
            out.append({
                "kind": kind,
                "profile": profile,
                "model": model,
                "sid": jsonl.stem,
                "cwd": meta["cwd"],
                "mtime": meta["last_ts_near_anchor"],
                "user_turns": meta["user_turns_near_anchor"],
                "title": meta["last_user_near"] or meta["first_user"] or "",
                "source_path": str(jsonl),
                "resume_bin": resume_bin,
                "home": str(projects_root.parent),
                "first_ts": meta["first_ts"],
            })
    return out


def scan(root, anchor, lookback, warn):
    projects = root / "projects"
    if not projects.is_dir():
        return []
    rows = _scan_cc_home(projects, "claude", anchor, lookback,
                         anchor - lookback - 1800, lambda _meta, _sid: ("CC", None), warn, forward_buffer=0)
    for row in rows:
        row["family"] = "claude"
    return rows
