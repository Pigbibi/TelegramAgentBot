# AGENTS.md

This repository contains TelegramAgentBot, a Telegram controller for live Codex
CLI and Claude Code sessions running in tmux (`TELEGRAM_AGENT_BOT_AGENT_TYPE`).
The CLI/package name is `telegram-agent-bot`.

## Common Commands

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/telegram_agent_bot/
uv run pytest
./scripts/restart.sh
telegram-agent-bot hook --install
```

## Working Notes

- Keep changes small and follow existing patterns.
- Do not hardcode machine-specific paths; prefer `Path.home()` or env vars.
- Preserve the topic -> tmux window -> session mapping.
- Validate with lint, typecheck, and relevant tests before committing.

## VPS Verification Policy

- This repository is deployed on a 2GB RAM, 2-vCPU VPS. Run only one lint, typecheck, or test command at a time.
- Start with the test file or test case closest to the change and wrap potentially long checks with a timeout.
- Run the full `pytest`, Ruff, or Pyright suite locally only when the change has broad impact and resource headroom is healthy.
- Use GitHub-hosted CI for full or expensive verification; do not offload it to a self-hosted runner on this VPS.
- Do not restart the AgentBot service while tmux-backed Codex tasks or durable input queues are active unless the change is an urgent runtime fix.
