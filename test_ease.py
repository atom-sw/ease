"""Tests for ease.py: the pure helpers, and every command against real git repositories.

Run the tests with:

    python -m unittest discover
"""

from __future__ import annotations

import contextlib
import datetime
import io
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import ease

CHANGELOG = """\
# Changelog

## [Unreleased]

## [0.2.0] - 2026-02-01

### Added

- Second feature.

## [0.1.01] - 2026-01-15

- A version that shares a prefix with 0.1.0.

## [0.1.0] - 2026-01-01

### Fixed

- First fix.

[Unreleased]: https://example.com/r/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/r/compare/v0.1.01...v0.2.0
[0.1.01]: https://example.com/r/compare/v0.1.0...v0.1.01
[0.1.0]: https://example.com/r/releases/tag/v0.1.0
"""


# A working tree with no files, for version sources that read none.
NO_FILES: dict[str, str] = {}


class ExtractNotesTest(unittest.TestCase):
    """The notes of a release are the body of its changelog section."""

    def test_extracts_each_section_body(self) -> None:
        """A section ends at the next heading, or at the link definitions after the last one."""
        cases = {
            "middle section": ("0.2.0", "### Added\n\n- Second feature."),
            "prefix of another version": ("0.1.0", "### Fixed\n\n- First fix."),
            "the version sharing the prefix": (
                "0.1.01",
                "- A version that shares a prefix with 0.1.0.",
            ),
            "missing section": ("9.9.9", ""),
            "empty unreleased section": ("Unreleased", ""),
        }
        for label, (version, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(ease.extract_notes(CHANGELOG, version), expected)


class PromoteChangelogTest(unittest.TestCase):
    """`ease changelog` turns Unreleased into a dated release section."""

    def setUp(self) -> None:
        """Build the fixture."""
        self.entry = "- Third feature."
        self.text = CHANGELOG.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{self.entry}\n")

    def test_moves_the_unreleased_body_under_the_new_heading(self) -> None:
        """The entries move to the dated section, leaving Unreleased empty."""
        version, date = "0.3.0", "2026-03-01"
        new_text, _ = ease.promote_changelog(self.text, version, date, f"v{version}")
        self.assertEqual(ease.extract_notes(new_text, version), self.entry)
        self.assertEqual(ease.extract_notes(new_text, "Unreleased"), "")
        self.assertIn(f"## [{version}] - {date}", new_text)

    def test_rewrites_the_compare_links(self) -> None:
        """Unreleased compares from the new tag; the new version from the previous one."""
        version = "0.3.0"
        tag = f"v{version}"
        new_text, warnings = ease.promote_changelog(self.text, version, "2026-03-01", tag)
        self.assertEqual(warnings, [])
        self.assertIn(f"[Unreleased]: https://example.com/r/compare/{tag}...HEAD\n", new_text)
        self.assertIn(f"[{version}]: https://example.com/r/compare/v0.2.0...{tag}\n", new_text)

    def test_warns_when_there_is_no_link_to_update(self) -> None:
        """Missing links are left to the user, with a warning."""
        text = "## [Unreleased]\n\n- Something.\n"
        _, warnings = ease.promote_changelog(text, "1.0.0", "2026-03-01", "v1.0.0")
        self.assertEqual(len(warnings), 1)

    def test_refuses_bad_input(self) -> None:
        """Nothing to promote, or a version already there, is an error."""
        cases = {
            "empty unreleased section": (CHANGELOG, "0.3.0"),
            "existing version": (self.text, "0.2.0"),
            "no unreleased section": ("## [0.1.0]\n\n- x\n", "0.3.0"),
        }
        for label, (text, version) in cases.items():
            with self.subTest(label), self.assertRaises(ease.EaseError):
                ease.promote_changelog(text, version, "2026-03-01", f"v{version}")


class MergeignoreTest(unittest.TestCase):
    """`.mergeignore` lines become git pathspecs."""

    def test_parse_skips_blank_lines_and_comments(self) -> None:
        """Only the patterns survive, trimmed, whatever the line endings."""
        patterns = ["TODO.md", "notes/"]
        text = f"# a comment\n\n  {patterns[0]}  \r\n{patterns[1]}\n"
        self.assertEqual(ease.parse_mergeignore(text), patterns)

    def test_to_pathspec(self) -> None:
        """Plain patterns gain glob magic; explicit magic passes through."""
        cases = {
            "plain pattern": ("*.md", ":(glob)*.md"),
            "trailing slash": ("notes/", ":(glob)notes"),
            "explicit magic": (":(icase)readme", ":(icase)readme"),
        }
        for label, (pattern, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(ease.to_pathspec(pattern), expected)


class ConfigTest(unittest.TestCase):
    """`.ease.toml` parses strictly, so a typo fails loudly instead of being ignored."""

    def test_empty_file_takes_the_defaults(self) -> None:
        """Every key is optional."""
        self.assertEqual(ease.config_from_toml(""), ease.Config())

    def test_reads_every_section(self) -> None:
        """Top-level keys, [version], and [hooks] all reach the Config."""
        remotes = ("origin", "public")
        hook = "make check"
        text = f"""
            release-remotes = {list(remotes)!r}
            [version]
            source = "file"
            path = "VERSION.txt"
            [hooks]
            pre-release = "{hook}"
        """
        config = ease.config_from_toml(textwrap.dedent(text).replace("'", '"'))
        self.assertEqual(config.release_remotes, remotes)
        self.assertEqual(config.version_source, "file")
        self.assertEqual(config.version_path, "VERSION.txt")
        self.assertEqual(config.hooks, {"pre-release": hook})

    def test_rejects_invalid_config(self) -> None:
        """Unknown keys, wrong types, and inconsistent values are errors."""
        hook = '\n[hooks]\npre-release = "x"'
        cases = {
            "unknown key": 'colour = "red"',
            "unknown hook": '[hooks]\npre-commit = "x"',
            "unknown version source": '[version]\nsource = "gemspec"',
            "command source without command": '[version]\nsource = "command"',
            "wrong type": "tag = 1",
            "same branches": 'dev-branch = "main"',
            "bad toml": "tag = ",
            "artifacts without a pre-release hook": 'artifacts = "build"',
            "artifacts outside the repository": 'artifacts = "../build"' + hook,
            "absolute artifacts": 'artifacts = "/tmp/build"' + hook,
            "a remote both staging and release": 'staging-remotes = ["origin"]',
            "staging without release remotes": 'staging-remotes = ["a"]\nrelease-remotes = []',
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ease.EaseError):
                ease.config_from_toml(text)


class ResolveVersionTest(unittest.TestCase):
    """Each version source reads its own file."""

    def resolve(self, source: str, files: dict[str, str]) -> str | None:
        """Resolve the version from in-memory files."""
        config = ease.Config(version_source=source)
        return ease.resolve_version(config, files.get, None)

    def test_reads_each_source(self) -> None:
        """Every file-based source finds the same version in its own format."""
        version = "1.2.3"
        cases = {
            "pyproject": {"pyproject.toml": f'[project]\nversion = "{version}"\n'},
            "package.json": {"package.json": f'{{"version": "{version}"}}'},
            "cargo": {"Cargo.toml": f'[package]\nversion = "{version}"\n'},
            "file": {"VERSION": f"\n{version}\n"},
            "changelog": {"CHANGELOG.md": f"## [Unreleased]\n\n## [{version}] - 2026-01-01\n"},
        }
        for source, files in cases.items():
            with self.subTest(source):
                self.assertEqual(self.resolve(source, files), version)

    def test_none_source_has_no_version(self) -> None:
        """A versionless project resolves to None."""
        self.assertIsNone(self.resolve("none", {}))

    def test_command_source_runs_the_command(self) -> None:
        """The command's stdout, stripped, is the version."""
        version = "4.5.6"
        config = ease.Config(version_source="command", version_command="ignored")
        self.assertEqual(
            ease.resolve_version(config, NO_FILES.get, lambda _: f"{version}\n"), version
        )

    def test_command_source_needs_the_working_tree(self) -> None:
        """Without a runner, the working tree is not the source."""
        config = ease.Config(version_source="command", version_command="true")
        with self.assertRaises(ease.EaseError):
            ease.resolve_version(config, NO_FILES.get, None)

    def test_reports_unusable_sources(self) -> None:
        """A source that yields no usable version is an error, not a guess."""
        cases: dict[str, tuple[str, dict[str, str]]] = {
            "missing file": ("pyproject", {}),
            "dynamic version": (
                "pyproject",
                {"pyproject.toml": '[project]\ndynamic = ["version"]'},
            ),
            "no released section": ("changelog", {"CHANGELOG.md": "## [Unreleased]\n"}),
            "empty file": ("file", {"VERSION": "\n"}),
        }
        for label, (source, files) in cases.items():
            with self.subTest(label), self.assertRaises(ease.EaseError):
                self.resolve(source, files)


class RenderTest(unittest.TestCase):
    """Templates fill `{placeholders}` and match their own renderings."""

    def test_fills_placeholders(self) -> None:
        """A known placeholder takes its value."""
        self.assertEqual(ease.render("v{version}", version="1.0"), "v1.0")

    def test_rejects_unknown_and_missing_placeholders(self) -> None:
        """A typo or a missing version is an error."""
        cases = {
            "unknown": ("{nope}", {"version": "1.0"}),
            "missing version": ("Release {version}.", {"version": None}),
        }
        for label, (template, values) in cases.items():
            with self.subTest(label), self.assertRaises(ease.EaseError):
                ease.render(template, **values)

    def test_template_regex_matches_any_rendering(self) -> None:
        """Literal parts are escaped, placeholders match anything."""
        pattern = ease.template_regex("Release [{version}].")
        self.assertTrue(pattern.match("Release [1.0]."))
        self.assertFalse(pattern.match("Release 1.0."))


# ── Commands, against real repositories ──────────────────────────────────────


class RepoTestCase(unittest.TestCase):
    """A project repository with an `origin` and a `public` bare remote, before `ease init`."""

    version = "0.1.0"

    def setUp(self) -> None:
        """Build the fixture."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        gitconfig = self.base / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n"
            "[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n"
        )
        environment = {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.remotes = {name: self.base / f"{name}.git" for name in ("origin", "public")}
        for path in self.remotes.values():
            self.git_in(self.base, "init", "--quiet", "--bare", str(path))
        self.repo = self.base / "project"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("remote", "add", "origin", str(self.remotes["origin"]))
        self.write("pyproject.toml", f'[project]\nname = "demo"\nversion = "{self.version}"\n')
        self.write("README.md", "Demo.\n")
        self.write("LICENSE", "License.\n")
        self.write("TODO.md", "Private notes.\n")
        self.write("docs/TODO.md", "Public docs.\n")
        self.commit("Initial commit.")
        self.git("push", "--quiet", "origin", "main")

    def git_in(self, directory: Path, *args: str) -> str:
        """Run git in a directory and return its stdout."""
        result = subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def git(self, *args: str) -> str:
        """Run git in the project repository."""
        return self.git_in(self.repo, *args)

    def write(self, path: str, content: str) -> None:
        """Write a file in the project's working tree."""
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def commit(self, message: str) -> None:
        """Commit every change in the working tree."""
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)

    def ease(self, *args: str) -> tuple[int, str, str]:
        """Run ease on the project, capturing its exit code and output."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ease.main(["-C", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def ease_ok(self, *args: str) -> str:
        """Run ease on the project and require success."""
        code, out, err = self.ease(*args)
        self.assertEqual(code, 0, f"ease {' '.join(args)} failed:\n{out}\n{err}")
        return out

    def initialized(self, entry: str = "- First feature.") -> None:
        """Run `ease init` with a public remote, then give the release some content."""
        self.ease_ok("init", "--remote", f"public={self.remotes['public']}")
        with (self.repo / ".mergeignore").open("a") as file:
            file.write("TODO.md\n")
        self.write("CHANGELOG.md", ease.CHANGELOG_TEMPLATE + f"\n{entry}\n")
        self.commit("Configure the release.")

    def prepared(self) -> None:
        """Initialize, and give the version a changelog section ready to release."""
        self.initialized()
        section = f"## [{self.version}] - 2026-01-01\n\n### Added\n"
        changelog = (self.repo / "CHANGELOG.md").read_text()
        self.write(
            "CHANGELOG.md", changelog.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{section}")
        )
        self.commit("Prepare the release.")

    def configure(self, old: str, new: str) -> None:
        """Replace text in the committed .ease.toml."""
        config = (self.repo / ease.CONFIG_FILE).read_text()
        self.assertIn(old, config)
        self.write(ease.CONFIG_FILE, config.replace(old, new))
        self.commit("Configure ease.")

    def remote_rev(self, remote: str, ref: str) -> str:
        """Resolve a ref in one of the bare remotes."""
        return self.git_in(self.remotes[remote], "rev-parse", ref)


class InitTest(RepoTestCase):
    """`ease init` turns a single-branch repository into a dev/main setup."""

    def test_sets_up_the_dev_branch(self) -> None:
        """The dev branch is created, checked out, and carries the three setup files."""
        self.ease_ok("init", "--remote", f"public={self.remotes['public']}")
        self.assertEqual(self.git("branch", "--show-current"), "dev")
        committed = self.git("ls-tree", "--name-only", "dev").splitlines()
        for path in (ease.CONFIG_FILE, ".mergeignore", "CHANGELOG.md"):
            self.assertIn(path, committed)
        config = ease.config_from_toml((self.repo / ease.CONFIG_FILE).read_text())
        self.assertEqual(config.release_remotes, ("origin", "public"))
        self.assertEqual(config.version_source, "pyproject")
        self.assertEqual(config.required, ("LICENSE", "README.md"))
        self.assertEqual(self.git("remote", "get-url", "public"), str(self.remotes["public"]))

    def test_staging_remote_leaves_the_release_remotes(self) -> None:
        """A remote named with --staging-remote stages releases instead of publishing them."""
        self.ease_ok(
            "init", "--remote", f"public={self.remotes['public']}", "--staging-remote", "origin"
        )
        config = ease.config_from_toml((self.repo / ease.CONFIG_FILE).read_text())
        self.assertEqual(config.staging_remotes, ("origin",))
        self.assertEqual(config.release_remotes, ("public",))

    def test_refuses_an_unknown_staging_remote(self) -> None:
        """A staging remote must be a remote."""
        code, _, err = self.ease("init", "--staging-remote", "nowhere")
        self.assertEqual(code, 1)
        self.assertIn("nowhere", err)

    def test_dry_run_changes_nothing(self) -> None:
        """No branch, no files."""
        self.ease_ok("init", "--dry-run")
        self.assertEqual(self.git("branch", "--list"), "* main")

    def test_refuses_an_existing_dev_branch(self) -> None:
        """Init never overwrites an existing setup."""
        self.git("branch", "dev")
        code, _, err = self.ease("init")
        self.assertEqual(code, 1)
        self.assertIn("--existing", err)

    def test_keeps_an_existing_mergeignore(self) -> None:
        """A .mergeignore already on the release branch survives init untouched."""
        mergeignore = "TODO.md\n"
        self.write(".mergeignore", mergeignore)
        self.commit("Add a mergeignore.")
        self.ease_ok("init")
        self.assertEqual(self.git("show", "dev:.mergeignore") + "\n", mergeignore)


class ExistingInitTest(RepoTestCase):
    """`ease init --existing` adopts a dev/main setup made by some other script."""

    def setUp(self) -> None:
        """Mimic publish.sh: dev with a .mergeignore, main on public only, no versions."""
        super().setUp()
        self.mergeignore = "TODO.md\n**/.gitignore\n"
        self.git("remote", "add", "public", str(self.remotes["public"]))
        self.git("push", "--quiet", "public", "main")
        self.git("update-ref", "-d", "refs/remotes/origin/main")  # main never went to origin
        self.git("checkout", "--quiet", "-b", "dev")
        self.git("rm", "--quiet", "pyproject.toml")
        self.write(".mergeignore", self.mergeignore)
        self.commit("Set up publishing.")

    def test_adopts_the_existing_setup(self) -> None:
        """Existing files stay, and the config follows what the repository already does."""
        self.ease_ok("init", "--existing")
        self.assertEqual(self.git("show", "dev:.mergeignore") + "\n", self.mergeignore)
        self.assertFalse((self.repo / "CHANGELOG.md").exists())
        config = ease.config_from_toml(self.git("show", f"dev:{ease.CONFIG_FILE}"))
        self.assertEqual(config.release_remotes, ("public",))
        self.assertEqual(config.changelog, "")
        self.assertEqual(config.version_source, "none")
        self.assertEqual(config.tag, "")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_adopted_setup_releases_with_a_message(self) -> None:
        """After adoption, a versionless release works as publish.sh did."""
        self.ease_ok("init", "--existing")
        message = "Publish a snapshot."
        self.ease_ok("release", "-y", "-m", message)
        self.assertEqual(self.git("log", "-1", "--format=%s", "main"), message)
        self.assertNotIn("TODO.md", self.git("ls-tree", "--name-only", "main").splitlines())

    def test_refuses_to_run_off_the_dev_branch(self) -> None:
        """The setup commit belongs on dev."""
        self.git("checkout", "--quiet", "main")
        code, _, err = self.ease("init", "--existing")
        self.assertEqual(code, 1)
        self.assertIn("check it out", err)


class ReleaseTest(RepoTestCase):
    """`ease release` snapshots dev onto main, with release.sh's safety refusals."""

    def setUp(self) -> None:
        """Build the fixture."""
        super().setUp()
        self.prepared()
        self.tag = f"v{self.version}"

    def test_commits_a_snapshot_without_the_ignored_files(self) -> None:
        """The release branch gets dev's tree minus the ignored paths, and none of dev's history."""
        main_before = self.git("rev-parse", "main")
        self.ease_ok("release", "-y")
        files = self.git("ls-tree", "-r", "--name-only", "main").splitlines()
        for dropped in ("TODO.md", ".mergeignore", ease.CONFIG_FILE):
            self.assertNotIn(dropped, files)
        # A top-level glob stops at a slash, so a same-named file deeper down survives.
        self.assertIn("docs/TODO.md", files)
        self.assertEqual(self.git("rev-parse", "main^"), main_before)
        self.assertFalse(self.git_ok("merge-base", "--is-ancestor", "dev", "main"))

    def test_tags_with_the_changelog_notes_intact(self) -> None:
        """The markdown headings survive in both the tag and the commit message."""
        self.ease_ok("release", "-y")
        message = self.git("tag", "-l", "--format=%(contents)", self.tag)
        self.assertEqual(message, f"Release {self.version}.\n\n### Added\n\n- First feature.")
        self.assertEqual(self.git("log", "-1", "--format=%B", "main").strip(), message)

    def test_dry_run_writes_nothing(self) -> None:
        """No commit, no tag."""
        main_before = self.git("rev-parse", "main")
        self.ease_ok("release", "--dry-run", "--offline")
        self.assertEqual(self.git("rev-parse", "main"), main_before)
        self.assertEqual(self.git("tag", "-l"), "")

    def test_refuses_to_release_a_version_twice(self) -> None:
        """An existing tag at the tip points at --amend."""
        self.ease_ok("release", "-y")
        self.write("README.md", "Changed.\n")
        self.commit("Change without a version bump.")
        code, _, err = self.ease("release", "-y")
        self.assertEqual(code, 1)
        self.assertIn("--amend", err)

    def test_amend_replaces_an_unpushed_release(self) -> None:
        """The replacement hangs off the same parent and takes the tag."""
        self.ease_ok("release", "-y")
        replaced = self.git("rev-parse", "main")
        self.write("README.md", "Fixed before pushing.\n")
        self.commit("Fix the readme.")
        self.ease_ok("release", "--amend", "-y")
        self.assertNotEqual(self.git("rev-parse", "main"), replaced)
        self.assertEqual(self.git("rev-parse", "main^"), self.git("rev-parse", f"{replaced}^"))
        self.assertEqual(
            self.git("rev-parse", f"{self.tag}^{{commit}}"), self.git("rev-parse", "main")
        )

    def test_amend_refuses_a_pushed_release(self) -> None:
        """Amending a pushed release would rewrite published history."""
        self.ease_ok("release", "-y", "--push")
        code, _, err = self.ease("release", "--amend", "-y")
        self.assertEqual(code, 1)
        self.assertIn("already on", err)

    def test_refuses_when_the_release_branch_is_behind_a_remote(self) -> None:
        """A stale main would orphan the releases pushed since."""
        self.ease_ok("release", "-y", "--push")
        self.git("update-ref", "refs/heads/main", "main^")
        self.git("tag", "-d", self.tag)
        code, _, err = self.ease("release", "-y")
        self.assertEqual(code, 1)
        self.assertIn("behind", err)

    def upstream_commit(self, message: str) -> None:
        """Push a commit onto origin's dev from another clone, as a second machine would."""
        other = self.base / "other"
        self.git_in(
            self.base,
            "clone",
            "--quiet",
            "--branch",
            "dev",
            str(self.remotes["origin"]),
            str(other),
        )
        (other / "UPSTREAM.md").write_text(f"{message}\n")
        self.git_in(other, "add", "--all")
        self.git_in(other, "commit", "--quiet", "-m", message)
        self.git_in(other, "push", "--quiet", "origin", "dev")

    def test_refuses_when_dev_is_behind_its_remote(self) -> None:
        """A release from a stale dev would ship without the commits only the remote has."""
        self.git("push", "--quiet", "origin", "dev")
        self.upstream_commit("Fixed on another machine.")
        main_before = self.git("rev-parse", "main")
        code, _, err = self.ease("release", "-y")
        self.assertEqual(code, 1)
        self.assertIn("'dev' is behind 'origin/dev'", err)
        self.assertEqual(self.git("rev-parse", "main"), main_before)

    def test_refuses_when_dev_has_diverged_from_its_remote(self) -> None:
        """Each side holds commits the other lacks: the release would drop the remote's."""
        self.git("push", "--quiet", "origin", "dev")
        self.upstream_commit("Fixed on another machine.")
        self.write("README.md", "Changed locally.\n")
        self.commit("Change locally.")
        code, _, err = self.ease("release", "-y")
        self.assertEqual(code, 1)
        self.assertIn("'dev' has diverged from 'origin/dev'", err)

    def test_releases_when_dev_is_ahead_of_its_remote(self) -> None:
        """Unpushed work on dev is the normal case, not a hazard."""
        self.git("push", "--quiet", "origin", "dev")
        self.write("README.md", "Changed locally.\n")
        self.commit("Change locally.")
        self.ease_ok("release", "-y")

    def test_force_releases_from_a_dev_behind_its_remote(self) -> None:
        """--force downgrades the refusal to a warning."""
        self.git("push", "--quiet", "origin", "dev")
        self.upstream_commit("Fixed on another machine.")
        code, _, err = self.ease("release", "-y", "--force")
        self.assertEqual(code, 0, err)
        self.assertIn("'dev' is behind 'origin/dev'", err)

    def test_refuses_to_drop_a_required_path(self) -> None:
        """An over-broad .mergeignore cannot ship a broken release."""
        with (self.repo / ".mergeignore").open("a") as file:
            file.write("LICENSE\n")
        self.commit("Ignore too much.")
        code, _, err = self.ease("release", "-y", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("LICENSE", err)

    def test_push_publishes_to_every_release_remote(self) -> None:
        """The release branch and its tag reach both remotes; dev reaches origin."""
        self.ease_ok("release", "-y", "--push")
        main = self.git("rev-parse", "main")
        for remote in self.remotes:
            with self.subTest(remote):
                self.assertEqual(self.remote_rev(remote, "main"), main)
                self.assertEqual(self.remote_rev(remote, f"{self.tag}^{{commit}}"), main)
        self.assertEqual(self.remote_rev("origin", "dev"), self.git("rev-parse", "dev"))

    def test_hooks_run_with_the_release_environment(self) -> None:
        """pre-release sees the version, post-release the tag and commit."""
        record = self.base / "hooks.log"
        config = (
            (self.repo / ease.CONFIG_FILE)
            .read_text()
            .replace(
                "[hooks]\n",
                f'[hooks]\npre-release = "echo pre $EASE_VERSION >> {record}"\n'
                f'post-release = "echo post $EASE_TAG $EASE_COMMIT >> {record}"\n',
            )
        )
        self.write(ease.CONFIG_FILE, config)
        self.commit("Add hooks.")
        self.ease_ok("release", "-y", "--offline")
        main = self.git("rev-parse", "main")
        self.assertEqual(
            record.read_text().splitlines(), [f"pre {self.version}", f"post {self.tag} {main}"]
        )

    def test_a_failing_pre_release_hook_aborts(self) -> None:
        """A failing gate leaves main untouched."""
        self.write(ease.CONFIG_FILE, (self.repo / ease.CONFIG_FILE).read_text().replace(
            "[hooks]\n", '[hooks]\npre-release = "exit 3"\n'
        ))  # fmt: skip
        self.commit("Add a failing gate.")
        main_before = self.git("rev-parse", "main")
        code, _, _ = self.ease("release", "-y", "--offline")
        self.assertEqual(code, 1)
        self.assertEqual(self.git("rev-parse", "main"), main_before)

    def git_ok(self, *args: str) -> bool:
        """Report whether a git command succeeds in the project."""
        return subprocess.run(["git", *args], cwd=self.repo, check=False).returncode == 0


class ArtifactsTest(RepoTestCase):
    """`artifacts` adds the pre-release hook's build outputs to the release."""

    artifact = "docs/guide.txt"

    def setUp(self) -> None:
        """Build the fixture."""
        super().setUp()
        self.prepared()

    def set_artifacts(self, hook: str, required: str = "") -> None:
        """Set the artifacts directory, the hook that fills it, and any required paths."""
        config = (self.repo / ease.CONFIG_FILE).read_text()
        config = config.replace('# artifacts = "build/release"', 'artifacts = "build/release"')
        config = config.replace("[hooks]\n", f"[hooks]\npre-release = {hook!r}\n")
        if required:
            config, count = re.subn(
                r"^required = \[", f'required = ["{required}", ', config, flags=re.M
            )
            self.assertEqual(count, 1, "no required list in the config")
        self.write(ease.CONFIG_FILE, config)
        self.write(".gitignore", "build/\n")
        self.commit("Build artifacts.")

    def build(self, path: str = artifact) -> str:
        """Return a hook command that writes one artifact."""
        target = f"build/release/{path}"
        return f"rm -rf build/release && mkdir -p {Path(target).parent} && echo built > {target}"

    def test_adds_the_artifacts_at_their_paths(self) -> None:
        """An artifact joins the release under its path, and can be required."""
        self.set_artifacts(self.build(), required=self.artifact)
        self.ease_ok("release", "-y", "--offline")
        self.assertEqual(self.git("show", f"main:{self.artifact}"), "built")
        self.assertFalse(self.git_ok_quiet("cat-file", "-e", f"dev:{self.artifact}"))

    def test_refuses_unusable_artifacts(self) -> None:
        """Missing, empty, stale, or overlapping artifacts never reach a release."""
        stale = self.repo / "build/release/old.txt"
        cases = {
            "no directory": ("true", "no artifacts directory"),
            "empty directory": ("mkdir -p build/release", "is empty"),
            "stale file": ("touch build/release", "predates"),
            "overlapping file": (self.build("README.md"), "would replace"),
        }
        base = self.git("rev-parse", "HEAD")
        for label, (hook, message) in cases.items():
            with self.subTest(label):
                self.git("reset", "--quiet", "--hard", base)
                subprocess.run(["rm", "-rf", str(self.repo / "build")], check=True)
                if label == "stale file":
                    stale.parent.mkdir(parents=True)
                    stale.write_text("old\n")
                    os.utime(stale, (0, 0))
                self.set_artifacts(hook)
                main_before = self.git("rev-parse", "main")
                code, _, err = self.ease("release", "-y", "--offline")
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)
                self.assertEqual(self.git("rev-parse", "main"), main_before)

    def test_status_expects_required_artifacts_from_the_hook(self) -> None:
        """Without a release, status cannot see the artifacts: a warning, not an error."""
        self.set_artifacts(self.build(), required=self.artifact)
        code, out, _ = self.ease("status", "--offline")
        self.assertEqual(code, 0, out)
        self.assertIn(f"must build them into 'build/release': {self.artifact}", out)

    def test_refuses_to_skip_the_hook_that_builds_the_artifacts(self) -> None:
        """Without the hook, the artifacts may not match the source."""
        self.set_artifacts(self.build())
        code, _, err = self.ease("release", "-y", "--offline", "--skip-hooks")
        self.assertEqual(code, 1)
        self.assertIn("only the pre-release hook builds", err)

    def git_ok_quiet(self, *args: str) -> bool:
        """Report whether a git command succeeds in the project, without output."""
        result = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=False)
        return result.returncode == 0


class EditMessageTest(RepoTestCase):
    """The release message can be written in git's editor, as with `git commit`."""

    notes = "### Added\n\n- First feature."

    def setUp(self) -> None:
        """Prepare a release with changelog notes."""
        super().setUp()
        self.prepared()

    def use_editor(self, script: str) -> None:
        """Make git's editor a shell script that receives the message file as $1."""
        editor = self.base / "editor"
        editor.write_text(f"#!/bin/sh\n{script}\n")
        editor.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"GIT_EDITOR": str(editor)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def released_message(self) -> str:
        """Return the message of the release commit, requiring the tag to match it."""
        message = self.git("log", "-1", "--format=%B", "main").strip()
        self.assertEqual(self.git("tag", "-l", "--format=%(contents)", f"v{self.version}"), message)
        return message

    def test_empty_subject_opens_the_editor(self) -> None:
        """The subject typed in the editor heads the notes; the instructions are dropped."""
        subject = "Edited subject."
        self.configure('subject = "Release {version}."', 'subject = ""')
        self.use_editor(f"sed -i '1s/^/{subject}/' \"$1\"")
        self.ease_ok("release", "-y", "--offline")
        self.assertEqual(self.released_message(), f"{subject}\n\n{self.notes}")

    def test_edit_flag_opens_the_editor_on_the_composed_message(self) -> None:
        """--edit starts from the usual subject and notes, markdown headings included."""
        suffix = " Edited."
        self.use_editor(f"sed -i '1s/$/{suffix}/' \"$1\"")
        self.ease_ok("release", "-y", "--offline", "--edit")
        expected = f"Release {self.version}.{suffix}\n\n{self.notes}"
        self.assertEqual(self.released_message(), expected)

    def test_message_option_skips_the_editor(self) -> None:
        """-m is the message, even when the subject is empty."""
        message = "Given on the command line."
        self.configure('subject = "Release {version}."', 'subject = ""')
        self.use_editor("exit 1")
        self.ease_ok("release", "-y", "--offline", "-m", message)
        self.assertEqual(self.released_message(), message)

    def test_an_empty_message_aborts(self) -> None:
        """Clearing the message, or a failing editor, writes nothing."""
        main_before = self.git("rev-parse", "main")
        for label, script in {"cleared": ': > "$1"', "editor fails": "exit 1"}.items():
            with self.subTest(label):
                self.use_editor(script)
                code, _, _ = self.ease("release", "-y", "--offline", "--edit")
                self.assertEqual(code, 1)
                self.assertEqual(self.git("rev-parse", "main"), main_before)
                self.assertEqual(self.git("tag", "-l"), "")


class ReleaseChangelogTest(RepoTestCase):
    """The changelog promotion, standalone and as part of a release."""

    def test_release_changelog_promotes_and_commits_on_dev(self) -> None:
        """The promoted changelog lands on dev, clean, and in the release."""
        entry = "- First feature."
        self.initialized(entry)
        self.ease_ok("release", "--changelog", "-y", "--offline")
        today = datetime.date.today().isoformat()
        changelog = (self.repo / "CHANGELOG.md").read_text()
        self.assertIn(f"## [{self.version}] - {today}", changelog)
        self.assertEqual(ease.extract_notes(changelog, self.version), entry)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("show", "main:CHANGELOG.md"), changelog.rstrip("\n"))

    def test_changelog_command_edits_only_the_working_tree(self) -> None:
        """`ease changelog` leaves committing to the user."""
        self.initialized()
        head = self.git("rev-parse", "HEAD")
        date = "2026-05-05"
        self.ease_ok("changelog", "--date", date)
        self.assertIn(f"## [{self.version}] - {date}", (self.repo / "CHANGELOG.md").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)


class StagingTest(RepoTestCase):
    """With staging-remotes, `push` stages a release and `publish` sends it on."""

    def setUp(self) -> None:
        """Stage on origin, publish to public, and record the publish hooks."""
        super().setUp()
        self.prepared()
        self.tag = f"v{self.version}"
        self.record = self.base / "hooks.log"
        self.configure(
            'release-remotes = ["origin", "public"]',
            'release-remotes = ["public"]\nstaging-remotes = ["origin"]',
        )
        self.configure(
            "[hooks]\n",
            f'[hooks]\npre-publish = "echo pre $EASE_TAG $EASE_COMMIT >> {self.record}"\n'
            f'post-publish = "echo post $EASE_TAG >> {self.record}"\n',
        )

    def on_remote(self, remote: str, ref: str) -> bool:
        """Report whether a ref exists in one of the bare remotes."""
        return self.git_in_ok(self.remotes[remote], "rev-parse", "--verify", "--quiet", ref)

    def git_in_ok(self, directory: Path, *args: str) -> bool:
        """Report whether a git command succeeds in a directory."""
        return subprocess.run(["git", *args], cwd=directory, capture_output=True).returncode == 0

    def test_push_stages_without_publishing(self) -> None:
        """The release reaches the staging remote only."""
        out = self.ease_ok("release", "-y", "--push")
        main = self.git("rev-parse", "main")
        self.assertEqual(self.remote_rev("origin", f"{self.tag}^{{commit}}"), main)
        self.assertFalse(self.on_remote("public", "main"))
        self.assertFalse(self.on_remote("public", self.tag))
        self.assertIn("ease publish", out)

    def test_publish_sends_the_staged_release_on(self) -> None:
        """Branch and tag reach the release remote, between the two publish hooks."""
        self.ease_ok("release", "-y", "--push")
        self.ease_ok("publish", "-y")
        main = self.git("rev-parse", "main")
        self.assertEqual(self.remote_rev("public", "main"), main)
        self.assertEqual(self.remote_rev("public", f"{self.tag}^{{commit}}"), main)
        self.assertEqual(
            self.record.read_text().splitlines(), [f"pre {self.tag} {main}", f"post {self.tag}"]
        )
        self.assertIn("already published", self.ease_ok("publish", "-y"))

    def test_release_publish_stages_then_publishes(self) -> None:
        """One command does both phases."""
        self.ease_ok("release", "-y", "--publish")
        main = self.git("rev-parse", "main")
        for remote in self.remotes:
            with self.subTest(remote):
                self.assertEqual(self.remote_rev(remote, "main"), main)

    def test_failing_pre_publish_hook_publishes_nothing(self) -> None:
        """The hook vets the staged release, and can veto its publication."""
        self.configure("pre-publish = ", 'pre-publish = "exit 3" # ')
        self.ease_ok("release", "-y", "--push")
        code, _, err = self.ease("publish", "-y")
        self.assertEqual(code, 1)
        self.assertIn("pre-publish", err)
        self.assertFalse(self.on_remote("public", "main"))

    def test_dry_run_runs_the_hook_and_publishes_nothing(self) -> None:
        """A dry run previews the verdict without pushing."""
        self.ease_ok("release", "-y", "--push")
        self.ease_ok("publish", "--dry-run")
        self.assertIn("pre ", self.record.read_text())
        self.assertFalse(self.on_remote("public", "main"))

    def test_publish_refuses_an_unstaged_release(self) -> None:
        """Whatever vets the staged release has not seen this one."""
        self.ease_ok("release", "-y")
        code, _, err = self.ease("publish", "-y")
        self.assertEqual(code, 1)
        self.assertIn("not staged on 'origin'", err)
        self.assertFalse(self.on_remote("public", "main"))

    def test_amend_replaces_a_staged_release_without_force(self) -> None:
        """Staging is private: a staged release can be rebuilt, twice over, and restaged."""
        self.ease_ok("release", "-y", "--push")
        for fix in ("First fix.", "Second fix."):
            with self.subTest(fix):
                self.write("README.md", f"{fix}\n")
                self.commit(fix)
                self.ease_ok("release", "--amend", "-y")
        self.ease_ok("push")
        main = self.git("rev-parse", "main")
        self.assertEqual(self.remote_rev("origin", "main"), main)
        self.assertEqual(self.remote_rev("origin", f"{self.tag}^{{commit}}"), main)

    def test_amend_refuses_a_published_release(self) -> None:
        """Only the release remotes make a release immutable."""
        self.ease_ok("release", "-y", "--publish")
        code, _, err = self.ease("release", "--amend", "-y")
        self.assertEqual(code, 1)
        self.assertIn("already on 'public'", err)

    def test_status_reports_a_staged_release_and_fix_does_not_publish(self) -> None:
        """--fix never pushes to a release remote: only `ease publish` does."""
        self.ease_ok("release", "-y", "--push")
        self.ease("status", "--fix")
        _, out, _ = self.ease("status")
        self.assertIn("ease publish", out)
        self.assertFalse(self.on_remote("public", "main"))

    def test_publish_needs_staging_remotes(self) -> None:
        """Without staging, `ease push` already publishes."""
        self.configure('staging-remotes = ["origin"]', "")
        code, _, err = self.ease("publish", "-y")
        self.assertEqual(code, 1)
        self.assertIn("staging-remotes", err)


class StatusTest(RepoTestCase):
    """`ease status` reports on a released project, and `--fix` repairs what is safe."""

    def setUp(self) -> None:
        """Build the fixture."""
        super().setUp()
        self.initialized()
        self.ease_ok("release", "--changelog", "-y", "--push")
        self.tag = f"v{self.version}"

    def status(self, *args: str) -> tuple[int, str]:
        """Run `ease status` and return its exit code and report."""
        code, out, _ = self.ease("status", *args)
        return code, out

    def test_reports_a_healthy_setup(self) -> None:
        """Right after a pushed release, everything is ok."""
        code, out = self.status()
        self.assertEqual(code, 0)
        self.assertNotIn("warn", out)
        self.assertIn("nothing to release", out)

    def test_fix_fast_forwards_a_branch_behind_its_remote(self) -> None:
        """A strictly-behind branch catches up."""
        main = self.git("rev-parse", "main")
        self.git("update-ref", "refs/heads/main", "main^")
        _, out = self.status()
        self.assertIn("is behind origin/main", out)
        self.assertEqual(self.status("--fix")[0], 0)
        self.assertEqual(self.git("rev-parse", "main"), main)

    def test_fix_pushes_a_missing_tag(self) -> None:
        """A release tag absent from a remote is pushed there."""
        self.git("push", "--quiet", "public", f":refs/tags/{self.tag}")
        _, out = self.status()
        self.assertIn(f"tag(s) missing on 'public': {self.tag}", out)
        self.status("--fix")
        self.assertEqual(
            self.remote_rev("public", self.tag), self.git("rev-parse", f"refs/tags/{self.tag}")
        )

    def test_diverged_branch_is_an_error_fix_leaves_alone(self) -> None:
        """Divergence needs a human: --fix never rewrites it."""
        diverged = self.git("commit-tree", "main^{tree}", "-p", "main^", "-m", "Stray.")
        self.git("update-ref", "refs/heads/main", diverged)
        code, out = self.status("--fix")
        self.assertEqual(code, 1)
        self.assertIn("diverged", out)
        self.assertEqual(self.git("rev-parse", "main"), diverged)

    def test_reports_pending_changes_needing_a_version_bump(self) -> None:
        """New changes under an already-released version call for a bump."""
        self.write("README.md", "Changed.\n")
        self.commit("Change after the release.")
        _, out = self.status("--offline")
        self.assertIn("1 file(s) to release: README.md", out)
        self.assertIn("bump it", out)

    def test_fix_creates_a_missing_dev_branch(self) -> None:
        """A lost dev branch comes back from its remote."""
        self.git("checkout", "--quiet", "--detach")
        self.git("branch", "-D", "dev")
        self.status("--fix")
        self.assertEqual(self.git("rev-parse", "dev"), self.remote_rev("origin", "dev"))


if __name__ == "__main__":
    unittest.main()
