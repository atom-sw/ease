# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-10-26

### Added

- `artifacts` adds the build outputs of the `pre-release` hook, such as a rendered manual, to
  each release, without tracking them on the development branch.

## [0.1.0] - 2026-10-26

First release of `ease` as a standalone project, moved out of another private repository.

### Added

- `ease init` sets up a dev/main release workflow: the development branch, `.ease.toml`, a
  commented `.mergeignore`, and a Keep a Changelog `CHANGELOG.md`.
- `ease init --existing` adopts a repository that already has both branches.
- `ease status [--fix]` checks the setup, and applies the repairs that lose nothing.
- `ease changelog` promotes the Unreleased section to a dated release section.
- `ease release` commits a snapshot of the development branch, minus the `.mergeignore` paths,
  onto the release branch, and tags it. It writes the message in git's editor when the subject
  is empty or with `--edit`.
- `ease release` refuses to release from a development branch that is behind its remote.
- `ease push` pushes the release branch, then its tags, then the development branch.
