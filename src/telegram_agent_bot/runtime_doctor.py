"""Read-only routing diagnostics for the local tmux backend."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TextIO


DoctorSeverity = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class DoctorFinding:
    """One sanitized, operator-facing runtime diagnostic."""

    severity: DoctorSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RuntimeDoctorReport:
    """A bounded report that never changes runtime state."""

    findings: tuple[DoctorFinding, ...]

    @property
    def status(self) -> DoctorSeverity:
        if any(finding.severity == "error" for finding in self.findings):
            return "error"
        if any(finding.severity == "warning" for finding in self.findings):
            return "warning"
        return "ok"

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "error" else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class TmuxProbe:
    """Result of one read-only tmux window listing."""

    available: bool
    window_ids: frozenset[str]


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def diagnose_routing_state(
    *,
    state: Mapping[str, Any],
    session_map: Mapping[str, Any],
    tmux_session_name: str,
    live_window_ids: set[str],
    tmux_available: bool,
) -> RuntimeDoctorReport:
    """Check persisted routing against the currently visible tmux windows."""
    findings: list[DoctorFinding] = []
    raw_bindings = _mapping(state.get("thread_bindings"))
    bindings_by_window: dict[str, int] = {}
    local_routes: list[tuple[str, str, str]] = []

    if raw_bindings is None:
        findings.append(
            DoctorFinding(
                "error",
                "invalid_thread_bindings",
                "State has an invalid topic binding section.",
            )
        )
    else:
        for user_id, raw_topics in raw_bindings.items():
            topics = _mapping(raw_topics)
            if topics is None:
                findings.append(
                    DoctorFinding(
                        "error",
                        "invalid_thread_bindings",
                        "State has an invalid user topic binding entry.",
                    )
                )
                continue
            for thread_id, window_id in topics.items():
                if not isinstance(window_id, str) or not window_id:
                    findings.append(
                        DoctorFinding(
                            "error",
                            "invalid_window_binding",
                            "State has a topic binding without a tmux window ID.",
                        )
                    )
                    continue
                bindings_by_window[window_id] = bindings_by_window.get(window_id, 0) + 1
                local_routes.append((str(user_id), str(thread_id), window_id))

    if local_routes:
        findings.append(
            DoctorFinding(
                "ok",
                "topic_bindings_loaded",
                f"Loaded {len(local_routes)} local topic binding(s).",
            )
        )
    else:
        findings.append(
            DoctorFinding("ok", "topic_bindings_empty", "No local topic bindings.")
        )

    duplicates = [
        window_id for window_id, count in bindings_by_window.items() if count > 1
    ]
    if duplicates:
        findings.append(
            DoctorFinding(
                "error",
                "duplicate_window_binding",
                f"{len(duplicates)} tmux window(s) are bound to multiple topics.",
            )
        )

    raw_targets = _mapping(state.get("thread_targets")) or {}
    raw_window_states = _mapping(state.get("window_states")) or {}
    session_map_prefix = f"{tmux_session_name}:"
    mapped_windows = {
        key[len(session_map_prefix) :]
        for key, value in session_map.items()
        if isinstance(key, str)
        and key.startswith(session_map_prefix)
        and isinstance(value, Mapping)
        and isinstance(value.get("session_id"), str)
        and value.get("session_id")
    }

    for user_id, thread_id, window_id in local_routes:
        user_targets = _mapping(raw_targets.get(user_id))
        target = _mapping(user_targets.get(thread_id)) if user_targets else None
        if target and target.get("backend_id") == "local":
            target_window_id = target.get("window_id")
            if target_window_id != window_id:
                findings.append(
                    DoctorFinding(
                        "error",
                        "local_target_mismatch",
                        "A local backend target does not match its legacy topic binding.",
                    )
                )

        window_state = _mapping(raw_window_states.get(window_id))
        state_session_id = (
            window_state.get("session_id")
            if window_state and isinstance(window_state.get("session_id"), str)
            else ""
        )
        target_session_id = (
            target.get("session_id")
            if target and isinstance(target.get("session_id"), str)
            else ""
        )
        if (
            not state_session_id
            and not target_session_id
            and window_id not in mapped_windows
        ):
            findings.append(
                DoctorFinding(
                    "warning",
                    "missing_session_identity",
                    "A bound tmux window has no persisted agent session identity yet.",
                )
            )

    if tmux_available:
        missing_bound_windows = {
            window_id
            for _user_id, _thread_id, window_id in local_routes
            if window_id not in live_window_ids
        }
        if missing_bound_windows:
            findings.append(
                DoctorFinding(
                    "error",
                    "bound_window_missing",
                    f"{len(missing_bound_windows)} bound tmux window(s) are not live.",
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    "ok", "tmux_bindings_live", "All bound tmux windows are live."
                )
            )

        stale_session_map_windows = mapped_windows - live_window_ids
        if stale_session_map_windows:
            findings.append(
                DoctorFinding(
                    "warning",
                    "stale_session_map_entry",
                    f"{len(stale_session_map_windows)} session map entry or entries are stale.",
                )
            )
    else:
        findings.append(
            DoctorFinding(
                "error" if local_routes else "warning",
                "tmux_unavailable",
                "Unable to inspect the configured tmux session.",
            )
        )

    return RuntimeDoctorReport(tuple(findings))


def _load_json_mapping(
    path: Path, *, label: str
) -> tuple[Mapping[str, Any], DoctorFinding | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, DoctorFinding(
            "error", f"{label}_unreadable", f"Unable to read {label.replace('_', ' ')}."
        )
    mapping = _mapping(value)
    if mapping is None:
        return {}, DoctorFinding(
            "error",
            f"{label}_invalid",
            f"{label.replace('_', ' ').capitalize()} must be an object.",
        )
    return mapping, None


def probe_tmux_windows(
    *,
    tmux_session_name: str,
    tmux_socket_path: Path | None,
) -> TmuxProbe:
    """List tmux windows through the CLI without creating a session or socket."""
    tmux = shutil.which("tmux")
    if not tmux:
        return TmuxProbe(False, frozenset())

    command = [tmux]
    if tmux_socket_path is not None:
        command.extend(("-S", str(tmux_socket_path)))
    command.extend(("list-windows", "-t", tmux_session_name, "-F", "#{window_id}"))
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return TmuxProbe(False, frozenset())
    if result.returncode != 0:
        return TmuxProbe(False, frozenset())
    return TmuxProbe(
        True,
        frozenset(line.strip() for line in result.stdout.splitlines() if line.strip()),
    )


def collect_runtime_doctor_report(config: Any) -> RuntimeDoctorReport:
    """Collect a routing report without importing or starting bot runtime services."""
    state, state_finding = _load_json_mapping(config.state_file, label="state_file")
    session_map, session_map_finding = _load_json_mapping(
        config.session_map_file,
        label="session_map_file",
    )
    probe = probe_tmux_windows(
        tmux_session_name=config.tmux_session_name,
        tmux_socket_path=config.tmux_socket_path,
    )
    report = diagnose_routing_state(
        state=state,
        session_map=session_map,
        tmux_session_name=config.tmux_session_name,
        live_window_ids=set(probe.window_ids),
        tmux_available=probe.available,
    )
    file_findings = tuple(
        finding
        for finding in (state_finding, session_map_finding)
        if finding is not None
    )
    return RuntimeDoctorReport((*file_findings, *report.findings))


def _format_report(report: RuntimeDoctorReport) -> str:
    lines = [f"AgentBot doctor: {report.status}"]
    lines.extend(
        f"{finding.severity.upper()} {finding.code}: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def doctor_main(
    argv: Sequence[str], *, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr
) -> int:
    """Run the read-only doctor command."""
    if any(argument in {"-h", "--help", "help"} for argument in argv):
        print("Usage: telegram-agent-bot doctor [--json]", file=stdout)
        return 0
    if any(argument != "--json" for argument in argv):
        print("Usage: telegram-agent-bot doctor [--json]", file=stderr)
        return 2

    from .config import config

    report = collect_runtime_doctor_report(config)
    if "--json" in argv:
        print(json.dumps(report.to_dict(), ensure_ascii=False), file=stdout)
    else:
        print(_format_report(report), file=stdout)
    return report.exit_code
