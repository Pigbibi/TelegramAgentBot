#!/usr/bin/env python3
"""Tests for repository documentation, anti-leak, and agent-file hygiene checks."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "check_hygiene.py"
SPEC = importlib.util.spec_from_file_location("check_hygiene", SCRIPT_PATH)
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)


class CheckHygieneTests(unittest.TestCase):
    def _init_minimal_repo(self, repo: Path, english_agents: bool = False, public: bool = False) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        sections = hygiene.AGENTS_SECTIONS_EN if english_agents else hygiene.AGENTS_SECTIONS_ZH
        agents = "\n".join(
            ["<!-- content-boundary:begin -->"] + [f"## {s}\nOK" for s in sections]
        )
        (repo / "AGENTS.md").write_text(agents + "\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (repo / "SECURITY.md").write_text("# Security\n\nReport issues privately.\n", encoding="utf-8")
        if public:
            (repo / "LICENSE").write_text("MIT\n", encoding="utf-8")
            (repo / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
            (repo / "CODE_OF_CONDUCT.md").write_text("# Code of Conduct\n", encoding="utf-8")
            (repo / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True, exist_ok=True)
            (repo / ".github" / "ISSUE_TEMPLATE" / "bug.yml").write_text("name: Bug\n", encoding="utf-8")
            (repo / ".github" / "pull_request_template.md").write_text("## Summary\n", encoding="utf-8")
        (repo / ".gitignore").write_text(
            "\n".join(hygiene.REQUIRED_GITIGNORE_PATTERNS) + "\n",
            encoding="utf-8",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "guide.md").write_text("# Guide\n\nSetup steps.\n", encoding="utf-8")
        (repo / "build" / "plans").mkdir(parents=True, exist_ok=True)
        (repo / "build" / "2026-01-01-audit.md").write_text("# Build artifact\n", encoding="utf-8")

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "add", "AGENTS.md", "CLAUDE.md", "SECURITY.md", ".gitignore"], cwd=repo, check=True)

    def _run_cli(self, repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--root", str(repo), *extra],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_repo_under_build_ancestor_scans_repo_and_skips_repo_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "build" / "repo"
            self._init_minimal_repo(repo)

            found_rels = {p.relative_to(repo).as_posix() for p in hygiene.iter_files(repo)}
            self.assertIn("docs/guide.md", found_rels)
            self.assertNotIn("build/2026-01-01-audit.md", found_rels)

            clean = self._run_cli(repo, "--private")
            self.assertEqual(clean.returncode, 0, msg=clean.stdout + clean.stderr)

            (repo / "plans").mkdir()
            (repo / "docs" / "postmortem.md").write_text("# Notes\n", encoding="utf-8")

            dirty = self._run_cli(repo, "--private")
            self.assertEqual(dirty.returncode, 1, msg=dirty.stdout + dirty.stderr)
            self.assertIn("plans/: do not split directories by phase", dirty.stdout)
            self.assertIn("docs/postmortem.md:", dirty.stdout)

    def test_english_agents_and_nested_agents_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo, english_agents=True)
            (repo / "apps" / "web").mkdir(parents=True)
            (repo / "apps" / "web" / "AGENTS.md").write_text("# Web rules\n", encoding="utf-8")
            res = self._run_cli(repo, "--private", "--strict")
            self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)

    def test_retired_engineering_doc_and_gitignore_exception_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo)
            (repo / ".github").mkdir(exist_ok=True)
            (repo / ".github" / "engineering.md").write_text("# Legacy\n", encoding="utf-8")
            gi = repo / ".gitignore"
            gi.write_text(gi.read_text(encoding="utf-8") + "!.github/engineering.md\n", encoding="utf-8")

            res = self._run_cli(repo, "--private")
            self.assertEqual(res.returncode, 1)
            self.assertIn(".github/engineering.md is retired", res.stdout)
            self.assertIn(".gitignore still unignores retired .github/engineering.md", res.stdout)

    def test_readme_patch_prose_phase_headings_and_post_license_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo)
            (repo / "README.md").write_text(
                "# Demo\n\n## P0–P6 Runtime Status\n\nBehavior remains unchanged in this PR.\n\n"
                "## License\n\nMIT\n\n## Weekly secret scan\n\nRuns weekly.\n",
                encoding="utf-8",
            )
            res = self._run_cli(repo, "--private")
            self.assertEqual(res.returncode, 1)
            self.assertIn("PR-diff narration in README prose", res.stdout)
            self.assertIn("phase-milestone heading in README", res.stdout)
            self.assertIn("section placed after License", res.stdout)

    def test_code_examples_in_readme_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo)
            (repo / "README.md").write_text(
                "# Demo\n\nDo not write `remains unchanged` in prose.\n\n"
                "```markdown\n## License\n\n## After License Example\n```\n\n"
                "## License\n\nMIT\n",
                encoding="utf-8",
            )
            res = self._run_cli(repo, "--private")
            self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)

    def test_public_vs_private_internal_artifacts_and_missing_gitignore_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo, public=True)
            (repo / "docs" / "changes").mkdir(parents=True)
            (repo / "docs" / "changes" / "card.md").write_text("# Card\n", encoding="utf-8")
            (repo / "docs" / "acceptance-policy.md").write_text("# Policy\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", "docs/changes/card.md", "docs/acceptance-policy.md"],
                cwd=repo,
                check=True,
            )

            pub = self._run_cli(repo)
            self.assertEqual(pub.returncode, 1)
            self.assertIn("docs/changes/", pub.stdout)
            self.assertIn("docs/acceptance-policy.md", pub.stdout)

            priv = self._run_cli(repo, "--private")
            self.assertEqual(priv.returncode, 1)
            self.assertIn("docs/acceptance-policy.md: internal artifact must not be tracked in Git", priv.stdout)

            subprocess.run(
                ["git", "rm", "-f", "--cached", "docs/changes/card.md", "docs/acceptance-policy.md"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "docs" / "changes" / "card.md").unlink()
            (repo / "docs" / "acceptance-policy.md").unlink()
            priv_clean = self._run_cli(repo, "--private")
            self.assertEqual(priv_clean.returncode, 0, msg=priv_clean.stdout + priv_clean.stderr)

            (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            missing_gi = self._run_cli(repo, "--private")
            self.assertEqual(missing_gi.returncode, 1)
            self.assertIn("missing required anti-leak patterns", missing_gi.stdout)

    def test_public_repo_requires_pr_and_issue_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo, public=True)
            clean = self._run_cli(repo)
            self.assertEqual(clean.returncode, 0, msg=clean.stdout + clean.stderr)

            (repo / ".github" / "pull_request_template.md").unlink()
            missing_pr_tpl = self._run_cli(repo)
            self.assertEqual(missing_pr_tpl.returncode, 1)
            self.assertIn(".github/pull_request_template.md", missing_pr_tpl.stdout)

            (repo / ".github" / "pull_request_template.md").write_text("## Summary\n", encoding="utf-8")
            (repo / ".github" / "ISSUE_TEMPLATE" / "bug.yml").unlink()
            empty_issue_tpl = self._run_cli(repo)
            self.assertEqual(empty_issue_tpl.returncode, 1)
            self.assertIn("ISSUE_TEMPLATE", empty_issue_tpl.stdout)

            priv = self._run_cli(repo, "--private")
            self.assertEqual(priv.returncode, 0, msg=priv.stdout + priv.stderr)

    def test_gitignored_local_notes_are_skipped_while_staged_notes_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._init_minimal_repo(repo, public=True)
            (repo / "docs" / "changes").mkdir(parents=True)
            (repo / "docs" / "changes" / "note.md").write_text("# Note\n", encoding="utf-8")
            (repo / "docs" / "plans").mkdir(parents=True)
            (repo / "docs" / "plans" / "draft.md").write_text("# Draft\n", encoding="utf-8")
            (repo / "docs" / "acceptance-policy.md").write_text("# Policy\n", encoding="utf-8")

            ignored_res = self._run_cli(repo)
            self.assertEqual(ignored_res.returncode, 0, msg=ignored_res.stdout + ignored_res.stderr)

            subprocess.run(["git", "add", "-f", "docs/changes/note.md"], cwd=repo, check=True)
            staged_res = self._run_cli(repo)
            self.assertEqual(staged_res.returncode, 1)
            self.assertIn("docs/changes/", staged_res.stdout)


    def test_english_only_flag_rejects_zh_cn_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_minimal_repo(root)
            (root / "README.md").write_text("# Demo\n\n[中文](README.zh-CN.md)\n", encoding="utf-8")
            (root / "README.zh-CN.md").write_text("# Demo\n\n[EN](README.md)\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md", "README.zh-CN.md"], cwd=root, check=True)
            ok = self._run_cli(root, "--private", "--strict")
            self.assertEqual(ok.returncode, 0, msg=ok.stdout + ok.stderr)
            fail = self._run_cli(root, "--private", "--strict", "--english-only")
            self.assertEqual(fail.returncode, 1)
            self.assertIn("README.zh-CN.md: .zh-CN.md files are retired", fail.stdout)

    def test_long_list_item_warns_under_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_minimal_repo(root)
            (root / "docs" / "architecture.md").write_text(
                "# Arch\n\n- " + ("word " * 180) + "\n", encoding="utf-8"
            )
            res = self._run_cli(root, "--private", "--strict")
            self.assertEqual(res.returncode, 1)
            self.assertIn("list item runs", res.stdout)

if __name__ == "__main__":
    unittest.main()
