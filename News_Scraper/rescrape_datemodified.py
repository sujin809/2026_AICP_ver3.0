#!/usr/bin/env python3
"""Re-fetch each study-window article URL to record its ``dateModified``.

The runtime news CSVs only carry the first-published ("입력") time.  For a
study that gates news by its *final edited* version, we must know each
article's ``dateModified`` ("수정") too, and expose the article only from
``effective = max(datePublished, dateModified)`` onward.

This script reads the re-scrape list (article_id, url, ...), visits each URL,
reads schema.org ``datePublished`` / ``dateModified`` from the article's
JSON-LD, and writes the results back in place.  It is polite (per-request
delay), resumable (rows already marked ``ok`` are skipped), and never edits
the source pickle.  Run it yourself — it performs network requests.

    python News_Scraper/rescrape_datemodified.py \
        --list preparation/rn_ab_source_candidate_v1/STUDY_WINDOW_NEWS_TO_RESCRAPE.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_mk import HEADERS, get_json_ld_modified_at, get_json_ld_published_at

DEFAULT_LIST = (
    Path(__file__).resolve().parents[1]
    / "preparation/rn_ab_source_candidate_v1/STUDY_WINDOW_NEWS_TO_RESCRAPE.csv"
)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _effective(published: str | None, modified: str | None) -> str:
    """Return ``max(published, modified)`` as ``YYYY-MM-DD HH:MM`` (KST wall clock)."""
    dp, dm = _parse_iso(published), _parse_iso(modified)
    best = max([d for d in (dp, dm) if d is not None], default=None)
    return best.strftime("%Y-%m-%d %H:%M") if best else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST, help="Re-scrape list CSV to update in place.")
    parser.add_argument("--delay", type=float, default=0.7, help="Seconds to wait between requests.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout seconds.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N pending rows (for a trial run).")
    args = parser.parse_args()

    path = Path(args.list)
    if not path.exists():
        parser.exit(2, f"re-scrape list not found: {path}\n")
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
        fieldnames = rows[0].keys() if rows else []
    for col in ("date_published(재수집)", "date_modified(재수집)", "effective_exposure_time(=max)", "rescrape_status"):
        if col not in fieldnames:
            parser.exit(2, f"list is missing expected column: {col}\n")

    def flush() -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(fieldnames))
            writer.writeheader()
            writer.writerows(rows)

    session = requests.Session()
    session.headers.update(HEADERS)
    done = ok = failed = 0
    for row in rows:
        if str(row.get("rescrape_status") or "").startswith("ok"):
            continue  # resume: already fetched
        url = str(row.get("url") or "").strip()
        if not url:
            row["rescrape_status"] = "error: no url"
            failed += 1
            continue
        if args.limit is not None and done >= args.limit:
            break
        done += 1
        try:
            resp = session.get(url, timeout=args.timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            published = get_json_ld_published_at(soup)
            modified = get_json_ld_modified_at(soup)
            row["date_published(재수집)"] = published or ""
            row["date_modified(재수집)"] = modified or ""
            row["effective_exposure_time(=max)"] = _effective(published, modified)
            row["rescrape_status"] = "ok" if published else "ok_no_jsonld_published"
            ok += 1
        except Exception as exc:  # noqa: BLE001
            row["rescrape_status"] = f"error: {type(exc).__name__}: {exc}"[:200]
            failed += 1
        if done % 25 == 0:
            flush()
            print(f"  ... {done} processed (ok={ok}, failed={failed})", flush=True)
        time.sleep(max(0.0, args.delay))

    flush()
    print(f"done. processed={done}, ok={ok}, failed={failed}")
    print(f"updated: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
