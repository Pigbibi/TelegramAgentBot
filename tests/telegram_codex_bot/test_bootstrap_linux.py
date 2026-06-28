from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-linux.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_repo(path: Path) -> Path:
    (path / "scripts").mkdir(parents=True)
    shutil.copy2(BOOTSTRAP_SCRIPT, path / "scripts" / "bootstrap-linux.sh")
    (path / ".env.example").write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=your_bot_token_here",
                "ALLOWED_USERS=123456789,987654321",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path / "scripts" / "bootstrap-linux.sh"


def _prepare_env(tmp_path: Path, home: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_executable(bin_dir / "uv", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "codex", "#!/usr/bin/env bash\nexit 0\n")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "TELEGRAM_CODEX_BOT_DIR": str(home / ".telegram-codex-bot"),
        }
    )
    return env


def test_linux_bootstrap_rejects_checkout_inside_default_projects(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = home / "Projects" / "TelegramAgentBot"
    script = _prepare_repo(repo)
    env = _prepare_env(tmp_path, home)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Unsafe TelegramAgentBot checkout location" in result.stderr
    assert str(home / "Projects") in result.stderr


def test_linux_bootstrap_allows_checkout_outside_project_roots(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = home / ".telegram-codex-bot" / "app" / "TelegramAgentBot"
    script = _prepare_repo(repo)
    env = _prepare_env(tmp_path, home)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    launcher = home / ".telegram-codex-bot" / "bin" / "telegram-codex-bot-launch"
    assert launcher.is_file()
    assert f'cd "{repo}"' in launcher.read_text(encoding="utf-8")
