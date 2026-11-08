PYTHON ?= python3
COMPOSE = docker compose

.PHONY: help test up down smoke load chaos latency recover logs check-config test-rules

help:
	@echo "up / down       — start / stop local lab"
	@echo "test / smoke    — unit / running service checks"
	@echo "load            — 300 successful requests"
	@echo "chaos / latency — 3 minute incident drills (Ctrl+C to stop)"
	@echo "recover         — 5.5 minutes of successful traffic"
	@echo "check-config / test-rules / logs"

test:
	$(PYTHON) -m unittest discover -s tests -v

up:
	$(COMPOSE) up --build -d --wait

down:
	$(COMPOSE) down

smoke:
	$(PYTHON) scripts/smoke.py

load:
	$(PYTHON) scripts/load.py --requests 300

chaos:
	$(PYTHON) scripts/load.py --duration 180 --failure-every 4

latency:
	$(PYTHON) scripts/load.py --duration 180 --delay-ms 600

recover:
	$(PYTHON) scripts/load.py --duration 330

logs:
	$(COMPOSE) logs --tail=80

check-config:
	$(COMPOSE) config --quiet
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml

test-rules:
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus test rules /etc/prometheus/rules.test.yml
