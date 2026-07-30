# Open-source boundary

forecast-loop is developed as a public-only core. The repository must remain
usable without private infrastructure, private datasets, or undisclosed
integrations.

## Repository boundary

The public repository contains:

- reusable application and validation code;
- public contracts and versioned schemas;
- synthetic or clearly redistributable fixtures;
- generic read-only provider and adapter examples;
- public documentation, tests, workflows, and release artifacts.

It must not contain or identify:

- private repositories, projects, research programs, or upstream relationships;
- operator names, personal paths, machine names, internal hostnames, tunnels,
  schedules, or deployment inventory;
- provider credentials, licensed field mappings, production schemas, account or
  order paths, or writable upstream integrations;
- actual databases, snapshots, checkpoints, handoffs, logs, source maps, or
  generated local output;
- operator-maintained Agent definitions, prompts, track records, Wiki pages,
  reflections, lessons, or proposal history.

Public interfaces use neutral concepts such as `provider`, `adapter`,
`snapshot`, `external source`, and `operator`. Private integrations consume a
tagged public release; the public repository never depends on them.

## Local ownership

The public source tree is not a synchronization target for an operator's
accumulated research:

- AgentSpec snapshots, accepted signals, opinions, evaluations, and scorecard
  evidence remain in the operator's database.
- Agent Wiki pages, their index, log, and proposals default to the Git-ignored
  `data/wiki/` tree or another configured local path.
- Custom Agent implementations and source mappings live in a separate
  extension or repository-external executable adapter.
- The checked-in `wiki/` directory contains only synthetic `demo-only`
  examples. Demo may use them when the local catalog is empty; Live never
  falls back to them.
- Human-readable Reflection and Lesson archives default to
  `data/reflection-archives/` and `data/lesson-archives/`.

## History policy

The public repository started from a reviewed snapshot with a new Git root. No
pre-publication object database, branch, tag, issue, pull request, release, or
hidden ref was imported.

All public refs are zero-tolerance for secrets, PII, skipped blobs, and
repository-external private-boundary matches. Deleting a file in a later commit
does not make an earlier public object safe.

## Contribution gate

Maintainers install the checked-in hooks with an external private pattern file:

```bash
make install-hooks PRIVATE_BOUNDARY_FILE="$FORECAST_LOOP_PRIVATE_BOUNDARY_FILE"
```

The file must remain outside the repository, contain one literal per line, use
owner-only permissions, and must never be printed or attached to CI.

Before commit:

```bash
git add <explicit-files>
make public-preflight-staged
```

Before push:

```bash
make public-preflight-range
```

Pull requests must pass generic privacy, complete-history, Gitleaks, dependency,
test, build, and container checks. A separately managed required check evaluates
private-boundary rules without exposing them to public Actions or forked code.

Hooks reduce mistakes but can be bypassed locally. Repository rulesets and
required checks are the authoritative merge boundary.

## Issue and pull-request text

Issue titles, bodies, comments, pull-request descriptions, branch names, commit
messages, and release notes are public content. Do not use them to report a
possible leak or vulnerability. Use the repository's private vulnerability
reporting channel instead.

Content submitted to a public pull request may already have been copied before a
check finishes. Work that might contain private context must be reviewed in a
private staging environment and exported as a neutral patch.

## Release gate

Releases are built from a reviewed tagged commit in an isolated clean worktree.
The release workflow scans:

- all fetched public branches, tags, and pull-request heads;
- source, wheel, sdist, frontend, SBOM, checksum, and tracked release-note
  artifacts;
- archive paths, member types, expanded sizes, secrets, PII, and private
  boundary matches.

Release artifacts containing source maps, non-regular archive members,
unscanned binary content, path traversal, or any boundary finding are rejected.

## Incident response

Treat any content pushed to a public ref or public pull request as already
copied. Revoke exposed credentials immediately, stop merging and publishing,
remove affected refs and artifacts, request platform cache cleanup, and evaluate
whether a new clean repository is required. History rewriting alone does not
prove that public copies disappeared.
