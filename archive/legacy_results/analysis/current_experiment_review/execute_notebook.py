#!/usr/bin/env python3
"""Execute the C00 audit notebook in place with the installed nbclient runtime."""

from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
NOTEBOOK_PATH = HERE / "current_experiment_audit.ipynb"


notebook = nbformat.read(NOTEBOOK_PATH, as_version=4)
client = NotebookClient(
    notebook,
    timeout=120,
    kernel_name="python3",
    resources={"metadata": {"path": str(PROJECT_ROOT)}},
)
client.execute()
nbformat.write(notebook, NOTEBOOK_PATH)
print(f"Executed {NOTEBOOK_PATH}")
