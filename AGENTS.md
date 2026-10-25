# AGENTS.md

## 项目是什么

把 Telegram forum topic 接到跑在 tmux 里的 agent 会话。可以开项目、恢复会话、发提示词和文件、回答交互问题、跟进 agent 状态。

## 技术栈

Python（`pyproject.toml`），uv 管理依赖，ruff + pytest。

## 目录

- `src/telegram_agent_bot/` — Bot 实现。`bridge.py` 是 Codex 桥接。
- `plugins/` — 后端插件。
- `prompts/` — 提示词模板。
- `docs/` — 英文为默认，中文加 `.zh-CN.md`。
- `tests/` — 测试。

## 本地验证

按改动选择相关离线检查，命令以仓库当前 CI 为准。先区分代码问题与环境条件，不为通过检查降低保护。

**Lint**

```bash
uv run ruff check src/ tests/
```

**格式检查**

```bash
uv run ruff format --check src/ tests/
```

**测试**

```bash
uv run pytest --tb=short -q
```

## 命名与文档

- 新增文档文件名小写、`-` 连接，中文版后缀 `.zh-CN.md`；`README.md`、`AGENTS.md` 等工具约定文件保留固定名称。源码沿用现有语言命名。
- 不提交审计报告、复盘、交接单、进度快照、升级计划。要追溯看 commit 历史。
- 改文件名时同一次提交改完所有引用。
- 提交前跑 `python scripts/check_hygiene.py`。

## 不要做

- 不要在消息里回显 Bot token、会话内容或本机路径。
- 不要改 tmux 会话的命名和生命周期语义。已有会话靠这套命名恢复。
- 不要在 handler 里直接执行 shell 命令。所有命令走已有的白名单路径。
- 不要扩大允许的 chat 和 topic 范围。

## 提交与 PR

Conventional Commits，标题中文，不超过 50 字。类型只用 `feat` `fix` `docs` `refactor` `test` `chore` `perf` `ci`。

分支名 `<type>/<主题>`，例如 `docs/normalize-filenames`。不直接推默认分支，不 force push 已 review 的分支。

提交、推送、合并及生产动作按用户已明确授权的范围执行；同范围已有授权不重复确认。

PR 描述写改了什么、为什么改、怎么验证。
