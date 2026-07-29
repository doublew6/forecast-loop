## Summary

Describe the problem and the scoped solution.

## Verification

- [ ] Backend tests and Ruff pass, or the exception is documented.
- [ ] Frontend test, lint and build pass when affected.
- [ ] Database changes include and verify an Alembic migration.
- [ ] User-visible or contract changes update documentation and `CHANGELOG.md`.

## Trust boundary

- [ ] Model output remains an untrusted draft until deterministic validation.
- [ ] Data adapters are read-only and fail closed on unverifiable inputs.
- [ ] No trade execution, account control, secret, private handoff or licensed
      production data is introduced.
- [ ] No personal path, private hostname/network, machine inventory, internal
      schedule, private schema, repository, project relationship or research
      identifier is introduced.
- [ ] New and changed fixtures are synthetic or have documented redistribution
      permission; logs and generated metadata were not copied into the change.
- [ ] `make public-preflight-staged` and `make public-preflight-range` pass.
- [ ] Public deployment changes preserve authentication/TLS requirements and do
      not expose management or finalize endpoints.

## Compatibility

Describe impacts on the public CLI, schemas, saved data, generic deployment and
third-party adapters.
