# Security

## Supported versions

The latest release receives security fixes.

## Reporting a vulnerability

Send an advisory through the repository's Security tab. Include the affected
version, CLI, operating system and the smallest reproduction you can share.
Use synthetic logs and remove local paths and account details.

[Report a vulnerability privately](https://github.com/elfenomenokim/tmux-llm-rescue/security/advisories/new)

Untrusted session metadata and recorded pane information are treated as data.
The command template in your own configuration is a trusted executable choice.
A restore that executes session content or replaces an existing live pane is
within scope for a security report.
