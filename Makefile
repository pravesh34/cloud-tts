VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
GATEWAY_URL ?= http://127.0.0.1:8000
WORKER_TOKEN ?= dev-worker-token

.PHONY: help setup dev fake-worker test icons secrets clean

help:
	@echo "make setup        create .venv and install dev dependencies"
	@echo "make dev          run the gateway on http://127.0.0.1:8000"
	@echo "make fake-worker  run a local worker (macOS 'say') against the gateway"
	@echo "make test         run the pytest suite"
	@echo "make icons        regenerate PWA icons"
	@echo "make secrets      print random USER_API_KEY / WORKER_TOKEN values"

setup:
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements-dev.txt

dev:
	$(UVICORN) app:app --app-dir gateway --reload --port 8000

fake-worker:
	GATEWAY_URL=$(GATEWAY_URL) WORKER_TOKEN=$(WORKER_TOKEN) $(PY) scripts/fake_worker.py

test:
	$(PY) -m pytest -q

icons:
	python3 scripts/generate_icons.py

secrets:
	@$(PY) -c "import secrets; print('USER_API_KEY=' + secrets.token_urlsafe(32)); print('WORKER_TOKEN=' + secrets.token_urlsafe(32))"

clean:
	rm -rf .pytest_cache .data
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
