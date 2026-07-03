"""Tests for saved account snapshot helpers."""

from telegram_agent_bot import account_manager
from telegram_agent_bot.config import config


def test_next_account_rotates_by_name(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    current_name_file = tmp_path / "current_name"
    for name in ("plus1", "plus2", "team"):
        account_dir = snapshot_dir / name
        account_dir.mkdir(parents=True)
        (account_dir / "auth.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "CURRENT_NAME_FILE", current_name_file)

    assert account_manager.get_default_account_name() is None
    assert account_manager.get_next_account_name("plus1") == "plus2"
    assert account_manager.get_next_account_name("plus2") == "team"
    assert account_manager.get_next_account_name("team") == "plus1"
    assert account_manager.get_next_account_name(None) is None

    current_name_file.write_text("plus2\n", encoding="utf-8")
    assert account_manager.get_current_account_name() == "plus2"
    assert account_manager.get_default_account_name() == "plus2"
    assert account_manager.get_next_account_name(None) == "plus2"


def test_ensure_account_home_copies_auth_and_config(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    account_home_dir = tmp_path / "homes"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)

    account_dir = snapshot_dir / "plus1"
    account_dir.mkdir(parents=True)
    (account_dir / "auth.json").write_text(
        '{"auth_mode":"chatgpt"}',
        encoding="utf-8",
    )
    (codex_dir / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")
    (codex_dir / "hooks.json").write_text('{"hooks":{}}\n', encoding="utf-8")

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CODEX_DIR", codex_dir)

    home = account_manager.ensure_account_home("plus1")

    assert home == account_home_dir / "plus1"
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"auth_mode":"chatgpt"}'
    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-5.4"\ncheck_for_update_on_startup = false\n'
    )
    assert (home / "hooks.json").read_text(encoding="utf-8") == '{"hooks":{}}\n'
    assert (home / "memories").is_dir()
    assert (home / "tmp").is_dir()


def test_ensure_claude_account_home_copies_credentials_and_settings(
    tmp_path, monkeypatch
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    account_home_dir = tmp_path / "homes"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True)

    account_dir = snapshot_dir / "main"
    account_dir.mkdir(parents=True)
    (account_dir / "credentials.db").write_bytes(b"sqlite-data")
    (claude_dir / "settings.json").write_text(
        '{"hooks":{"SessionStart":[]}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "agent_type", "claude")
    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CLAUDE_DIR", claude_dir)

    home = account_manager.ensure_account_home("main")

    assert home == account_home_dir / "main"
    assert (home / ".claude" / "credentials.db").read_bytes() == b"sqlite-data"
    assert (home / ".claude" / "settings.json").read_text(encoding="utf-8") == (
        '{"hooks":{"SessionStart":[]}}\n'
    )
    assert not (home / "auth.json").exists()
    assert not (home / "credentials.db").exists()
    assert not (home / "config.toml").exists()
    assert not (home / "hooks.json").exists()
    assert (home / ".claude" / "projects").is_dir()


def test_ensure_claude_account_home_copies_json_credentials(
    tmp_path, monkeypatch
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    account_home_dir = tmp_path / "homes"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True)

    account_dir = snapshot_dir / "main"
    account_dir.mkdir(parents=True)
    (account_dir / ".credentials.json").write_text(
        '{"account":"claude"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "agent_type", "claude")
    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CLAUDE_DIR", claude_dir)

    home = account_manager.ensure_account_home("main")

    assert (home / ".claude" / ".credentials.json").read_text(
        encoding="utf-8"
    ) == '{"account":"claude"}'


def test_ensure_account_home_writes_update_check_before_tables(
    tmp_path, monkeypatch
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    account_home_dir = tmp_path / "homes"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)

    account_dir = snapshot_dir / "plus1"
    account_dir.mkdir(parents=True)
    (account_dir / "auth.json").write_text("{}", encoding="utf-8")
    (codex_dir / "config.toml").write_text(
        "[notice]\nhide_full_access_warning = true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CODEX_DIR", codex_dir)

    home = account_manager.ensure_account_home("plus1")

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        "check_for_update_on_startup = false\n\n"
        "[notice]\n"
        "hide_full_access_warning = true\n"
    )


def test_disable_codex_update_prompt_uses_default_codex_home(
    tmp_path, monkeypatch
) -> None:
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(account_manager, "CODEX_DIR", codex_dir)

    account_manager.disable_codex_update_prompt()

    assert (codex_dir / "config.toml").read_text(encoding="utf-8") == (
        "check_for_update_on_startup = false\n"
    )


def test_account_name_validation() -> None:
    assert account_manager.is_valid_account_name("main") is True
    assert account_manager.is_valid_account_name("plus_1.backup") is True
    assert account_manager.is_valid_account_name("../bad") is False
    assert account_manager.is_valid_account_name("bad/name") is False
    assert account_manager.is_valid_account_name("") is False


def test_save_account_snapshot_copies_auth(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)

    snapshot = account_manager.save_account_snapshot("main", codex_dir)

    assert snapshot == snapshot_dir / "main"
    assert (snapshot / "auth.json").read_text(encoding="utf-8") == (
        '{"auth_mode":"chatgpt"}'
    )


def test_save_claude_account_snapshot_copies_credentials(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "credentials.db").write_bytes(b"sqlite-data")

    monkeypatch.setattr(config, "agent_type", "claude")
    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)

    snapshot = account_manager.save_account_snapshot("main", claude_dir)

    assert snapshot == snapshot_dir / "main"
    assert (snapshot / "credentials.db").read_bytes() == b"sqlite-data"


def test_save_claude_account_snapshot_copies_json_credentials(
    tmp_path, monkeypatch
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".credentials.json").write_text(
        '{"account":"claude"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "agent_type", "claude")
    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)

    snapshot = account_manager.save_account_snapshot("main", claude_dir)

    assert snapshot == snapshot_dir / "main"
    assert (snapshot / ".credentials.json").read_text(encoding="utf-8") == (
        '{"account":"claude"}'
    )


def test_clear_current_account_removes_selection(tmp_path, monkeypatch) -> None:
    current_name_file = tmp_path / "accounts" / "current_name"
    current_name_file.parent.mkdir(parents=True)
    current_name_file.write_text("main\n", encoding="utf-8")

    monkeypatch.setattr(account_manager, "CURRENT_NAME_FILE", current_name_file)

    account_manager.clear_current_account()

    assert not current_name_file.exists()


def test_prepare_account_home_does_not_require_auth(tmp_path, monkeypatch) -> None:
    account_home_dir = tmp_path / "homes"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CODEX_DIR", codex_dir)

    home = account_manager.prepare_account_home("main")

    assert home == account_home_dir / "main"
    assert not (home / "auth.json").exists()
    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-5.4"\ncheck_for_update_on_startup = false\n'
    )
    assert (home / "memories").is_dir()
    assert (home / "tmp").is_dir()
