.PHONY: help test lint scrape
.DEFAULT_GOAL := help

PY := .venv/bin/python

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

test:  ## run the test suite (offline, no live calls, no spend)
	$(PY) -m pytest -q

lint:  ## check style
	$(PY) -m ruff check src tests

scrape:  ## one board: make scrape BOARD=mcf
	$(PY) -m bto collect --board $(BOARD)
