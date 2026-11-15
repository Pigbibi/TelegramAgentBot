# `src/telegram_agent_bot` Module Guide

See root [AGENTS.md](../../AGENTS.md) for repository-wide rules, commit format, and security boundaries.

## Module Boundaries

- [bot.py](bot.py): Telegram `Update`, command, and callback orchestration, handler registration, runtime lifecycle, and module-level runtime globals (`agent_backend`, `session_monitor`, `config`). Keep Telegram transport and handler wiring here.
- Leaf routing and profile helpers ([browser_routing.py](browser_routing.py), [profile_helpers.py](profile_helpers.py), [agent_profile.py](agent_profile.py), [output_mode.py](output_mode.py)): Pure normalization, root/path routing, and profile option resolution. These modules must stay free of `bot.py` imports and Telegram handler state mutations.
- `handlers/`: Telegram UI keyboard builders, message queue/sender utilities, status polling, and topic cleanup helpers.
- `backends/`: `AgentBackend` and `AgentBrowser` protocols, plugin registry, and backend implementations.

## Backward-Compatible Re-exports

- Existing callers and tests import or `mock.patch` attributes directly on `telegram_agent_bot.bot` (such as `_profile_models`, `_resolve_profile_effort`, `_default_directory_browser_path`, `_adapt_forward_command_for_agent`, and `agent_backend`).
- When moving pure helpers out of [bot.py](bot.py), keep backward-compatible aliases or thin wrappers in [bot.py](bot.py) that pass `bot.py` globals (`config`, `agent_backend`, `CC_COMMANDS`) so module-attribute patches continue to work.

## Verification Commands

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/telegram_agent_bot/
uv run pytest --tb=short -q
python3 scripts/check_hygiene.py
```
