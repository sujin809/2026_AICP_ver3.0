#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from report_common import pick_representative_agents


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_DIR = PROJECT_ROOT / "outputs" / "logs" / "current"
REPORT_DIR = PROJECT_ROOT / "outputs" / "reports"
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


def report_dir_for_run(run_id: str) -> Path:
    match = re.search(r"(20\d{6})", run_id)
    folder = match.group(1) if match else "unknown_date"
    return REPORT_DIR / folder


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
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--representative-agents", type=int, default=4)
    args = parser.parse_args()

    run_dir = args.run_dir

    font = register_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="KTitle", parent=styles["Title"], fontName=font, fontSize=18, leading=24, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="KHeading1", parent=styles["Heading1"], fontName=font, fontSize=14, leading=18, textColor=colors.HexColor("#23395d"), spaceBefore=12, spaceAfter=8))
    styles.add(ParagraphStyle(name="KHeading2", parent=styles["Heading2"], fontName=font, fontSize=11.5, leading=15, textColor=colors.HexColor("#1f4e79"), spaceBefore=8, spaceAfter=5))
    styles.add(ParagraphStyle(name="KBody", parent=styles["BodyText"], fontName=font, fontSize=8.6, leading=12, alignment=TA_LEFT, spaceAfter=5))
    styles.add(ParagraphStyle(name="KSmall", parent=styles["BodyText"], fontName=font, fontSize=7.1, leading=9.8, alignment=TA_LEFT))

    meta = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    run_id = str(meta.get("run_id") or run_dir.name)
    output = args.output or report_dir_for_run(run_id) / f"{run_id}_community_report.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    posts = read_csv(run_dir / "community_posts.csv")
    interactions = read_csv(run_dir / "community_interactions.csv")
    best_posts = read_csv(run_dir / "community_best_posts.csv")
    logs = read_csv(run_dir / "community_logs.csv")
    selection_inputs = read_jsonl(run_dir / "community_selection_inputs.jsonl")
    agent_turns = read_jsonl(run_dir / "agent_turns.jsonl")
    portfolio_updates = read_jsonl(run_dir / "portfolio_updates.jsonl")
    order_rows = read_csv(run_dir / "submitted_orders.csv")
    fill_rows = read_csv(run_dir / "exchange_fills.csv")

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

    posts_by_date = defaultdict(list)
    for row in posts:
        posts_by_date[row["date"]].append(row)
    interactions_by_date = defaultdict(list)
    for row in interactions:
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
    for row in interactions:
        if row.get("post_id"):
            reaction_counts_by_post[str(row["post_id"])][row.get("reaction") or "read"] += 1

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
    story.append(para(f"실행 ID: {meta['run_id']} / 기간 {meta.get('start_date')}부터 {meta.get('date_count')}거래일 / Agent {meta.get('agent_count')}명", styles["KBody"]))

    reactions = Counter(row.get("reaction") for row in interactions if row.get("reaction"))
    summary = [
        ["항목", "값"],
        ["게시글", f"{len(posts)}건"],
        ["선택 후보 화면", f"{len(selection_inputs)}건"],
        ["읽기/반응", f"{len(interactions)}건 ({', '.join(f'{k}: {v}' for k, v in sorted(reactions.items()))})"],
        ["Best 선정", f"{len(best_posts)}건"],
        ["Community Thinking", f"{sum(len(v) for v in thinking_by_date.values())}건"],
        ["대표 에이전트", ", ".join(f"{agent_id} ({representative_reasons.get(agent_id, '')})" for agent_id in representative_agents)],
    ]
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in summary], [40 * mm, 130 * mm]))

    dates = sorted(set(posts_by_date) | set(interactions_by_date) | set(best_by_date) | selection_dates | set(thinking_by_date))
    story.append(para("1. 커뮤니티 압력 요약", styles["KHeading1"]))
    pressure_rows = [["일자", "게시/반응", "Best 신호", "읽기 반응", "분석"]]
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
            analysis = "커뮤니티 의견이 에이전트 판단을 강화하는 방향으로 작동했다."
        elif unlike_count > like_count:
            read_signal = "반박 우위"
            analysis = "노출은 있었지만 그대로 수용되기보다 경계/반대 반응이 더 컸다."
        else:
            read_signal = "혼조"
            analysis = "특정 방향으로 합의가 생기기보다 의견 탐색 기능이 컸다."
        pressure_rows.append(
            [
                date,
                f"게시 {len(day_posts)} / 반응 {len(day_interactions)}",
                best_titles,
                f"{read_signal}\nlike {like_count}, unlike {unlike_count}, read {day_reactions.get('read', 0)}",
                analysis,
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in pressure_rows], [22 * mm, 27 * mm, 60 * mm, 31 * mm, 30 * mm]))

    story.append(para("2. 영향력 큰 게시글", styles["KHeading1"]))
    post_rows = [["post_id", "일자", "작성자/유형", "제목", "반응", "해석"]]
    ranked_posts = []
    for row in posts:
        post_id = str(row.get("post_id") or "")
        reactions_for_post = reaction_counts_by_post.get(post_id) or Counter()
        best_rank = min((num(best.get("rank"), 99) for best in best_posts if str(best.get("post_id")) == post_id), default=99)
        score = reactions_for_post.get("like", 0) * 2 + reactions_for_post.get("read", 0) - reactions_for_post.get("unlike", 0) + max(0, 6 - best_rank)
        ranked_posts.append((score, row, reactions_for_post, best_rank))
    for score, row, reactions_for_post, best_rank in sorted(ranked_posts, key=lambda item: item[0], reverse=True)[:10]:
        like_count = reactions_for_post.get("like", 0)
        unlike_count = reactions_for_post.get("unlike", 0)
        if best_rank < 99 and like_count >= unlike_count:
            interpretation = "상위 노출과 동조 반응이 겹쳐 커뮤니티 신호로 작동했다."
        elif unlike_count > like_count:
            interpretation = "노출은 컸지만 반대 반응이 많아 역신호로 해석될 수 있다."
        else:
            interpretation = "읽힌 정도는 있으나 방향성은 제한적이다."
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
        rows = [row for row in interactions if row.get("agent_id") == agent_id]
        reactions_for_agent = Counter(row.get("reaction") for row in rows if row.get("reaction"))
        notable = sorted(
            rows,
            key=lambda row: abs(reaction_counts_by_post.get(str(row.get("post_id")) or "", Counter()).get("like", 0) - reaction_counts_by_post.get(str(row.get("post_id")) or "", Counter()).get("unlike", 0)),
            reverse=True,
        )[:3]
        titles = "\n".join(f"{row.get('reaction')}: {short(row.get('title'), 70)}" for row in notable) or "-"
        if reactions_for_agent.get("like", 0) > reactions_for_agent.get("unlike", 0):
            interpretation = "커뮤니티 의견을 판단 강화 재료로 사용한 성향이 강하다."
        elif reactions_for_agent.get("unlike", 0) > reactions_for_agent.get("like", 0):
            interpretation = "커뮤니티를 반대 검증 장치로 활용한 성향이 강하다."
        else:
            interpretation = "읽기는 했지만 특정 방향으로 기울지 않았다."
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
            point = "커뮤니티 의견을 그대로 따르지 않고 리스크 필터로 사용했다."
        elif "공감" in lower or "반영" in lower:
            point = "커뮤니티 의견이 다음 투자 판단에 반영될 가능성이 높다."
        else:
            point = "탐색적 읽기에 가까워 직접 영향은 제한적이다."
        thinking_rows.append([date, agent_id, short(thinking, 360), point])
    if len(thinking_rows) == 1:
        thinking_rows.append(["-", "-", "대표 에이전트의 Community Thinking 로그가 없습니다.", "-"])
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in thinking_rows], [20 * mm, 18 * mm, 100 * mm, 42 * mm]))

    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title="TwinMarket Community Report")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(output)


if __name__ == "__main__":
    main()
