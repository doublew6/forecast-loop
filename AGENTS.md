# forecast-loop Agent Guide

## Communication

- Communicate with the user in the language they use.
- Keep code, identifiers, filenames, and code comments in English unless they
  are user-facing copy.
- Treat this repository as public. Never add information copied from private
  repositories, workspaces, conversations, infrastructure, or datasets.

## Public-Only Boundary

- forecast-loop is the canonical public core. It must build, test, document, and
  run its demo path without private extensions or operator infrastructure.
- Use neutral terms such as `provider`, `adapter`, `snapshot`, and `external
  source`. Do not identify private projects, machines, accounts, directory
  layouts, database schemas, schedules, tunnels, or research programs.
- External integrations must use documented public contracts and read-only
  adapters. Keep credentials, licensed field mappings, production paths, and
  private deployment configuration outside this repository.
- Tests and examples must use synthetic or clearly redistributable data,
  example domains, generic identities, and portable paths.
- Do not add `.env` files, databases, checkpoints, handoffs, logs, source maps,
  generated release output, or local absolute paths. Do not force-add ignored
  files.
- Before committing, run `make public-preflight`. Before pushing, run
  `make public-preflight-range`.

## Trust Boundary

- forecast-loop is a research, evidence, and audit system. It must not place
  orders, control accounts, or write to an upstream production database.
- Model output is an untrusted draft until deterministic schema, time, source,
  hash, and persistence checks pass.
- Preserve the explicit prepare, draft, and finalize boundaries. A draft writer
  must not edit sealed inputs, receipts, historical results, or upstream data.
- Reject path traversal, symlink escapes, stale evidence, unverifiable source
  times, and incomplete inputs. Do not weaken a fail-closed check to make an
  example or test pass.

## Development

- Keep changes scoped and preserve compatibility unless the change explicitly
  updates the public contract and migration path.
- Use Pydantic models for untrusted API and file input. Reject unexpected fields
  where the contract is strict.
- Keep frontend secrets server-side; browser-delivered configuration is public.
- Run `make lint`, `make test`, and `make build` for affected code.
- Update public documentation and `CHANGELOG.md` for user-visible behavior.

## Wiki And Lessons

- Predictions may use only evidence and Wiki material published before their
  cutoff. Never rewrite historical snapshots or forecasts.
- Keep stable frameworks in `wiki/`, immutable facts in evidence snapshots, and
  learned lessons in a separate append-only lifecycle.
- Proposed Wiki changes require inspectable public sources, stable entry and
  section IDs, deterministic validation, semantic versioning, and an audit
  trail.
