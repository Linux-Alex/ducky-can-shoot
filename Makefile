# Developer shortcuts. Nothing here is needed to install DCS: see README.md.
# Every target runs from the checkout, without installing anything.

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= ruff
PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH))

.DEFAULT_GOAL := help
.PHONY: help run cli test test-gui lint format build install uninstall doctor clean screenshots

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

run: ## Start the graphical interface from the checkout
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m dcs gui

cli: ## Start the command line interface (pass ARGS=... to add arguments)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m dcs $(ARGS)

test: ## Run the whole test suite (offscreen Qt)
	QT_QPA_PLATFORM=offscreen PYTHONPATH=$(PYTHONPATH) $(PYTEST) -q

test-gui: ## Run only the Qt and QML tests
	QT_QPA_PLATFORM=offscreen PYTHONPATH=$(PYTHONPATH) $(PYTEST) -q tests/test_gui.py

lint: ## Check the code with ruff
	$(RUFF) check src tests

format: ## Apply the safe ruff fixes
	$(RUFF) check src tests --fix

build: ## Build the sdist and the wheel into dist/
	$(PYTHON) -m build

install: ## Install for the current user (~/.local, desktop entry and icon)
	./scripts/install.sh

uninstall: ## Remove the user level installation
	./scripts/uninstall.sh

doctor: ## Check every dependency and the configuration
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m dcs doctor

screenshots: ## Re-render the documentation screenshots (offscreen)
	PYTHONPATH=$(PYTHONPATH) QT_QPA_PLATFORM=offscreen $(PYTHON) scripts/make-screenshots.py

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
