# CLI execution retirement

Codex and Cursor topic launch, resume, login, model-discovery processes and CLI updater execution are retired. The picker offers Claude only. Historical transcript parsers and agent identifiers remain for reading saved state; they do not authorize execution. The normal Claude permission mode is mandatory; Full mode cannot launch, including via saved profiles or direct calls.

Input to retired or unidentified existing windows is refused. Existing processes are not killed or restarted by this source change. An owner must inspect in-flight work, choose a supported default, label/rebind valid Claude sessions and plan the deployment transition. An old Codex default deliberately fails closed instead of silently switching to a billed API profile. This PR stays draft because live-service behavior changes.

No credentials, secrets or variables were read or deleted. No server, paid model request or deployment was used for validation.
