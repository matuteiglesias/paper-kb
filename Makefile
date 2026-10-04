SHELL := /bin/bash

CORPUS ?= tesislcd
SOURCE_DIR ?=
REPLACE ?=
TOP_LEVEL_ONLY ?=
FIXTURE_LEVEL ?= metadata
ALLOW_TEXT_DERIVATIVES ?=
FIXTURE_REPLACE ?= $(REPLACE)
PORT ?= 9000
FRONTEND_PORT ?= 3000
MAX_FILES ?=
MIN_LEN ?= 50
CHUNK_SET_DIR ?=

DOCTOR_CMD = python3 -m pipeline.adapter.manager doctor --corpus $(CORPUS)
GROBID_CHECK_CMD = python3 -m pipeline.adapter.grobid_preflight
GROBID_CMD = python3 -m pipeline.adapter.manager grobid --corpus $(CORPUS) --recursive $(if $(MAX_FILES),--max-files $(MAX_FILES),)
PARSE_CMD = python3 -m pipeline.adapter.manager parse --corpus $(CORPUS) --min-len $(MIN_LEN) $(if $(CHUNK_SET_DIR),--chunk-set-dir $(CHUNK_SET_DIR),)
VALIDATE_CMD = python3 -m pipeline.adapter.manager doctor --corpus $(CORPUS) --strict --json
REGISTER_CMD = python3 -m pipeline.sources.corpus_intake register --corpus $(CORPUS) --source-dir "$(SOURCE_DIR)" $(if $(REPLACE),--replace,) $(if $(TOP_LEVEL_ONLY),--top-level-only,)
REQUIRE_PDFS_CMD = python3 -m pipeline.sources.corpus_intake require-pdfs --corpus $(CORPUS)
FIXTURE_CMD = python3 -m pipeline.sources.corpus_fixture --corpus $(CORPUS) --level $(FIXTURE_LEVEL) $(if $(ALLOW_TEXT_DERIVATIVES),--allow-text-derivatives,) $(if $(FIXTURE_REPLACE),--replace,)
EXPORT_REVIEW_RECORDS_CMD = python3 -m pipeline.projections.review_records --corpus $(CORPUS)
EXPORT_CATALOG_RECORDS_CMD = python3 -m pipeline.projections.catalog_records --corpus $(CORPUS)
EXPORT_REVIEW_CSV_CMD = python3 -m backend.exports.export_review_csv --corpus $(CORPUS)
API_CORPUS_CMD = PAPER_KB_CORPUS=$(CORPUS) PAPER_KB_CHUNK_SETS_DIR=corpora/$(CORPUS)/chunk_sets STORAGE_BACKEND=chunk_set uvicorn backend.app.main:app --reload --port $(PORT)

.PHONY: help corpus-register corpus-register-dry-run corpus-check-input corpus-check-grobid corpus-build corpus-fixture corpus-doctor corpus-grobid corpus-parse corpus-validate corpus-parse-validate contract-review-record contract-catalog-record architecture-check read-model-identity export-review-records export-catalog-records export-review-catalog-records export-review-csv export-review api-corpus frontend-prepare frontend-dev kill-port legacy-smoke legacy-run-all legacy-run

help:
	@echo "Operator targets (run from repo root):"
	@echo "  make corpus-register CORPUS=my-corpus SOURCE_DIR=/path/to/pdfs"
	@echo "  make corpus-register-dry-run CORPUS=my-corpus SOURCE_DIR=/path/to/pdfs"
	@echo "  make corpus-check-input CORPUS=my-corpus         # fail unless at least one PDF is present"
	@echo "  make corpus-check-grobid                         # fail unless configured GROBID is reachable"
	@echo "  make corpus-build CORPUS=my-corpus              # full GROBID -> chunk_set -> projections"
	@echo "  make corpus-fixture CORPUS=my-corpus            # metadata-only repository fixture"
	@echo "  make corpus-fixture CORPUS=my-corpus FIXTURE_LEVEL=consumer ALLOW_TEXT_DERIVATIVES=1"
	@echo "  make corpus-doctor CORPUS=tesislcd"
	@echo "  make corpus-grobid CORPUS=tesislcd MAX_FILES=2"
	@echo "  make corpus-parse CORPUS=tesislcd"
	@echo "  make corpus-validate CORPUS=tesislcd"
	@echo "  make contract-review-record"
	@echo "  make contract-catalog-record"
	@echo "  make architecture-check                    # executable modular-monorepo boundary rules"
	@echo "  make read-model-identity                   # paper_uid survives chunk_set -> read/API model"
	@echo "  make export-review-records CORPUS=tesislcd  # review-oriented paper.review-record@1 JSONL"
	@echo "  make export-catalog-records CORPUS=tesislcd # bibliography/catalog paper.catalog-record@1 JSONL"
	@echo "  make api-corpus CORPUS=tesislcd PORT=9000"
	@echo "  make frontend-prepare                         # verify/refresh locked Next dependencies"
	@echo "  make frontend-dev PORT=9000 FRONTEND_PORT=3000 # API port 9000, Next workbench port 3000"
	@echo "  make kill-port PORT=9000"
	@echo ""
	@echo "Registration flags: REPLACE=1 replaces an existing input snapshot and clears stale derived outputs; TOP_LEVEL_ONLY=1 disables recursive PDF discovery."
	@echo "Fixture flags: FIXTURE_LEVEL=metadata|consumer; consumer requires ALLOW_TEXT_DERIVATIVES=1; REPLACE=1 replaces an existing fixture (FIXTURE_REPLACE=1 remains supported)."
	@echo "Workbench ports: PORT selects the Paper KB API port; FRONTEND_PORT selects the Next dev-server port (default 3000)."
	@echo "Workbench dev bundler: frontend-dev uses Next's supported --webpack opt-out while the pinned Next 16.0.3 Turbopack dev failure is handled separately."
	@echo "GROBID endpoint: set GROBID_URL to override the default http://localhost:8070/api/processFulltextDocument."
	@echo ""
	@echo "Compatibility targets:"
	@echo "  make export-review-csv CORPUS=tesislcd       # legacy/convenience CSV"
	@echo "  make export-review CORPUS=tesislcd           # deprecated alias for export-review-csv"
	@echo ""
	@echo "Legacy placeholders retained as explicit legacy-* targets."

corpus-register:
	@test -n "$(SOURCE_DIR)" || { echo "SOURCE_DIR is required"; exit 2; }
	echo $(REGISTER_CMD)
	$(REGISTER_CMD)

corpus-register-dry-run:
	@test -n "$(SOURCE_DIR)" || { echo "SOURCE_DIR is required"; exit 2; }
	echo $(REGISTER_CMD) --dry-run
	$(REGISTER_CMD) --dry-run

corpus-check-input:
	echo $(REQUIRE_PDFS_CMD)
	$(REQUIRE_PDFS_CMD)

corpus-check-grobid:
	echo $(GROBID_CHECK_CMD)
	$(GROBID_CHECK_CMD)

corpus-build:
	$(MAKE) corpus-check-input CORPUS=$(CORPUS)
	$(MAKE) corpus-doctor CORPUS=$(CORPUS)
	$(MAKE) corpus-grobid CORPUS=$(CORPUS) MAX_FILES=
	$(MAKE) corpus-parse CORPUS=$(CORPUS)
	$(MAKE) corpus-validate CORPUS=$(CORPUS)
	$(MAKE) export-review-records CORPUS=$(CORPUS)
	$(MAKE) export-catalog-records CORPUS=$(CORPUS)

corpus-fixture:
	echo $(FIXTURE_CMD)
	$(FIXTURE_CMD)

corpus-doctor:
	echo $(DOCTOR_CMD)
	$(DOCTOR_CMD)

corpus-grobid: corpus-check-grobid
	echo $(GROBID_CMD)
	$(GROBID_CMD)

corpus-parse:
	echo $(PARSE_CMD)
	$(PARSE_CMD)

corpus-validate:
	echo $(VALIDATE_CMD)
	$(VALIDATE_CMD)

corpus-parse-validate: corpus-parse corpus-validate

contract-review-record:
	python3 tests/test_review_record_contract.py

contract-catalog-record:
	python3 tests/test_catalog_record_contract.py
	python3 tests/test_catalog_record_projection.py

architecture-check:
	python3 tests/test_component_boundaries.py

read-model-identity:
	python3 tests/test_read_model_identity.py

export-review-records:
	echo $(EXPORT_REVIEW_RECORDS_CMD)
	$(EXPORT_REVIEW_RECORDS_CMD)

export-catalog-records:
	echo $(EXPORT_CATALOG_RECORDS_CMD)
	$(EXPORT_CATALOG_RECORDS_CMD)

export-review-catalog-records: export-review-records export-catalog-records

export-review-csv:
	@echo "[COMPATIBILITY] CSV review export; preferred machine interface for review is export-review-records."
	echo $(EXPORT_REVIEW_CSV_CMD)
	$(EXPORT_REVIEW_CSV_CMD)

export-review:
	@echo "[DEPRECATED ALIAS] use 'make export-review-csv CORPUS=$(CORPUS)' or, preferably, 'make export-review-records CORPUS=$(CORPUS)'."
	$(MAKE) export-review-csv CORPUS=$(CORPUS)

api-corpus:
	python3 -c "import socket; s=socket.socket(); s.settimeout(0.2); busy=(s.connect_ex(('127.0.0.1', int('$(PORT)')))==0); s.close(); import sys; sys.exit(1 if busy else 0)" || { echo "Port $(PORT) is occupied. Run: make kill-port PORT=$(PORT)"; exit 2; }
	echo $(API_CORPUS_CMD)
	$(API_CORPUS_CMD)

frontend-prepare:
	@set -euo pipefail; \
	expected=$$(sha256sum frontend/package-lock.json | awk '{print $$1}'); \
	actual=$$(cat frontend/node_modules/.paper-kb-package-lock.sha256 2>/dev/null || true); \
	expected_next=$$(node -p "require('./frontend/package-lock.json').packages['node_modules/next'].version"); \
	actual_next=$$(node -e "try { process.stdout.write(require('./frontend/node_modules/next/package.json').version) } catch (_) {} "); \
	if [ "$$expected" = "$$actual" ] && [ "$$expected_next" = "$$actual_next" ]; then \
		echo "Frontend dependencies ready (Next $$actual_next, locked install)."; \
		exit 0; \
	fi; \
	echo "Refreshing frontend dependencies from package-lock.json"; \
	rm -rf frontend/node_modules frontend/.next; \
	cd frontend; \
	npm ci; \
	actual_next=$$(node -p "require('./node_modules/next/package.json').version"); \
	test "$$actual_next" = "$$expected_next" || { echo "Next install mismatch: expected $$expected_next, got $$actual_next"; exit 2; }; \
	printf '%s\n' "$$expected" > node_modules/.paper-kb-package-lock.sha256

frontend-dev: frontend-prepare
	python3 -c "import socket; s=socket.socket(); s.settimeout(0.2); busy=(s.connect_ex(('127.0.0.1', int('$(FRONTEND_PORT)')))==0); s.close(); import sys; sys.exit(1 if busy else 0)" || { echo "Frontend port $(FRONTEND_PORT) is occupied. Run: make kill-port PORT=$(FRONTEND_PORT)"; exit 2; }
	echo "cd frontend && PORT=$(FRONTEND_PORT) NEXT_PUBLIC_API_BASE=http://127.0.0.1:$(PORT) npm run dev -- --webpack"
	cd frontend && PORT=$(FRONTEND_PORT) NEXT_PUBLIC_API_BASE=http://127.0.0.1:$(PORT) npm run dev -- --webpack

kill-port:
	echo "Killing listeners on port $(PORT)"
	pids=$$(lsof -ti tcp:$(PORT) 2>/dev/null || true); \
	if [ -z "$$pids" ]; then echo "No process is listening on port $(PORT)"; exit 0; fi; \
	echo "kill $$pids"; \
	kill $$pids

legacy-smoke:
	echo "[LEGACY] smoke placeholder removed; use corpus-doctor/corpus-validate instead."
	exit 2

legacy-run-all:
	echo "[LEGACY] run_all placeholder removed; use corpus-grobid + corpus-parse + api-corpus."
	exit 2

legacy-run: legacy-run-all