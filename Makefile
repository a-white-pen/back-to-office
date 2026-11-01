.PHONY: help test lint scrape

PY := .venv/bin/python

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

test:  ## offline tests, including private/tests when present
	$(PY) -m pytest -q tests $(wildcard private/tests)

lint:  ## check style
	$(PY) -m ruff check src tests $(wildcard private/tests)

scrape:  ## collect one board: make scrape BOARD=mcf
	$(PY) -m bto collect --board $(BOARD)
