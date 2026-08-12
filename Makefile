.PHONY: install install-hooks public-preflight public-preflight-staged public-preflight-range dev backend worker frontend test lint build release-artifacts release-history-audit demo benchmark-verify agent-eval agent-eval-list agent-eval-prepare agent-eval-finalize agent-eval-status research-v2-prepare research-v2-finalize research-v2-shadow-manual research-v2-shadow-quant research-v2-reasoning-finalize research-v2-shadow-reasoning-finalize research-v2-reasoning-review research-v2-evaluate research-v2-reflection-create research-v2-reflection-review research-v2-activate snapshot prediction-snapshot codex-prepare codex-finalize codex-retry reflection-prepare reflection-freeze-sources reflection-finalize reflection-review reflection-render lesson-replay lesson-approve lesson-revalidate lesson-due lesson-verify judgment-record judgment-export judgment-verify market-snapshot market-import market-block migrate database-status migration-smoke recovery-backup recovery-verify recovery-restore docker-config docker-smoke docker-up docker-down

PYTHON ?= .venv/bin/python
RELEASE_VERSION ?= 0.1.0
RELEASE_OUTPUT ?= dist/release/v$(RELEASE_VERSION)

install:
	uv sync --frozen
	uv sync --frozen --reinstall-package forecast-loop
	cd frontend && npm ci

install-hooks:
	git config core.hooksPath .githooks
	@if test -n "$(PRIVATE_BOUNDARY_FILE)"; then \
		case "$(PRIVATE_BOUNDARY_FILE)" in /*) ;; *) echo "PRIVATE_BOUNDARY_FILE must be absolute" >&2; exit 2;; esac; \
		git config forecastloop.privateBoundaryFile "$(PRIVATE_BOUNDARY_FILE)"; \
		git config forecastloop.privateBoundaryRequired true; \
	else \
		git config --unset-all forecastloop.privateBoundaryFile >/dev/null 2>&1 || true; \
		git config forecastloop.privateBoundaryRequired false; \
		echo "Installed generic public hooks; maintainers must reinstall with PRIVATE_BOUNDARY_FILE=/absolute/private-patterns"; \
	fi

public-preflight:
	PYTHONPATH=. $(PYTHON) scripts/audit_public_boundary.py --repository .

public-preflight-staged:
	PYTHONPATH=. $(PYTHON) scripts/audit_public_boundary.py --repository . --staged

public-preflight-range: public-preflight release-history-audit

dev:
	@echo "Run 'make backend', 'make worker', and 'make frontend' in separate terminals."

backend:
	$(PYTHON) -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000

worker:
	PYTHONPATH=backend $(PYTHON) -m app.cli worker run $(ARGS)

frontend:
	cd frontend && npm run dev

test:
	uv run pytest
	cd frontend && npm test -- --run

lint:
	uv run ruff check backend scripts examples
	cd frontend && npm run lint

build:
	cd frontend && npm run build

release-artifacts:
	$(PYTHON) scripts/build_release_artifacts.py --version "$(RELEASE_VERSION)" --output-dir "$(RELEASE_OUTPUT)"

release-history-audit:
	PYTHONPATH=. $(PYTHON) scripts/audit_release_history.py --repository . --public-gate

demo:
	PYTHONPATH=backend $(PYTHON) -m app.demo

benchmark-verify:
	PYTHONPATH=backend $(PYTHON) -m app.cli benchmark verify benchmarks/cross-source-v1

agent-eval:
	@test -n "$(ARGS)" || (echo 'usage: make agent-eval ARGS="--suite agent-workflow-v1 --baseline baseline-v1 --candidate candidate-v2"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli agent-eval run $(ARGS)

agent-eval-list:
	PYTHONPATH=backend $(PYTHON) -m app.cli agent-eval list

agent-eval-prepare:
	@test -n "$(ARGS)" || (echo 'usage: make agent-eval-prepare ARGS="--suite SUITE --baseline BASELINE --candidate CANDIDATE [--source private]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli agent-eval prepare $(ARGS)

agent-eval-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make agent-eval-finalize ARGS="/absolute/job/path/from-prepare"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli agent-eval finalize $(ARGS)

agent-eval-status:
	@test -n "$(ARGS)" || (echo 'usage: make agent-eval-status ARGS="/absolute/job/path/from-prepare"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli agent-eval status $(ARGS)

research-v2-prepare:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-prepare ARGS="--mode demo|live --snapshot /absolute/snapshot.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 prepare $(ARGS)

research-v2-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-finalize ARGS="/absolute/job/path"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 finalize $(ARGS)

research-v2-shadow-manual:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-shadow-manual ARGS="/absolute/manual-shadow-input.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 shadow-manual $(ARGS)

research-v2-shadow-quant:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-shadow-quant ARGS="RUN_ID --root /trusted/bundle/root --manifest relative/manifest.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 shadow-quant $(ARGS)

research-v2-reasoning-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-reasoning-finalize ARGS="/absolute/job/path"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 reasoning-finalize $(ARGS)

research-v2-shadow-reasoning-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-shadow-reasoning-finalize ARGS="/absolute/shadow-reasoning/job/path"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 shadow-reasoning-finalize $(ARGS)

research-v2-reasoning-review:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-reasoning-review ARGS="REVIEW_ID --decision approved|rejected --reviewer NAME"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 reasoning-review $(ARGS)

research-v2-evaluate:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-evaluate ARGS="/absolute/outcome.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 evaluate $(ARGS)

research-v2-reflection-create:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-reflection-create ARGS="/absolute/reflection.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 reflection-create $(ARGS)

research-v2-reflection-review:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-reflection-review ARGS="REFLECTION_ID --decision approved|rejected --reviewer NAME"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 reflection-review $(ARGS)

research-v2-activate:
	@test -n "$(ARGS)" || (echo 'usage: make research-v2-activate ARGS="--agent-eval-report /absolute/report.json --actor NAME"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli research-v2 activate $(ARGS)

snapshot:
	@test -n "$(DRAFT)" -a -n "$(OUTPUT)" || (echo "usage: make snapshot DRAFT=path OUTPUT=path" && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/build_snapshot.py "$(DRAFT)" "$(OUTPUT)"

prediction-snapshot:
	@test -n "$(ARGS)" || (echo 'usage: make prediction-snapshot ARGS="--base-session YYYY-MM-DD --captured-at ISO8601 --output PATH"' && exit 2)
	@test -n "$(EVIDENCE_SNAPSHOT_BUILDER)" || (echo 'set EVIDENCE_SNAPSHOT_BUILDER to an executable adapter' && exit 2)
	$(EVIDENCE_SNAPSHOT_BUILDER) $(ARGS)

codex-prepare:
	PYTHONPATH=backend $(PYTHON) scripts/codex_handoff.py prepare $(ARGS)

codex-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make codex-finalize ARGS="[--mode demo|live] /absolute/job/path/from-prepare"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/codex_handoff.py finalize $(ARGS)

codex-retry:
	@test -n "$(ARGS)" || (echo 'usage: make codex-retry ARGS="[--mode demo|live] /absolute/failed-v3-job/path"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/codex_handoff.py retry $(ARGS)

reflection-prepare:
	@test -n "$(ARGS)" || (echo 'usage: make reflection-prepare ARGS="<source-run-id> --horizon D1|D2 --market-snapshot /absolute/path.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/reflection_handoff.py prepare $(ARGS)

reflection-freeze-sources:
	@test -n "$(ARGS)" || (echo 'usage: make reflection-freeze-sources ARGS="<job-dir> [--sources /absolute/captures.json]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/reflection_handoff.py freeze-sources $(ARGS)

reflection-finalize:
	@test -n "$(ARGS)" || (echo 'usage: make reflection-finalize ARGS="<job-dir>"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/reflection_handoff.py finalize $(ARGS)

reflection-review:
	@test -n "$(ARGS)" || (echo 'usage: make reflection-review ARGS="<reflection-id> --decision approved|rejected --reviewer NAME [--notes-file PATH]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/reflection_handoff.py review $(ARGS)

reflection-render:
	@test -n "$(ARGS)" || (echo 'usage: make reflection-render ARGS="<reflection-id>"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/render_reflection_markdown.py $(ARGS)

lesson-replay:
	@test -n "$(ARGS)" || (echo 'usage: make lesson-replay ARGS="/absolute/replay.json --submitted-by NAME [--recorded-at ISO8601]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/lesson_lifecycle.py replay $(ARGS)

lesson-approve:
	@test -n "$(ARGS)" || (echo 'usage: make lesson-approve ARGS="<lesson-id> --reviewer NAME --notes-file /absolute/notes.md [--supersedes ID]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/lesson_lifecycle.py approve $(ARGS)

lesson-revalidate:
	@test -n "$(ARGS)" || (echo 'usage: make lesson-revalidate ARGS="<lesson-id> --reviewer NAME --notes-file /absolute/notes.md [--checklist-valid true|false]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/lesson_lifecycle.py revalidate $(ARGS)

lesson-due:
	PYTHONPATH=backend $(PYTHON) scripts/lesson_lifecycle.py due $(ARGS)

lesson-verify:
	@test -n "$(ARGS)" || (echo 'usage: make lesson-verify ARGS="<lesson-id>"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/lesson_lifecycle.py verify $(ARGS)

judgment-record:
	@test -n "$(ARGS)" || (echo 'usage: make judgment-record ARGS="--forecast-id ID --direction up|down --confidence 0.6 --rationale-file PATH --counter-evidence-file PATH --invalidation-file PATH [--blind]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli judgment record $(ARGS)

judgment-export:
	@test -n "$(ARGS)" || (echo 'usage: make judgment-export ARGS="<judgment-id> [--output-root PATH] [--include-actor-id]"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli judgment export $(ARGS)

judgment-verify:
	@test -n "$(ARGS)" || (echo 'usage: make judgment-verify ARGS="<judgment-id|bundle-path>"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli judgment verify $(ARGS)

market-snapshot:
	@test -n "$(ARGS)" || (echo 'usage: make market-snapshot ARGS="--target-date YYYY-MM-DD --horizon D1|D2 --captured-at ISO8601 --output PATH"' && exit 2)
	@test -n "$(MARKET_OUTCOME_SNAPSHOT_BUILDER)" || (echo 'set MARKET_OUTCOME_SNAPSHOT_BUILDER to an executable adapter' && exit 2)
	$(MARKET_OUTCOME_SNAPSHOT_BUILDER) $(ARGS)

market-import:
	@test -n "$(ARGS)" || (echo 'usage: make market-import ARGS="import /absolute/market-snapshot.json"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/market_outcome.py $(ARGS)

market-block:
	@test -n "$(ARGS)" || (echo 'usage: make market-block ARGS="block --target-date YYYY-MM-DD --horizon D1|D2 --reason-code CODE --error MESSAGE"' && exit 2)
	PYTHONPATH=backend $(PYTHON) scripts/market_outcome.py $(ARGS)

migrate:
	PYTHONPATH=backend $(PYTHON) -m app.cli database migrate $(ARGS)

database-status:
	PYTHONPATH=backend $(PYTHON) -m app.cli database status --deep $(ARGS)

migration-smoke:
	PYTHONPATH=backend $(PYTHON) scripts/migration_smoke.py

recovery-backup:
	@test -n "$(ARGS)" || (echo 'usage: make recovery-backup ARGS="--database PATH --checkpoint PATH --root NAME=PATH --output-root PATH"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli recovery backup $(ARGS)

recovery-verify:
	@test -n "$(ARGS)" || (echo 'usage: make recovery-verify ARGS="/absolute/backup-bundle"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli recovery verify $(ARGS)

recovery-restore:
	@test -n "$(ARGS)" || (echo 'usage: make recovery-restore ARGS="/absolute/backup-bundle --target-root /empty/isolated/path"' && exit 2)
	PYTHONPATH=backend $(PYTHON) -m app.cli recovery restore $(ARGS)

docker-config:
	VERICOUNCIL_ENV_FILE=.env.example docker compose config --quiet

docker-smoke:
	$(PYTHON) scripts/docker_smoke.py

docker-up:
	docker compose up --build

docker-down:
	docker compose down
