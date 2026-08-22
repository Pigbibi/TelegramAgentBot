# Contributing to TelegramAgentBot

Thank you for helping improve TelegramAgentBot. Focused bug reports, tests,
documentation fixes, and small pull requests are the easiest contributions to
review and maintain.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening an issue

- Search existing issues and pull requests.
- Confirm the problem still occurs on the latest release or `main`.
- Remove tokens, user IDs, private repository names, prompts, transcripts, and
  local file paths from logs and screenshots.
- Use [SUPPORT.md](SUPPORT.md) to distinguish a usage question from a bug.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

A useful bug report includes the operating system, Python version, agent CLI and
version, install method, relevant configuration with secrets removed, exact
steps, expected behavior, actual behavior, and a minimal log excerpt.

## Development setup

Requirements:

- Python 3.12 or newer;
- uv;
- tmux;
- Git.

Clone and install all development dependencies:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
uv sync --all-extras
```

Use a test bot token and disposable tmux session for manual integration work.
Never run the test checkout against a production bot token or irreplaceable
agent session.

## Making a change

1. Create a branch from the latest `main`.
2. Keep the change focused on one problem.
3. Follow existing module boundaries and typing style.
4. Add or update tests for behavior changes.
5. Update public documentation when configuration or user behavior changes.
6. Run the closest test first, then the full checks appropriate to the change.

Important project invariants:

- one Telegram topic resolves to one backend target and active agent session;
- tmux remains the process owner for the local backend;
- durable queues and bindings survive an ordinary bot restart;
- agent credentials and transcription keys must not reach child processes or
  logs;
- remote paths must be authorized on the backend that accesses them;
- public model reasoning must never be inferred from private internal data.

## Validation

Targeted example:

```bash
uv run pytest tests/telegram_agent_bot/test_maintenance_cleanup.py -q
```

Full project checks:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/telegram_agent_bot/
uv run pytest --tb=short -q
```

GitHub Actions runs the full suite on supported Python versions. Do not hide a
failing check by weakening an unrelated rule or deleting a test.

## Documentation style

- Write for someone installing the project for the first time.
- Describe the supported behavior, not the sequence of internal rewrites that
  produced it.
- Put installation and first use in the README; place detailed operation in
  `docs/`.
- Use repository-relative links and portable placeholder paths.
- Keep examples free of real tokens, user IDs, private repositories, hostnames,
  and home directories.
- Treat `.env.example` and code defaults as the source of truth for settings.
- Keep the English and Simplified Chinese README aligned when changing their
  shared content.

## Pull requests

A pull request should include:

- a short problem statement;
- the behavior changed;
- validation commands and results;
- compatibility or operational risks;
- documentation updates when users must act differently.

Open a draft pull request when the design is still changing. Mark it ready only
after the branch is reviewable and checks pass. Maintainers may ask to split a
large change into smaller pull requests.

Contributors retain copyright in their work and agree to license submitted
contributions under the repository's [MIT License](LICENSE).
