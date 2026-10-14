# GitHub bridge retirement

The old bridge no longer sends issue text or Enter keys to local Codex/tmux sessions. Dispatch refuses before any local process runs. Historical issue parsing and dry-run rendering remain for reading existing records. Do not reset dispatch state or replay old work during migration. See [CLI retirement](cli-retirement.md). No server, secret or deployed service was changed.
