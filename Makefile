PYTHON ?= python3
RUFF ?= .venv/bin/ruff

.PHONY: test lint package

test:
	PYTHONPATH=iot-digital-twin-ingest $(PYTHON) -m unittest discover -s iot-digital-twin-ingest/tests -v

lint:
	$(RUFF) check iot-digital-twin-ingest scripts
	$(RUFF) format --check iot-digital-twin-ingest scripts

package:
	$(PYTHON) scripts/package.py
