# ease 🚀

`ease` publishes release snapshots of a development branch onto a release branch.

Work happens on `dev`. Each release becomes a single commit on `main` whose tree is `dev`'s tree
minus the paths listed in `.mergeignore`, and whose parent is the previous release commit. The
development history never reaches `main`, so you can push `main` to a public repository without
leaking work-in-progress commits or any version of the files you keep private.

`ease` is one Python file with no dependencies beyond Python 3.12 and `git`.

```sh
ease init --remote public=git@github.com:org/project.git   # once per project
ease status                                                # anytime
ease release --changelog --push                            # each release
```

To vet each release before it goes public, such as with CI in a private repository, stage it
first: see [Staging releases](#-staging-releases).

## 📦 Install

Copy `ease.py` somewhere on your `PATH` as `ease`, or run it in place:

```sh
install -m 755 ease.py ~/.local/bin/ease
```

## 🧭 Commands

Every command accepts `-C DIR` to run as if started in `DIR`, and `--help` for all its options.

### `ease init`

Turns a repository with only its default branch into a dev/main setup:

1. It creates `dev` from the current branch and checks it out.
2. It writes `.ease.toml`, a commented `.mergeignore`, and a Keep a Changelog `CHANGELOG.md` (unless
   one exists).
3. It adds any `--remote NAME=URL` and lists every remote under `release-remotes`, except those
   named with `--staging-remote NAME`, which go under `staging-remotes`.
4. It commits the setup on `dev`. It pushes nothing.

`init` guesses the version source from the manifests it finds (`pyproject.toml`, `package.json`,
`Cargo.toml`, `VERSION`); override the guess with `--version-source`.

`init` never overwrites a file: an existing `.mergeignore` or `CHANGELOG.md` is kept as is.

To adopt a project that already has both branches, for example one released by an older script,
run `ease init --existing` from `dev`. It writes only the missing files and derives the config
from the repository:

- `release-remotes` lists the remotes that already have the release branch (`--release-branch`,
  default `main`); fetch first so that the remote-tracking refs are current. Without any, it
  lists every remote.
- Without a `CHANGELOG.md`, it sets `changelog = ""`, and, unless a manifest supplies a version,
  `source = "none"` and `tag = ""`. You then release with `ease release -m "..."`.

### `ease status [--fix]`

Checks the setup and prints one line per aspect, as `ok`, `warn`, or `error`:

- the config parses, and its working-tree copy matches the committed one;
- `dev`, `main`, `.mergeignore`, and the changelog exist and are committed;
- the configured remotes exist, and each branch is equal to, ahead of, behind, or diverged from
  its remote;
- the release tip carries its tag, and every release tag is on every release remote;
- the version on `dev` resolves, and whether it still needs a bump or a changelog section;
- which files a release would change.

It exits with 1 when any error remains. `status` fetches first; `--offline` skips the fetch and
the tag queries.

`--fix` applies only repairs that lose nothing: it fast-forwards a branch that is strictly behind
its remote, pushes a branch that is strictly ahead, pushes missing tags, recreates a missing
branch from its remote, and writes a missing `.ease.toml`, `.mergeignore`, or changelog to the
working tree for you to review and commit. It reports a diverged branch or a moved tag, and never
touches it. With staging remotes, it never pushes to a release remote: it reports a staged release
as not yet published, for `ease publish` to handle.

### `ease changelog [VERSION]`

Renames the `## [Unreleased]` section to `## [VERSION] - YYYY-MM-DD` and opens a fresh, empty
Unreleased section above it. When the `[Unreleased]:` link definition at the foot is a
`.../compare/A...B` URL, it also moves that link to compare from the new tag and adds a link for
the new version. `VERSION` defaults to the configured version source.

The command edits the working tree only: review the result and commit it on `dev`.

### `ease release`

Commits a snapshot of `dev` onto `main` and tags it. Nothing is checked out: `ease` builds the
snapshot in a temporary index, so your working tree and current branch stay untouched. The commit
and the annotated tag share one message: the `subject` template (`Release X.Y.Z.`) followed by the
changelog's `## [X.Y.Z]` section, with its markdown headings intact.

When the rendered `subject` is empty, or with `--edit`, `release` opens git's editor (the one
`git commit` uses) on the composed message, after the pre-release hook and before the review. Set
`subject = ""` to write every release message by hand. Everything below the scissors line is
dropped, but lines starting with `#` above it stay, since markdown headings start with one. An
empty message aborts the release. `-m` skips the editor, and `--dry-run` never opens it.

Before writing anything, `release` prints the per-pattern drop counts, the dropped files, a
`diff --stat` against `main`, and the message, then asks for confirmation.

It always refuses when the working tree has uncommitted changes, when `main` is checked out, or
when the snapshot would drop a path listed under `required`. It also refuses, unless you pass
`--force`, when:

- `main` is behind or has diverged from a release remote (it fetches first; `--offline` skips it);
- `dev` is behind or has diverged from `dev-remote`, so the snapshot would miss commits that are
  only on the remote, such as a fix released from another machine (being ahead is fine);
- the tag already exists, meaning the version needs a bump;
- `--amend` would replace a release that is already on a release remote (a staging remote does
  not count);
- the snapshot is identical to `main`.

Useful options:

| Option | Effect |
|---|---|
| `--changelog` | Promote the Unreleased section first, committing it on `dev` |
| `--push` | Run `ease push` afterward; otherwise `release` prints the push commands |
| `--publish` | With staging remotes, run `ease push` and then `ease publish` |
| `--amend` | Replace the unpushed release at the tip of `main`, moving its tag |
| `-F FILE` | Take the release notes from `FILE` instead of the changelog |
| `-m MSG` | Use `MSG` as the whole commit message (for versionless projects) |
| `-e`, `--edit` | Edit the message in git's editor before committing |
| `--version X` | Override the configured version source |
| `-n`, `--dry-run` | Print the review and stop |
| `-y`, `--yes` | Skip the confirmation |

### `ease push`

Pushes `main` to every release remote, then the tags at its tip, then `dev` to `dev-remote` when
it is ahead (`--no-dev` skips that). Pushing the branch before the tag matters when the tag
triggers CI: the workflow then finds the commit on the branch. After `release --amend`, `push`
replaces the old release with `--force-with-lease`; it refuses any other divergence.

With staging remotes, `push` sends the release to them instead, and never to a release remote.

### `ease publish`

Only with staging remotes: pushes the staged release at the tip of `main`, and its tags, on to
every release remote.

1. It refuses unless the release is on every staging remote, tag included (`--force` overrides).
2. It runs the `pre-publish` hook, which can veto the publication by failing.
3. It prints the pushes and asks for confirmation (`-y` skips it; `--dry-run` stops here).
4. It pushes, then runs the `post-publish` hook.

It never overwrites a release remote, since a published release is final: `--force` allows
replacing a release that `release --amend --force` rewrote.

## ⚙️ Configuration

`ease` reads `.ease.toml` from the committed tip of the development branch, so a release never
depends on uncommitted edits. Every key is optional; these are the defaults:

```toml
dev-branch = "dev"
release-branch = "main"
dev-remote = "origin"            # "" for none
release-remotes = ["origin"]     # e.g. ["origin", "public"]
staging-remotes = []             # e.g. ["origin"], with release-remotes = ["public"]
mergeignore = ".mergeignore"
changelog = "CHANGELOG.md"       # "" to pass notes with -F or -m
tag = "v{version}"               # "" disables tagging
subject = "Release {version}."   # {version} and {date}; "" opens the editor
changelog-commit = "Update the changelog for {version}."
required = []                    # paths a snapshot must keep
artifacts = ""                   # build outputs to add, e.g. "build/release"

[version]
source = "changelog"
# path = "..."                   # overrides the source's default file
# command = "..."                # for source = "command"

[hooks]
# pre-release = "..."
# post-release = "..."
# post-push = "..."
# pre-publish = "..."
# post-publish = "..."
```

`ease` always drops `.ease.toml` and the mergeignore file from a snapshot.

### `.mergeignore`

Each line is a `git` pathspec with globs, which behaves like `.gitignore`: `*` stops at a `/`,
`**` crosses it, and a directory name drops its whole subtree. For example, a top-level `TODO.md` drops only
`TODO.md`, and leaves `docs/TODO.md` alone. A line starting with `:` goes to git verbatim, for
other pathspec magic. Blank lines and `#` comments are ignored.

### Release artifacts

Some outputs belong in a release but not in the development history: a rendered manual, a
generated reference. Set `artifacts` to a directory, and make the `pre-release` hook build into
it. The directory mirrors the snapshot: `build/release/docs/manual.pdf` becomes `docs/manual.pdf`
in the release commit, while `dev` never tracks it.

```toml
artifacts = "build/release"
required = ["docs/manual.pdf"]   # required paths may name artifacts

[hooks]
pre-release = "make check && rm -rf build/release && make manual OUT=build/release/docs"
```

`release` refuses, and writes nothing, when:

- the pre-release hook did not run (`--skip-hooks`, or the working tree is not at the source),
  since the artifacts could then come from another tree;
- the directory is missing or empty, or holds a symbolic link;
- a file is older than the hook's start, a leftover of an earlier build: let the hook rebuild
  the whole directory, as `ease` never deletes it;
- an artifact would replace a file of the snapshot.

The review lists the added artifacts, `--dry-run` included. `status` cannot see them, as only a
release runs the hook: it warns about required paths missing from `dev` instead of failing. An
`artifacts` setting needs a `pre-release` hook.

### Version sources

`ease` reads the version from the source ref, not the working tree:

| `source` | Reads |
|---|---|
| `pyproject` | `[project].version` in `pyproject.toml` |
| `package.json` | `version` in `package.json` |
| `cargo` | `[package].version` in `Cargo.toml` |
| `file` | the first non-empty line of `VERSION` |
| `changelog` | the topmost released `## [X.Y.Z]` heading |
| `command` | the stdout of `command`, run in the working tree |
| `none` | no version: no tag, and `release` needs `-m` |

Use `command` for anything else, such as a dynamic Python version:
`command = "uv version --short"`. It runs only when the working tree is at the source commit.

With `source = "changelog"`, the newest heading is the *previous* release until you promote
Unreleased, so pass the new version explicitly: `ease changelog 1.2.0`, or
`ease release --changelog --version 1.2.0`.

### Hooks

Hooks are shell commands, run from the repository root:

| Hook | Runs | On failure |
|---|---|---|
| `pre-release` | before the snapshot, also on `--dry-run` | aborts the release |
| `post-release` | after the commit and tag | warns |
| `post-push` | after `ease push` | warns |
| `pre-publish` | before `ease publish` pushes, also on `--dry-run` | aborts the publication |
| `post-publish` | after `ease publish` | warns |

`pre-release` is the place for the project's gate, such as `mise run check` or `make test`. It
runs only when the working tree is at the source commit, since that is the tree it inspects.
`pre-publish` is the place to vet a staged release, such as waiting for its CI run.
`--skip-hooks` skips all hooks.

The release hooks receive these environment variables. `post-push` receives only `EASE_TAG`,
`EASE_COMMIT`, and the branch and remote variables; the publish hooks receive those and
`EASE_DRY_RUN`.

| Variable | Value |
|---|---|
| `EASE_VERSION` | the version being released |
| `EASE_TAG` | the tag name |
| `EASE_PREVIOUS_TAG` | the newest earlier release tag |
| `EASE_COMMIT` | the new release commit (not in `pre-release`) |
| `EASE_NOTES_FILE` | a file holding the commit and tag message |
| `EASE_DRY_RUN` | `1` on `--dry-run`, else `0` |
| `EASE_DEV_BRANCH`, `EASE_RELEASE_BRANCH` | the branch names |
| `EASE_REMOTES` | the release remotes, space-separated |
| `EASE_STAGING_REMOTES` | the staging remotes, space-separated |

For example, a Python package published by a tag-triggered workflow:

```toml
[version]
source = "pyproject"

[hooks]
pre-release = "mise run check"
post-release = 'echo "Pushing $EASE_TAG triggers the PyPI upload."'
```

## 🔀 Variants

- **Two repositories**: `release-remotes = ["origin", "public"]` pushes `main` and its tags to
  both, while `dev` goes only to `origin`.
- **One repository**: `release-remotes = ["origin"]` keeps both branches there.
- **Staged**: `staging-remotes = ["origin"]` and `release-remotes = ["public"]`; see below.
- **No versions**: `[version] source = "none"`, `tag = ""`, and `changelog = ""`; publish with
  `ease release -m "Describe the snapshot."`, or set `subject = ""` to write the message in the
  editor.

## 🎭 Staging releases

Publishing a release can be final: a package index may fetch it at once and never let it be
replaced, and a tag-triggered workflow may upload it. Staging puts a step in between, where the
release has been pushed somewhere, but can still be amended:

```toml
release-remotes = ["public"]
staging-remotes = ["origin"]

[hooks]
pre-publish = "./scripts/wait-for-ci.sh $EASE_COMMIT"   # fail to veto the publication
```

```sh
ease release --changelog --push      # stage: origin runs CI on the release
ease release --amend --push          # if CI fails: fix on dev, rebuild, restage
ease publish                         # once CI passes: on to public
```

`ease release --publish` does both phases in one go. Staging is worth it when:

- CI runs only on a push, such as on a clean runner in a private repository, or a workflow
  triggered by the tag, and you want it to pass before anything is public;
- a registry consumes whatever reaches the release remote, and never gives a version back;
- someone reviews the release commit before it goes out.

Without any of these, skip it: `ease push` to the release remotes is one step less.

## 🧪 Development

```sh
python -m unittest      # tests, against throwaway git repositories
ruff format && ruff check
pyright
```

## 🏷️ Releasing

`ease` releases itself: develop on `dev`, and publish snapshots onto `main`. The version lives in
`[project].version` in `pyproject.toml`, and `CHANGELOG.md` supplies the release notes.

1. While developing, record each notable change under `## [Unreleased]` in `CHANGELOG.md`.
2. To release, bump `version` in `pyproject.toml` and commit it on `dev`.
3. Run the release, which runs the tests and linters as its `pre-release` hook:

   ```sh
   python ease.py release --changelog --push
   ```

   `--changelog` renames Unreleased to `## [X.Y.Z] - YYYY-MM-DD` and commits that on `dev`. The
   release commit and its `vX.Y.Z` tag take their message from that section.
