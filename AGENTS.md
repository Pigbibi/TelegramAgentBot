# AGENTS.md

This repository contains TelegramCodexBot, a Telegram controller for live Codex
sessions running in tmux. The CLI/package name is `telegram-codex-bot`.

## Common Commands

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/telegram_codex_bot/
uv run pytest
./scripts/restart.sh
telegram-codex-bot hook --install
```

## Working Notes

- Keep changes small and follow existing patterns.
- Do not hardcode machine-specific paths; prefer `Path.home()` or env vars.
- Preserve the topic -> tmux window -> session mapping.
- Validate with lint, typecheck, and relevant tests before committing.
