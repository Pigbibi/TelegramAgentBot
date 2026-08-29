# Features

TelegramAgentBot connects Telegram forum topics to live agent CLI sessions. It
uses tmux for process ownership and agent transcript files for structured
output delivery.

## Session control

- Create a session in a project selected from Telegram.
- Choose Codex or Claude Code for each new topic.
- Select a discovered or configured model and reasoning level.
- Toggle Fast mode independently from the reasoning level.
- Resume a tracked session in the selected directory.
- Bind a topic to an existing tracked tmux window.
- Detach a topic without stopping its terminal window.
- Stop a window and remove its topic binding.

TelegramAgentBot stores bindings and monitor offsets under
`$TELEGRAM_AGENT_BOT_DIR`. Restarting the bot does not stop the tmux windows it
manages.

## Input

The bot accepts:

- text messages;
- Telegram voice messages through a configured transcription provider;
- photos and files, saved locally before their paths are sent to the agent;
- Escape and interrupt controls;
- agent-native active-turn guidance and next-turn queueing;
- agent slash commands, skills, and plugins;
- inline-keyboard answers for supported interactive prompts.

When a local Codex or Claude Code session is working, a text message offers
**Guide current turn**, **Queue next turn**, **Interrupt and send**, and
**Cancel** actions. Guidance maps to each CLI's Enter behavior. Codex next-turn
input maps to native Tab; because Claude Code has no equivalent ordinary-text
key, its next-turn action uses the durable AgentBot FIFO. `/steer <message>` and
`/queue <message>` provide the same routing without the picker. If the agent
becomes idle before the action is selected, the message safely starts a normal
new turn.

The Telegram command menu exposes common native commands including `/skills`,
`/plugins`, `/status`, `/permissions`, `/mcp`, `/plan`, and `/init`. `/plugins`
is translated to Codex `/plugins` or Claude Code `/plugin` for the bound topic.
Use `/agentcmd /plugin-name:skill-name arguments` for plugin skills whose native
command names cannot be represented as Telegram bot command names.

Pending routing choices and submissions are persisted without putting prompt
text in Telegram callback data. Sessions without native routing support retain
the bounded durable FIFO. Slash commands sent while Claude Code is responding
use Claude Code's own command queue. Queue size, expiry, startup timeout, and
maximum active turns are configurable.

## Output

Telegram delivery can include:

- assistant replies;
- public progress shown by the CLI;
- tool calls and tool results;
- local command output;
- permission requests and structured questions;
- usage-limit and authentication failures;
- terminal screenshots requested by the user.

`clean` mode favors final answers and compact progress. `trace` mode adds public
tool summaries. Private model reasoning is not forwarded.

Long Telegram messages are paginated, and Markdown output falls back to plain
text when Telegram rejects the formatted form.

## Authentication and accounts

The default session uses the agent credentials of the service user. Telegram
commands can also create and select named account snapshots for Codex and Claude
Code.

Account selection affects new topics. Existing topics remain attached to their
current sessions. Automatic account rotation after a usage-limit error is
disabled unless explicitly enabled.

Some Claude Code credentials may be stored in an OS keychain rather than copied
files. Verify named-account switching on the target host before relying on it.

## Project browsing

By default, the directory picker opens at `~/Projects`. Operators can configure
named roots such as local workspaces or mounted remote filesystems:

```ini
TELEGRAM_AGENT_BOT_PROJECT_ROOTS=Local=~/Projects,Remote=/mnt/remote-projects
```

The picker does not navigate above the selected root. Hidden directories are
excluded unless explicitly enabled.

## Health and operations

The built-in health monitor observes a bounded set of host and runtime signals:

- available memory;
- swap usage;
- disk usage;
- age of the oldest queued input;
- transcript delivery lag.

Alerts are sent only to allowed operators. Unchanged problems respect a
configurable cooldown, and recovery must remain stable before a recovery notice
is sent. `/health` provides an on-demand snapshot.

Source-checkout installations can check for AgentBot and agent CLI updates while
the bot is idle. Update settings are opt-in at the application level; review the
annotated [`.env.example`](../.env.example) before enabling automatic changes.

## Extensibility

The built-in `local` backend controls tmux on the same machine as the Telegram
bot. The backend plugin API supports alternative transports and remote agent
nodes. See [Agent backend plugins](agent_backend_plugins.md).

The separate `telegram-agent-bridge` command can translate selected GitHub
issues into structured prompts for existing Codex windows. See
[GitHub issue bridge](github_codex_bridge.md).

## Data locations

| Path | Contents |
| --- | --- |
| `$TELEGRAM_AGENT_BOT_DIR/.env` | Runtime configuration and secrets |
| `$TELEGRAM_AGENT_BOT_DIR/state.json` | Topic bindings, targets, display state, and hidden sessions |
| `$TELEGRAM_AGENT_BOT_DIR/session_map.json` | Hook-generated window-to-session mappings |
| `$TELEGRAM_AGENT_BOT_DIR/monitor_state.json` | Transcript monitor offsets |
| `$TELEGRAM_AGENT_BOT_DIR/runtime.sqlite3` | Durable input queue, routing choices, and health alert state |
| `$TELEGRAM_AGENT_BOT_DIR/accounts/` | Optional named account snapshots |
| `~/.codex/` | Default Codex transcript and configuration root |
| `~/.claude/projects/` | Default Claude Code transcript root |

Do not commit files from the application directory. They may contain tokens,
account material, private paths, prompts, or repository names.
