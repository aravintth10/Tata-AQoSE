"""Builds the Kaggle notebooks from pcap_flow_tables.py.

    python kaggle/build_notebook.py

Writes datasets_flow_tables.ipynb with the registration fields empty, which
is safe to commit. If kaggle/registration.local.json exists, also writes
datasets_flow_tables.local.ipynb with them filled in: import that one into
Kaggle. Both *.local.* files are git-ignored.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
FIELDS = ["first_name", "last_name", "email", "institution", "job_title", "country"]
INTRO = """# Public traffic datasets to flow tables

Fetches the datasets listed at the top of the last cell and reduces them to small per-flow tables. As it stands that is the UNSW cloud gaming sample: about 270 captures, 157 GB, several hours.

1. Settings > **Turn on internet**.
2. Check the six fields in the first code cell.
3. **Save Version > Save & Run All (Commit)**.

Results appear under Output: one `flows-<dataset>.jsonl.gz` and one `audit-<dataset>.csv` per dataset. Keep this notebook private."""


def registration_cell(values):
    lines = ["# What the UNB datasets' own registration form asks for.",
             "# Keep this notebook private.", "REGISTRATION = {"]
    lines += [f"    {json.dumps(k)}: {json.dumps(values.get(k, ''))}," for k in FIELDS]
    return "\n".join(lines + ["}"]) + "\n"


def build(values, name):
    def cell(kind, text):
        body = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code":
            body.update(execution_count=None, outputs=[])
        return body

    notebook = {
        "cells": [cell("markdown", INTRO), cell("code", registration_cell(values)),
                  cell("code", (HERE / "pcap_flow_tables.py").read_text())],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5,
    }
    (HERE / name).write_text(json.dumps(notebook, indent=1))
    print("wrote", HERE / name)


build({}, "datasets_flow_tables.ipynb")
local = HERE / "registration.local.json"
if local.exists():
    build(json.loads(local.read_text()), "datasets_flow_tables.local.ipynb")
