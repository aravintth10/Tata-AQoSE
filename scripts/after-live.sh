#!/usr/bin/env bash
# What the night of Oct 12 left running: seed 2 of the damage table's three
# conditions, then the table. Kept because it shows the order; it is not
# part of the build.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=ml
.venv/bin/python -u -m aqos_ml.damage run --seeds 1-2
.venv/bin/python -m aqos_ml.damage score
