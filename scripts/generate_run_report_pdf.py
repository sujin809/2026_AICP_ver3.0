#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import sqlite3
import argparse
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
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

from report_common import pick_representative_agents


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = PROJECT_ROOT / "outputs" / "logs" / "current"
REPORT_DIR = PROJECT_ROOT / "outputs" / "reports"
REPORT_PATH: Path | None = None
SYS_100_DB = PROJECT_ROOT / "outputs" / "sys_100.db"
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


def load_csv(name: str) -> list[dict[str, str]]:
    path = RUN_DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_jsonl(name: str) -> list[dict[str, Any]]:
    path = RUN_DIR / name
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_json(name: str) -> dict[str, Any]:
    return json.loads((RUN_DIR / name).read_text(encoding="utf-8"))


def load_error_logs() -> list[dict[str, Any]]:
    paths = [RUN_DIR / "errors.jsonl"]
    chunks_dir = RUN_DIR / "chunks"
    if chunks_dir.exists():
        paths.extend(sorted(chunks_dir.glob("*/errors.jsonl")))
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            rows.extend(json.loads(line) for line in f if line.strip())
    return rows


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    return f"{num(value):,.0f}원"


def pct(value: Any) -> str:
    return f"{num(value) * 100:.3f}%"


def short(text: Any, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def action_ko(action: str) -> str:
    return {"buy": "매수", "sell": "매도", "hold": "보유"}.get(action, action)


def row_agent_id(row: dict[str, Any]) -> str:
    return str(row.get("agent_id") or row.get("user_id") or "")


def row_subturn(row: dict[str, Any]) -> str:
    return str(row.get("subturn") or (row.get("context") or {}).get("subturn") or "").lower()


def report_dir_for_run(run_id: str) -> Path:
    match = re.search(r"(20\d{6})", run_id)
    folder = match.group(1) if match else "unknown_date"
    return REPORT_DIR / folder


def row_action(row: dict[str, Any]) -> str:
    return str(row.get("action") or row.get("direction") or "").lower()


def row_quantity(row: dict[str, Any]) -> float:
    return num(row.get("quantity") or row.get("executed_quantity") or row.get("filled_quantity"))


def row_close(row: dict[str, Any]) -> float:
    return num(row.get("close_price") or row.get("closing_price") or row.get("announced_price"))


def final_daily_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the final exchange summary when a resumed chunk left a blank duplicate."""
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("subturn") or ""),
            str(row.get("stock_code") or ""),
        )
        current = selected.get(key)
        score = (
            num(row.get("submitted_orders")),
            num(row.get("fill_count")),
            num(row.get("volume")),
        )
        current_score = (
            num(current.get("submitted_orders")),
            num(current.get("fill_count")),
            num(current.get("volume")),
        ) if current else (-1.0, -1.0, -1.0)
        if score > current_score:
            selected[key] = row
    return sorted(selected.values(), key=lambda row: (row.get("date", ""), int(num(row.get("turn"))), row.get("subturn", "")))


def pct_points(value: Any) -> str:
    return f"{num(value) * 100:+.2f}pp"


def order_price_text(row: dict[str, Any]) -> str:
    return money(row.get("announced_price") or row.get("price") or row.get("executed_price"))


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
                ("FONTSIZE", (0, 1), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#23395d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b9c2d0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fb")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def load_initial_values(agent_ids: list[str]) -> dict[str, float]:
    if not SYS_100_DB.exists():
        return {agent_id: 100_000_000.0 for agent_id in agent_ids}
    with sqlite3.connect(SYS_100_DB) as conn:
        rows = conn.execute(
            "SELECT agent_id, ini_cash FROM agents WHERE agent_id IN (%s)"
            % ",".join("?" for _ in agent_ids),
            agent_ids,
        ).fetchall()
    values = {str(agent_id): float(ini_cash) for agent_id, ini_cash in rows}
    return {agent_id: values.get(agent_id, 100_000_000.0) for agent_id in agent_ids}


def latest_portfolios(
    updates: list[dict[str, Any]],
    final_close: float,
    initial_values: dict[str, float],
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for row in updates:
        state = row["state"]
        states[state["agent_id"]] = state
    for state in states.values():
        positions = state.get("positions") or []
        cash = num(state.get("cash"))
        stock_value = 0.0
        for pos in positions:
            qty = int(pos.get("quantity") or 0)
            pos["current_price"] = final_close
            pos["unrealized_pnl"] = (final_close - num(pos.get("avg_cost"))) * qty
            stock_value += final_close * qty
        state["total_value_marked_final"] = cash + stock_value
        initial_value = initial_values.get(state["agent_id"], 100_000_000.0)
        state["return_rate_marked_final"] = (
            (cash + stock_value - initial_value) / initial_value if initial_value else 0.0
        )
    return states


def page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Korean", 8)
    canvas.setFillColor(colors.HexColor("#5f6b7a"))
    canvas.drawString(18 * mm, 10 * mm, "TwinMarket Korea 실행 결과 보고서")
    canvas.drawRightString(192 * mm, 10 * mm, f"{doc.page}")
    canvas.restoreState()


def main() -> None:
    global RUN_DIR, REPORT_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=RUN_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--representative-agents", type=int, default=4)
    args = parser.parse_args()

    RUN_DIR = args.run_dir

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    font = register_font()

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="KTitle",
            parent=styles["Title"],
            fontName=font,
            fontSize=19,
            leading=25,
            alignment=TA_CENTER,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KHeading1",
            parent=styles["Heading1"],
            fontName=font,
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#23395d"),
            spaceBefore=12,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KHeading2",
            parent=styles["Heading2"],
            fontName=font,
            fontSize=11.5,
            leading=15,
            textColor=colors.HexColor("#1f4e79"),
            spaceBefore=8,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KBody",
            parent=styles["BodyText"],
            fontName=font,
            fontSize=8.7,
            leading=12.2,
            alignment=TA_LEFT,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KSmall",
            parent=styles["BodyText"],
            fontName=font,
            fontSize=7.2,
            leading=10.2,
            alignment=TA_LEFT,
        )
    )

    meta = load_json("run_metadata.json")
    run_id = str(meta.get("run_id") or RUN_DIR.name)
    REPORT_PATH = args.output or report_dir_for_run(run_id) / f"{run_id}_report.pdf"
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    complete = load_json("run_complete.json")
    agent_rows = load_csv("agent_turns.csv")
    daily_rows = final_daily_rows(load_csv("daily_exchange_summary.csv"))
    order_rows = load_csv("submitted_orders.csv")
    fill_rows = load_csv("exchange_fills.csv")
    community_posts = load_csv("community_posts.csv")
    community_interactions = load_csv("community_interactions.csv")
    community_best_posts = load_csv("community_best_posts.csv")
    community_logs = load_csv("community_logs.csv")
    community_selection_inputs = load_csv("community_selection_inputs.csv")
    turn_rows = load_jsonl("agent_turns.jsonl")
    portfolio_updates = load_jsonl("portfolio_updates.jsonl")
    error_rows = load_error_logs()

    # Chunked runs record their aggregate settings but not the legacy agent list.
    # Recover it from the canonical turn log so both run formats remain reportable.
    agent_ids = list(meta.get("agent_ids") or [])
    if not agent_ids:
        agent_ids = sorted(
            {
                str(row.get("agent_id"))
                for row in agent_rows
                if row.get("agent_id")
            }
        )
    if not agent_ids:
        agent_ids = sorted(
            {
                str((row.get("agent") or {}).get("agent_id"))
                for row in turn_rows
                if (row.get("agent") or {}).get("agent_id")
            }
        )
    meta["agent_ids"] = agent_ids
    meta.setdefault("agent_count", len(agent_ids))
    meta.setdefault("date_count", len({row.get("date") for row in daily_rows if row.get("date")}))
    meta.setdefault("random_seed", "N/A")
    if not meta.get("agent_depths"):
        meta["agent_depths"] = {
            str((row.get("agent") or {}).get("agent_id")): int((row.get("agent") or {}).get("news_depth", 0))
            for row in turn_rows
            if (row.get("agent") or {}).get("agent_id")
        }

    turns_by_session = defaultdict(list)
    by_agent = defaultdict(list)
    for row in turn_rows:
        turns_by_session[(row["date"], row_subturn(row))].append(row)
        by_agent[row["agent"]["agent_id"]].append(row)
    for rows in turns_by_session.values():
        rows.sort(key=lambda x: x["agent"]["agent_id"])
    for rows in by_agent.values():
        rows.sort(key=lambda x: x["turn"])

    fills_by_session = defaultdict(list)
    fills_by_agent = defaultdict(list)
    for row in fill_rows:
        fills_by_session[(row["date"], row_subturn(row))].append(row)
        aid = row_agent_id(row)
        fills_by_agent[aid].append(row)

    orders_by_session = defaultdict(list)
    for row in order_rows:
        orders_by_session[(row["date"], row_subturn(row))].append(row)

    final_close = row_close(daily_rows[-1])
    initial_values = load_initial_values(agent_ids)
    final_states = latest_portfolios(portfolio_updates, final_close, initial_values)
    representative_agents, representative_reasons = pick_representative_agents(
        agent_ids,
        final_states=final_states,
        order_rows=order_rows,
        fill_rows=fill_rows,
        community_posts=community_posts,
        community_interactions=community_interactions,
        limit=args.representative_agents,
    )
    story: list[Any] = []
    story.append(para("TwinMarket Korea 시뮬레이션 실행 결과 보고서", styles["KTitle"]))
    story.append(
        para(
            f"실행 ID: {meta['run_id']} / 대상 종목: 삼성전자(005930) / 기간: "
            f"{daily_rows[0]['date']} ~ {daily_rows[-1]['date']} / "
            f"에이전트: {', '.join(agent_ids)}",
            styles["KBody"],
        )
    )

    action_counts = Counter(row["action"] for row in agent_rows)
    total_order_qty = sum(int(num(row["quantity"])) for row in order_rows)
    total_fill_qty = sum(int(row_quantity(row)) for row in fill_rows)
    story.append(para("1. 실행 개요", styles["KHeading1"]))
    overview = [
        ["항목", "내용"],
        [
            "실행 조건",
            f"에이전트 {meta['agent_count']}명, {meta['date_count']}거래일, "
            f"기간={meta.get('start_date') or daily_rows[0]['date']}~{meta.get('end_date') or daily_rows[-1]['date']}, "
            f"seed={meta['random_seed']}, concurrency={meta['concurrency']}, agent_selection={meta.get('agent_selection', 'legacy')}, "
            f"information_mode={meta.get('information_mode', 'pre_close_cutoff')}, exchange_mode={meta.get('exchange_mode', 'announced_price_binary')}",
        ],
        ["전체 에이전트", ", ".join(agent_ids)],
        [
            "보고서 대표 에이전트",
            ", ".join(f"{agent_id} ({representative_reasons.get(agent_id, '')})" for agent_id in representative_agents),
        ],
        ["전체 판단", f"총 {len(agent_rows)}건: 매수 {action_counts.get('buy', 0)}건, 보유 {action_counts.get('hold', 0)}건, 매도 {action_counts.get('sell', 0)}건"],
        ["주문/체결", f"제출 주문 {len(order_rows)}건, 제출 수량 {total_order_qty:,}주, 체결 수량 {total_fill_qty:,}주"],
        ["로그 위치", str(RUN_DIR)],
        ["완료 정보", f"{complete.get('run_id')} / {complete.get('date_count', meta['date_count'])}일 실행 완료"],
    ]
    if error_rows:
        error_summary = ", ".join(
            f"{row.get('date')} turn {row.get('turn')} {row.get('agent_id')} ({row.get('error_type')})"
            for row in error_rows[:5]
        )
        if len(error_rows) > 5:
            error_summary += f" 외 {len(error_rows) - 5}건"
        overview.append(["실행 오류", f"에이전트 턴 오류 {len(error_rows)}건: {error_summary}"])
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in overview], [35 * mm, 135 * mm]))

    if community_posts or community_interactions or community_logs:
        story.append(para("1-1. Community 기능 체크리스트", styles["KHeading1"]))
        depth_by_agent = {str(agent_id): int(depth) for agent_id, depth in (meta.get("agent_depths") or {}).items()}
        depth0_ids = {agent_id for agent_id, depth in depth_by_agent.items() if depth == 0}
        depth12_ids = {agent_id for agent_id, depth in depth_by_agent.items() if depth >= 1}
        post_agents = {row.get("agent_id") for row in community_posts}
        reading_agents = {row.get("agent_id") for row in community_interactions if row.get("post_id")}
        thinking_agents = {
            row["agent"]["agent_id"]
            for row in turn_rows
            if (row.get("context") or {}).get("community_thinking")
        }
        profile_rows = [row for row in community_interactions if row.get("author_profile") not in {"", "null", None}]
        checks = [
            ["항목", "상태", "근거"],
            ["Depth 0 커뮤니티 미참여", "OK" if not (post_agents | reading_agents | thinking_agents) & depth0_ids else "확인 필요", f"Depth0={sorted(depth0_ids)}"],
            ["Depth 1/2 포스팅", "OK" if post_agents <= depth12_ids else "확인 필요", f"post_agents={sorted(post_agents)}"],
            ["게시글 후보 입력 로그", "OK" if community_selection_inputs else "누락", f"{len(community_selection_inputs)} rows"],
            ["읽기/반응 로그", "OK" if community_interactions else "누락", f"{len(community_interactions)} rows"],
            ["Depth 2 작성자 프로필", "OK" if profile_rows else "확인 필요", f"profile rows={len(profile_rows)}"],
            ["Best 게시글 선정", "OK" if community_best_posts else "누락", f"{len(community_best_posts)} rows"],
            ["다음날 Community Thinking", "OK" if thinking_agents else "확인 필요", f"thinking_agents={sorted(thinking_agents)}"],
        ]
        story.append(table([[para(c, styles["KSmall"]) for c in row] for row in checks], [45 * mm, 25 * mm, 100 * mm]))
        reactions = Counter(row.get("reaction") for row in community_interactions if row.get("reaction"))
        by_post_agent = Counter(row.get("agent_id") for row in community_posts)
        by_read_agent = Counter(row.get("agent_id") for row in community_interactions if row.get("post_id"))
        community_summary = [
            ["항목", "내용"],
            ["커뮤니티 규모", f"게시글 {len(community_posts)}건, 읽기/반응 {len(community_interactions)}건, Best {len(community_best_posts)}건, 로그 {len(community_logs)}건"],
            ["반응 분포", ", ".join(f"{key}: {value}" for key, value in sorted(reactions.items())) or "-"],
            ["Agent별 포스팅", ", ".join(f"{key}: {value}" for key, value in sorted(by_post_agent.items())) or "-"],
            ["Agent별 읽기", ", ".join(f"{key}: {value}" for key, value in sorted(by_read_agent.items())) or "-"],
        ]
        story.append(table([[para(c, styles["KSmall"]) for c in row] for row in community_summary], [35 * mm, 135 * mm]))

    story.append(para("2. 일자·회차별 전체 거래 현황", styles["KHeading1"]))
    daily_table = [["일자/회차", "거래가격", "당일 시가", "당일 종가", "주문 건수", "체결 수량/건수", "순방향", "판단 분포", "해석"]]
    open_by_date = {
        row["date"]: num(row.get("announced_price"))
        for row in daily_rows
        if str(row.get("subturn", "")).lower() == "am"
    }
    daily_insights: list[dict[str, Any]] = []
    for row in daily_rows:
        date = row["date"]
        subturn = row_subturn(row)
        turns = turns_by_session[(date, subturn)]
        counts = Counter(t["decision"]["action"] for t in turns)
        session_orders = orders_by_session.get((date, subturn), [])
        order_counts = Counter(row_action(order) for order in session_orders)
        agent_fills = [
            fill
            for fill in fills_by_session.get((date, subturn), [])
            if row_agent_id(fill) != "INSTITUTIONAL"
        ]
        buy_qty = sum(int(row_quantity(fill)) for fill in agent_fills if row_action(fill) == "buy")
        sell_qty = sum(int(row_quantity(fill)) for fill in agent_fills if row_action(fill) == "sell")
        net_qty = buy_qty - sell_qty
        if net_qty > 0:
            net_text = f"순매수 {net_qty:,}주"
        elif net_qty < 0:
            net_text = f"순매도 {abs(net_qty):,}주"
        else:
            net_text = "중립"
        sentiments = Counter(t["news_interpretation"].get("news_sentiment", "") for t in turns)
        main_sentiment = sentiments.most_common(1)[0][0] if sentiments else ""
        market = turns[0]["context"]["market_features"] if turns else {}
        daily_insights.append(
            {
                "date": date,
                "turn": row.get("turn"),
                "close": row_close(row),
                "pct_chg": num(market.get("pct_chg")),
                "net_qty": net_qty,
                "buy_count": counts.get("buy", 0),
                "sell_count": counts.get("sell", 0),
                "hold_count": counts.get("hold", 0),
                "main_sentiment": main_sentiment,
                "turns": turns,
            }
        )
        note = (
            f"뉴스 감성은 {main_sentiment} 중심. "
            f"주문은 매수 {order_counts.get('buy', 0)}건, 매도 {order_counts.get('sell', 0)}건. "
        )
        if num(row["volume"]) == 0:
            note += "제출 주문은 있었지만 당일 체결은 발생하지 않음."
        elif counts.get("buy", 0) >= 4:
            note += "초기 저가/AI 모멘텀 인식이 매수로 강하게 연결됨."
        else:
            note += "보유 판단이 늘며 매수 강도는 둔화됨."
        daily_table.append(
            [
                f"{date}\n{row.get('subturn', '').upper()}",
                f"{'시가' if str(row.get('subturn', '')).lower() == 'am' else '종가'} {money(row.get('announced_price'))}",
                money(open_by_date.get(date)),
                money(row_close(row)),
                f"매수 {order_counts.get('buy', 0)} / 매도 {order_counts.get('sell', 0)}",
                f"{int(num(row['volume'])):,}주 / {row['fill_count']}건",
                net_text,
                f"매수 {counts.get('buy', 0)} / 보유 {counts.get('hold', 0)} / 매도 {counts.get('sell', 0)}",
                note,
            ]
        )
    story.append(
        table(
            [[para(c, styles["KSmall"]) for c in row] for row in daily_table],
            [18 * mm, 19 * mm, 17 * mm, 17 * mm, 22 * mm, 21 * mm, 20 * mm, 25 * mm, 19 * mm],
        )
    )

    story.append(para("3. 핵심 관찰", styles["KHeading1"]))
    returns = [
        (agent_id, num(final_states.get(agent_id, {}).get("return_rate_marked_final")))
        for agent_id in agent_ids
        if agent_id in final_states
    ]
    returns.sort(key=lambda item: item[1])
    best = returns[-1] if returns else ("-", 0)
    worst = returns[0] if returns else ("-", 0)
    net_total = sum(item["net_qty"] for item in daily_insights)
    buy_bias_days = sum(1 for item in daily_insights if item["buy_count"] > item["sell_count"])
    sell_bias_days = sum(1 for item in daily_insights if item["sell_count"] > item["buy_count"])
    fallback_count = sum(
        1
        for row in agent_rows
        if "fallback_decision_after_invalid_llm_output" in str(row.get("order_corrections") or "")
    )
    observation_rows = [
        ["관찰 포인트", "분석"],
        [
            "성과 분산",
            f"최고 {best[0]} {pct(best[1])}, 최저 {worst[0]} {pct(worst[1])}. "
            f"단순 평균보다 상하위 격차가 행동 차이를 설명하는 핵심 신호다.",
        ],
        [
            "집단 방향성",
            f"전체 체결 기준 {'순매수' if net_total > 0 else '순매도' if net_total < 0 else '중립'} {abs(net_total):,.0f}주. "
            f"매수 우위일 {buy_bias_days}회, 매도 우위일 {sell_bias_days}회로 판단 쏠림을 확인했다.",
        ],
        [
            "모델 안정성",
            f"의사결정 폴백 {fallback_count}건. 이 값이 늘면 투자 행동 분석보다 모델 출력 안정성 이슈가 결과를 오염시킬 수 있다.",
        ],
    ]
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in observation_rows], [38 * mm, 132 * mm]))

    story.append(para("4. 변곡일 분석", styles["KHeading1"]))
    pivot_days = sorted(
        daily_insights,
        key=lambda item: (abs(item["net_qty"]), abs(item["pct_chg"])),
        reverse=True,
    )[: min(5, len(daily_insights))]
    pivot_rows = [["일자", "가격/변동", "집단 행동", "핵심 뉴스/해석", "왜 중요한가"]]
    for item in pivot_days:
        news_titles: list[str] = []
        for turn in item["turns"]:
            for news in turn["context"]["news_context"].get("read_contents", []):
                title = news.get("title")
                if title and title not in news_titles:
                    news_titles.append(title)
        if item["net_qty"] > 0:
            net_text = f"순매수 {item['net_qty']:,}주"
        elif item["net_qty"] < 0:
            net_text = f"순매도 {abs(item['net_qty']):,}주"
        else:
            net_text = "중립"
        if item["buy_count"] > item["sell_count"]:
            meaning = "가격/뉴스 충격을 매수 기회로 해석한 날이다."
        elif item["sell_count"] > item["buy_count"]:
            meaning = "리스크 관리가 성장 기대보다 우선한 날이다."
        else:
            meaning = "의견이 갈려 에이전트 성향 차이가 드러난 날이다."
        pivot_rows.append(
            [
                str(item["date"]),
                f"{money(item['close'])}\n{pct_points(item['pct_chg'])}",
                f"{net_text}\n매수 {item['buy_count']} / 매도 {item['sell_count']} / 보유 {item['hold_count']}",
                f"감성 {item['main_sentiment']}\n" + short(" / ".join(news_titles[:3]), 190),
                meaning,
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in pivot_rows], [21 * mm, 25 * mm, 34 * mm, 60 * mm, 30 * mm]))

    story.append(para("5. 대표/이상 에이전트 최종 포트폴리오 및 해석", styles["KHeading1"]))
    final_table = [["에이전트", "최종 보유", "현금", "평가 총자산", "평가 수익률", "요약 해석"]]
    for agent_id in representative_agents:
        state = final_states.get(agent_id, {})
        positions = state.get("positions") or []
        pos_text = "-"
        if positions:
            pos = positions[0]
            pos_text = f"{int(pos.get('quantity') or 0):,}주 / 평균 {money(pos.get('avg_cost'))}"
        rows = by_agent[agent_id]
        buys = sum(1 for r in rows if r["decision"]["action"] == "buy")
        sells = sum(1 for r in rows if r["decision"]["action"] == "sell")
        latest_view = rows[-1]["belief"].get("belief_summary") if rows else ""
        final_table.append(
            [
                agent_id,
                pos_text,
                money(state.get("cash")),
                money(state.get("total_value_marked_final")),
                pct(state.get("return_rate_marked_final")),
                f"매수 {buys}회 / 매도 {sells}회. {short(latest_view, 140)}",
            ]
        )
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in final_table], [18 * mm, 32 * mm, 28 * mm, 30 * mm, 20 * mm, 42 * mm]))

    story.append(para("6. 종합 결론", styles["KHeading1"]))
    story.append(
        para(
            f"이번 {meta['date_count']}거래일 실행은 depth 0/1/2가 섞인 소규모 샘플에서 뉴스 접근 깊이에 따른 "
            "판단 로그와 주문 결과를 확인하기 위한 검증 실행이다. 보고서의 일자별 표와 에이전트별 흐름은 각 turn의 "
            "뉴스 해석, belief 변화, 주문 판단, 체결 여부를 원 로그에서 집계한 것이다. Depth 2 에이전트는 추가 검색 "
            "키워드와 검색 결과 수가 별도 컬럼으로 기록되므로, 후속 분석에서는 동일 날짜의 depth별 belief 변화와 "
            "매수/보유/매도 판단 차이를 비교하는 방식으로 확장할 수 있다.",
            styles["KBody"],
        )
    )

    doc = SimpleDocTemplate(
        str(REPORT_PATH),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="TwinMarket Korea 시뮬레이션 실행 결과 보고서",
    )
    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    print(REPORT_PATH)


if __name__ == "__main__":
    main()
