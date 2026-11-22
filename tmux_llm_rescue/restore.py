"""Plan and create only new tmux windows and panes."""

from __future__ import annotations

import re
import shlex
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .topology import Socket


@dataclass(frozen=True)
class Launch:
    number: int
    session: str
    window_id: str
    window: str
    cwd: str
    argv: tuple[str, ...]


def numbers(value: str) -> frozenset[int]:
    if not re.fullmatch(r"[1-9]\d*(?:,[1-9]\d*)*", value):
        raise ValueError("--only needs comma-separated positive row numbers")
    return frozenset(map(int, value.split(",")))


def plan(snapshot, rows, matches, current, alive, only=None):
    existing = (current or {}).get("panes", [])
    unknown = {(pane["family"], pane["cwd"]) for pane in existing
               if pane.get("family") and not pane.get("sid")}
    numbered = {id(row): i for i, row in enumerate(rows, 1)}
    if only and max(only) > len(rows):
        raise ValueError(f"--only names a row outside 1–{len(rows)}")
    wanted = only if only is not None else set(numbered.values())
    launches = []
    notes = []
    for pane in snapshot["panes"]:
        match = matches.get(pane["pane_id"])
        if not match:
            continue
        candidate, _ = match
        number = numbered[id(candidate)]
        if number not in wanted:
            continue
        wanted -= {number}
        cwd = pane["cwd"]
        if (candidate["family"], candidate["sid"]) in alive:
            notes.append(f"{number}. Already running")
        elif (candidate["family"], cwd) in unknown or (candidate["family"], candidate.get("cwd")) in unknown:
            notes.append(f"{number}. A live {candidate['family']} pane in this directory has no recorded ID")
        elif not cwd or not cwd.isprintable() or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            notes.append(f"{number}. The recorded working directory is unavailable")
        else:
            launches.append(Launch(number, pane["session"], pane["window_id"], pane["window"], cwd,
                                   tuple(candidate["argv"])))
    notes.extend(f"{number}. No unambiguous recorded pane" for number in sorted(wanted))
    return launches, notes


def describe(launches, current, socket: Socket):
    existing = {pane["session"] for pane in (current or {}).get("panes", [])}
    seen_windows = set()
    for launch in launches:
        key = launch.session, launch.window_id
        if key in seen_windows:
            action = "Split new window"
        elif launch.session in existing:
            action = "Create window"
        else:
            action = "Create session and window"
        seen_windows.add(key)
        existing.add(launch.session)
        print(f"{launch.number}. {action} {launch.session!r} / {launch.window!r} "
              f"on {socket.option} {shlex.quote(socket.address)}")
        print(f"   cd {shlex.quote(launch.cwd)} && {shlex.join(launch.argv)}")


def _tmux_arg(value: str) -> str:
    # The tmux CLI treats an unescaped final semicolon as a command separator.
    return value[:-1] + "\\;" if value.endswith(";") else value


def _create(socket, args, launch):
    # tmux expands the -c format. Double literal hashes so a directory such
    # as /work/#{session_name} stays a literal directory.
    cwd = launch.cwd.replace("#", "##")
    # More than one shell-command argument tells tmux to exec the argv
    # directly. The env prefix also handles single-argument templates.
    command = (*args, "-c", cwd, "/usr/bin/env", *launch.argv)
    result = socket.run(*map(_tmux_arg, command))
    if result.returncode:
        raise RuntimeError(f"row {launch.number}: {result.stderr.strip() or 'tmux creation failed'}")
    return result.stdout.strip()


def execute(launches, current, socket: Socket):
    sessions = {pane["session"]: pane["session_id"] for pane in (current or {}).get("panes", [])}
    groups = defaultdict(list)
    for launch in launches:
        if not shutil.which(launch.argv[0]):
            raise RuntimeError(f"row {launch.number}: executable {launch.argv[0]!r} is unavailable")
        groups[(launch.session, launch.window_id)].append(launch)
    for (session, _), group in groups.items():
        first, *rest = group
        fmt = ("-P", "-F", "#{session_id} #{window_id}")
        window_name = first.window.replace("#", "##")
        if session in sessions:
            args = ("new-window", "-d", "-t", f"{sessions[session]}:", "-n", window_name, *fmt)
        else:
            session_name = session.replace("#", "##")
            args = ("new-session", "-d", "-s", session_name, "-n", window_name, *fmt)
        created_session, window = _create(socket, args, first).split()
        if not re.fullmatch(r"\$\d+", created_session) or not re.fullmatch(r"@\d+", window):
            raise RuntimeError("tmux returned an invalid new session or window ID")
        sessions[session] = created_session
        for launch in rest:
            _create(socket, ("split-window", "-d", "-t", window), launch)
            # Distribute space before another split, targeting this new window.
            result = socket.run("select-layout", "-t", window, "tiled")
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "tmux could not arrange the new window")
