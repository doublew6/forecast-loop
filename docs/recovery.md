# Migration, backup, and recovery

forecast-loop treats migrations and recovery as explicit operator actions. The
API and worker never call `create_all()` in normal runtime startup. They refuse
to start unless the database has every current Alembic head and the required
core tables.

## Startup and migration order

For a local process:

```bash
make migrate
make database-status
make backend
```

`make migrate` is the only normal schema creation/upgrade path. Tests that need
an in-memory-style fixture opt into `create_app(...,
allow_schema_bootstrap=True)` explicitly; that switch is not exposed through
environment configuration.

Compose adds a one-shot `migrate` service. Both `api` and `worker` wait for it
to exit successfully, and `web` waits for the API health check:

```bash
make docker-config
docker compose up --build
```

Published API and web ports remain bound to `127.0.0.1`. Setting
`FORECAST_LOOP_DATA_DIR` changes only the host directory mounted at
`/app/data`; the Docker smoke test uses this to avoid the repository's real
`data/` directory.

The runtime image uses the fixed non-root identity `10001:10001`. Before a
production Compose start, create the host data directory for that identity and
keep it private:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 ./runtime-data
FORECAST_LOOP_DATA_DIR=./runtime-data docker compose up --build
```

Do not solve bind-mount errors by running the API or worker as root. The
disposable smoke test relaxes permissions only on its own synthetic temporary
directory, which is removed after the test.

## What a recovery bundle contains

The recovery CLI accepts sources explicitly. It does not infer or traverse the
configured `data/` tree:

- the main SQLite database, captured with the SQLite online backup API;
- the LangGraph SQLite checkpoint database, also captured online;
- zero or more explicitly named mutable roots, copied without following
  symlinks.

Typical mutable roots are `wiki`, `handoffs`, `reflections`,
`reflection-archives`, `lesson-archives`, `market-snapshots`,
`evidence-snapshots`, `prediction-status`, and `user-wiki`. The
operator-maintained Agent Wiki and human-readable archives are local data and
must be backed up explicitly. The repository contains only disposable
`demo-only` examples and layout documentation.

Each SQLite file is internally consistent even while it is live. For one
application-consistent recovery point across both databases and file roots,
pause new API work and stop the worker before taking the bundle. Resume them
after `recovery verify` succeeds.

Example:

```bash
make recovery-backup ARGS="\
  --database ./runtime-data/forecast-loop.sqlite3 \
  --checkpoint ./runtime-data/checkpoints/langgraph.sqlite3 \
  --root wiki=./runtime-data/wiki \
  --root handoffs=./runtime-data/handoffs \
  --root reflections=./runtime-data/reflections \
  --root reflection-archives=./runtime-data/reflection-archives \
  --root lesson-archives=./runtime-data/lesson-archives \
  --root market-snapshots=./runtime-data/market-snapshots \
  --root evidence-snapshots=./runtime-data/evidence-snapshots \
  --root prediction-status=./runtime-data/prediction-status \
  --root user-wiki=./runtime-data/user-wiki \
  --output-root ./backups"

make recovery-verify ARGS="./backups/backup-..."
```

The bundle directory is mode `0700`; its manifest and artifacts are mode
`0600`. `manifest.json` seals the exact artifact set, byte sizes, SHA-256
digests, migration heads, and core-table row counts. This detects corruption
and accidental modification, but it is not a digital signature or encryption.
Store bundles on an access-controlled encrypted volume.

Backup refuses:

- symlinked sources or symlinks anywhere inside a named root;
- output paths that overlap a source;
- non-SQLite database/checkpoint files;
- a main database that is not at the current migration head;
- SQLite integrity or foreign-key failures.

## Restore and verification

Restore never overwrites an existing deployment. The target must be a new or
completely empty, non-symlink directory:

```bash
make recovery-restore ARGS="\
  ./backups/backup-... \
  --target-root ./restore-drill/2026-07-28"
```

The command performs this fail-closed sequence:

1. verify the source manifest, exact artifact set, permissions, sizes, hashes,
   SQLite integrity, foreign keys, migration heads, and core row counts;
2. copy with exclusive creation into the empty isolated target;
3. run Alembic `upgrade head` against only the restored database;
4. rerun integrity, foreign-key, migration-head, core-table, and row-count
   checks;
5. write a mode-`0600` `restore-receipt.json`.

The restored layout is:

```text
target/
├── files/
│   ├── database.sqlite3
│   └── checkpoint.sqlite3
├── roots/
│   └── <logical-root-name>/...
└── restore-receipt.json
```

If any step fails, no receipt is written. Inspect or discard that isolated
target; the command never switches production paths automatically.

## Reproducible synthetic drill

These checks create state only under temporary directories and never read or
back up the repository's real `data/`:

```bash
uv run pytest -q backend/tests/test_schema_recovery.py
make migration-smoke
make docker-smoke
```

Last verified on 2026-07-28: the recovery test suite, previous-revision
migration smoke, Compose configuration validation, and disposable Docker smoke
all passed with synthetic state.

`migration-smoke` migrates a synthetic database to head, moves it to the
previous revision, inserts a sentinel run, and upgrades back to head while
checking the row, migration head, integrity, foreign keys, and core tables.

`docker-smoke` builds a disposable Compose project with a temporary bind mount
and random loopback ports. It requires a working Docker daemon, waits for the
one-shot migration plus API/web health checks, probes both endpoints and the
Web security-header baseline, then tears down the disposable project.
