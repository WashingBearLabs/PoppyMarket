# PoppyMarket

The app and theme registry for Poppy, a personal AI operating system by WashingBearLabs.

## What is this?

PoppyMarket is a JSON registry that Poppy fetches to discover available apps and themes. When you open
the marketplace in Poppy, it pulls `registry.json` from this repo to show what can be installed.

## Registry format

`registry.json` is one JSON object with three keys:

| Key | Description |
|-----|-------------|
| `version` | Registry format version (non-empty string) |
| `updated_at` | Date of the last change, `YYYY-MM-DD` |
| `apps` | Array of entries (apps and themes), at most 500 |

The file must be in canonical form: `json.dumps(doc, indent=2, ensure_ascii=False)` plus a trailing
newline, at most 1 MiB, with no duplicate keys. Each entry has a `kind`, `"app"` (the default when
`kind` is absent) or `"theme"`. An app and a theme may not share a `name`.

### Fields shared by every entry

| Field | Description |
|-------|-------------|
| `name` | Unique identifier: 1–63 lowercase letters, digits or hyphens, starting with a letter or digit |
| `description` | Short description (shown on the card) |
| `version` | Current version |
| `author` | Publisher name |
| `tags` | Optional array of searchable keywords |

`name`, `description`, `version` and `author` are required, non-empty strings.

### Apps (`kind: app`)

| Field | Description |
|-------|-------------|
| `image` | Pre-built container image reference. An app needs `image` or `repo_url` (or both) |
| `repo_url` | Git repository URL; must be `https://` |
| `icon` | An `https://` URL, or a repo-relative path (`.png`, `.svg`, `.jpg`, `.jpeg`) that must exist in this repo |
| `hero_image` | Same rules as `icon` |
| `port` | Container internal port, an integer from 1 to 65535 |
| `manifest_url` | URL of the app's full manifest |
| `category` | Top-level grouping (development, productivity, security, ...) |
| `long_description` | Detailed, natural-language description |
| `capabilities` | Array of strings: what the app can do |
| `use_cases` | Array of strings: when you would use the app |

Optional fields may be omitted or `null`. Repo-relative asset files are at most 2 MiB each.

### Themes (`kind: theme`)

A theme entry may carry only these fields:

| Field | Required | Rule |
|-------|----------|------|
| `name` | yes | The theme id. Not a reserved id (`active`, `default`, `marketplace`, `installed`, `files`, `sdk`, `dark`, `paper`, `poppy-glass`) and not starting with `wb-` |
| `kind` | yes | `"theme"` |
| `display_name` | no | At most 64 characters |
| `description` | yes | At most 500 characters |
| `long_description` | no | At most 4000 characters |
| `version` | yes | ASCII semver (`1.2.3`, optional `-pre` and `+build`), at most 64 characters |
| `author` | yes | At most 64 characters |
| `category` | no | At most 40 characters |
| `tags` | no | At most 20 tags, each at most 32 characters |
| `repo_url` | yes | `https://github.com/WashingBearLabs/poppy-theme-<name>` (optionally ending in `.git`) |
| `ref` | yes | The 40-character lowercase commit of tag `v<version>` in that repo |
| `preview_image` | yes | `themes/<name>/preview.<ext>` with `<ext>` one of `png`, `jpg`, `jpeg`, `webp`; the file must exist in this repo, match its extension's image format and be at most 2 MiB |

A theme must not set `image`, `port`, `icon`, `hero_image`, `manifest_url`, `capabilities`,
`use_cases`, or any other field. Character counts are Unicode code points.

**Public display text.** `display_name`, `description`, `long_description`, `author`, `category` and
every tag are shown to users as plain text. They must not contain control characters (tabs and line
breaks included), bidirectional controls (U+202A–U+202E, U+2066–U+2069), U+200B, U+FEFF, U+2028 or
U+2029. Emoji, ZWJ/ZWNJ and soft hyphens are fine.

The theme's repo must be public: `WashingBearLabs/poppy-theme-<name>`, with tag `v<version>` pointing at
`ref` and a `theme.yaml` at that commit whose `id` and `version` match the entry.

## Adding or changing an entry

PRs on this repo are opened by members of the WashingBearLabs organization only. Outside contributions
are not accepted; please open an issue instead. A green check on a PR from a fork is structural only
and is never a reason to merge.

**Themes arrive through Poppy's publish flow.** Poppy pushes the theme to its public
`poppy-theme-<name>` repo, then opens a PR on this repo (not from a fork) on the branch
`theme/<name>-v<version>`, as the organization's publish bot account. A later publish of the same
version updates that PR rather than opening a duplicate. The repository owner reviews, approves and
merges every PR; nothing merges automatically.

## Checks

`.github/workflows/validate-registry.yml` runs `validate-registry` on every PR to `main` and on every
push to `main`. It checks:

- the document shape, canonical form and size;
- every entry against the rules above;
- that every repo-relative asset and preview file exists, is a regular file, is within its size limit
  and (for previews) matches its extension;
- for theme entries the PR adds or changes (online, PRs only): that the repo exists and is public,
  that tag `v<version>` points at `ref`, and that `theme.yaml` at `ref` carries the entry's `id` and
  `version`.

The full gate for themes is in Poppy, before a PR is ever opened. These checks are structural: they
cannot judge a theme's content. **A green check is structural only.** PRs from outside the
organization, and any PR that touches `.github/`, need a full manual review of every file.

**Change summary.** The job summary lists "Registry changes in this PR", one `CHANGED` line per added,
removed or changed entry. When an existing entry's `image`, `repo_url`, `ref`, `manifest_url` or `port`
changes, the run also warns "install source changed": review that change by hand.

**`UNVERIFIED … could not verify`** means an online check could not complete (a GitHub rate limit, an
outage, or the job's time budget). The run is not failed, but the entry was not verified: re-run the
job before merging. **`REPO_NOT_FOUND`** means the theme repo was not found or is not public.

### Running the validator locally

```sh
python3 .github/scripts/validate_registry.py --root . registry.json
python3 .github/scripts/validate_registry.py --root . --online registry.json
```

It needs Python 3.12 or later and nothing outside the standard library. `--online` may use a token from
`POPPYMARKET_GITHUB_TOKEN` to raise GitHub's API rate limit; the token is sent only to `api.github.com`.
`--changed-against <file>` limits online checks to entries that differ from a base copy of the
registry. Exit codes: `0` clean, `1` findings, `3` some online checks could not complete.

### Editor support

`.github/scripts/registry.schema.json` is a JSON Schema for `registry.json`. Do not add a `$schema` key
to `registry.json` (the check rejects it); associate the schema in your editor settings instead, for
example in VS Code:

```json
"json.schemas": [{"fileMatch": ["/registry.json"], "url": "./.github/scripts/registry.schema.json"}]
```

## Branch protection

`main` changes only through a PR approved by the code owner (see `.github/CODEOWNERS`) after the PR's
latest push, with a passing `validate-registry` check on an up-to-date branch.

- **The validator comes from `main`.** On a PR, CI runs the base branch's copy of
  `.github/scripts/validate_registry.py` against the PR's data, so a PR's own edits to `.github/scripts/`
  do not change what checks it.
- **Workflow edits are not self-checking.** GitHub runs a PR's own copy of the workflow file, so for a
  PR that edits `.github/workflows/`, its check, warnings and job summary are not evidence. Every file
  in such a PR gets a by-hand review of the raw diff.
- **Owner changes.** A PR the owner authored or last pushed to (including a theme PR the owner brought
  up to date with "Update branch", which is routine when two theme publications overlap) cannot be
  approved by its only code owner. The owner merges it with an admin bypass that is pinned to the exact
  reviewed commit (`gh pr merge --match-head-commit <sha>`), refused if that commit is behind `main`,
  and removed again by the same script whether the merge succeeds or fails. The bypass is checked to be
  gone after every such merge and before every approval.
- **Break-glass.** If the check cannot pass for every PR (a broken workflow, a validator bug, or an
  Actions outage), the fix PR is merged through the same pinned owner-change procedure after a by-hand
  review of its raw diff, and the reason is recorded. The ruleset is never disabled.

## Available apps

| App | Description | Author |
|-----|-------------|--------|
| Acorn Atelier | Visual development environment for managing software projects | WashingBearLabs |


