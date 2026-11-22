"""Per-CLI session roots and trusted resume command templates."""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None

CONFIG_NAME = "tmux-llm-rescue"


@dataclass(frozen=True)
class Home:
    family: str
    root: Path
    resume_command: str

    def argv(self, sid: str) -> tuple[str, ...]:
        return tuple(arg.replace("{sid}", sid) for arg in shlex.split(self.resume_command))


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / CONFIG_NAME / "config.toml"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / CONFIG_NAME


def _stock(family: str, variable: str, default: str, command: str) -> Home:
    override = os.environ.get(variable)
    root = Path(override or Path.home() / default).expanduser().resolve()
    if override:
        command = f"env {shlex.quote(f'{variable}={root}')} {command}"
    return Home(family, root, command)


def default_homes() -> list[Home]:
    return [
        _stock("claude", "CLAUDE_CONFIG_DIR", ".claude", "claude --resume {sid}"),
        _stock("codex", "CODEX_HOME", ".codex", "codex resume {sid}"),
        _stock("kimi", "KIMI_SHARE_DIR", ".kimi", "kimi --resume {sid}"),
        _stock("kimi", "KIMI_CODE_HOME", ".kimi-code", "kimi-code --session {sid}"),
    ]


def _read_home_tables(text: str) -> dict:
    """Read the documented TOML subset on Python 3.9 and 3.10."""
    header = re.compile(r"^\[\[homes\]\]\s*(?:#.*)?$")
    entry = re.compile(r"""^(family|root|resume_command)\s*=\s*("(?:\\.|[^"\\])*"|'[^']*')\s*(?:#.*)?$""")
    homes = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if header.fullmatch(line):
            homes.append({})
            continue
        match = entry.fullmatch(line)
        if not homes or not match:
            raise ValueError(f"line {number}: expected [[homes]] or a quoted home field")
        key, raw = match.groups()
        if key in homes[-1]:
            raise ValueError(f"line {number}: duplicate {key}")
        homes[-1][key] = json.loads(raw) if raw.startswith('"') else raw[1:-1]
    return {"homes": homes}


def load_homes(path: Path | None = None) -> list[Home]:
    path = config_path() if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_homes()
    try:
        data = tomllib.loads(text) if tomllib is not None else _read_home_tables(text)
        records = data.get("homes", [])
        if not isinstance(records, list):
            raise ValueError("homes must be an array of tables")
        homes = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"family", "root", "resume_command"}:
                raise ValueError("each home needs family, root and resume_command")
            family, root, command = (record[key] for key in ("family", "root", "resume_command"))
            if not all(isinstance(value, str) and value.strip() for value in (family, root, command)):
                raise ValueError("home fields must be nonempty strings")
            if "{sid}" not in command or not shlex.split(command):
                raise ValueError("resume_command must contain {sid}")
            expanded = Path(root).expanduser()
            if not expanded.is_absolute():
                raise ValueError("root must be absolute or start with ~")
            homes.append(Home(family, expanded, command))
        return homes
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
