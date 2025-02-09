PYTHON=python3
PYTHONFLAGS=-W error
PIP=pip3
PIPFLAGS=--upgrade
PYLINTFLAGS=
NPM=npm
NPMFLAGS=--no-package-lock

.PHONY: test
test:
	$(PYTHON) $(PYTHONFLAGS) -m unittest

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
	$(NPM) update $(NPMFLAGS)

.PHONY: dependencies-dev
dependencies-dev:
	$(PIP) install $(PIPFLAGS) --requirement=requirements-dev.txt

.PHONY: clean
clean:
	rm -rf $$(find -name __pycache__) .mypy_cache streamfarer/res/static/vendor node_modules
