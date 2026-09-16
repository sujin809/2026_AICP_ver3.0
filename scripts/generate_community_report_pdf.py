#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from report_common import (
    is_reaction_row,
    pick_representative_agents,
    summarize_canonical_lineage,
    summarize_community_exposures,
    summarize_reasoning_off,
)
from twinmarket_kr.run_integrity import require_publication_ready_run


FONT_PATHS = [
    Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]


def register_font() -> str:
    for path in FONT_PATHS:
        if path.exists():
            pdfmetrics.registerFont(TTFont("Korean", str(path)))
            return "Korean"
    return "Helvetica"


def require_external_output(output: Path, run_dir: Path) -> Path:
    """Keep derived PDFs out of the signed run artifact tree."""

    resolved_output = output.resolve()
    resolved_run = run_dir.resolve()
    if resolved_output == resolved_run or resolved_output.is_relative_to(resolved_run):
        raise RuntimeError(
            "--output must be outside --run-dir so a derived PDF cannot alter "
            "the sealed run artifact hash"
        )
    return resolved_output


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def short(text: Any, limit: int = 180) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def para(text: Any, style: ParagraphStyle) -> Paragraph:
    safe = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe.replace("\n", "<br/>"), style)


def table(data: list[list[Any]], widths: list[float] | None = None) -> Table:
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Korean"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.5),
                ("FONTSIZE", (0, 1), (-1, -1), 7.2),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4050")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c4ccd8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def latest_states(updates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for row in updates:
        state = row.get("state") or {}
        agent_id = str(state.get("agent_id") or "")
        if agent_id:
            states[agent_id] = state
    return states


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Korean", 8)
    canvas.setFillColor(colors.HexColor("#5f6b7a"))
    canvas.drawString(18 * mm, 10 * mm, "TwinMarket Korea Community Report")
    canvas.drawRightString(192 * mm, 10 * mm, f"{doc.page}")
    canvas.restoreState()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Explicit completed run directory; latest/current inference is forbidden.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit PDF path outside --run-dir.",
    )
    parser.add_argument("--representative-agents", type=int, default=4)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    validation = require_publication_ready_run(run_dir)
    if validation["community_mode"] != "on":
        raise RuntimeError(
            "Community report requires a publication-ready community-on run"
        )

    font = register_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="KTitle", parent=styles["Title"], fontName=font, fontSize=18, leading=24, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="KHeading1", parent=styles["Heading1"], fontName=font, fontSize=14, leading=18, textColor=colors.HexColor("#23395d"), spaceBefore=12, spaceAfter=8))
    styles.add(ParagraphStyle(name="KHeading2", parent=styles["Heading2"], fontName=font, fontSize=11.5, leading=15, textColor=colors.HexColor("#1f4e79"), spaceBefore=8, spaceAfter=5))
    styles.add(ParagraphStyle(name="KBody", parent=styles["BodyText"], fontName=font, fontSize=8.6, leading=12, alignment=TA_LEFT, spaceAfter=5))
    styles.add(ParagraphStyle(name="KSmall", parent=styles["BodyText"], fontName=font, fontSize=7.1, leading=9.8, alignment=TA_LEFT))

    meta = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    run_id = str(meta.get("run_id") or run_dir.name)
    output = require_external_output(args.output, run_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    posts = read_csv(run_dir / "community_posts.csv")
    interactions = read_csv(run_dir / "community_interactions.csv")
    reaction_rows = [row for row in interactions if is_reaction_row(row)]
    best_posts = read_csv(run_dir / "community_best_posts.csv")
    selection_inputs = read_jsonl(run_dir / "community_selection_inputs.jsonl")
    agent_turns = read_jsonl(run_dir / "agent_turns.jsonl")
    portfolio_updates = read_jsonl(run_dir / "portfolio_updates.jsonl")
    order_rows = read_csv(run_dir / "submitted_orders.csv")
    fill_rows = read_csv(run_dir / "exchange_fills.csv")
    exposure_summary = summarize_community_exposures(
        interactions,
        best_posts,
        posts,
    )

    # Chunked runs omit the legacy agent list from aggregate metadata.
    agent_ids = list(meta.get("agent_ids") or [])
    if not agent_ids:
        agent_ids = sorted(
            {
                str((row.get("agent") or {}).get("agent_id"))
                for row in agent_turns
                if (row.get("agent") or {}).get("agent_id")
            }
        )
    meta["agent_ids"] = agent_ids
    meta.setdefault("agent_count", len(agent_ids))
    trading_dates = [
        str(value)
        for value in (meta.get("trading_dates") or [])
        if str(value)
    ]
    start_date = str(
        meta.get("start_date")
        or (trading_dates[0] if trading_dates else "-")
    )
    end_date = str(
        meta.get("end_date")
        or (trading_dates[-1] if trading_dates else "-")
    )
    date_count = int(
        meta.get("date_count")
        or len(trading_dates)
        or len(
            {
                str(row.get("date"))
                for row in agent_turns
                if row.get("date")
            }
        )
    )
    lineage_summary = summarize_canonical_lineage(
        run_dir,
        metadata=meta,
        agent_ids=agent_ids,
        turn_rows=agent_turns,
        community_posts=posts,
    )
    reasoning_summary = summarize_reasoning_off(run_dir, meta)

    posts_by_date = defaultdict(list)
    for row in posts:
        posts_by_date[row["date"]].append(row)
    interactions_by_date = defaultdict(list)
    for row in reaction_rows:
        interactions_by_date[row["date"]].append(row)
    best_by_date = defaultdict(list)
    for row in best_posts:
        best_by_date[row["date"]].append(row)
    selection_dates: set[str] = set()
    for row in selection_inputs:
        selection_dates.add(str(row.get("date") or ""))
    thinking_by_date = defaultdict(list)
    for row in agent_turns:
        thinking = (row.get("context") or {}).get("community_thinking")
        if thinking:
            thinking_by_date[row["date"]].append((row["agent"]["agent_id"], thinking))

    reaction_counts_by_post = defaultdict(Counter)
    for row in reaction_rows:
        if row.get("post_id"):
            reaction_counts_by_post[str(row["post_id"])][row.get("reaction") or "none"] += 1

    final_states = latest_states(portfolio_updates)
    representative_agents, representative_reasons = pick_representative_agents(
        agent_ids,
        final_states=final_states,
        order_rows=order_rows,
        fill_rows=fill_rows,
        community_posts=posts,
        community_interactions=interactions,
        limit=args.representative_agents,
    )
    representative_agent_set = set(representative_agents)

    story: list[Any] = []
    story.append(para("TwinMarket Korea 커뮤니티 종토방 보고서", styles["KTitle"]))
    story.append(
        para(
            f"실행 ID: {meta['run_id']} / 기간 {start_date} ~ {end_date} "
            f"({date_count}거래일) / Agent {meta.get('agent_count')}명",
            styles["KBody"],
        )
    )

    reactions = Counter(row.get("reaction") for row in reaction_rows if row.get("reaction"))
    summary = [
        ["항목", "값"],
        ["게시글", f"{len(posts)}건"],
        ["선택 후보 화면", f"{len(selection_inputs)}건"],
        ["후보 제목 노출", f"{exposure_summary['title_only_count']}건"],
        [
            "전체 full-body 노출",
            (
                f"{exposure_summary['full_body_count']}건 "
                f"(PM 선택 {exposure_summary['pm_selected_full_body_count']}, "
                f"Best AM {exposure_summary['best_full_body_delivery_count']}, "
                f"선택 재전달 {exposure_summary['selected_full_body_replay_count']})"
            ),
        ],
        ["PM 본문 읽기/반응", f"{len(reaction_rows)}건 ({', '.join(f'{k}: {v}' for k, v in sorted(reactions.items()))})"],
        ["Best 선정", f"{len(best_posts)}건"],
        [
            "Best 자기 글 제외",
            (
                f"기록 {exposure_summary['self_excluded_count']}건 / "
                f"실제 자기 글 전달 위반 "
                f"{exposure_summary['self_delivery_violation_count']}건"
            ),
        ],
        [
            "노출 provenance",
            (
                f"{exposure_summary['provenance_count']}건 / "
                f"중복 {exposure_summary['duplicate_provenance_count']}건 / "
                f"orphan {exposure_summary['orphan_exposure_count']}건 / "
                f"title-only 본문 유출 "
                f"{exposure_summary['title_only_body_leak_count']}건 / "
                f"full-body 본문·hash 누락 "
                f"{exposure_summary['missing_full_body_count']}/"
                f"{exposure_summary['missing_body_hash_count']}건"
            ),
        ],
        [
            "게시글 계보",
            (
                f"{lineage_summary['community_post_linked_count']}/"
                f"{lineage_summary['community_post_count']} "
                "post-fill LTB/fill/decision 연결"
            ),
        ],
        [
            "Reasoning-off",
            (
                f"{reasoning_summary['status']} / "
                f"provider returns {reasoning_summary['provider_return_count']}건"
            ),
        ],
        ["Community Thinking", f"{sum(len(v) for v in thinking_by_date.values())}건"],
        ["대표 에이전트", ", ".join(f"{agent_id} ({representative_reasons.get(agent_id, '')})" for agent_id in representative_agents)],
    ]
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in summary], [40 * mm, 130 * mm]))

    dates = sorted(set(posts_by_date) | set(interactions_by_date) | set(best_by_date) | selection_dates | set(thinking_by_date))
    story.append(para("1. 커뮤니티 노출·반응 요약", styles["KHeading1"]))
    pressure_rows = [["일자", "게시/반응", "Best 게시글", "읽기 반응", "기술통계"]]
    for date in dates:
        day_posts = posts_by_date.get(date, [])
        day_interactions = interactions_by_date.get(date, [])
        day_best = best_by_date.get(date, [])
        day_reactions = Counter(row.get("reaction") for row in day_interactions if row.get("reaction"))
        best_titles = " / ".join(short(row.get("title"), 45) for row in day_best[:3]) or "-"
        like_count = day_reactions.get("like", 0)
        unlike_count = day_reactions.get("unlike", 0)
        if like_count > unlike_count:
            read_signal = "동조 우위"
            analysis = "관찰된 반응에서 like가 unlike보다 많았다."
        elif unlike_count > like_count:
            read_signal = "반박 우위"
            analysis = "관찰된 반응에서 unlike가 like보다 많았다."
        else:
            read_signal = "혼조"
            analysis = "like와 unlike만으로 뚜렷한 우위를 확인할 수 없다."
        pressure_rows.append(
            [
                date,
                f"게시 {len(day_posts)} / 반응 {len(day_interactions)}",
                best_titles,
                f"{read_signal}\nlike {like_count}, unlike {unlike_count}, none {day_reactions.get('none', 0)}",
                analysis,
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in pressure_rows], [22 * mm, 27 * mm, 60 * mm, 31 * mm, 30 * mm]))

    story.append(para("2. 기존 score 상위 게시글", styles["KHeading1"]))
    story.append(
        para(
            "정렬에는 실험의 기존 Best 규칙인 score = like - unlike만 사용한다. "
            "노출·반응만으로 거래 또는 belief에 인과적 영향을 주었다고 판단하지 않는다.",
            styles["KBody"],
        )
    )
    post_rows = [["post_id", "일자", "작성자/유형", "제목", "반응", "해석"]]
    ranked_posts = []
    for row in posts:
        post_id = str(row.get("post_id") or "")
        reactions_for_post = reaction_counts_by_post.get(post_id) or Counter()
        best_rank = min((num(best.get("rank"), 99) for best in best_posts if str(best.get("post_id")) == post_id), default=99)
        score = reactions_for_post.get("like", 0) - reactions_for_post.get("unlike", 0)
        ranked_posts.append((score, row, reactions_for_post, best_rank))
    for score, row, reactions_for_post, best_rank in sorted(ranked_posts, key=lambda item: item[0], reverse=True)[:10]:
        like_count = reactions_for_post.get("like", 0)
        unlike_count = reactions_for_post.get("unlike", 0)
        if best_rank < 99 and like_count >= unlike_count:
            interpretation = "기존 score 순위에서 Best에 포함됐고 like가 unlike 이상이다."
        elif unlike_count > like_count:
            interpretation = "관찰된 반응에서 unlike가 like보다 많다."
        else:
            interpretation = "반응 집계만으로 후속 거래 방향은 판단할 수 없다."
        reaction_text = ", ".join(f"{key} {value}" for key, value in sorted(reactions_for_post.items())) or "-"
        post_rows.append(
            [
                row.get("post_id"),
                row.get("date"),
                f"{row.get('agent_id')}\n{row.get('post_type')}",
                short(row.get("title"), 95),
                reaction_text,
                interpretation,
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in post_rows], [16 * mm, 20 * mm, 25 * mm, 57 * mm, 24 * mm, 38 * mm]))

    story.append(para("3. 대표 에이전트 반응 패턴", styles["KHeading1"]))
    agent_rows = [["Agent", "선정 기준", "읽은 글/반응", "주요 수용 또는 반박", "해석"]]
    for agent_id in representative_agents:
        rows = [row for row in reaction_rows if row.get("agent_id") == agent_id]
        reactions_for_agent = Counter(row.get("reaction") for row in rows if row.get("reaction"))
        notable = sorted(
            rows,
            key=lambda row: abs(reaction_counts_by_post.get(str(row.get("post_id")) or "", Counter()).get("like", 0) - reaction_counts_by_post.get(str(row.get("post_id")) or "", Counter()).get("unlike", 0)),
            reverse=True,
        )[:3]
        titles = "\n".join(f"{row.get('reaction')}: {short(row.get('title'), 70)}" for row in notable) or "-"
        if reactions_for_agent.get("like", 0) > reactions_for_agent.get("unlike", 0):
            interpretation = "이 에이전트의 관찰 반응은 like가 더 많았다."
        elif reactions_for_agent.get("unlike", 0) > reactions_for_agent.get("like", 0):
            interpretation = "이 에이전트의 관찰 반응은 unlike가 더 많았다."
        else:
            interpretation = "관찰 반응 수만으로 뚜렷한 방향을 말하기 어렵다."
        agent_rows.append(
            [
                agent_id,
                representative_reasons.get(agent_id, ""),
                f"총 {len(rows)}건\n" + ", ".join(f"{k} {v}" for k, v in sorted(reactions_for_agent.items())),
                titles,
                interpretation,
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in agent_rows], [18 * mm, 34 * mm, 28 * mm, 62 * mm, 38 * mm]))

    story.append(para("4. Community Thinking 대표 사례", styles["KHeading1"]))
    thinking_rows = [["일자", "Agent", "Thinking 요약", "분석 포인트"]]
    thinking_items = [
        (date, agent_id, thinking)
        for date in dates
        for agent_id, thinking in thinking_by_date.get(date, [])
        if agent_id in representative_agent_set
    ][:12]
    for date, agent_id, thinking in thinking_items:
        lower = str(thinking)
        if "반대" in lower or "경계" in lower or "위험" in lower:
            point = "기록된 해석 문장에 반대·경계·위험 표현이 포함됐다."
        elif "공감" in lower or "반영" in lower:
            point = "기록된 해석 문장에 공감·반영 표현이 포함됐다."
        else:
            point = "키워드만으로 후속 STB·거래 반영 여부를 판단하지 않는다."
        thinking_rows.append([date, agent_id, short(thinking, 360), point])
    if len(thinking_rows) == 1:
        thinking_rows.append(["-", "-", "대표 에이전트의 Community Thinking 로그가 없습니다.", "-"])
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in thinking_rows], [20 * mm, 18 * mm, 100 * mm, 42 * mm]))

    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title="TwinMarket Community Report")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(output)


if __name__ == "__main__":
    main()
