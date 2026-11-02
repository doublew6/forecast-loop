# Focused v2 OpenAI Responses drafts

The optional Responses writer uses `gpt-6.1-sol` with `reasoning.effort=high`
for predictions and outcome-blind reasoning reviews. Operator-run tasks prefer
the API and explicitly seal the permitted Codex fallback at preparation.
The CLI retains the legacy Codex default for compatibility.
The API writer only produces drafts; the existing deterministic commands own
validation, CIO aggregation, database persistence, receipts and human gates.
Historical Codex packages and Agent Eval replay runners are unchanged.
Pre-market tasks have their own opt-in v2 handoff; see `premarket-open-to-open.md`.

## Local configuration

Create `.env.openai` in the repository root, restrict it to the owner (`chmod
600 .env.openai`), and fill the project API key. `.env.*` is already ignored:

```dotenv
OPENAI_API_KEY=
FORECAST_LOOP_OPENAI_MODEL=gpt-6.1-sol
FORECAST_LOOP_OPENAI_REASONING_EFFORT=high
FORECAST_LOOP_OPENAI_TIMEOUT_SECONDS=180
FORECAST_LOOP_OPENAI_MAX_OUTPUT_TOKENS=32768
```

Only the explicit `draft` and `reasoning-draft` commands load this file. An
alternative private file can be supplied with `--env-file`. Process environment
variables take precedence. The key never belongs in a browser, command-line
argument, checked-in file, draft, receipt or trace. The API profile rejects a
different model or effort; selecting another profile requires a versioned
contract change. Legacy `LLM_*` settings do not control this writer.

Requests go to `https://api.openai.com/v1/responses`, disable redirects, and
respect the process's `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`
settings. The writer does not alter client login or proxy configuration.
On hosts where a system proxy is configured outside these environment variables,
the operator launcher must explicitly inherit that existing route before model
execution; the API client does not change system network settings.

## Prediction

After database migration and preparation of a reviewed snapshot:

```bash
make research-v2-prepare ARGS="--mode live --snapshot ./input/snapshot-v2.json --provider openai_responses --codex-fallback"
make research-v2-draft ARGS="./data/handoffs/v2/<run-id> --codex-binary /path/to/installed/codex"
make research-v2-finalize ARGS="./data/handoffs/v2/<run-id>"
```

Prepare does not need an API key and sends no content. The provider is frozen
at prepare. A same-day package already sealed to Codex cannot be switched to
the API, or vice versa; future anchor dates can select either provider.
Changing credentials or configuration never reinterprets a prepared package.

Live validation must preserve cutoff-safe prior natural-horizon state and supply
the research evidence required by the configured Wiki. A price-only snapshot
may be insufficient for macro, event or industry probabilities. Rejected output
stays rejected; enrich a new snapshot from verified sources rather than invent
probabilities, rewrite an old snapshot or use Codex to conceal the rejection.

`draft` is the explicit content-send boundary: the frozen snapshot, Wiki,
assignment matrix and prior context signals are submitted to OpenAI for cloud
inference. With a sealed fallback policy and an explicit installed client,
confirmed quota or availability failures can invoke Codex. Neither writer has
web search, tool access or upstream write permission. `store=false` disables Responses application-state
storage; it does not imply offline inference or override OpenAI's applicable
API retention policies. Local handoffs and receipts remain private runtime
files rather than a cloud synchronization feature.

The API streams strict structured JSON over SSE so long reasoning/output phases
can keep the connection active. Only the full `response.completed` payload is
accepted; text deltas and interrupted streams are never published. The host wraps it with the frozen
identity, actual response model, response ID and reported input/output token
usage. Signals and traces retain the actual response model identity, including
a dated snapshot if the API resolves the requested alias to one. It then repeats
database/request checks and the existing complete
assignment, evidence, Wiki and schema validation before publishing only
`drafts.json`. Generation does not finalize or create forecasts.

Requests have bounded timeout and output tokens. Refusals, incomplete output,
unexpected model/tool output and invalid drafts publish nothing. Only confirmed
quota/credit exhaustion, connection failure, timeout or service unavailability
can trigger the sealed fallback. Missing credentials, authentication failures,
unsupported models, ordinary rate limits and deterministic validation failures
stop the task without fallback. There is no automatic API retry. Existing drafts
are never overwritten; a repeated command stops before sending another request.
If a request fails after reaching OpenAI, it may still have incurred usage;
review platform usage before manually retrying an ambiguous network failure.

Streaming error events support both a top-level `code` and a nested `error.code`.
An explicit top-level code takes precedence if the two disagree. Only the same
allowlisted quota and service codes trigger fallback; an `insufficient_quota`
error type without a confirming code is not sufficient. Incomplete or cancelled
responses never become valid drafts or authorize fallback by themselves.

## Blind reasoning review

Prediction finalize prepares the outcome-blind review task as before:

```bash
make research-v2-reasoning-draft ARGS="./data/handoffs/v2/<run-id> --codex-binary /path/to/installed/codex"
make research-v2-reasoning-finalize ARGS="./data/handoffs/v2/<run-id>"
```

The writer sends only the sealed blind review inputs. It verifies each input
against the persisted signal before sending, and again before publication.
Realized outcomes are excluded. All five rubric dimensions, deterministic
checks and required human review stay in place. Prediction and review calls
have separate response identities and usage records in their private drafts.
API-first review tasks inherit the prediction's sealed fallback policy. A
fallback uses the installed official client and its existing login with
`gpt-5.6-sol / high`, a temporary working directory and tools disabled. Drafts,
signals, rubric records and traces retain the actual provider/model. Fallback
provenance includes the primary failure category and time; it never claims an
API response identity. No model downloads or login changes occur.

## Contracts and compatibility

API predictions without fallback use `forecast-loop.responses-handoff/v2`, which explicitly
hash-binds `mode=demo|live` and mode-specific instructions. Demo outputs are
illustrative fixtures; Live runs still require verified evidence and never use
Demo defaults. Mode-free API v1 packages retain their original protocol and
instructions and cannot be silently upgraded. API review tasks
and drafts use `forecast-loop.responses-reasoning-task/v1` and
`forecast-loop.responses-reasoning-drafts/v1`. Provider/model/effort identity
is hash-bound, and API output cannot impersonate the Codex surface.
New API-first predictions with fallback use `forecast-loop.responses-handoff/v3`;
their blind tasks/drafts use the corresponding Responses review v2 contracts.
Both providers are admitted only through that sealed policy. Omitting
`--codex-fallback` or `--codex-binary` leaves fallback disabled. Export the
prediction schema with:

```bash
uv run forecast-loop contract schema responses-handoff-v3
```

Legacy `forecast-loop.codex-handoff/v3` and its `gpt-5.6-sol / high` checks are
unchanged. Finalize chooses the contract from the frozen request, and a draft
from the other provider is rejected. Historical snapshots, forecasts and
receipts are never rewritten. The public core adds no credentials, licensed
data mappings or source-specific adapters.

Direct API usage is billed to the API organization that owns the project key.
Eligible claimed subscription credits can cover this usage; API-key Codex
usage is excluded from that benefit. See the official
[credit rules](https://developers.openai.com/api/docs/guides/chatgpt-api-credits),
[model capabilities](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [API data controls](https://developers.openai.com/api/docs/guides/your-data).
