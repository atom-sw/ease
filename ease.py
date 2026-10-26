#!/usr/bin/env python3
"""Publish release snapshots of a development branch onto a release branch.

Usage:

    ease init --remote public=git@github.com:org/project.git
    ease status [--fix]
    ease changelog 1.2.0
    ease release [--changelog] [--push]
    ease push

A repository managed by ease keeps two lines of history. Work happens on the development branch
(`dev`). Each release is a single commit on the release branch (`main`) whose parent is the
previous release commit and whose tree is the development tree minus the paths listed in
`.mergeignore`. The development history is never merged in, so the release branch carries neither
the development commits nor any historical version of the excluded files. The release branch and
its tags can then go to one or more remotes, such as a private repository and a public one.

Nothing is checked out to build a snapshot: ease assembles it in a temporary index, leaving the
working tree and the current branch untouched. Each project configures ease in a `.ease.toml`
committed on its development branch, including shell-command hooks run around a release.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

PROG = "ease"
CONFIG_FILE = ".ease.toml"

VERSION_SOURCES = ("pyproject", "package.json", "cargo", "file", "changelog", "command", "none")
DEFAULT_VERSION_PATHS = {
    "pyproject": "pyproject.toml",
    "package.json": "package.json",
    "cargo": "Cargo.toml",
    "file": "VERSION",
}
HOOKS = ("pre-release", "post-release", "post-push")

# A link reference definition ('[0.1.0]: https://…'). Keep a Changelog collects them at the foot
# of the file, so they end the newest section, which has no '## ' heading after it.
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:[ \t]")
UNRELEASED_HEADING = "## [unreleased]"

MERGEIGNORE_TEMPLATE = """\
# Paths to leave out of release snapshots, one per line. Each line is a git pathspec with glob
# magic, which behaves like .gitignore: `*` does not cross a `/`, `**` does, and a directory drops
# its whole subtree. A line starting with `:` goes to git verbatim, as pathspec magic.
# ease always drops this file and .ease.toml.
#
# TODO.md
# notes/
# **/*.draft.md
"""

CHANGELOG_TEMPLATE = """\
# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]
"""


class EaseError(Exception):
    """A failure to report to the user as a one-line error, without a traceback."""


def info(message: str) -> None:
    """Print a progress message."""
    print(f"{PROG}: {message}", flush=True)


def warn(message: str) -> None:
    """Print a warning to stderr."""
    print(f"warning: {message}", file=sys.stderr, flush=True)


def confirm(prompt: str) -> bool:
    """Ask a yes/no question, defaulting to no."""
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    """The settings of one project, read from `.ease.toml`."""

    dev_branch: str = "dev"
    release_branch: str = "main"
    dev_remote: str = "origin"
    release_remotes: tuple[str, ...] = ("origin",)
    mergeignore: str = ".mergeignore"
    changelog: str = "CHANGELOG.md"
    tag: str = "v{version}"
    subject: str = "Release {version}."
    changelog_commit: str = "Update the changelog for {version}."
    required: tuple[str, ...] = ()
    artifacts: str = ""
    version_source: str = "changelog"
    version_path: str = ""
    version_command: str = ""
    hooks: Mapping[str, str] = field(default_factory=dict[str, str])


# TOML key: Config attribute. Each value is a string unless listed in LIST_KEYS.
TOP_LEVEL_KEYS = {
    "dev-branch": "dev_branch",
    "release-branch": "release_branch",
    "dev-remote": "dev_remote",
    "release-remotes": "release_remotes",
    "mergeignore": "mergeignore",
    "changelog": "changelog",
    "tag": "tag",
    "subject": "subject",
    "changelog-commit": "changelog_commit",
    "required": "required",
    "artifacts": "artifacts",
}
LIST_KEYS = ("release-remotes", "required")
VERSION_KEYS = {"source": "version_source", "path": "version_path", "command": "version_command"}


def _string(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise EaseError(f"{CONFIG_FILE}: '{key}' must be a string")
    return value


def _string_list(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) for v in cast(list[object], value)
    ):
        raise EaseError(f"{CONFIG_FILE}: '{key}' must be a list of strings")
    return tuple(cast(list[str], value))


def _table(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EaseError(f"{CONFIG_FILE}: '{key}' must be a table")
    return cast(dict[str, object], value)


def config_from_toml(text: str) -> Config:
    """Parse and validate the contents of a `.ease.toml`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EaseError(f"{CONFIG_FILE}: {exc}") from exc

    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in TOP_LEVEL_KEYS:
            parse = _string_list if key in LIST_KEYS else _string
            values[TOP_LEVEL_KEYS[key]] = parse(value, key)
        elif key == "version":
            for sub, sub_value in _table(value, key).items():
                if sub not in VERSION_KEYS:
                    raise EaseError(f"{CONFIG_FILE}: unknown key 'version.{sub}'")
                values[VERSION_KEYS[sub]] = _string(sub_value, f"version.{sub}")
        elif key == "hooks":
            hooks: dict[str, str] = {}
            for sub, sub_value in _table(value, key).items():
                if sub not in HOOKS:
                    raise EaseError(
                        f"{CONFIG_FILE}: unknown hook '{sub}' (known: {', '.join(HOOKS)})"
                    )
                hooks[sub] = _string(sub_value, f"hooks.{sub}")
            values["hooks"] = hooks
        else:
            raise EaseError(f"{CONFIG_FILE}: unknown key '{key}'")

    config = Config(**values)
    if config.version_source not in VERSION_SOURCES:
        raise EaseError(
            f"{CONFIG_FILE}: unknown version.source '{config.version_source}' "
            f"(known: {', '.join(VERSION_SOURCES)})"
        )
    if config.version_source == "command" and not config.version_command:
        raise EaseError(f"{CONFIG_FILE}: version.source = 'command' needs version.command")
    if not config.dev_branch or not config.release_branch:
        raise EaseError(f"{CONFIG_FILE}: the branch names must not be empty")
    if config.dev_branch == config.release_branch:
        raise EaseError(f"{CONFIG_FILE}: dev-branch and release-branch must differ")
    if config.artifacts:
        parts = Path(config.artifacts).parts
        if Path(config.artifacts).is_absolute() or ".." in parts or not parts:
            raise EaseError(f"{CONFIG_FILE}: 'artifacts' must be a directory inside the repository")
        if not config.hooks.get("pre-release"):
            raise EaseError(f"{CONFIG_FILE}: 'artifacts' needs a pre-release hook to build them")
    return config


def config_template(
    *,
    dev_branch: str,
    release_branch: str,
    dev_remote: str,
    release_remotes: Sequence[str],
    version_source: str,
    required: Sequence[str],
    changelog: str = "CHANGELOG.md",
    tag: str = "v{version}",
) -> str:
    """Render a commented `.ease.toml` for `init`."""

    def q(value: str) -> str:
        return json.dumps(value)  # a JSON string is a valid TOML basic string

    def q_list(values: Sequence[str]) -> str:
        return "[" + ", ".join(q(v) for v in values) + "]"

    return f"""\
# ease configuration. ease reads the copy committed on the development branch.

dev-branch = {q(dev_branch)}
release-branch = {q(release_branch)}
# Where the development branch is pushed; "" for nowhere.
dev-remote = {q(dev_remote)}
# Where the release branch and its tags are pushed.
release-remotes = {q_list(release_remotes)}
mergeignore = ".mergeignore"
# Supplies the release notes; "" to pass them with -m or -F instead.
changelog = {q(changelog)}
# Placeholders: {{version}}, and {{date}} in `subject`. An empty tag disables tagging, and an
# empty subject opens the editor for the message.
tag = {q(tag)}
subject = "Release {{version}}."
changelog-commit = "Update the changelog for {{version}}."
# Paths a release snapshot must never drop.
required = {q_list(required)}
# A directory the pre-release hook fills with build outputs to add to each release, laid out
# as in the snapshot: <artifacts>/docs/guide.pdf becomes docs/guide.pdf.
# artifacts = "build/release"

[version]
# One of: {", ".join(VERSION_SOURCES)}.
source = {q(version_source)}
# path = "VERSION"
# command = "uv version --short"

[hooks]
# Shell commands run from the repository root, with EASE_VERSION, EASE_TAG, EASE_COMMIT and
# friends in the environment. A failing pre-release hook aborts the release.
# pre-release = "make check"
# post-release = "echo 'Pushing the tag triggers the package upload.'"
# post-push = ""
"""


# ── Git ──────────────────────────────────────────────────────────────────────


class Git:
    """Run git commands in one repository, always with argument lists and never a shell."""

    def __init__(self, root: Path) -> None:
        """Operate on the repository whose top level is `root`."""
        self.root = root

    def _exec(
        self, args: Sequence[str], *, input: str | None, env: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=input,
            env={**os.environ, **env} if env else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def run(
        self, *args: str, input: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git and return its stdout without the trailing newline, or raise EaseError."""
        result = self._exec(args, input=input, env=env)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise EaseError(f"git {' '.join(args)} failed: {detail}")
        return result.stdout.rstrip("\n")

    def ok(self, *args: str, env: Mapping[str, str] | None = None) -> bool:
        """Run git and report whether it succeeded, discarding its output."""
        return self._exec(args, input=None, env=env).returncode == 0

    def rev(self, ref: str) -> str | None:
        """Return the object a ref names, or None when it does not exist."""
        result = self._exec(["rev-parse", "--verify", "--quiet", ref], input=None, env=None)
        return result.stdout.strip() if result.returncode == 0 else None

    def commit(self, ref: str) -> str | None:
        """Return the commit a ref resolves to, peeling tags, or None."""
        return self.rev(f"{ref}^{{commit}}")

    def show(self, ref: str, path: str) -> str | None:
        """Return a file's contents at a ref, or None when it is not there."""
        result = self._exec(["cat-file", "blob", f"{ref}:{path}"], input=None, env=None)
        return result.stdout if result.returncode == 0 else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Report whether one commit is an ancestor of (or equal to) another."""
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def remotes(self) -> list[str]:
        """List the configured remotes."""
        return self.run("remote").split()

    def checked_out(self) -> dict[str, Path]:
        """Map each branch checked out in some worktree to that worktree's path."""
        branches: dict[str, Path] = {}
        worktree = self.root
        for line in self.run("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                worktree = Path(line.removeprefix("worktree "))
            elif line.startswith("branch refs/heads/"):
                branches[line.removeprefix("branch refs/heads/")] = worktree
        return branches

    def is_clean(self) -> bool:
        """Report whether tracked files have no uncommitted changes (untracked ones are fine)."""
        self.ok("update-index", "-q", "--refresh")
        return self.ok("diff-index", "--quiet", "HEAD", "--")

    def short(self, rev: str) -> str:
        """Abbreviate a commit name."""
        return self.run("rev-parse", "--short", rev)

    def subject(self, rev: str) -> str:
        """Return a commit's subject line."""
        return self.run("log", "-1", "--format=%s", rev)

    def remote_tags(self, remote: str) -> dict[str, str]:
        """Map each tag on a remote to the object it names. Needs the network."""
        tags: dict[str, str] = {}
        for line in self.run("ls-remote", "--tags", remote).splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.endswith("^{}"):
                tags[ref.removeprefix("refs/tags/")] = sha
        return tags


def open_repo(directory: Path) -> Git:
    """Locate the repository containing a directory."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EaseError(f"{directory} is not inside a git repository")
    return Git(Path(result.stdout.strip()))


def load_config(git: Git) -> Config:
    """Read `.ease.toml` from the development branch, where a release reads everything else.

    The working-tree copy only supplies the development branch's name, in case it is not the
    default. Without any config file, every setting takes its default.
    """
    worktree = git.root / CONFIG_FILE
    local = config_from_toml(worktree.read_text()) if worktree.is_file() else Config()
    committed = git.show(f"refs/heads/{local.dev_branch}", CONFIG_FILE)
    return local if committed is None else config_from_toml(committed)


def remote_ref(remote: str, branch: str) -> str:
    """Name the remote-tracking ref of a branch."""
    return f"refs/remotes/{remote}/{branch}"


def relation(git: Git, local: str, remote: str) -> str:
    """Describe a local commit against its remote counterpart."""
    if local == remote:
        return "equal"
    if git.is_ancestor(remote, local):
        return "ahead"
    if git.is_ancestor(local, remote):
        return "behind"
    return "diverged"


def fetch(git: Git, remotes: Sequence[str]) -> None:
    """Refresh the remote-tracking refs, leaving local tags alone."""
    existing = set(git.remotes())
    for remote in dict.fromkeys(remotes):
        if remote in existing:
            git.run("fetch", "--quiet", "--no-tags", remote)


def fast_forward(git: Git, branch: str, target: str) -> None:
    """Move a branch forward to a descendant commit, keeping any checkout of it in sync."""
    worktree = git.checked_out().get(branch)
    if worktree is None:
        git.run("update-ref", f"refs/heads/{branch}", target, git.run("rev-parse", branch))
    elif worktree == git.root and git.is_clean():
        git.run("merge", "--ff-only", "--quiet", target)
    else:
        raise EaseError(f"'{branch}' is checked out in {worktree} with local changes")


# ── Templates, versions, and tags ────────────────────────────────────────────


def render(template: str, **values: str | None) -> str:
    """Fill a template's `{placeholders}`; a None value counts as missing."""
    present = {k: v for k, v in values.items() if v is not None}
    try:
        return template.format_map(present)
    except KeyError as exc:
        name = exc.args[0]
        if name in values:
            raise EaseError(
                f"'{template}' needs a {name}, and there is none: pass --version, "
                "or -m for the whole message"
            ) from exc
        raise EaseError(f"unknown placeholder {{{name}}} in '{template}'") from exc
    except (ValueError, IndexError) as exc:
        raise EaseError(f"malformed template '{template}': {exc}") from exc


def template_regex(template: str) -> re.Pattern[str]:
    """Build a regex matching any rendering of a template."""
    parts = re.split(r"\{[^{}]*\}", template)
    return re.compile("^" + ".+".join(re.escape(p) for p in parts) + "$")


def template_glob(template: str) -> str:
    """Build a git glob matching any rendering of a template."""
    return re.sub(r"\{[^{}]*\}", "*", template)


def release_tags(git: Git, config: Config, *extra: str) -> list[str]:
    """List the tags that look like releases, newest version first."""
    if not config.tag:
        return []
    return git.run("tag", "-l", template_glob(config.tag), "--sort=-v:refname", *extra).splitlines()


def tags_at(git: Git, config: Config, commit: str) -> list[str]:
    """List the release tags pointing at a commit."""
    return release_tags(git, config, "--points-at", commit)


def is_release_commit(git: Git, config: Config, commit: str) -> bool:
    """Report whether a commit looks like one ease produced: a release subject, or a tag."""
    if template_regex(config.subject).match(git.subject(commit)):
        return True
    return bool(tags_at(git, config, commit))


def single_parent(git: Git, commit: str) -> str | None:
    """Return a commit's only parent, or None for a root or a merge commit."""
    parents = git.run("rev-list", "--parents", "-n1", commit).split()[1:]
    return parents[0] if len(parents) == 1 else None


def replaced_release(git: Git, config: Config, old: str, new: str) -> bool:
    """Report whether `new` replaced the release commit `old`, as `release --amend` does."""
    if not is_release_commit(git, config, old):
        return False
    parent = single_parent(git, old)
    return parent is not None and git.is_ancestor(parent, new) and not git.is_ancestor(old, new)


def _manifest_table(data: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    table = data.get(key)
    if not isinstance(table, dict):
        raise EaseError(f"{path} has no [{key}] table")
    return cast(dict[str, Any], table)


def parse_manifest_version(source: str, text: str, path: str) -> str:
    """Read the version out of a package manifest's text."""
    try:
        if source == "package.json":
            version = cast(dict[str, Any], json.loads(text)).get("version")
        else:
            data = tomllib.loads(text)
            table = _manifest_table(data, "project" if source == "pyproject" else "package", path)
            version = table.get("version")
            if version is None and "version" in table.get("dynamic", []):
                raise EaseError(
                    f"{path} declares a dynamic version: set version.source = 'command'"
                )
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise EaseError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(version, str):
        raise EaseError(f"{path} declares no literal version string")
    return version


def latest_changelog_version(text: str) -> str | None:
    """Return the topmost released version in a changelog, skipping Unreleased."""
    for match in re.finditer(r"^## \[([^\]]+)\]", text, re.MULTILINE):
        if match.group(1).lower() != "unreleased":
            return match.group(1)
    return None


def resolve_version(
    config: Config,
    read: Callable[[str], str | None],
    run_command: Callable[[str], str] | None,
) -> str | None:
    """Determine the project version from the configured source.

    `read` fetches a file's text, from a ref or the working tree. `run_command` runs a shell
    command in the working tree; it is None when the working tree is not the tree being read.
    """
    source = config.version_source
    if source == "none":
        return None
    if source == "command":
        if run_command is None:
            raise EaseError(
                "version.command runs in the working tree, which is not at the source ref: "
                "check out the source, or pass --version"
            )
        version = run_command(config.version_command)
    else:
        default = config.changelog if source == "changelog" else DEFAULT_VERSION_PATHS[source]
        path = config.version_path or default
        if not path:
            raise EaseError(f"version.source = '{source}' needs a file to read")
        text = read(path)
        if text is None:
            raise EaseError(f"cannot read the version: '{path}' not found")
        if source == "changelog":
            version = latest_changelog_version(text)
            if version is None:
                raise EaseError(
                    f"{path} has no released '## [X.Y.Z]' section to take a version from"
                )
        elif source == "file":
            version = next((line.strip() for line in text.splitlines() if line.strip()), "")
        else:
            version = parse_manifest_version(source, text, path)
    version = version.strip()
    if not version:
        raise EaseError(f"version.source = '{source}' yields an empty version")
    return version


def run_shell(root: Path) -> Callable[[str], str]:
    """Build a runner that executes a shell command in `root` and returns its stdout."""

    def run(command: str) -> str:
        # The command comes from the project's own committed .ease.toml, not from outside.
        result = subprocess.run(
            command, shell=True, cwd=root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise EaseError(f"'{command}' failed: {result.stderr.strip()}")
        return result.stdout

    return run


def run_hook(name: str, config: Config, root: Path, env: Mapping[str, str]) -> bool:
    """Run a configured hook with the ease environment. Return False when it fails."""
    command = config.hooks.get(name, "")
    if not command:
        return True
    info(f"running {name} hook: {command}")
    sys.stdout.flush()
    # The command comes from the project's own committed .ease.toml, not from outside.
    result = subprocess.run(command, shell=True, cwd=root, env={**os.environ, **env}, check=False)
    return result.returncode == 0


def hook_env(config: Config, **values: str | None) -> dict[str, str]:
    """Build the EASE_* environment for a hook."""
    env = {
        "EASE_DEV_BRANCH": config.dev_branch,
        "EASE_RELEASE_BRANCH": config.release_branch,
        "EASE_REMOTES": " ".join(config.release_remotes),
    }
    for key, value in values.items():
        env[f"EASE_{key.upper()}"] = value or ""
    return env


# ── Changelog ────────────────────────────────────────────────────────────────


def _section_end(lines: Sequence[str], start: int) -> int:
    """Return the index just past the section whose heading is at `start`."""
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## ") or LINK_DEFINITION.match(lines[index]):
            return index
    return len(lines)


def _find_heading(lines: Sequence[str], prefix: str, *, fold: bool = False) -> int | None:
    # A literal prefix rather than a regex: a version's dots and plus signs need no escaping,
    # and the closing bracket keeps '## [0.1.0]' from matching '## [0.1.01]'.
    for index, line in enumerate(lines):
        if (line.lower() if fold else line).startswith(prefix):
            return index
    return None


def _strip_blank(lines: Sequence[str]) -> list[str]:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def extract_notes(text: str, version: str) -> str:
    """Return the body of a changelog's `## [version]` section, or "" when there is none."""
    lines = text.splitlines()
    start = _find_heading(lines, f"## [{version}]")
    if start is None:
        return ""
    return "\n".join(_strip_blank(lines[start + 1 : _section_end(lines, start)]))


def has_section(text: str, version: str) -> bool:
    """Report whether a changelog has a `## [version]` heading."""
    return _find_heading(text.splitlines(), f"## [{version}]") is not None


def promote_changelog(text: str, version: str, date: str, tag: str | None) -> tuple[str, list[str]]:
    """Turn the Unreleased section into a `## [version] - date` one, under a fresh Unreleased.

    When the `[Unreleased]` link definition is a `.../compare/A...B` URL, it moves on to compare
    from the new tag, and the new version gets a link comparing the previous release with it.
    Return the new text and any warnings.
    """
    lines = text.splitlines()
    start = _find_heading(lines, UNRELEASED_HEADING, fold=True)
    if start is None:
        raise EaseError("the changelog has no '## [Unreleased]' section")
    if has_section(text, version):
        raise EaseError(f"the changelog already has a '## [{version}]' section")
    if not _strip_blank(lines[start + 1 : _section_end(lines, start)]):
        raise EaseError("the changelog's Unreleased section is empty: nothing to release")

    lines[start : start + 1] = ["## [Unreleased]", "", f"## [{version}] - {date}"]

    warnings: list[str] = []
    if tag is None:
        warnings.append("no tag, so the changelog's link definitions are left alone")
    else:
        link = next(
            (i for i, line in enumerate(lines) if line.lower().startswith("[unreleased]:")), None
        )
        match = link is not None and re.match(
            r"^\[unreleased\]:\s*(\S*/compare/)(\S+?)\.\.\.\S+\s*$", lines[link], re.IGNORECASE
        )
        if link is None:
            warnings.append("the changelog has no '[Unreleased]:' link definition to update")
        elif not match:
            warnings.append(
                "the '[Unreleased]:' link is not a '.../compare/A...B' URL: "
                "update the links by hand"
            )
        else:
            base, previous = match.group(1), match.group(2)
            lines[link : link + 1] = [
                f"[Unreleased]: {base}{tag}...HEAD",
                f"[{version}]: {base}{previous}...{tag}",
            ]

    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), warnings


def commit_file(git: Git, parent: str, path: str, content: str, message: str) -> str:
    """Create, without moving any ref, a commit changing one file on top of `parent`."""
    with tempfile.TemporaryDirectory(prefix="ease-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git.run("read-tree", parent, env=env)
        entry = git.run("ls-tree", parent, "--", path)
        mode = entry.split()[0] if entry else "100644"
        blob = git.run("hash-object", "-w", "--stdin", input=content)
        git.run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        tree = git.run("write-tree", env=env)
    return git.run("commit-tree", tree, "-p", parent, "-m", message)


# ── Snapshot ─────────────────────────────────────────────────────────────────


def parse_mergeignore(text: str) -> list[str]:
    """Return the patterns of a `.mergeignore`, without blank lines and comments."""
    patterns: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def to_pathspec(pattern: str) -> str:
    """Translate a `.mergeignore` pattern into a git pathspec.

    :(glob) buys .gitignore-like intuition: `*` stops at a `/`, so a top-level `*.md` cannot
    reach into `tests/fixtures/`. A trailing slash is redundant, and a pattern that already
    carries pathspec magic passes through.
    """
    pattern = pattern.rstrip("/")
    return pattern if pattern.startswith(":") else f":(glob){pattern}"


@dataclass(frozen=True)
class Snapshot:
    """A release tree, what building it dropped, and the build outputs it added."""

    tree: str
    dropped: list[str]
    counts: list[tuple[str, int]]
    added: list[str] = field(default_factory=list[str])
    missing: list[str] = field(default_factory=list[str])
    """Required paths absent from a snapshot built with `strict=False`."""


def collect_artifacts(root: Path, directory: str, since: float) -> list[tuple[str, Path]]:
    """List the files under the artifacts directory, as (snapshot path, file) pairs.

    Every file must be newer than `since`, the start of the pre-release hook: an older one is a
    leftover of an earlier build, which may not match the source being released.
    """
    base = root / directory
    if not base.is_dir():
        raise EaseError(f"the pre-release hook left no artifacts directory '{directory}'")
    artifacts: list[tuple[str, Path]] = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise EaseError(f"artifact '{path.relative_to(root)}' is a symbolic link")
        if not path.is_file():
            continue
        # One second of slack for file systems with coarse timestamps.
        if path.stat().st_mtime < since - 1:
            raise EaseError(
                f"artifact '{path.relative_to(root)}' predates the pre-release hook: "
                "make the hook rebuild the whole directory"
            )
        artifacts.append((path.relative_to(base).as_posix(), path))
    if not artifacts:
        raise EaseError(f"the artifacts directory '{directory}' is empty")
    return artifacts


def build_snapshot(
    git: Git,
    source: str,
    config: Config,
    artifacts: Sequence[tuple[str, Path]] = (),
    *,
    strict: bool = True,
) -> Snapshot:
    """Build the tree of `source` minus the `.mergeignore` paths, in a temporary index.

    The `artifacts`, from `collect_artifacts`, join the tree at their paths; none may replace a
    file of the source. A missing required path is an error, unless `strict` is off: `status`
    builds snapshots without the artifacts, which only a release builds.
    """
    text = git.show(source, config.mergeignore)
    if text is None:
        raise EaseError(f"'{config.mergeignore}' not found at {source}: commit it there first")
    patterns = parse_mergeignore(text)

    with tempfile.TemporaryDirectory(prefix="ease-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def ls(*specs: str) -> list[str]:
            return [p for p in git.run("ls-files", "-z", "--", *specs, env=env).split("\0") if p]

        git.run("read-tree", source, env=env)
        counts = [(p, len(ls(to_pathspec(p)))) for p in patterns]
        # Both files describe the release process, so they never belong in a release.
        specs = [f":(literal){config.mergeignore}", f":(literal){CONFIG_FILE}"]
        dropped = ls(*specs, *(to_pathspec(p) for p in patterns))
        if dropped:
            git.run(
                "update-index", "--force-remove", "-z", "--stdin",
                input="".join(f"{p}\0" for p in dropped), env=env,
            )  # fmt: skip
        for path, file in artifacts:
            if ls(f":(literal){path}"):
                raise EaseError(f"artifact '{path}' would replace a file of the snapshot")
            blob = git.run("hash-object", "-w", "--", str(file))
            mode = "100755" if os.access(file, os.X_OK) else "100644"
            git.run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        missing = [
            path
            for path in config.required
            if not git.ok("ls-files", "--error-unmatch", "--", path, env=env)
        ]
        if missing and strict:
            raise EaseError(
                f"the snapshot lacks '{missing[0]}', which a release cannot omit: "
                f"check {config.mergeignore} and the source"
            )
        tree = git.run("write-tree", env=env)
    added = [path for path, _ in artifacts]
    return Snapshot(tree=tree, dropped=dropped, counts=counts, added=added, missing=missing)


# ── init ─────────────────────────────────────────────────────────────────────


def detect_version_source(root: Path) -> str:
    """Guess the version source from the manifests in a working tree."""
    for source in ("pyproject", "package.json", "cargo"):
        path = root / DEFAULT_VERSION_PATHS[source]
        if path.is_file():
            try:
                parse_manifest_version(source, path.read_text(), path.name)
            except EaseError:
                continue
            return source
    if (root / DEFAULT_VERSION_PATHS["file"]).is_file():
        return "file"
    return "changelog"


def cmd_init(args: argparse.Namespace, git: Git) -> int:
    """Set up a development branch, `.ease.toml`, `.mergeignore`, and a changelog.

    With --existing, adopt a repository that already has both branches instead: write only
    the files that are missing, and derive the config from what is already there.
    """
    if git.commit("HEAD") is None:
        raise EaseError("the repository has no commits yet: commit something on it first")
    if not git.is_clean():
        raise EaseError("the working tree has uncommitted changes: commit or stash them first")
    current = git.run("symbolic-ref", "--short", "-q", "HEAD")
    dev = args.dev_branch
    dev_exists = git.rev(f"refs/heads/{dev}") is not None
    if args.existing:
        release = args.release_branch or "main"
        if not dev_exists:
            raise EaseError(f"branch '{dev}' does not exist: run init without --existing")
        if current != dev:
            raise EaseError(f"--existing commits on '{dev}': check it out first")
        if git.rev(f"refs/heads/{release}") is None:
            raise EaseError(f"release branch '{release}' does not exist: pass --release-branch")
    else:
        release = args.release_branch or current
        if dev_exists:
            raise EaseError(
                f"branch '{dev}' already exists: to adopt this setup, run "
                f"`ease init --existing` from '{dev}'"
            )
        if current != release:
            raise EaseError(f"init branches off '{release}': check it out first")
    if dev == release:
        raise EaseError("the development and release branches must differ")
    if (git.root / CONFIG_FILE).exists():
        raise EaseError(f"{CONFIG_FILE} already exists: run `ease status` instead")

    existing = git.remotes()
    new_remotes: dict[str, str] = {}
    for spec in args.remote:
        name, sep, url = cast(str, spec).partition("=")
        if not sep or not name or not url:
            raise EaseError(f"--remote takes NAME=URL, not '{spec}'")
        if name in existing:
            current_url = git.run("remote", "get-url", name)
            if current_url != url:
                raise EaseError(f"remote '{name}' already points at {current_url}")
        else:
            new_remotes[name] = url
    remotes = sorted({*existing, *new_remotes}, key=lambda r: (r != "origin", r))
    # An existing setup shows where its release branch goes; without that evidence, use all.
    tracking = [r for r in remotes if git.rev(remote_ref(r, release))] if args.existing else []

    changelog = "CHANGELOG.md"
    version_source = args.version_source or detect_version_source(git.root)
    tag = "v{version}"
    if args.existing and not (git.root / changelog).exists():
        # A project that has done without a changelog keeps doing so: notes come from -m or -F.
        changelog = ""
        if version_source == "changelog":
            version_source = "none"
    if version_source == "none":
        tag = ""

    tracked = git.run("ls-tree", "--name-only", "HEAD").splitlines()
    required = [p for p in tracked if re.match(r"(LICEN[CS]E|COPYING|README)", p, re.IGNORECASE)]
    candidates = {
        CONFIG_FILE: config_template(
            dev_branch=dev,
            release_branch=release,
            dev_remote="origin" if "origin" in remotes else "",
            release_remotes=tracking or remotes,
            version_source=version_source,
            required=required,
            changelog=changelog,
            tag=tag,
        ),
        ".mergeignore": MERGEIGNORE_TEMPLATE,
    }
    if changelog:
        candidates[changelog] = CHANGELOG_TEMPLATE
    # Never overwrite: an existing .mergeignore or changelog is exactly what init should adopt.
    files = {p: c for p, c in candidates.items() if not (git.root / p).exists()}

    for name, url in new_remotes.items():
        info(f"add remote {name}: {url}")
    if not args.existing:
        info(f"create branch '{dev}' from '{release}' and check it out")
    for path in candidates:
        info(f"write {path}" if path in files else f"keep the existing {path}")
    if args.dry_run:
        info("dry run: nothing written")
        return 0

    for name, url in new_remotes.items():
        git.run("remote", "add", name, url)
    if not args.existing:
        git.run("checkout", "--quiet", "-b", dev)
    for path, content in files.items():
        (git.root / path).write_text(content)
    git.run("add", "--", *files)
    git.run("commit", "--quiet", "-m", "chore: set up the ease release workflow")
    info(f"committed the setup on '{dev}'. Review {CONFIG_FILE} and .mergeignore, then:")
    info(f"  ease status        # check the setup (push '{dev}' when you are ready)")
    if changelog:
        info("  ease changelog X   # when the Unreleased section is ready")
    info("  ease release" + ("" if changelog and tag else ' -m "..."'))
    return 0


# ── release ──────────────────────────────────────────────────────────────────


def check_lag(
    git: Git, branch: str, tip: str, remotes: Sequence[str], force: bool, stale: str
) -> None:
    """Refuse to release while a local branch trails one of its remotes.

    Both the release branch and the source branch are checked: a stale release branch would
    orphan every release pushed since, and a stale source branch would ship a snapshot missing
    commits already published to its remote. ``stale`` says which, in the refusal. Being ahead
    is fine: it is unpushed work. The check reads remote-tracking refs, so it is as fresh as
    the last fetch.
    """
    for remote in remotes:
        remote_sha = git.rev(remote_ref(remote, branch))
        if remote_sha is None:
            continue
        kind = relation(git, tip, remote_sha)
        if kind == "behind":
            message = (
                f"'{branch}' is behind '{remote}/{branch}': {stale}. "
                "Catch up first: ease status --fix"
            )
        elif kind == "diverged":
            message = (
                f"'{branch}' has diverged from '{remote}/{branch}': each holds commits the "
                "other does not. Reconcile them before releasing"
            )
        else:
            continue
        if not force:
            raise EaseError(message)
        warn(f"{message}: proceeding (--force)")


def published_on(git: Git, config: Config, commit: str) -> str | None:
    """Return the first release remote whose release branch contains a commit."""
    for remote in config.release_remotes:
        remote_sha = git.rev(remote_ref(remote, config.release_branch))
        if remote_sha is not None and git.is_ancestor(commit, remote_sha):
            return remote
    return None


def build_message(
    args: argparse.Namespace,
    git: Git,
    config: Config,
    source: str,
    version: str | None,
    today: str,
) -> str:
    """Compose the commit and tag message: a subject, then the release notes."""
    if args.message:
        return str(args.message).strip() + "\n"
    subject = render(config.subject, version=version, date=today)
    if args.notes_file:
        try:
            body = Path(args.notes_file).read_text().strip("\n")
        except OSError as exc:
            raise EaseError(f"cannot read notes file: {exc}") from exc
        if not body.strip():
            raise EaseError(f"notes file '{args.notes_file}' is empty")
        info(f"notes from {args.notes_file}")
    elif config.changelog:
        text = git.show(source, config.changelog)
        if text is None:
            raise EaseError(f"'{config.changelog}' not found at the source: pass -F or -m")
        if version is None:
            raise EaseError("there is no version to find release notes for: pass --version or -m")
        body = extract_notes(text, version)
        if not body:
            raise EaseError(
                f"{config.changelog} has no '## [{version}]' section: run `ease changelog`, "
                "pass --changelog, or pass -F"
            )
        info(f"notes from {config.changelog}, section [{version}]")
        if f"[{version}]:" not in text:
            warn(f"{config.changelog} has no '[{version}]:' link definition at its foot")
    else:
        body = ""
    return f"{subject}\n\n{body}\n" if body else f"{subject}\n"


# Everything from this line down is dropped from an edited message. A scissors line rather than
# comment stripping, because release notes are markdown and `### Added` looks like a comment.
SCISSORS = "# ------------------------ >8 ------------------------"


def edit_message(git: Git, path: Path, summary: str) -> None:
    """Let the user write the message in path with git's editor, as `git commit` does."""
    draft = path.read_text().rstrip("\n")
    path.write_text(
        f"{draft}\n\n{SCISSORS}\n"
        "# Write the release message above this line: a subject, a blank line, then the notes.\n"
        "# Everything below the line is dropped. Lines above it starting with '#' are kept.\n"
        "# An empty message aborts the release.\n"
        f"#\n# {summary}\n"
    )
    editor = git.run("var", "GIT_EDITOR")
    # Like git, run the editor through the shell: the setting may carry arguments of its own.
    # It comes from the user's own git configuration or environment.
    if subprocess.run(["sh", "-c", f'{editor} "$@"', editor, str(path)], check=False).returncode:
        raise EaseError("the editor failed: nothing written")
    message = path.read_text().split(SCISSORS)[0].strip()
    if not message:
        raise EaseError("empty message: nothing written")
    path.write_text(message + "\n")


def push_commands(
    config: Config, tag: str | None, force_branch: bool, force_tag: bool
) -> list[list[str]]:
    """List the pushes that publish the release branch, then its tag, to every remote."""
    branch = [
        ["push", *(["--force-with-lease"] if force_branch else []), r, config.release_branch]
        for r in config.release_remotes
    ]
    tags = [
        ["push", *(["--force"] if force_tag else []), r, f"refs/tags/{tag}"]
        for r in config.release_remotes
        if tag
    ]
    return branch + tags


def cmd_release(args: argparse.Namespace, git: Git, config: Config) -> int:
    """Commit a snapshot of the development branch onto the release branch, and tag it."""
    branch = config.release_branch
    release_ref = f"refs/heads/{branch}"
    source_ref = args.source or config.dev_branch
    today = datetime.date.today().isoformat()

    if not git.is_clean():
        raise EaseError("the working tree has uncommitted changes: commit or stash them first")
    source = git.commit(source_ref)
    if source is None:
        raise EaseError(f"source ref '{source_ref}' does not exist")
    tip = git.rev(release_ref)
    if tip is None:
        raise EaseError(f"release branch '{branch}' does not exist")
    # The ref moves without a checkout, which would desync any worktree that has it checked out.
    if branch in git.checked_out():
        raise EaseError(f"'{branch}' is checked out: switch to {config.dev_branch} and re-run")
    missing = [r for r in config.release_remotes if r not in git.remotes()]
    for remote in missing:
        warn(f"release remote '{remote}' is not defined: git remote add {remote} <url>")
    if args.push and missing:
        raise EaseError("cannot --push to undefined remotes")

    dev_remotes = [config.dev_remote] if config.dev_remote else []
    if not args.offline and (config.release_remotes or dev_remotes):
        info("fetching the remotes")
        fetch(git, [*config.release_remotes, *dev_remotes])
    check_lag(
        git,
        branch,
        tip,
        config.release_remotes,
        args.force,
        "the release would be built on an outdated history",
    )
    # A source given as a tag or SHA has no remote-tracking counterpart, so it is skipped.
    check_lag(
        git,
        source_ref,
        source,
        dev_remotes,
        args.force,
        "the release would miss commits that are only on the remote",
    )

    head = git.commit("HEAD")
    worktree_at_source = source == head

    # ── Version ──
    if args.changelog and config.version_source == "changelog" and not args.version:
        raise EaseError("--changelog with version.source = 'changelog' needs --version")
    version = args.version or resolve_version(
        config,
        lambda path: git.show(source, path),
        run_shell(git.root) if worktree_at_source else None,
    )
    if version is not None:
        info(f"version at {source_ref}: {version}")
    tag = render(config.tag, version=version) if config.tag and version else None
    if tag is not None and not git.ok("check-ref-format", f"refs/tags/{tag}"):
        raise EaseError(f"'{tag}' is not a valid tag name")
    previous = next(iter(release_tags(git, config)), None)
    if tag is not None:
        info(f"previous release: {previous}" if previous else "no release tags yet: first release")

    # ── What the new commit hangs off, and what happens to the tag ──
    tag_commit = git.commit(f"refs/tags/{tag}") if tag else None
    tag_exists = tag_commit is not None
    parent = tip
    stale_tags: list[str] = []
    pushed_to: str | None = None
    if args.amend:
        if not is_release_commit(git, config, tip):
            raise EaseError(f"--amend: the tip of {branch} is not a release commit")
        amend_parent = single_parent(git, tip)
        if amend_parent is None:
            raise EaseError(f"--amend: the tip of {branch} has no single parent to rebuild from")
        pushed_to = published_on(git, config, tip)
        if pushed_to:
            message = (
                f"the release at the tip of {branch} is already on '{pushed_to}': amending it "
                "rewrites published history"
            )
            if not args.force:
                raise EaseError(f"{message}. Bump the version instead, or use --force")
            warn(f"{message}: proceeding (--force)")
        if tag_exists and tag_commit != tip:
            if not args.force:
                raise EaseError(
                    f"tag {tag} exists, but not on the tip of {branch}: bump the version"
                )
            warn(f"tag {tag} exists elsewhere: moving it (--force)")
        stale_tags = [t for t in tags_at(git, config, tip) if t != tag]
        parent = amend_parent
    elif tag_exists:
        if args.force:
            warn(f"tag {tag} exists: moving it (--force)")
        elif tag_commit == tip:
            raise EaseError(
                f"{tag} is the release at the tip of {branch}: use --amend, or bump the version"
            )
        else:
            raise EaseError(f"tag {tag} already exists: bump the version")

    # ── Changelog promotion, as a commit on the development branch not yet adopted ──
    changelog_commit: str | None = None
    snapshot_source = source
    if args.changelog:
        if not config.changelog:
            raise EaseError("--changelog needs a changelog file in the config")
        if (
            args.source
            or git.run("symbolic-ref", "-q", "HEAD") != f"refs/heads/{config.dev_branch}"
        ):
            raise EaseError(f"--changelog commits on '{config.dev_branch}': check it out first")
        if version is None:
            raise EaseError("--changelog needs a version: pass --version")
        text = git.show(source, config.changelog)
        if text is None:
            raise EaseError(f"'{config.changelog}' not found on {config.dev_branch}")
        if has_section(text, version):
            info(f"{config.changelog} already has a [{version}] section: leaving it as is")
        else:
            new_text, warnings = promote_changelog(text, version, today, tag)
            for warning in warnings:
                warn(warning)
            changelog_commit = commit_file(
                git, source, config.changelog, new_text,
                render(config.changelog_commit, version=version),
            )  # fmt: skip
            snapshot_source = changelog_commit
            info(f"promoted the Unreleased section of {config.changelog} to [{version}]")

    with tempfile.TemporaryDirectory(prefix="ease-") as tmp:
        message_file = Path(tmp) / "message"
        message_file.write_text(build_message(args, git, config, snapshot_source, version, today))
        env = hook_env(
            config,
            version=version,
            tag=tag,
            previous_tag=previous,
            notes_file=str(message_file),
            dry_run="1" if args.dry_run else "0",
        )

        # ── Gate ──
        hook_start: float | None = None
        if "pre-release" in config.hooks:
            if args.skip_hooks:
                warn("skipping the pre-release hook (--skip-hooks)")
            elif not worktree_at_source:
                warn(f"skipping the pre-release hook: the working tree is not at {source_ref}")
            else:
                hook_start = time.time()
                if not run_hook("pre-release", config, git.root, env):
                    raise EaseError("the pre-release hook failed")

        # ── Snapshot and review ──
        artifacts: list[tuple[str, Path]] = []
        if config.artifacts:
            if hook_start is None:
                raise EaseError(
                    "the release adds artifacts, which only the pre-release hook builds: "
                    f"run it without --skip-hooks, with the working tree at {source_ref}"
                )
            artifacts = collect_artifacts(git.root, config.artifacts, hook_start)
        snapshot = build_snapshot(git, snapshot_source, config, artifacts)
        print()
        info(f"=== {config.mergeignore} (at {source_ref}) ===")
        for pattern, count in snapshot.counts:
            print(f"  {pattern:<24} {count} file(s)")
        # Compared with the parent, not the tip: an amend restoring the pre-release tree
        # releases nothing.
        if snapshot.tree == git.run("rev-parse", f"{parent}^{{tree}}"):
            if not args.force:
                raise EaseError(f"the snapshot is identical to {branch}: nothing to release")
            warn(f"the snapshot is identical to {branch}: proceeding (--force)")
        if args.amend and snapshot.tree == git.run("rev-parse", f"{tip}^{{tree}}"):
            warn(
                "the snapshot is identical to the release being replaced: only its message changes"
            )

        # An empty subject asks for a message rather than committing one; -m already is one.
        subject = message_file.read_text().partition("\n")[0].strip()
        if (args.edit or not subject) and not args.message:
            if args.dry_run:
                warn("the editor would open for the message (skipped on --dry-run)")
            else:
                edit_message(git, message_file, f"Release of {source_ref} onto {branch}")

        print()
        if args.amend:
            info("=== replacing ===")
            print(f"  {git.short(tip)} {git.subject(tip)}")
            for stale in stale_tags:
                print(f"  deleting dangling tag: {stale}")
            print()
        if changelog_commit:
            info(f"=== new commit on {config.dev_branch} ===")
            print(f"  {git.subject(changelog_commit)}")
            print()
        info(f"=== dropped from the snapshot ({len(snapshot.dropped)}) ===")
        for path in snapshot.dropped:
            print(f"  {path}")
        print()
        if config.artifacts:
            info(f"=== added from {config.artifacts} ({len(snapshot.added)}) ===")
            for path in snapshot.added:
                print(f"  {path}")
            print()
        info(f"=== {branch} ({git.short(parent)}) to the snapshot ===")
        print(git.run("diff", "--stat", parent, snapshot.tree))
        print()
        info("=== commit and tag message ===" if tag else "=== commit message ===")
        for line in message_file.read_text().splitlines():
            print(f"  {line}".rstrip())
        print()

        if args.dry_run:
            info("dry run: nothing written")
            return 0
        action = f"Replace {git.short(tip)} on {branch}" if args.amend else f"Commit onto {branch}"
        if not args.yes and not confirm(f"{action}{f' and tag {tag}' if tag else ''}?"):
            info("aborted: nothing written")
            return 0

        # ── Commit and tag ──
        if changelog_commit:
            git.run("merge", "--ff-only", "--quiet", changelog_commit)
        commit = git.run("commit-tree", snapshot.tree, "-p", parent, "-F", str(message_file))
        # A compare-and-swap: a concurrent update to the branch aborts rather than being lost.
        git.run("update-ref", release_ref, commit, tip)
        if tag:
            # --cleanup=verbatim: `git tag` defaults to `strip`, which deletes every line that
            # looks like a comment, and the notes come from markdown, where `### Added` is one.
            force = ["-f"] if tag_exists else []
            git.run("tag", "-a", "--cleanup=verbatim", *force, "-F", str(message_file), tag, commit)
        for stale in stale_tags:
            git.run("tag", "-d", stale)
            info(f"deleted {stale}: it pointed only at the replaced commit")
        replaced = f"replaced {git.short(tip)} with" if args.amend else "committed"
        info(f"{replaced} {git.short(commit)} onto {branch}{f', tagged {tag}' if tag else ''}")

        env["EASE_COMMIT"] = commit
        if not args.skip_hooks and not run_hook("post-release", config, git.root, env):
            warn("the post-release hook failed")

    print()
    if not config.release_remotes:
        if args.push:
            warn("release-remotes is empty: there is nowhere to push")
        return 0
    if args.push:
        return push(git, config, dry_run=False, push_dev=True, skip_hooks=args.skip_hooks)
    info("To publish, push the branch first, then the tag (or run `ease push`):")
    for command in push_commands(config, tag, bool(pushed_to), tag_exists):
        info(f"  git {' '.join(command)}")
    for stale in stale_tags:
        for remote in config.release_remotes:
            info(f"  git push {remote} :refs/tags/{stale}   # only if {stale} was ever pushed")
    return 0


# ── push ─────────────────────────────────────────────────────────────────────


def push(git: Git, config: Config, *, dry_run: bool, push_dev: bool, skip_hooks: bool) -> int:
    """Push the release branch, then the tags at its tip, then the development branch."""
    branch = config.release_branch
    remotes = git.remotes()
    missing = [r for r in config.release_remotes if r not in remotes]
    if missing:
        raise EaseError(f"undefined release remote(s): {', '.join(missing)}")
    tip = git.rev(f"refs/heads/{branch}")
    if tip is None:
        raise EaseError(f"release branch '{branch}' does not exist")
    dev_remote = config.dev_remote if push_dev and config.dev_remote in remotes else ""
    fetch(git, [*config.release_remotes, *([dev_remote] if dev_remote else [])])

    tags = tags_at(git, config, tip)
    branch_pushes: list[list[str]] = []
    tag_pushes: list[list[str]] = []
    for remote in config.release_remotes:
        remote_sha = git.rev(remote_ref(remote, branch))
        kind = "ahead" if remote_sha is None else relation(git, tip, remote_sha)
        rewritten = False
        if kind == "ahead":
            branch_pushes.append(["push", remote, f"refs/heads/{branch}:refs/heads/{branch}"])
        elif kind == "behind":
            raise EaseError(f"'{branch}' is behind '{remote}/{branch}': run `ease status --fix`")
        elif kind == "diverged":
            assert remote_sha is not None
            if not replaced_release(git, config, remote_sha, tip):
                raise EaseError(
                    f"'{branch}' has diverged from '{remote}/{branch}': reconcile by hand"
                )
            rewritten = True
            branch_pushes.append(
                ["push", f"--force-with-lease=refs/heads/{branch}:{remote_sha}", remote,
                 f"refs/heads/{branch}:refs/heads/{branch}"]
            )  # fmt: skip
        on_remote = git.remote_tags(remote) if tags else {}
        for tag in tags:
            remote_tag = on_remote.get(tag)
            if remote_tag == git.rev(f"refs/tags/{tag}"):
                continue
            if remote_tag is not None and not rewritten:
                raise EaseError(f"tag {tag} on '{remote}' differs from the local one")
            force = ["--force"] if remote_tag is not None else []
            tag_pushes.append(["push", *force, remote, f"refs/tags/{tag}"])

    dev_pushes: list[list[str]] = []
    if dev_remote:
        dev = config.dev_branch
        dev_sha = git.rev(f"refs/heads/{dev}")
        remote_sha = git.rev(remote_ref(dev_remote, dev))
        if dev_sha is not None:
            kind = "ahead" if remote_sha is None else relation(git, dev_sha, remote_sha)
            if kind == "ahead":
                dev_pushes.append(["push", dev_remote, f"refs/heads/{dev}:refs/heads/{dev}"])
            elif kind != "equal":
                warn(f"'{dev}' is {kind} '{dev_remote}/{dev}': not pushing it")

    commands = branch_pushes + tag_pushes + dev_pushes
    if not commands:
        info("everything is already pushed")
        return 0
    for command in commands:
        info(f"{'would run' if dry_run else 'running'}: git {' '.join(command)}")
        if not dry_run:
            git.run(*command)
    if dry_run:
        return 0
    env = hook_env(config, tag=tags[0] if tags else None, commit=tip)
    if not skip_hooks and not run_hook("post-push", config, git.root, env):
        warn("the post-push hook failed")
    return 0


def cmd_push(args: argparse.Namespace, git: Git, config: Config) -> int:
    """Push the release to every release remote."""
    return push(
        git, config, dry_run=args.dry_run, push_dev=not args.no_dev, skip_hooks=args.skip_hooks
    )


# ── changelog ────────────────────────────────────────────────────────────────


def cmd_changelog(args: argparse.Namespace, git: Git, config: Config) -> int:
    """Promote the changelog's Unreleased section to a release section, in the working tree."""
    if not config.changelog:
        raise EaseError("the config names no changelog")
    path = git.root / config.changelog
    if not path.is_file():
        raise EaseError(f"{config.changelog} not found")
    if config.version_source == "changelog" and not args.version:
        raise EaseError("version.source = 'changelog': pass the new VERSION")

    def read(name: str) -> str | None:
        file = git.root / name
        return file.read_text() if file.is_file() else None

    version = args.version or resolve_version(config, read, run_shell(git.root))
    if version is None:
        raise EaseError("version.source = 'none': pass the new VERSION")
    date = args.date or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(date)
    except ValueError as exc:
        raise EaseError(f"--date takes YYYY-MM-DD, not '{date}'") from exc
    tag = render(config.tag, version=version) if config.tag else None

    text = path.read_text()
    new_text, warnings = promote_changelog(text, version, date, tag)
    for warning in warnings:
        warn(warning)
    if args.dry_run:
        diff = difflib.unified_diff(
            text.splitlines(),
            new_text.splitlines(),
            config.changelog,
            config.changelog,
            lineterm="",
        )
        print("\n".join(diff))
        info("dry run: nothing written")
        return 0
    path.write_text(new_text)
    info(f"promoted Unreleased to [{version}] - {date} in {config.changelog}: review and commit it")
    return 0


# ── status ───────────────────────────────────────────────────────────────────


@dataclass
class Check:
    """One line of the status report, with an optional repair."""

    level: str  # "ok", "warn", or "error"
    text: str
    fix: Callable[[], object] | None = None
    fix_text: str = ""


class Report:
    """Collects the checks of `ease status`."""

    def __init__(self) -> None:
        """Start an empty report."""
        self.checks: list[Check] = []

    def ok(self, text: str) -> None:
        """Record a passing check."""
        self.checks.append(Check("ok", text))

    def warn(self, text: str, fix: Callable[[], object] | None = None, fix_text: str = "") -> None:
        """Record a problem that does not block a release."""
        self.checks.append(Check("warn", text, fix, fix_text))

    def error(self, text: str, fix: Callable[[], object] | None = None, fix_text: str = "") -> None:
        """Record a problem that blocks a release."""
        self.checks.append(Check("error", text, fix, fix_text))


def _write_file(path: Path, content: str) -> Callable[[], object]:
    return lambda: path.write_text(content)


def _branch_checks(
    git: Git, report: Report, branch: str, remotes: Sequence[str], role: str
) -> None:
    """Compare a local branch with its remote-tracking counterparts."""
    local = git.rev(f"refs/heads/{branch}")
    if local is None:
        return
    for remote in remotes:
        remote_sha = git.rev(remote_ref(remote, branch))
        name = f"{remote}/{branch}"
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"

        def push_it(remote: str = remote, refspec: str = refspec) -> None:
            git.run("push", "--quiet", remote, refspec)

        if remote_sha is None:
            report.warn(f"{role} '{branch}' is not on '{remote}'", push_it, f"push it to {remote}")
            continue
        kind = relation(git, local, remote_sha)
        if kind == "equal":
            report.ok(f"{role} '{branch}' matches {name}")
        elif kind == "ahead":
            count = git.run("rev-list", "--count", f"{remote_sha}..{local}")
            report.warn(
                f"{role} '{branch}' is {count} commit(s) ahead of {name}",
                push_it,
                f"push to {remote}",
            )
        elif kind == "behind":

            def catch_up(target: str = remote_sha) -> None:
                fast_forward(git, branch, target)

            report.warn(f"{role} '{branch}' is behind {name}", catch_up, f"fast-forward to {name}")
        else:
            report.error(f"{role} '{branch}' has diverged from {name}: reconcile by hand")


def collect_checks(git: Git, config: Config, config_error: str | None, offline: bool) -> Report:
    """Inspect the repository's ease setup and report on every aspect of it."""
    report = Report()
    dev, branch = config.dev_branch, config.release_branch
    dev_sha = git.rev(f"refs/heads/{dev}")
    release_sha = git.rev(f"refs/heads/{branch}")

    # ── Config ──
    local_path = git.root / CONFIG_FILE
    local_config = local_path.read_text() if local_path.is_file() else None
    committed_config = git.show(f"refs/heads/{dev}", CONFIG_FILE) if dev_sha else None
    if config_error:
        report.error(config_error)
    elif committed_config is None and local_config is None:
        template = config_template(
            dev_branch=dev,
            release_branch=branch,
            dev_remote=config.dev_remote,
            release_remotes=config.release_remotes,
            version_source=detect_version_source(git.root),
            required=[],
        )
        report.warn(
            f"no {CONFIG_FILE}: every setting takes its default",
            _write_file(local_path, template),
            f"write a {CONFIG_FILE} to review and commit on '{dev}'",
        )
    elif committed_config is None:
        report.warn(f"{CONFIG_FILE} is not committed on '{dev}', where ease reads it")
    elif local_config is not None and local_config != committed_config:
        report.warn(f"{CONFIG_FILE} has uncommitted edits: ease reads the copy on '{dev}'")
    else:
        report.ok(f"config: {CONFIG_FILE} on '{dev}'")

    # ── Remotes ──
    remotes = git.remotes()
    wanted = list(dict.fromkeys([*config.release_remotes, *filter(None, [config.dev_remote])]))
    for remote in wanted:
        if remote not in remotes:
            report.error(f"remote '{remote}' is not defined: git remote add {remote} <url>")
    present = [r for r in wanted if r in remotes]
    online = not offline
    if online:
        try:
            fetch(git, present)
        except EaseError as exc:
            report.warn(f"fetch failed, so remote state may be stale: {exc}")
            online = False
    release_remotes = [r for r in config.release_remotes if r in remotes]
    dev_remotes = [config.dev_remote] if config.dev_remote in remotes else []

    # ── Branches ──
    if release_sha is None:
        tracking = next((r for r in release_remotes if git.rev(remote_ref(r, branch))), None)
        if tracking:
            report.error(
                f"release branch '{branch}' does not exist",
                lambda: git.run("branch", branch, f"{tracking}/{branch}"),
                f"create it from {tracking}/{branch}",
            )
        else:
            report.error(f"release branch '{branch}' does not exist")
    if dev_sha is None:
        tracking = next((r for r in dev_remotes if git.rev(remote_ref(r, dev))), None)
        start = f"{tracking}/{dev}" if tracking else (branch if release_sha else None)
        if start:
            report.error(
                f"development branch '{dev}' does not exist",
                lambda: git.run("branch", dev, start),
                f"create it from {start}",
            )
        else:
            report.error(f"development branch '{dev}' does not exist")
    else:
        mergeignore_path = git.root / config.mergeignore
        if git.show(dev_sha, config.mergeignore) is not None:
            report.ok(f"{config.mergeignore} is committed on '{dev}'")
        elif mergeignore_path.exists():
            report.error(f"{config.mergeignore} is not committed on '{dev}': commit it")
        else:
            report.error(
                f"no {config.mergeignore} on '{dev}'",
                _write_file(mergeignore_path, MERGEIGNORE_TEMPLATE),
                f"write a {config.mergeignore} to review and commit",
            )
        if config.changelog:
            changelog_path = git.root / config.changelog
            if git.show(dev_sha, config.changelog) is not None:
                report.ok(f"{config.changelog} is committed on '{dev}'")
            elif changelog_path.exists():
                report.warn(f"{config.changelog} is not committed on '{dev}': commit it")
            else:
                report.warn(
                    f"no {config.changelog} on '{dev}'",
                    _write_file(changelog_path, CHANGELOG_TEMPLATE),
                    f"write a {config.changelog} to review and commit",
                )

    if branch in git.checked_out():
        report.warn(f"'{branch}' is checked out: switch to '{dev}' before releasing")
    if git.commit("HEAD") and not git.is_clean():
        report.warn("the working tree has uncommitted changes, which a release refuses")

    _branch_checks(git, report, branch, release_remotes, "release branch")
    _branch_checks(git, report, dev, dev_remotes, "development branch")

    # ── Release tip and tags ──
    if release_sha is not None:
        tip_tags = tags_at(git, config, release_sha)
        short = git.short(release_sha)
        if tip_tags:
            report.ok(f"'{branch}' tip {short} is tagged {', '.join(tip_tags)}")
        elif not config.tag:
            report.ok(f"'{branch}' tip {short}: {git.subject(release_sha)}")
        elif not is_release_commit(git, config, release_sha):
            report.ok(f"'{branch}' tip {short} is not a release: no release yet")
        elif config.tag:
            report.warn(f"'{branch}' tip {short} looks like a release, but has no tag")
        if online and config.tag:
            local_tags = release_tags(git, config, "--merged", release_sha)
            for remote in release_remotes:
                on_remote = git.remote_tags(remote)
                absent = [t for t in local_tags if t not in on_remote]
                differ = [
                    t
                    for t in local_tags
                    if t in on_remote and on_remote[t] != git.rev(f"refs/tags/{t}")
                ]
                if absent:

                    def push_tags(remote: str = remote, absent: list[str] = absent) -> None:
                        git.run("push", "--quiet", remote, *(f"refs/tags/{t}" for t in absent))

                    report.warn(
                        f"tag(s) missing on '{remote}': {', '.join(absent)}",
                        push_tags,
                        f"push them to {remote}",
                    )
                if differ:
                    report.error(f"tag(s) differ on '{remote}': {', '.join(differ)}")
                if local_tags and not absent and not differ:
                    report.ok(f"every release tag is on '{remote}'")

    # ── Version, changelog, and pending changes ──
    if dev_sha is None:
        return report
    version: str | None = None
    try:
        head = git.commit("HEAD")
        version = resolve_version(
            config,
            lambda path: git.show(dev_sha, path),
            run_shell(git.root) if head == dev_sha else None,
        )
    except EaseError as exc:
        report.error(f"version: {exc}")
    tag = render(config.tag, version=version) if config.tag and version else None
    released = tag is not None and git.rev(f"refs/tags/{tag}") is not None
    if version:
        report.ok(f"version at '{dev}': {version}" + (f" (tag {tag} exists)" if released else ""))
    if config.artifacts:
        report.ok(f"releases add the build outputs in '{config.artifacts}' (pre-release hook)")

    changelog = git.show(dev_sha, config.changelog) if config.changelog else None
    if (
        changelog is not None
        and _find_heading(changelog.splitlines(), UNRELEASED_HEADING, fold=True) is None
    ):
        report.warn(f"{config.changelog} has no '## [Unreleased]' section")

    if release_sha is None or git.show(dev_sha, config.mergeignore) is None:
        return report
    try:
        snapshot = build_snapshot(git, dev_sha, config, strict=not config.artifacts)
    except EaseError as exc:
        report.error(f"snapshot: {exc}")
        return report
    if snapshot.missing:
        report.warn(
            f"required path(s) not in '{dev}', so the pre-release hook must build them into "
            f"'{config.artifacts}': {', '.join(snapshot.missing)}"
        )
    changed = git.run("diff", "--name-only", release_sha, snapshot.tree).splitlines()
    if not changed:
        report.ok(f"'{branch}' matches the snapshot of '{dev}': nothing to release")
        return report
    listing = ", ".join(changed[:5]) + (
        f", and {len(changed) - 5} more" if len(changed) > 5 else ""
    )
    report.ok(f"{len(changed)} file(s) to release: {listing}")
    if released:
        report.warn(f"version {version} is already released as {tag}: bump it before releasing")
    elif version and changelog is not None and not has_section(changelog, version):
        report.warn(
            f"{config.changelog} has no '## [{version}]' section: run `ease changelog`, "
            "or `ease release --changelog`"
        )
    return report


def cmd_status(args: argparse.Namespace, git: Git, config: Config, config_error: str | None) -> int:
    """Report on the setup, and with --fix repair what can be repaired safely."""
    report = collect_checks(git, config, config_error, args.offline)
    unresolved = False
    for check in report.checks:
        suffix = f"  [--fix: {check.fix_text}]" if check.fix and not args.fix else ""
        print(f"  {check.level:<6} {check.text}{suffix}")
    if args.fix:
        fixes = [c for c in report.checks if c.fix]
        if fixes:
            print()
        for check in fixes:
            assert check.fix is not None
            info(f"fixing: {check.fix_text}")
            try:
                check.fix()
            except EaseError as exc:
                warn(f"could not {check.fix_text}: {exc}")
                unresolved = unresolved or check.level == "error"
        if not fixes:
            info("nothing to fix")
    for check in report.checks:
        if check.level == "error" and (check.fix is None or not args.fix):
            unresolved = True
    return 1 if unresolved else 0


# ── Command line ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Publish release snapshots of a development branch onto a release branch.",
    )
    parser.add_argument(
        "-C", dest="directory", default=".", help="run as if started in this directory"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    init = commands.add_parser(
        "init", help="set up the development branch", description=cmd_init.__doc__
    )
    init.add_argument(
        "--remote",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="add a release remote (repeatable)",
    )
    init.add_argument("--dev-branch", default="dev", help="development branch name (default: dev)")
    init.add_argument(
        "--release-branch",
        help="release branch name (default: the current branch, or main with --existing)",
    )
    init.add_argument(
        "--existing",
        action="store_true",
        help="adopt an existing dev/main setup: run from the dev branch, and keep existing files",
    )
    init.add_argument(
        "--version-source", choices=VERSION_SOURCES, help="where the version comes from"
    )
    init.add_argument("-n", "--dry-run", action="store_true", help="report, change nothing")

    status = commands.add_parser("status", help="check the setup", description=cmd_status.__doc__)
    status.add_argument("--fix", action="store_true", help="apply the safe repairs")
    status.add_argument(
        "--offline", action="store_true", help="neither fetch nor query the remotes"
    )

    changelog = commands.add_parser(
        "changelog",
        help="promote the Unreleased changelog section",
        description=cmd_changelog.__doc__,
    )
    changelog.add_argument(
        "version", nargs="?", help="the version to release (default: the configured source)"
    )
    changelog.add_argument("--date", help="release date, YYYY-MM-DD (default: today)")
    changelog.add_argument("-n", "--dry-run", action="store_true", help="print the diff only")

    release = commands.add_parser(
        "release", help="commit and tag a release snapshot", description=cmd_release.__doc__
    )
    release.add_argument("-s", "--source", help="ref to snapshot (default: the dev branch)")
    release.add_argument("--version", help="the version, overriding the configured source")
    notes = release.add_mutually_exclusive_group()
    notes.add_argument("-F", "--notes-file", help="take the release notes from this file")
    notes.add_argument("-m", "--message", help="the whole commit message")
    release.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="edit the message in git's editor (automatic when the subject is empty)",
    )
    release.add_argument(
        "--changelog",
        action="store_true",
        help="promote the Unreleased section first, committing it on the dev branch",
    )
    release.add_argument(
        "--amend", action="store_true", help="replace the unpushed release at the tip"
    )
    release.add_argument("--push", action="store_true", help="push the release afterwards")
    release.add_argument("--offline", action="store_true", help="do not fetch the remotes first")
    release.add_argument("-n", "--dry-run", action="store_true", help="report, write nothing")
    release.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="override the safety refusals: move a tag, release onto a stale branch, "
        "amend a pushed release, allow an empty release",
    )
    release.add_argument("--skip-hooks", action="store_true", help="run no hooks")
    release.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    push_parser = commands.add_parser(
        "push", help="push the release to its remotes", description=cmd_push.__doc__
    )
    push_parser.add_argument("--no-dev", action="store_true", help="do not push the dev branch")
    push_parser.add_argument("--skip-hooks", action="store_true", help="run no hooks")
    push_parser.add_argument("-n", "--dry-run", action="store_true", help="print the pushes only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ease. Return the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        git = open_repo(Path(args.directory))
        if args.command == "init":
            return cmd_init(args, git)
        if args.command == "status":
            try:
                config, config_error = load_config(git), None
            except EaseError as exc:
                config, config_error = Config(), str(exc)
            return cmd_status(args, git, config, config_error)
        config = load_config(git)
        handlers = {"release": cmd_release, "push": cmd_push, "changelog": cmd_changelog}
        return handlers[args.command](args, git, config)
    except EaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
