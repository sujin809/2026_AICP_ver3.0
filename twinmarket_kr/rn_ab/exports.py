"""Human-readable, hash-recorded RN result exports."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Mapping, Protocol

from twinmarket_kr.experiment_runtime import file_sha256
from twinmarket_kr.rn_ab.spec import RN_CONDITIONS


class FinalFillExporter(Protocol):
    def export_canonical_final_fill_ledger(
        self,
        path: Path | str,
        *,
        evaluator_contract_sha256: str,
    ) -> Path: ...


def export_final_fill_csvs(
    output_dir: Path | str,
    *,
    evaluator_contract_sha256: str,
    stores: Mapping[str, FinalFillExporter],
) -> Path:
    """Write reviewable final-fill CSVs and a machine-verifiable export index.

    CSV is retained as the reviewer-facing artifact.  The adjacent index pins
    its file hash and row count, so execution/report consumers never infer
    scientific identity from a mutable filename alone.
    """
    if set(stores) != set(RN_CONDITIONS):
        raise ValueError("Final fill export requires both RN conditions")
    if len(evaluator_contract_sha256) != 64:
        raise ValueError("evaluator_contract_sha256 must be a SHA-256 digest")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    exports: dict[str, dict[str, object]] = {}
    for condition_id in RN_CONDITIONS:
        path = root / f"{condition_id.lower()}_final_fill_ledger.csv"
        exported = Path(
            stores[condition_id].export_canonical_final_fill_ledger(
                path,
                evaluator_contract_sha256=evaluator_contract_sha256,
            )
        )
        with exported.open("r", encoding="utf-8", newline="") as handle:
            row_count = sum(1 for _ in csv.DictReader(handle))
        if row_count < 1:
            raise ValueError(f"Empty final-fill CSV for {condition_id}")
        exports[condition_id] = {
            "path": exported.name,
            "sha256": file_sha256(exported),
            "row_count": row_count,
            "format": "rn_canonical_final_fill_csv_v1",
        }
    index = root / "final_fill_export_index.json"
    temporary = index.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "artifact_type": "rn_final_fill_export_index",
                "version": "1",
                "evaluator_contract_sha256": evaluator_contract_sha256,
                "exports": exports,
            },
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(index)
    return index
