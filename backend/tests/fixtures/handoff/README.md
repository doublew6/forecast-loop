# Handoff compatibility fixtures

`pre-upgrade-v2-input.json` and `pre-upgrade-v2-db-seal.json` are the
byte-for-byte `input.json` and immutable `WorkflowRun` seal emitted by public
commit `d1f39eb89d01316895cf040c02701cc4b4bf8dd2` before handoff v3 existed. They
use only the synthetic Demo evidence and Wiki content from the public test
suite.

Their SHA-256 digests are:

- `pre-upgrade-v2-input.json`:
  `cb4cc7a60e8d9250d0c2f063a8e8b2b7ec8d063f3db2eff8414071131a25288d`
- `pre-upgrade-v2-db-seal.json`:
  `f5c9eca628f3e0ca2ebbc2086627c6e99bae94e6b8eb54bdee70be11b786b762`

The compatibility test installs these frozen bytes and their matching
database-side seal directly. Do not regenerate the fixture through the current
handoff writer: its purpose is to prove that a package already frozen by the
v2 writer remains finalizable after an upgrade.

`pre-upgrade-v2-configurable-input.json` and
`pre-upgrade-v2-configurable-db-seal.json` were captured independently from
`ebe69b15060f358668a9e7fe0fab09eb883fe2e8`, the first public release commit
that shipped configurable-universe handoff v2. They freeze the synthetic
`v2-compatible-universe` request at workflow/schema `0.4.0` / `0.5.0`; the
current writer is not involved in their compatibility test.

Their SHA-256 digests are:

- `pre-upgrade-v2-configurable-input.json`:
  `02710e7a13fb816a236c81d73a5138ad8020fd5852fe96988aaf345ca547a311`
- `pre-upgrade-v2-configurable-db-seal.json`:
  `8fb9c9eb46505fbfb63bc7d799151dac506f4732ec01a9d09fc140305e331eb2`
