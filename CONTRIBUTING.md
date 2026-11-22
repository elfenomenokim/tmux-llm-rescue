# Contributing

## Before you start

Open an issue first for a change to the restore behavior or to a supported
storage format. Include the CLI version and operating system. Reduce session
logs to their shape and use fake content and paths.

Use a private advisory for security reports. [Security](SECURITY.md)

## Set up

From the repository root, install the package into a virtual environment.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Use a disposable named socket for a real tmux check. See
[AGENTS.md](AGENTS.md) for the package and restore boundaries.

## Send a patch

- Add or update a fixture when a storage format or restore rule changes.
- Update both READMEs and add a changelog line for user-visible changes.
- Include the commands you ran and name platforms you could not try.
- Put each restoration change in a pull request that describes the initial
  socket state and the expected windows after it runs.
