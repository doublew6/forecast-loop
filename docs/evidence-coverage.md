# Evidence coverage contract

New coverage-aware tasks use Responses handoff v5 or premarket handoff v3.
They seal `EvidenceCoverageV1` inside the request hash. Historical Responses
v1–v4 and premarket v1–v2 retain their original schema and validation semantics.
Enable coverage by passing `evidence_coverage` to `prepare_research_run` or
`prepare_premarket_run`; focused research also requires the existing API-first
readiness and snapshot-bound admission contracts. Provider/fallback policy is
unchanged.

Live source provenance accepts the reviewed Tushare HTTPS domain, including its
API and documentation hosts. Domain acceptance does not establish freshness or
turn a provider response into an original publisher document. Monthly index
weights keep their actual basis date and cannot satisfy a requirement for a
current-session full weight matrix.

The strict contract binds the snapshot hash, requested collection start and
cutoff, required source groups, fresh collection receipts, stable event IDs,
supporting evidence IDs and hashes, and mandatory macro/Strategy agents.
Receipts must cover the requested start and observe through at least cutoff
minus 15 minutes; captures after cutoff are rejected. Premarket requires its
entire evidence window. Focused research requires at least the full cutoff day.
A successful HTTP request alone does not prove an interval was completely read;
collectors must attest interval completeness, including pagination.

Verified events need frozen original facts. Unresolved leads remain explicit
requirements. Missing/failed/stale collection, unresolved originals, hash/time
mismatches or inaccessible required evidence fail with `input_incomplete`.
Zero discovered events does not substitute for complete collection receipts.
Both macro and Strategy must cite at least one permitted supporting document
for every required event in each of their assignments. Missing citations report
the event and assignment IDs. No probabilities or direction are prescribed.
Requested, allocated and deployed amounts must stay distinct.

Common finalization repeats the gate before forecast persistence, including
callers that bypass generation/dispatcher validation. Premarket's pure finalizer
also verifies the new handoff seal. The check establishes structural citation
coverage; horizon-specific explanations remain subject to independent semantic
review. Reposts/revisions share one event identity rather than independent votes.

Recovery uses a fresh pre-cutoff collection and new immutable preparation when
no task has yet sealed its anchor. Never edit an existing handoff, downgrade a
new task to a legacy schema, or rewrite historical snapshots. A sealed task with
incomplete inputs cannot be repaired by changing its evidence in place.

## Opt-in attributed media coverage

New focused Responses v5 tasks may explicitly seal `EvidenceCoverageV2`
(`forecast-loop.evidence-coverage/v2`) with
`media_policy=attributed-unverified-media/v1`. Existing v1 coverage and premarket
contracts retain their strict official-original requirement. Nothing enables
the exception by default or changes a previously sealed task.

A `media_reported` event must bind captured, attributed report IDs and hashes,
retain an explicit missing-original reason, and set
`verification_warning=未独立核实`. The supporting snapshot summaries must carry
the same marker. The adapter owns source approval, exact report-to-lead matching,
and capture integrity; a similarly worded headline is not independent verification.
Collection completeness, cutoff, source identity, citation permissions and all
other evidence requirements remain enforced. Unresolved events still block.

Every required macro/Strategy draft must cite the report and include
`未独立核实` in its rationale; any other draft citing that report must also retain
the marker. The v2 instructions require attributed, conditional interpretation,
not a claim that an official announcement was independently verified. Common
finalization rejects omitted warnings before persistence and deterministically
labels the CIO signal and stored forecast rationale as a degraded prediction.
Probabilities are not assigned by this exception, and independent semantic review
still applies. A missing critical input can still produce `not_ready`.

## Partial evidence for new premarket tasks

An operator may explicitly seal `EvidenceCoverageV3` with
`availability_policy=partial-evidence/v1` in a new premarket v4 handoff.
Missing, failed, stale or short collection receipts and unresolved policy
originals become disclosed gaps rather than blocking a probability forecast.
The model must distinguish leads from verified facts and explain missing inputs.
Verified events still require supporting citations. Snapshot contents, source
hashes, cutoff, Wiki bindings, persistence and finalization deadline remain strict.
The snapshot still requires usable news, global-equity and FX/rates inputs;
this policy does not fabricate absent observations or permit empty snapshots.

When gaps exist the deterministic finalizer shrinks Strategy probabilities at
least 35% toward a uniform baseline, labels the stored rationale and owner brief
`证据不完整：降级概率预测`, and includes the gap identities. This is a conservative
uncertainty adjustment, not an empirically calibrated probability guarantee.
No historical handoff or forecast is upgraded in place. Late retries cannot
produce a backdated premarket forecast. Existing v1–v3 premarket gates retain
their original semantics.
