.PHONY: test demo bench build

test:
	python3 -m pytest

demo:
	python3 -m wallclock demo --out demo

bench:
	python3 scripts/bench.py

build:
	python3 -m pip wheel . --no-deps -w dist
