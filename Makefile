PYTHON ?= python3
RUFF ?= .venv/bin/ruff

.PHONY: test lint package

test:
	PYTHONPATH=iot-digital-twin-ingest $(PYTHON) -m unittest discover -s iot-digital-twin-ingest/tests -v

lint:
	$(RUFF) check iot-digital-twin-ingest iot-interview-demo scripts
	$(RUFF) format --check iot-digital-twin-ingest iot-interview-demo scripts

package:
	$(PYTHON) scripts/package.py

.PHONY: test-demo
test-demo:
	PYTHONPATH=iot-interview-demo:$(MODEL_REPO)/src $(PYTHON) -m unittest discover -s iot-interview-demo/tests -v

.PHONY: package-demo
package-demo:
	$(PYTHON) scripts/package_demo.py --model-repo $(MODEL_REPO) --model-commit $(shell cat iot-interview-demo/model-commit.txt)
