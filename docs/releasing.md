# Release process

forecast-loop releases include reproducible source, Python, and frontend
artifacts, checksums, an SPDX SBOM, and provenance attestation.

## Preconditions

A release commit must:

- be reachable from the protected default branch;
- pass CI, Security, privacy-boundary, dependency, migration, and Docker checks;
- contain matching versions in `pyproject.toml`, `uv.lock`,
  `frontend/package.json`, and `frontend/package-lock.json`;
- update `CHANGELOG.md`, compatibility documentation, upgrade instructions, and
  a tracked release-notes file;
- contain no secret, PII, skipped blob, private-boundary finding, source map, or
  unreviewed generated file.

The repository must enable release immutability, secret scanning, push
protection, private vulnerability reporting, and rulesets for the default branch
and release tags. The `public-release` Environment must require an independent
review and allow only protected release tags.

## Local preflight

Install hooks with the maintainer-only pattern file:

```bash
make install-hooks PRIVATE_BOUNDARY_FILE="$FORECAST_LOOP_PRIVATE_BOUNDARY_FILE"
```

Run:

```bash
make lint
make test
make build
make migration-smoke
make docker-smoke
make public-preflight-range
```

The private pattern file stays outside the repository with owner-only
permissions. It must never be printed, uploaded, or copied into release output.

## Reproducible artifacts

Build into a new output directory:

```bash
make release-artifacts \
  RELEASE_VERSION=0.1.0 \
  RELEASE_OUTPUT=dist/release/v0.1.0
```

The builder resolves the selected revision to immutable Git objects, uses the
commit timestamp as `SOURCE_DATE_EPOCH`, creates clean detached worktrees, and
builds twice. Any byte or filename mismatch fails the build.

Expected artifacts:

- `forecast-loop-0.1.0-source.tar.gz`
- `forecast_loop-0.1.0-py3-none-any.whl`
- `forecast_loop-0.1.0.tar.gz`
- `forecast-loop-0.1.0-frontend.tar.gz`
- `SHA256SUMS`

The release workflow adds the SPDX SBOM and updates `SHA256SUMS`.

Before upload, every archive and metadata file is inspected without extracting
to the workspace:

```bash
uv run python scripts/audit_release_artifacts.py \
  --repository . \
  --artifact-dir dist/release/v0.1.0
```

The scanner rejects unsafe member paths, links and device entries, expansion
limits, source maps, binary or oversized members that cannot be inspected,
secrets, PII, and private-boundary matches.

## Tag and publish

Create a signed annotated tag from the reviewed default-branch commit:

```bash
git tag --sign --annotate v0.1.0 \
  --message "forecast-loop v0.1.0"
git tag --verify v0.1.0
git push origin v0.1.0
```

The workflow verifies the annotated tag and GitHub signature, confirms the
commit is on the default branch, reruns all gates, scans all public branch/tag/PR
refs, builds and scans artifacts, attests provenance, and publishes using the
tracked `docs/releases/v0.1.0.md`. Generated release notes are prohibited because
they bypass the reviewed text boundary.

Release tags and published assets are immutable. Withdraw a defective release
and publish a new patch version; never move a tag or overwrite an asset.

## Incident response

If a release contains sensitive information:

1. stop further publication and merging;
2. revoke exposed credentials immediately;
3. withdraw affected assets and refs;
4. request platform cache cleanup;
5. determine whether a new clean repository is required;
6. publish a new version and incident note without repeating sensitive content.

Assume public artifacts and refs have already been copied. Rewriting history is
remediation, not proof of erasure.
