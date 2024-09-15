PYTHON=python3
PIP=pip3
PIPFLAGS=--upgrade
PYLINTFLAGS=

.PHONY: test
test:
	$(PYTHON) -m unittest

.PHONY: type
type:
	mypy

.PHONY: lint
lint:
	pylint $(PYLINTFLAGS) streamfarer

.PHONY: check
check: type test lint

.PHONY: dependencies
dependencies:
	$(PIP) install $(PIPFLAGS) --requirement=requirements.txt

.PHONY: dependencies-dev
dependencies-dev:
	$(PIP) install $(PIPFLAGS) --requirement=requirements-dev.txt

.PHONY: clean
clean:
	rm --force --recursive $$(find -name __pycache__) .mypy_cache
