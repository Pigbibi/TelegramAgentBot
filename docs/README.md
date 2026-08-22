# TelegramAgentBot documentation

Start with the main [README](../README.md) for installation and the first
Telegram session. Use the guides below for configuration and optional
components.

## User guides

- [Features](features.md) — supported workflows, message types, session
  behavior, and operator controls
- [Configuration](configuration.md) — environment variables, agent selection,
  queue limits, health alerts, and authentication
- [Deployment](deployment.md) — Linux and macOS services, logs, upgrades, and
  troubleshooting

## Optional components

- [Agent backend plugins](agent_backend_plugins.md) — backend contract and
  remote-node architecture
- [Socket backend](../plugins/socket_backend/README.md) — center bot and remote
  agent node setup
- [GitHub issue bridge](github_codex_bridge.md) — dispatch GitHub issues to
  live Codex windows
- [VPS cleanup timer](vps_cleanup.md) — bounded cleanup for small Linux hosts

## Project participation

- [Contributing](../CONTRIBUTING.md)
- [Support](../SUPPORT.md)
- [Security policy](../SECURITY.md)
- [Code of conduct](../CODE_OF_CONDUCT.md)

Configuration examples in this repository use placeholders. Keep real bot
tokens, API keys, account files, repository lists, and host paths outside the
Git checkout.
