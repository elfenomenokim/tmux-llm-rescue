"""Command-line entry point for scan, record and opt-in restore."""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, restore, topology
from .config import load_homes
from .scanners import UUID_RE, claude, codex, kimi, timestamp

_SCANNERS = {"claude": claude.scan, "codex": codex.scan, "kimi": kimi.scan}


def warn(message):
    print(f"tmux-llm-rescue: {message}", file=sys.stderr)


def duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", value.strip())
    if not match:
        raise ValueError(f"invalid duration {value!r}; use a value such as 12h")
    amount, unit = match.groups()
    seconds = float(amount) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if not math.isfinite(seconds) or not 0 < seconds <= 366 * 86400:
        raise ValueError("duration must be positive and at most 366 days")
    return seconds


def anchor_value(value):
    if value is None:
        return None
    parsed = timestamp(value)
    return parsed if parsed is not None else time.time() - duration(value)


def discover(homes, anchor, lookback, snapshot, alive):
    rows = {}
    cwds = [pane["cwd"] for pane in (snapshot or {}).get("panes", []) if pane.get("cwd")]
    for home in homes:
        scanner = _SCANNERS.get(home.family)
        if scanner is None:
            raise ValueError(f"unknown CLI family {home.family!r}")
        options = {"extra_cwds": cwds} if home.family == "kimi" else {}
        for row in scanner(home.root, anchor, lookback, warn, **options):
            pattern = kimi.KIMI_SID if home.family == "kimi" else UUID_RE
            sid, cwd, mtime = row["sid"], row.get("cwd"), row["mtime"]
            if not isinstance(sid, str) or not pattern.fullmatch(sid):
                continue
            if not math.isfinite(mtime) or not anchor - lookback <= mtime <= anchor:
                continue
            if row.get("warn") or not isinstance(cwd, str) or not cwd.isprintable() or not Path(cwd).is_absolute():
                cwd = None
            argv = home.argv(sid)
            candidate = {
                "family": home.family, "sid": sid, "cwd": cwd, "mtime": mtime,
                "title": row["title"], "home": str(home.root),
                "first_ts": row.get("first_ts"),
                "alive": (home.family, sid) in alive,
                "argv": argv,
                "resume": f"cd {shlex.quote(cwd)} && {shlex.join(argv)}" if cwd else None,
            }
            key = home.family, home.root.resolve(), argv, sid
            previous = rows.get(key)
            if previous is None or (cwd in cwds, mtime) > (previous["cwd"] in cwds, previous["mtime"]):
                rows[key] = candidate
    return sorted(rows.values(), key=lambda row: (-row["mtime"], row["family"], row["sid"], row["home"]))


def report(rows, snapshot, matches, anchor, as_json):
    locations = {}
    for pane in (snapshot or {}).get("panes", []):
        match = matches.get(pane["pane_id"])
        if match:
            row, evidence = match
            locations[id(row)] = {"session": pane["session"], "window": pane["window"],
                                  "pane": pane["pane_id"], "cwd": pane["cwd"], "evidence": evidence}
    if as_json:
        print(json.dumps({
            "anchor": anchor,
            "snapshot": snapshot["ts"] if snapshot else None,
            "candidates": [{"number": i, **row, "pane": locations.get(id(row))}
                           for i, row in enumerate(rows, 1)],
        }, indent=2))
        return
    print(f"Recovery at {datetime.fromtimestamp(anchor).astimezone().isoformat(timespec='seconds')}")
    if not rows:
        print("No sessions found in the lookback window.")
        return
    print(" #  CLI       Updated           Session ID                              Recorded window")
    for i, row in enumerate(rows, 1):
        place = locations.get(id(row))
        location = f"{place['session']!r}/{place['window']!r}" if place else "—"
        status = " [running]" if row["alive"] else ""
        updated = datetime.fromtimestamp(row["mtime"]).strftime("%Y-%m-%d %H:%M")
        print(f"{i:>2}  {row['family']:<9} {updated}  {row['sid']:<39} {location}{status}")
        title = " ".join(row["title"].split())
        if title:
            print("    " + "".join(c for c in title[:100] if c.isprintable()))
        if not row["alive"]:
            if row["resume"]:
                print(f"    {row['resume']}")
            else:
                print("    Working directory unavailable. Open the session in the CLI's session picker.")


def parser(command):
    prog = "tmux-llm-rescue" + (f" {command}" if command else "")
    p = argparse.ArgumentParser(prog=prog, description=(
        "Record the selected server's panes." if command == "record" else
        "Create new panes for recorded sessions." if command == "restore" else
        "Find local Claude Code, Codex and Kimi sessions. Commands: record, restore."))
    p.add_argument("-L", "--socket", metavar="NAME", help="select a named tmux socket")
    if command != "record":
        p.add_argument("--since", metavar="TIME", help="crash time as an ISO time or relative duration")
        p.add_argument("--lookback", default="12h", help="session activity window, default 12h")
    if command == "restore":
        p.add_argument("--dry-run", action="store_true", help="show the plan without changing tmux")
        p.add_argument("--only", metavar="N,M", help="restore selected row numbers from the scan")
    if command is None:
        p.add_argument("--json", action="store_true", help="print machine-readable candidates")
        p.add_argument("--version", action="version", version=__version__)
    return p


def _scan(args, socket, command):
    identities, children = topology.process_identities()
    alive = {(family, sid) for family, sid in identities.values() if sid}
    current = topology.capture(socket, identities, children)
    as_of = anchor_value(args.since)
    snapshot = topology.select_snapshot(topology.read_history(socket.history), current, as_of)
    if snapshot is None:
        if command == "restore":
            raise ValueError("no recorded snapshot for this socket; run record while the source server is alive")
        warn("no recorded snapshot for this socket; showing the session list")
    anchor = as_of if as_of is not None else snapshot["ts"] if snapshot else time.time()
    rows = discover(load_homes(), anchor, duration(args.lookback), snapshot, alive)
    matches = topology.match_panes(snapshot, rows) if snapshot else {}
    if command != "restore":
        report(rows, snapshot, matches, anchor, args.json)
        return 0

    only = restore.numbers(args.only) if args.only is not None else None
    launches, notes = restore.plan(snapshot, rows, matches, current, alive, only)
    for note in notes:
        print(note)
    if not launches:
        print("No sessions to restore.")
        return 0
    restore.describe(launches, current, socket)
    if not args.dry_run:
        restore.execute(launches, current, socket)
    return 0


def main(argv=None):
    from fcntl import LOCK_EX, flock

    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args and args[0] in ("record", "restore") else None
    args = parser(command).parse_args(args)
    try:
        socket = topology.socket_target(args.socket)
        if command == "record":
            snapshot = topology.record(socket)
            print(f"Recorded {len(snapshot['panes'])} pane(s).")
            return 0
        if command == "restore" and not args.dry_run:
            if not socket.history.is_file():
                raise ValueError("no recorded snapshot for this socket")
            with socket.history.with_suffix(".restore.lock").open("a") as lock:
                flock(lock, LOCK_EX)
                return _scan(args, socket, command)
        return _scan(args, socket, command)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        warn(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
