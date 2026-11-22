## Set up for a user

1. Run `uname -s`, `tmux -V` and `python3 --version`. Expect `Darwin` or
   `Linux`, a tmux version and Python 3.9 or newer. Install missing prerequisites
   with the user's existing OS package manager.
2. Run `uv tool install tmux-llm-rescue` or `pipx install tmux-llm-rescue`.
   Add the tool manager's executable directory to PATH when the installer asks.
3. Run `tmux-llm-rescue --version` and expect a version number. Inside the user's
   selected tmux server, run `tmux-llm-rescue record` and expect `Recorded N pane(s).`
   Run `tmux-llm-rescue --json` and check that the output has a `candidates` list.
4. If a command is missing, use the path printed by the installer. If there is
   no server, run scan without recording or select an existing server with
   `-L NAME`. If the list is empty, confirm the CLI session root and the activity
   window. Use the configuration example in README.md for custom homes.

Nothing in setup requires a login to the recovery tool. The assistant CLI
keeps its own account and setup requirements.

## Working in the repo

- `tmux_llm_rescue/scanners/` reads the three CLI storage formats.
- `topology.py` selects the socket and records window and pane identity.
- `restore.py` plans and creates new sessions, windows and panes.
- `cli.py` handles the command arguments and table and JSON output.
- From the repository root, run `python -m unittest discover -s tests -v`.
- Build a wheel with `python -m pip wheel --no-deps .`. The package has zero
  runtime Python dependencies. Keep the English and Korean commands in sync.

## Code Review Rules

- Scanners must leave CLI session databases and files unchanged.
- Test tmux commands only on a disposable, explicitly named socket.
- A dry run must leave tmux untouched. Restore must only create windows and
  sessions and may split panes only in a window it just created.
- A launch command comes from the trusted resume configuration and the session
  ID. Recorded process arguments and conversation content are input data.
- Ambiguous joins and known running sessions must stay out of the restore plan.
- Fixture data and demo output use made-up sessions and machine paths.
