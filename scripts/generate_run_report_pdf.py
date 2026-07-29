#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import sqlite3
import argparse
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
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

from report_common import (
    OUTCOME_HORIZONS,
    is_reaction_row,
    pick_representative_agents,
    summarize_canonical_lineage,
    summarize_community_exposures,
    summarize_reasoning_off,
)
from twinmarket_kr.run_integrity import require_publication_ready_run


RUN_DIR: Path | None = None
REPORT_PATH: Path | None = None
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


def load_csv(name: str) -> list[dict[str, str]]:
    if RUN_DIR is None:
        raise RuntimeError("--run-dir must be resolved before loading artifacts")
    path = RUN_DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_jsonl(name: str) -> list[dict[str, Any]]:
    if RUN_DIR is None:
        raise RuntimeError("--run-dir must be resolved before loading artifacts")
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
    if RUN_DIR is None:
        raise RuntimeError("--run-dir must be resolved before loading artifacts")
    return json.loads((RUN_DIR / name).read_text(encoding="utf-8"))


def load_error_logs() -> list[dict[str, Any]]:
    if RUN_DIR is None:
        raise RuntimeError("--run-dir must be resolved before loading artifacts")
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


def load_initial_values(
    agent_ids: list[str],
    *,
    run_db: Path,
) -> dict[str, float]:
    if not run_db.is_file():
        raise RuntimeError(
            f"report requires the canonical run-local database: {run_db}"
        )
    try:
        with sqlite3.connect(f"{run_db.as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT agent_id, pre_portfolio_json
                FROM simulation_fills
                ORDER BY turn, agent_id
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"cannot read canonical run-local database: {run_db}"
        ) from exc
    first_values: dict[str, float] = {}
    for agent_id, payload in rows:
        key = str(agent_id)
        if key in first_values:
            continue
        try:
            first_values[key] = float(json.loads(payload)["cash"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    missing = sorted(set(agent_ids) - set(first_values))
    if missing:
        raise RuntimeError(
            "canonical run-local database lacks initial portfolio values for "
            + ", ".join(missing[:10])
        )
    return {agent_id: first_values[agent_id] for agent_id in agent_ids}


def instrument_label(
    metadata: dict[str, Any],
    fill_rows: list[dict[str, Any]],
) -> str:
    instrument = metadata.get("instrument") or {}
    code = str(
        instrument.get("stock_code")
        or metadata.get("stock_code")
        or next(
            (
                row.get("stock_code")
                for row in fill_rows
                if row.get("stock_code")
            ),
            "",
        )
    )
    name = str(
        instrument.get("stock_name")
        or metadata.get("stock_name")
        or ""
    )
    if name and code:
        return f"{name}({code})"
    return name or code or "미기록 종목"


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
        try:
            initial_value = initial_values[state["agent_id"]]
        except KeyError as exc:
            raise RuntimeError(
                "canonical run-local database lacks the initial portfolio value "
                f"for {state['agent_id']}"
            ) from exc
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

    RUN_DIR = args.run_dir.resolve()
    require_publication_ready_run(RUN_DIR)

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
    REPORT_PATH = require_external_output(args.output, RUN_DIR)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    complete = load_json("run_complete.json")
    agent_rows = load_csv("agent_turns.csv")
    daily_rows = final_daily_rows(load_csv("daily_exchange_summary.csv"))
    order_rows = load_csv("submitted_orders.csv")
    fill_rows = load_csv("exchange_fills.csv")
    community_posts = load_csv("community_posts.csv")
    community_interactions = load_csv("community_interactions.csv")
    community_reactions = [
        row for row in community_interactions if is_reaction_row(row)
    ]
    community_title_exposures = [
        row
        for row in community_interactions
        if row.get("exposure_level") == "title_only"
    ]
    community_best_deliveries = [
        row
        for row in community_interactions
        if row.get("delivery_status") == "delivered_am"
        and str(row.get("is_best") or "").lower() == "true"
    ]
    community_selected_replays = [
        row
        for row in community_interactions
        if row.get("delivery_status") == "delivered_am"
        and str(row.get("replay") or "").lower() == "true"
    ]
    community_best_posts = load_csv("community_best_posts.csv")
    community_logs = load_csv("community_logs.csv")
    community_selection_inputs = load_csv("community_selection_inputs.csv")
    turn_rows = load_jsonl("agent_turns.jsonl")
    portfolio_updates = load_jsonl("portfolio_updates.jsonl")
    error_rows = load_error_logs()
    if not daily_rows:
        raise RuntimeError(
            f"Completed run has no daily_exchange_summary.csv rows: {RUN_DIR}"
        )

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
    # Paper checkpoint runs store the field as `seed`; legacy runs used
    # `random_seed`. Preserve both formats in the report.
    meta.setdefault("random_seed", meta.get("seed", "N/A"))
    if not meta.get("agent_depths"):
        meta["agent_depths"] = {
            str((row.get("agent") or {}).get("agent_id")): int((row.get("agent") or {}).get("news_depth", 0))
            for row in turn_rows
            if (row.get("agent") or {}).get("agent_id")
        }

    lineage_summary = summarize_canonical_lineage(
        RUN_DIR,
        metadata=meta,
        agent_ids=agent_ids,
        turn_rows=turn_rows,
        community_posts=community_posts,
    )
    exposure_summary = summarize_community_exposures(
        community_interactions,
        community_best_posts,
        community_posts,
    )
    reasoning_summary = summarize_reasoning_off(RUN_DIR, meta)

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
    if not lineage_summary.get("db_path"):
        raise RuntimeError("report requires a canonical database inside --run-dir")
    run_db_path = Path(lineage_summary["db_path"])
    initial_values = load_initial_values(agent_ids, run_db=run_db_path)
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
            f"실행 ID: {meta['run_id']} / 대상 종목: "
            f"{instrument_label(meta, fill_rows)} / 기간: "
            f"{daily_rows[0]['date']} ~ {daily_rows[-1]['date']} / "
            f"에이전트: {len(agent_ids)}명",
            styles["KBody"],
        )
    )

    action_counts = Counter(
        str(row.get("action") or "missing") for row in agent_rows
    )
    action_count_text = ", ".join(
        f"{action_ko(action)} {count}건"
        for action, count in sorted(action_counts.items())
    )
    total_order_qty = sum(int(num(row["quantity"])) for row in order_rows)
    total_fill_qty = sum(int(row_quantity(row)) for row in fill_rows)
    story.append(para("1. 실행 개요", styles["KHeading1"]))
    overview = [
        ["항목", "내용"],
        [
            "실행 조건",
            f"에이전트 {meta['agent_count']}명, {meta['date_count']}거래일, "
            f"기간={meta.get('start_date') or daily_rows[0]['date']}~{meta.get('end_date') or daily_rows[-1]['date']}, "
            f"seed={meta['random_seed']}, concurrency={meta.get('concurrency', 'N/A')}, agent_selection={meta.get('agent_selection', 'legacy')}, "
            f"information_mode={meta.get('information_mode', 'pre_close_cutoff')}, exchange_mode={meta.get('exchange_mode', 'announced_price_binary')}",
        ],
        ["전체 에이전트", ", ".join(agent_ids)],
        [
            "보고서 대표 에이전트",
            ", ".join(f"{agent_id} ({representative_reasons.get(agent_id, '')})" for agent_id in representative_agents),
        ],
        ["전체 판단", f"총 {len(agent_rows)}건: {action_count_text}"],
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

    story.append(
        para(
            "1-1. 정본 계보·기억·체결·성과 커버리지",
            styles["KHeading1"],
        )
    )
    expected = int(lineage_summary["expected_event_rows"])
    lineage_status = {
        "complete": "완전",
        "incomplete": "불완전",
        "missing_db": "DB 누락",
        "missing_tables": "필수 테이블 누락",
        "missing_scope": "agent/event 범위 누락",
    }.get(str(lineage_summary["status"]), str(lineage_summary["status"]))
    outcome_status = (
        "종료 처리 완료"
        if complete.get("outcome_finalized")
        else "실험 종료 전 또는 종료 처리 미완료"
    )
    horizon_labels = {
        "next_turn": "성과 next-turn",
        "h1": "성과 H1",
        "h5": "성과 H5",
    }
    horizon_descriptions = {
        "next_turn": (
            "다음 decision event에 도래하는 동일 fill episode의 첫 성과 관측"
        ),
        "h1": (
            "다음 거래일 동일 subturn에 시간 gate가 열리는 동일 fill "
            "episode 성과"
        ),
        "h5": (
            "5거래일 뒤 동일 subturn에 시간 gate가 열리는 동일 fill "
            "episode 성과"
        ),
    }
    horizon_rows = []
    for horizon in OUTCOME_HORIZONS:
        counts = lineage_summary["outcome_horizon_counts"][horizon]
        horizon_rows.append(
            [
                horizon_labels[horizon],
                (
                    f"matured {counts['matured_count']}, "
                    f"right-censored {counts['right_censored_count']}, "
                    f"consumed {counts['consumed_count']} "
                    f"(LTB linked {counts['consumption_linked_count']})"
                ),
                (
                    f"terminal {counts['total_count']}/"
                    f"{counts['expected_terminal_count']}; "
                    f"{horizon_descriptions[horizon]}"
                ),
            ]
        )
    lineage_rows = [
        ["구간", "관측/기대", "판정·근거"],
        [
            "초기 LTB",
            f"{lineage_summary['initial_ltb_count']}/{lineage_summary['agent_count']}",
            "에이전트별 turn 0 장기 신념",
        ],
        [
            "STB → analysis",
            f"{lineage_summary['stb_count']}/{expected}, "
            f"{lineage_summary['analysis_count']}/{expected}",
            (
                "DB 정본 집계; agent_turns.jsonl analysis export "
                f"{lineage_summary['analysis_log_count']}/{expected}"
            ),
        ],
        [
            "decision → full fill",
            f"{lineage_summary['decision_count']}/{expected}, "
            f"{lineage_summary['fill_count']}/{expected}",
            "의도와 실제 체결을 별도 정본 행으로 집계",
        ],
        [
            "post-fill LTB",
            f"{lineage_summary['post_fill_ltb_count']}/{expected}",
            "현재 fill 이후 생성되어 다음 event부터 가시",
        ],
        [
            "완전 연결 chain",
            f"{lineage_summary['complete_chain_count']}/{expected}",
            (
                f"{lineage_status}; source LTB/STB·analysis·decision·fill "
                "FK 연결"
            ),
        ],
        [
            "거래 성과(전체 horizon)",
            (
                f"matured {lineage_summary['outcome_matured_count']}, "
                f"right-censored {lineage_summary['outcome_right_censored_count']}, "
                f"consumed {lineage_summary['outcome_consumed_count']} "
                f"(LTB linked "
                f"{lineage_summary['outcome_consumption_linked_count']})"
            ),
            (
                f"terminal {lineage_summary['outcome_total_count']}/"
                f"{lineage_summary['expected_terminal_outcome_count']}; "
                f"{outcome_status}"
            ),
        ],
        *horizon_rows,
        [
            "커뮤니티 게시 계보",
            (
                f"{lineage_summary['community_post_linked_count']}/"
                f"{lineage_summary['community_post_count']}"
            ),
            "게시글이 동일 에이전트·event의 post-fill LTB/fill/decision과 연결",
        ],
        [
            "정본 DB",
            short(lineage_summary.get("db_path") or "누락", 120),
            (
                "누락 테이블: "
                + ", ".join(lineage_summary.get("missing_tables") or [])
                if lineage_summary.get("missing_tables")
                else "읽기 전용 집계"
            ),
        ],
    ]
    story.append(
        table(
            [[para(c, styles["KSmall"]) for c in row] for row in lineage_rows],
            [42 * mm, 47 * mm, 81 * mm],
        )
    )
    story.append(
        para(
            "next-turn/H1/H5는 체결을 세 번 센 독립 거래가 아니라, 각 fill "
            "episode를 서로 다른 도래 시점에서 관찰한 결과다. H1/H5는 due "
            "event가 실제 도래한 뒤에만 성숙·소비되며, 미도래 관측은 "
            "right-censored로 남는다.",
            styles["KSmall"],
        )
    )

    story.append(para("1-2. Reasoning-off 실행 증거", styles["KHeading1"]))
    reasoning_status = {
        "verified": "run-local telemetry 확인",
        "missing_run_local_telemetry": "run-local telemetry 누락",
        "invalid_policy": "strict-off 정책 불일치",
        "no_successful_provider_return": "성공 provider 응답 없음",
        "telemetry_mismatch": "telemetry 불일치",
    }.get(str(reasoning_summary["status"]), str(reasoning_summary["status"]))
    reasoning_rows = [
        ["항목", "값", "해석"],
        [
            "정책",
            "strict" if reasoning_summary["policy_strict"] else "invalid",
            "reasoning effort=none, 단일 provider, fallback 금지",
        ],
        [
            "상태",
            reasoning_status,
            "run 폴더 밖의 공용 audit는 증거로 사용하지 않음",
        ],
        [
            "API 물리 호출",
            (
                f"attempt {reasoning_summary['provider_attempt_count']}, "
                f"return {reasoning_summary['provider_return_count']}, "
                f"error {reasoning_summary['provider_error_count']}"
            ),
            f"acceptance {reasoning_summary['acceptance_count']}",
        ],
        [
            "reasoning token",
            (
                f"zero {reasoning_summary['reasoning_zero_count']}, "
                f"nonzero {reasoning_summary['reasoning_nonzero_count']}, "
                f"missing {reasoning_summary['reasoning_missing_count']}"
            ),
            (
                "reasoning payload present "
                f"{reasoning_summary['response_reasoning_present_count']}"
            ),
        ],
        [
            "model/provider",
            (
                f"{', '.join(reasoning_summary['models']) or '-'} / "
                f"{', '.join(reasoning_summary['providers']) or '-'}"
            ),
            f"audit files {len(reasoning_summary['audit_paths'])}개",
        ],
    ]
    story.append(
        table(
            [[para(c, styles["KSmall"]) for c in row] for row in reasoning_rows],
            [38 * mm, 62 * mm, 70 * mm],
        )
    )

    if community_posts or community_interactions or community_logs:
        story.append(para("1-3. Community 기능 체크리스트", styles["KHeading1"]))
        depth_by_agent = {str(agent_id): int(depth) for agent_id, depth in (meta.get("agent_depths") or {}).items()}
        depth0_ids = {agent_id for agent_id, depth in depth_by_agent.items() if depth == 0}
        depth12_ids = {agent_id for agent_id, depth in depth_by_agent.items() if depth >= 1}
        post_agents = {row.get("agent_id") for row in community_posts}
        selective_reading_agents = {
            row.get("agent_id")
            for row in community_reactions
            if row.get("post_id")
        }
        best_recipient_agents = {
            row.get("agent_id")
            for row in community_best_deliveries
            if row.get("post_id")
        }
        delivered_best_keys = {
            (
                str(row.get("source_date") or row.get("date") or ""),
                str(row.get("source_turn") or row.get("turn") or ""),
                str(row.get("post_id") or ""),
            )
            for row in community_best_deliveries
            if row.get("post_id")
        }
        depth0_best_deliveries = {
            (
                str(row.get("source_date") or row.get("date") or ""),
                str(row.get("source_turn") or row.get("turn") or ""),
                str(row.get("post_id") or ""),
                str(row.get("agent_id") or ""),
            )
            for row in community_best_deliveries
            if str(row.get("agent_id") or "") in depth0_ids
            and row.get("exposure_level") == "full_body"
            and str(row.get("content") or "").strip()
        }
        expected_depth0_best_deliveries = len(delivered_best_keys) * len(depth0_ids)
        if not delivered_best_keys:
            depth0_best_status = "해당 없음"
        elif len(depth0_best_deliveries) == expected_depth0_best_deliveries:
            depth0_best_status = "OK"
        else:
            depth0_best_status = "확인 필요"
        thinking_agents = {
            row["agent"]["agent_id"]
            for row in turn_rows
            if (row.get("context") or {}).get("community_thinking")
        }
        profile_rows = [
            row
            for row in community_interactions
            if row.get("author_profile") not in {"", "null", None}
        ]
        profile_reader_ids = {str(row.get("agent_id") or "") for row in profile_rows}
        depth2_ids = {agent_id for agent_id, depth in depth_by_agent.items() if depth == 2}
        checks = [
            ["항목", "상태", "근거"],
            ["Depth 0 선택 읽기·작성 금지", "OK" if not (post_agents | selective_reading_agents) & depth0_ids else "확인 필요", f"Depth0={sorted(depth0_ids)}"],
            [
                "Depth 0 Best 원문 수신",
                depth0_best_status,
                (
                    f"full-body deliveries={len(depth0_best_deliveries)}/"
                    f"{expected_depth0_best_deliveries}, "
                    f"recipients={sorted(best_recipient_agents & depth0_ids)}"
                ),
            ],
            ["Depth 1/2 포스팅", "OK" if post_agents <= depth12_ids else "확인 필요", f"post_agents={sorted(post_agents)}"],
            ["게시글 후보 입력 로그", "OK" if community_selection_inputs else "누락", f"{len(community_selection_inputs)} rows"],
            ["후보 title-only 로그", "OK" if community_title_exposures else "누락", f"{len(community_title_exposures)} rows"],
            ["PM 본문 읽기/반응 로그", "OK" if community_reactions else "누락", f"{len(community_reactions)} rows"],
            ["Depth 2 전용 상세 프로필", "OK" if profile_reader_ids <= depth2_ids else "확인 필요", f"profile readers={sorted(profile_reader_ids)}"],
            ["Best 게시글 선정", "OK" if community_best_posts else "누락", f"{len(community_best_posts)} rows"],
            [
                "Best 자기 글 제외",
                (
                    "OK"
                    if exposure_summary["self_delivery_violation_count"] == 0
                    else "위반"
                ),
                (
                    f"기록된 제외 {exposure_summary['self_excluded_count']}건, "
                    f"자기 글 실제 전달 {exposure_summary['self_delivery_violation_count']}건"
                ),
            ],
            [
                "본문·provenance 무결성",
                (
                    "OK"
                    if exposure_summary["missing_full_body_count"] == 0
                    and exposure_summary["missing_body_hash_count"] == 0
                    and exposure_summary["title_only_body_leak_count"] == 0
                    and exposure_summary["orphan_exposure_count"] == 0
                    and exposure_summary["duplicate_provenance_count"] == 0
                    else "확인 필요"
                ),
                (
                    f"본문 누락 {exposure_summary['missing_full_body_count']}건, "
                    f"본문 hash 누락 {exposure_summary['missing_body_hash_count']}건, "
                    f"title-only 본문 유출 {exposure_summary['title_only_body_leak_count']}건, "
                    f"orphan {exposure_summary['orphan_exposure_count']}건, "
                    f"중복 provenance {exposure_summary['duplicate_provenance_count']}건"
                ),
            ],
            ["다음날 Community Thinking", "OK" if thinking_agents else "확인 필요", f"thinking_agents={sorted(thinking_agents)}"],
        ]
        story.append(table([[para(c, styles["KSmall"]) for c in row] for row in checks], [45 * mm, 25 * mm, 100 * mm]))
        reactions = Counter(row.get("reaction") for row in community_reactions if row.get("reaction"))
        by_post_agent = Counter(row.get("agent_id") for row in community_posts)
        by_read_agent = Counter(
            row.get("agent_id")
            for row in community_reactions
            if row.get("post_id")
        )
        community_summary = [
            ["항목", "내용"],
            [
                "커뮤니티 규모",
                (
                    f"게시글 {len(community_posts)}건, "
                    f"title-only {exposure_summary['title_only_count']}건, "
                    f"full-body {exposure_summary['full_body_count']}건, "
                    f"PM 선택 본문 {exposure_summary['pm_selected_full_body_count']}건, "
                    f"Best AM 본문 {exposure_summary['best_full_body_delivery_count']}건, "
                    f"선택 본문 재전달 {exposure_summary['selected_full_body_replay_count']}건, "
                    f"Best 선정 {len(community_best_posts)}건"
                ),
            ],
            ["반응 분포", ", ".join(f"{key}: {value}" for key, value in sorted(reactions.items())) or "-"],
            ["Agent별 포스팅", ", ".join(f"{key}: {value}" for key, value in sorted(by_post_agent.items())) or "-"],
            ["Agent별 PM 선택 읽기", ", ".join(f"{key}: {value}" for key, value in sorted(by_read_agent.items())) or "-"],
            [
                "Best delivery 상태",
                ", ".join(
                    f"{key}: {value}"
                    for key, value in exposure_summary[
                        "best_delivery_statuses"
                    ].items()
                )
                or "-",
            ],
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
                "other_count": sum(
                    value
                    for action, value in counts.items()
                    if action not in {"buy", "sell"}
                ),
                "main_sentiment": main_sentiment,
                "turns": turns,
            }
        )
        note = (
            f"기록된 뉴스 감성 최빈값은 {main_sentiment or '-'}, "
            f"주문은 매수 {order_counts.get('buy', 0)}건, "
            f"매도 {order_counts.get('sell', 0)}건. "
        )
        if num(row["volume"]) == 0:
            note += "제출 주문은 있었지만 당일 체결은 발생하지 않음."
        else:
            note += (
                f"체결 순수량은 {net_qty:+,}주이며, "
                "뉴스·커뮤니티의 인과 효과는 이 표만으로 판단하지 않음."
            )
        daily_table.append(
            [
                f"{date}\n{row.get('subturn', '').upper()}",
                f"{'시가' if str(row.get('subturn', '')).lower() == 'am' else '종가'} {money(row.get('announced_price'))}",
                money(open_by_date.get(date)),
                money(row_close(row)),
                f"매수 {order_counts.get('buy', 0)} / 매도 {order_counts.get('sell', 0)}",
                f"{int(num(row['volume'])):,}주 / {row['fill_count']}건",
                net_text,
                (
                    f"매수 {counts.get('buy', 0)} / "
                    f"매도 {counts.get('sell', 0)} / "
                    f"기타 {sum(value for key, value in counts.items() if key not in {'buy', 'sell'})}"
                ),
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
            "이는 실행 종료 시점의 기술통계이며 원인을 뜻하지 않는다.",
        ],
        [
            "집단 방향성",
            f"전체 체결 기준 {'순매수' if net_total > 0 else '순매도' if net_total < 0 else '중립'} {abs(net_total):,.0f}주. "
            f"매수 우위 event {buy_bias_days}회, 매도 우위 event {sell_bias_days}회가 관찰됐다.",
        ],
        [
            "모델 안정성",
            f"의사결정 폴백 {fallback_count}건. 폴백은 행동 결과와 분리해 품질 이상으로 확인해야 한다.",
        ],
    ]
    story.append(table([[para(c, styles["KSmall"]) for c in row] for row in observation_rows], [38 * mm, 132 * mm]))

    story.append(para("4. 변곡일 분석", styles["KHeading1"]))
    pivot_days = sorted(
        daily_insights,
        key=lambda item: (abs(item["net_qty"]), abs(item["pct_chg"])),
        reverse=True,
    )[: min(5, len(daily_insights))]
    pivot_rows = [["일자", "가격/변동", "집단 행동", "노출 뉴스 요약", "관찰 메모"]]
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
            meaning = "이 event에서는 매수 판단 수가 매도 판단 수보다 많았다."
        elif item["sell_count"] > item["buy_count"]:
            meaning = "이 event에서는 매도 판단 수가 매수 판단 수보다 많았다."
        else:
            meaning = "매수·매도 판단 수가 같았다."
        pivot_rows.append(
            [
                str(item["date"]),
                f"{money(item['close'])}\n{pct_points(item['pct_chg'])}",
                (
                    f"{net_text}\n매수 {item['buy_count']} / "
                    f"매도 {item['sell_count']} / 기타 {item['other_count']}"
                ),
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

    story.append(
        KeepTogether(
            [
                para("6. 종합 결론", styles["KHeading1"]),
                para(
                    f"이번 {meta['date_count']}거래일 실행 보고서는 각 event의 STB, 분석, decision, 실제 fill, "
                    "post-fill LTB와 사용 가능해진 거래 성과를 정본 계보로 대조했다. 커뮤니티 ON이면 title-only 후보 "
                    "노출과 full-body 읽기, Best 다음 AM 전달을 서로 다른 관계로 집계했다. 표에 제시한 뉴스·커뮤니티 "
                    "노출과 거래 방향의 동시 관찰은 인과 효과를 뜻하지 않는다. 조건 간 효과 판단은 사전에 정한 "
                    "RN_COMM_OFF/ON 비교 지표와 별도 통계 검정을 사용해야 한다.",
                    styles["KBody"],
                ),
            ]
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
