# Wiki Atlas

This is forecast-loop's temporary standalone Wiki frontend. It is intentionally
separate from `frontend/` while keeping the same authenticated, deterministic
`/api/wiki/*` contracts and the same private data store.

The primary information architecture is:

1. knowledge domains;
2. immutable raw materials collected or uploaded into each domain;
3. versioned Wiki pages synthesized from those materials;
4. provenance, stable sections, and append-only self-review feedback.

The interface uses D1 (the next valid trading session) as the only current
prediction scope. It never edits an active Wiki page directly. Uploads create a
maintenance job; validated drafts publish automatically and update `index.md`
while appending `log.md`. Historical forecasts retain their frozen snapshots.

## Local development

```bash
npm ci
npm run dev
```

The development server binds to `127.0.0.1:5174`. The production security
gateway binds to `127.0.0.1:4174`. Both server-side proxies read
`FORECAST_LOOP_OPERATOR_TOKEN` from the repository root `.env`, strips any
browser-supplied authorization header, and injects the operator token only for
the loopback API proxy.

## Authenticated public access

Production uses a loopback-only Express gateway in front of the built Wiki. It
requires HTTP Basic authentication over an external TLS terminator, validates
the public Host and write Origin, adds strict browser security headers, and
never forwards browser credentials or cookies to the API. The API receives only
the private operator token.

Provision a random password and slow scrypt hash with:

```bash
npm run build
npm run provision-auth -- https://wiki-host.example wiki-admin
npm start
```

The command writes the hash to the ignored private file
`data/private/wiki-web.env` and prints the one-time plaintext password to
standard output. Pipe it directly to the operator's password manager or
clipboard; do not save or log it. Existing credentials are never overwritten
unless `--force` is supplied explicitly.

Login failures are limited to five per username and source IP in 30 minutes;
the sixth attempt is blocked. A second limiter allows at most 20 failures per
source IP per hour across usernames. Successful logins and the browser's first
credential-free challenge do not consume attempts. Counters are held in the
single gateway process and reset only when that process restarts.

## Future merge

When the standalone phase ends, move this knowledge atlas into the main
frontend's Wiki route while retaining the API contract, immutable
version/section targets, source upload path, and deterministic backend rules.
