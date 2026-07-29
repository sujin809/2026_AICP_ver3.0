#!/usr/bin/env python3
"""재검토 판정(keep/drop)을 split JSON의 `필터링 여부`에 반영한다.

규약: 다섯 폴더 모두 **N = 유지, Y = 제외** 로 통일한다.
재검토는 기존에 Y(제외)로 판정된 기사만 대상으로 했으므로,
keep 이면 N 으로 되돌리고 drop 이면 Y 를 유지한다.

반영 후 모든 기사가 정확히 'Y' 또는 'N' 하나만 갖는지 검증한다.

    python News_Scraper/apply_recheck.py --dry-run
    python News_Scraper/apply_recheck.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

FOLDERS = [
    "samsung_split",
    "semiconductor_split",
    "macro_economic-policy_split",
    "macro_trade_split",
    "macro_business-index_split",
]
RECHECK_DIR = Path("outputs/worklots_recheck")
OUT_ROOT = Path("outputs")


def load_verdicts() -> tuple[dict[str, str], list[str]]:
    """모든 재검토 결과를 모은다. 같은 ref가 두 번 나오면 오류로 본다."""
    verdicts: dict[str, str] = {}
    conflicts: list[str] = []
    for path in sorted(RECHECK_DIR.glob("recheck_[rw]*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ref = str(row.get("ref") or "")
            decision = str(row.get("decision") or "")
            if decision not in {"keep", "drop"}:
                conflicts.append(f"{ref}: 잘못된 decision={decision!r} ({path.name})")
                continue
            if ref in verdicts and verdicts[ref] != decision:
                conflicts.append(f"{ref}: 판정 충돌 {verdicts[ref]} vs {decision}")
                continue
            verdicts[ref] = decision
    return verdicts, conflicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    verdicts, conflicts = load_verdicts()
    print(f"재검토 판정 {len(verdicts)}건 수집")
    if conflicts:
        print(f"경고: 충돌/오류 {len(conflicts)}건")
        for c in conflicts[:10]:
            print(f"   {c}")

    changed = Counter()
    unchanged = Counter()
    unmatched = []
    final = defaultdict(Counter)

    for folder in FOLDERS:
        folder_path = OUT_ROOT / folder
        for path in sorted(folder_path.glob("*.json"), key=lambda p: int(p.stem)):
            articles = json.loads(path.read_text(encoding="utf-8"))
            dirty = False
            for idx, article in enumerate(articles):
                ref = f"{folder}/{path.name}#{idx}"
                current = str(article.get("필터링 여부") or "")
                decision = verdicts.get(ref)
                if decision == "keep":
                    if current != "N":
                        article["필터링 여부"] = "N"
                        dirty = True
                        changed[folder] += 1
                    else:
                        unchanged[folder] += 1
                elif decision == "drop":
                    if current != "Y":
                        article["필터링 여부"] = "Y"
                        dirty = True
                        changed[folder] += 1
                    else:
                        unchanged[folder] += 1
                # 재검토 대상이 아니었던 기사(이미 N)는 그대로 둔다.
                value = str(article.get("필터링 여부") or "")
                if value not in {"Y", "N"}:
                    unmatched.append(f"{ref}: {value!r}")
                final[folder][value or "(빈값)"] += 1
            if dirty and args.apply:
                path.write_text(
                    json.dumps(articles, ensure_ascii=False, indent=1), encoding="utf-8"
                )

    print()
    print(f"{'폴더':30s}{'변경':>7s}{'유지':>7s}")
    for folder in FOLDERS:
        print(f"{folder:30s}{changed[folder]:>7d}{unchanged[folder]:>7d}")

    print()
    print(f"{'폴더':30s}{'N(유지)':>9s}{'Y(제외)':>9s}{'기타':>7s}")
    ok = True
    for folder in FOLDERS:
        c = final[folder]
        other = sum(v for k, v in c.items() if k not in {"Y", "N"})
        if other:
            ok = False
        print(f"{folder:30s}{c['N']:>9d}{c['Y']:>9d}{other:>7d}")

    if unmatched:
        ok = False
        print(f"\nY/N 이외의 값 {len(unmatched)}건:")
        for u in unmatched[:10]:
            print(f"   {u}")

    print()
    if args.dry_run:
        print("--dry-run 이므로 파일을 수정하지 않았습니다.")
    else:
        print("반영 완료.")
    print(f"최종 검증: 모든 값이 Y 또는 N == {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
