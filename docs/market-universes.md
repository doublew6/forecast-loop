# Versioned Market Universes

A `forecast-loop.market-universe/v1` document defines the targets and market
clock for a run. The same public contract can describe indexes or individual
equities without importing provider-specific code into the core.

Example files are available under `examples/market-universes/`.

## Contract

Each Universe declares:

- a stable `universe_id` and semantic `version`;
- market, timezone, exchange calendar, currency, and session close;
- ordered forecast horizons;
- one or more instruments with stable codes and display names;
- optional asset type, sector, strategy bucket, tags, and Wiki binding;
- optional per-instrument Agent briefs;
- a canonical content hash over the complete document.

The loader rejects duplicate instruments, invalid timezones, unsupported
horizons, unresolved Wiki bindings, inconsistent metadata, and incorrect
hashes.

## Run binding

Every run stores the selected Universe identity and content hash. Forecasts,
Signal Envelopes, Evidence Snapshots, evaluations, scorecards, and exported
bundles must refer to the same ordered instrument set.

A change to instruments, market clock, Wiki mapping, or Agent briefs creates a
new Universe version and hash. It never rewrites historical runs.

## Evidence and outcomes

Configuring a Universe does not download or license data. A Live run requires
a read-only adapter that returns the public Evidence Snapshot contract for the
complete target set. Outcome evaluation likewise requires a reviewed,
hash-sealed market snapshot.

Provider credentials, licensed fields, quality rules, and directory layouts
remain outside the public repository. Adapters may read their authorized
sources, but they must write only the requested snapshot output and must never
write back to a source owner.

## Quant signals

The generic Quant port accepts a sealed bundle whose targets and
`market_universe_hash` exactly match the run. The public core verifies the
bundle and keeps Quant participation shadow-only unless a separate,
versioned governance policy explicitly activates it.

Training libraries, feature definitions, model parameters, and production
data exports are extension concerns and are not shipped by the public core.

## Scheduling

Market close, timezone, daylight-saving transitions, and evidence cutoff come
from the selected Universe and run inputs. The public repository deliberately
does not prescribe a host, scheduler, model, or execution time. Operators are
responsible for scheduling preparation after their target market's data is
complete.

## Adding an example

An example Universe must:

1. contain only public metadata;
2. use no credentials, licensed data, or operator paths;
3. bind every referenced Wiki entry that is included in the repository;
4. pass deterministic schema and content-hash validation;
5. make clear that the file configures identity and time, not data access.
