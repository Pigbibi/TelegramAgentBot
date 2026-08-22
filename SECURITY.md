# Security policy

TelegramAgentBot controls local agent CLIs through Telegram and tmux. Security
reports involving authorization, credential handling, command injection, path
boundaries, remote backend transport, file upload, or transcript disclosure are
especially important.

## Supported versions

Security fixes target the latest release and the `main` branch. Older releases
may not receive backports. Reproduce against a current version before reporting
when it is safe to do so.

## Reporting a vulnerability

Do not open a public issue with vulnerability details, credentials, private
prompts, transcripts, or proof-of-concept exploit code.

Private vulnerability reporting is not currently enabled for this repository.
Open a minimal issue stating that you need a private security contact, without
including technical details. A maintainer will provide an appropriate private
channel. You may also use contact information published on the repository
owner's GitHub profile.

Include the following in the private report:

- affected version or commit;
- deployment type and backend;
- required attacker access;
- reproduction steps or a minimal proof of concept;
- impact and data exposed;
- suggested mitigation, if known;
- whether the issue is already public.

There is no guaranteed response SLA. Maintainers will try to acknowledge valid
reports, reproduce the issue, coordinate a fix, and credit the reporter when
requested and appropriate.

## Scope

In scope:

- bypassing `ALLOWED_USERS` authorization;
- leaking bot tokens, provider keys, agent credentials, private prompts, or
  transcripts;
- command, tmux target, file-name, or path injection;
- escaping configured project roots through the Telegram browser;
- unauthorized socket-backend operations;
- unsafe file upload or overwrite behavior;
- privilege escalation caused by project code or service templates.

Usually out of scope:

- vulnerabilities in Telegram, Codex CLI, Claude Code, tmux, or an external
  transcription provider that do not result from this project's integration;
- an operator deliberately enabling an agent's unsafe approval or sandbox
  flags;
- denial of service that requires an already authorized operator and has no
  privilege or boundary impact;
- findings based only on version banners or automated scanner output without a
  reproducible impact.

Do not test against systems, bots, accounts, or repositories you do not own or
have explicit permission to assess.

## Deployment guidance

- Restrict `ALLOWED_USERS` to trusted numeric IDs.
- Keep `.env`, account snapshots, state files, and bridge configuration outside
  the repository and owner-readable only.
- Bind socket-backend nodes to loopback and transport them through SSH or a
  private network.
- Review agent approval, sandbox, hook, and update settings before unattended
  deployment.
- Keep the bot process unprivileged and grant narrow sudo rules only when an
  explicit maintenance feature requires them.
- Rotate credentials immediately if a log, issue, or pull request exposes them.
