#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_DIR = PROJECT_ROOT / "validation"
DEFAULT_RUN_DIR = PROJECT_ROOT / "outputs" / "logs" / "current"
ACTUAL_INVESTOR_COLUMNS = [
    "Individuals",
    "Subtotal-Institutions",
    "Total of foreign",
    "Other corporations",
]
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare simulated LLM net buy/sell direction with Samsung Electronics investor net trading data."
    )
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR, help="Simulation log directory.")
    parser.add_argument("--actual-value", type=Path, default=VALIDATION_DIR / "data_trading_value.csv")
    parser.add_argument("--actual-volume", type=Path, default=VALIDATION_DIR / "data_trading_volume.csv")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--stock-code", default="005930")
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=None,
        help="Optional inclusive validation start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=None,
        help="Optional inclusive validation end date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--skip-initial-days",
        type=int,
        default=5,
        help="Exclude the first N overlapping trading days from validation metrics and charts.",
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"unsupported date format: {value!r}")


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def sign(value: float, eps: float = 1e-9) -> int:
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def direction_label(value: float) -> str:
    side = sign(value)
    if side > 0:
        return "net_buy"
    if side < 0:
        return "net_sell"
    return "flat"


def direction_label_ko(value: float) -> str:
    side = sign(value)
    if side > 0:
        return "순매수"
    if side < 0:
        return "순매도"
    return "중립"


def fmt_int(value: Any) -> str:
    return f"{num(value):,.0f}"


def pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value * 100:.1f}%"


def score_text(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:.3f}"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / denom


def cosine(xs: list[float], ys: list[float]) -> float | None:
    if not xs or len(xs) != len(ys):
        return None
    denom = math.sqrt(sum(x * x for x in xs) * sum(y * y for y in ys))
    if denom == 0:
        return None
    return sum(x * y for x, y in zip(xs, ys)) / denom


def max_abs_normalize(values: list[float]) -> list[float]:
    scale = max([abs(value) for value in values] or [0.0])
    if scale == 0:
        return [0.0 for _ in values]
    return [value / scale for value in values]


def z_score_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    std = math.sqrt(variance)
    if std == 0:
        return [0.0 for _ in values]
    return [(value - mean) / std for value in values]


def cumulative(values: list[float]) -> list[float]:
    total = 0.0
    result = []
    for value in values:
        total += value
        result.append(total)
    return result


def direction_match_rate(xs: list[float], ys: list[float], *, ignore_zero: bool = False) -> float | None:
    pairs = []
    for x, y in zip(xs, ys):
        sx, sy = sign(x), sign(y)
        if ignore_zero and (sx == 0 or sy == 0):
            continue
        pairs.append((sx, sy))
    if not pairs:
        return None
    return sum(1 for sx, sy in pairs if sx == sy) / len(pairs)


def sign_metrics(sim_values: list[float], actual_values: list[float]) -> dict[str, Any]:
    sim_signs = [sign(value) for value in sim_values]
    actual_signs = [sign(value) for value in actual_values]
    pairs = list(zip(sim_signs, actual_signs))
    confusion = {
        "actual_buy": {"pred_buy": 0, "pred_sell": 0, "pred_flat": 0},
        "actual_sell": {"pred_buy": 0, "pred_sell": 0, "pred_flat": 0},
        "actual_flat": {"pred_buy": 0, "pred_sell": 0, "pred_flat": 0},
    }
    actual_key = {1: "actual_buy", -1: "actual_sell", 0: "actual_flat"}
    pred_key = {1: "pred_buy", -1: "pred_sell", 0: "pred_flat"}
    for pred, actual in pairs:
        confusion[actual_key[actual]][pred_key[pred]] += 1

    actual_buy = sum(1 for value in actual_signs if value > 0)
    actual_sell = sum(1 for value in actual_signs if value < 0)
    buy_recall = (
        sum(1 for pred, actual in pairs if actual > 0 and pred > 0) / actual_buy
        if actual_buy
        else None
    )
    sell_recall = (
        sum(1 for pred, actual in pairs if actual < 0 and pred < 0) / actual_sell
        if actual_sell
        else None
    )
    recalls = [value for value in (buy_recall, sell_recall) if value is not None]
    balanced_accuracy = sum(recalls) / len(recalls) if recalls else None
    nonzero_pairs = [(pred, actual) for pred, actual in pairs if pred != 0 and actual != 0]
    nonzero_match = (
        sum(1 for pred, actual in nonzero_pairs if pred == actual) / len(nonzero_pairs)
        if nonzero_pairs
        else None
    )
    return {
        "direction_match_rate": direction_match_rate(sim_values, actual_values),
        "nonzero_direction_match_rate": nonzero_match,
        "buy_recall": buy_recall,
        "sell_recall": sell_recall,
        "sell_day_recall": sell_recall,
        "balanced_accuracy": balanced_accuracy,
        "confusion_matrix": confusion,
        "actual_direction_counts": {
            "buy": actual_buy,
            "sell": actual_sell,
            "flat": sum(1 for value in actual_signs if value == 0),
        },
        "predicted_direction_counts": {
            "buy": sum(1 for value in sim_signs if value > 0),
            "sell": sum(1 for value in sim_signs if value < 0),
            "flat": sum(1 for value in sim_signs if value == 0),
        },
    }


def compute_direction_metrics(llm_net_buy: list[float], real_net_buy: list[float]) -> dict[str, float]:
    if len(llm_net_buy) != len(real_net_buy):
        raise ValueError("llm_net_buy and real_net_buy must have the same length")
    if not llm_net_buy:
        return {
            "direction_match_rate": 0.0,
            "buy_recall": 0.0,
            "sell_recall": 0.0,
            "balanced_accuracy": 0.0,
        }
    tp_buy = sum(1 for llm, real in zip(llm_net_buy, real_net_buy) if llm > 0 and real > 0)
    fn_buy = sum(1 for llm, real in zip(llm_net_buy, real_net_buy) if llm <= 0 and real > 0)
    tp_sell = sum(1 for llm, real in zip(llm_net_buy, real_net_buy) if llm < 0 and real < 0)
    fn_sell = sum(1 for llm, real in zip(llm_net_buy, real_net_buy) if llm >= 0 and real < 0)
    direction_match = sum(
        1
        for llm, real in zip(llm_net_buy, real_net_buy)
        if (llm > 0 and real > 0) or (llm < 0 and real < 0)
    ) / len(llm_net_buy)
    buy_recall = tp_buy / (tp_buy + fn_buy) if (tp_buy + fn_buy) else 0.0
    sell_recall = tp_sell / (tp_sell + fn_sell) if (tp_sell + fn_sell) else 0.0
    return {
        "direction_match_rate": round(direction_match, 4),
        "buy_recall": round(buy_recall, 4),
        "sell_recall": round(sell_recall, 4),
        "balanced_accuracy": round((buy_recall + sell_recall) / 2, 4),
    }


def baseline_metrics(actual_values: list[float], market_return_values: list[float] | None = None) -> dict[str, Any]:
    n = len(actual_values)
    if n == 0:
        return {}
    actual_signs = [sign(value) for value in actual_values]
    buy_ratio = sum(1 for value in actual_signs if value > 0) / n
    rng_seed = 20260625

    def random_series(prob_buy: float) -> list[float]:
        import random

        rng = random.Random(rng_seed + int(prob_buy * 1000))
        return [1.0 if rng.random() < prob_buy else -1.0 for _ in range(n)]

    previous_actual = [0.0]
    previous_actual.extend(float(value) for value in actual_signs[:-1])
    previous_market = [0.0 for _ in range(n)]
    if market_return_values:
        market_signs = [sign(value) for value in market_return_values]
        previous_market = [0.0, *[float(value) for value in market_signs[:-1]]][:n]

    baselines = {
        "always_buy": [1.0] * n,
        "always_sell": [-1.0] * n,
        "random_50_50": random_series(0.5),
        "actual_ratio_random": random_series(buy_ratio),
        "previous_day_individual_direction": previous_actual,
        "previous_day_market_return_direction": previous_market,
    }
    return {name: sign_metrics(values, actual_values) for name, values in baselines.items()}


def load_actual(path: Path) -> dict[str, dict[str, float]]:
    rows = read_csv(path)
    actual: dict[str, dict[str, float]] = {}
    for row in rows:
        date = parse_date(row["Date"])
        actual[date] = {column: num(row.get(column)) for column in ACTUAL_INVESTOR_COLUMNS}
    return actual


def load_simulation(run_dir: Path, stock_code: str) -> tuple[str, dict[str, dict[str, float]]]:
    run_dir = run_dir.resolve()
    fills_path = run_dir / "exchange_fills.csv"
    daily_path = run_dir / "daily_exchange_summary.csv"
    metadata_path = run_dir / "run_metadata.json"
    fills = read_csv(fills_path)
    run_id = run_dir.name
    if metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as f:
            run_id = json.load(f).get("run_id") or run_id

    daily: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "llm_volume": 0.0,
            "llm_value": 0.0,
            "llm_buy_volume": 0.0,
            "llm_sell_volume": 0.0,
            "closing_price": 0.0,
            "market_return": 0.0,
        }
    )
    if daily_path.exists():
        daily_rows = [row for row in read_csv(daily_path) if str(row.get("stock_code") or stock_code) == stock_code]
        daily_rows.sort(key=lambda row: (parse_date(row["date"]), int(num(row.get("turn")))))
        close_by_date: dict[str, float] = {}
        for row in daily_rows:
            date = parse_date(row["date"])
            close_by_date[date] = num(row.get("close_price") or row.get("closing_price") or row.get("announced_price"))
        previous_close = None
        for date, close in sorted(close_by_date.items()):
            daily[date]["closing_price"] = close
            if previous_close and previous_close != 0:
                daily[date]["market_return"] = (close - previous_close) / previous_close
            previous_close = close
    for row in fills:
        if str(row.get("stock_code") or stock_code) != stock_code:
            continue
        date = parse_date(row["date"])
        qty = num(row.get("quantity") or row.get("executed_quantity") or row.get("filled_quantity"))
        price = num(row.get("executed_price"))
        action = str(row.get("action") or row.get("direction") or "").lower()
        signed_qty = qty if action == "buy" else -qty
        signed_value = signed_qty * price
        daily[date]["llm_volume"] += signed_qty
        daily[date]["llm_value"] += signed_value
        if signed_qty > 0:
            daily[date]["llm_buy_volume"] += qty
        elif signed_qty < 0:
            daily[date]["llm_sell_volume"] += qty
    return run_id, dict(daily)


def load_run_metadata(run_dir: Path) -> dict[str, Any]:
    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.exists():
        return {}
    with metadata_path.open(encoding="utf-8") as f:
        return json.load(f)


def metric_bundle(sim_values: list[float], actual_values: list[float]) -> dict[str, Any]:
    sim_max_abs = max_abs_normalize(sim_values)
    actual_max_abs = max_abs_normalize(actual_values)
    sim_z = z_score_normalize(sim_values)
    actual_z = z_score_normalize(actual_values)
    sim_cumulative = max_abs_normalize(cumulative(sim_values))
    actual_cumulative = max_abs_normalize(cumulative(actual_values))
    primary = sign_metrics(sim_values, actual_values)
    return {
        "days": len(sim_values),
        **primary,
        "pearson_correlation": pearson(sim_values, actual_values),
        "cosine_similarity": cosine(sim_values, actual_values),
        "max_abs_normalized": {
            "pearson_correlation": pearson(sim_max_abs, actual_max_abs),
            "cosine_similarity": cosine(sim_max_abs, actual_max_abs),
        },
        "z_score_normalized": {
            "pearson_correlation": pearson(sim_z, actual_z),
            "cosine_similarity": cosine(sim_z, actual_z),
        },
        "cumulative_max_abs_normalized": {
            "pearson_correlation": pearson(sim_cumulative, actual_cumulative),
            "cosine_similarity": cosine(sim_cumulative, actual_cumulative),
        },
    }


def build_comparison_rows(
    *,
    label: str,
    actual: dict[str, dict[str, float]],
    simulation: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sim_key = "llm_value" if label == "value" else "llm_volume"
    for date in sorted(set(actual) & set(simulation)):
        sim = simulation[date]
        actual_row = actual[date]
        row: dict[str, Any] = {
            "date": date,
            "llm_net": sim[sim_key],
            "llm_direction": direction_label(sim[sim_key]),
            "market_return": sim.get("market_return", 0.0),
        }
        for column in ACTUAL_INVESTOR_COLUMNS:
            row[column] = actual_row[column]
            row[f"{column}_direction"] = direction_label(actual_row[column])
        row["llm_matches_individuals"] = int(sign(sim[sim_key]) == sign(actual_row["Individuals"]))
        rows.append(row)
    return rows


def skip_initial_rows(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    if days <= 0:
        return rows
    return rows[days:]


def filter_date_range(
    rows: list[dict[str, Any]],
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    """Keep rows inside the optional inclusive validation date range."""
    return [
        row
        for row in rows
        if (start_date is None or row["date"] >= start_date)
        and (end_date is None or row["date"] <= end_date)
    ]


def summarize_dimension(rows: list[dict[str, Any]], metric_key: str) -> dict[str, Any]:
    llm_values = [num(row["llm_net"]) for row in rows]
    actual_individuals = [num(row["Individuals"]) for row in rows]
    market_returns = [num(row.get("market_return")) for row in rows]
    primary_metrics = compute_direction_metrics(llm_values, actual_individuals)
    bundle = metric_bundle(llm_values, actual_individuals)
    summary: dict[str, Any] = {
        "metric": metric_key,
        "overlap_days": len(rows),
        "primary_metrics": primary_metrics,
        "reference_metrics": {
            "pearson_daily": bundle["pearson_correlation"],
            "pearson_cumulative": bundle["cumulative_max_abs_normalized"]["pearson_correlation"],
            "note": "누적 Pearson은 상승장 + 단일가 체결 구조상 구조적 역전이 발생할 수 있음",
        },
        "llm_vs_individuals": bundle,
        "baselines_vs_individuals": baseline_metrics(actual_individuals, market_returns),
    }
    return summary


def build_normalized_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series_names = ["llm_net", *ACTUAL_INVESTOR_COLUMNS]
    raw_series = {name: [num(row[name]) for row in rows] for name in series_names}
    max_abs_series = {name: max_abs_normalize(values) for name, values in raw_series.items()}
    z_score_series = {name: z_score_normalize(values) for name, values in raw_series.items()}
    cumulative_series = {name: max_abs_normalize(cumulative(values)) for name, values in raw_series.items()}

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized: dict[str, Any] = {
            "date": row["date"],
            "llm_direction": row["llm_direction"],
            "llm_matches_individuals": row["llm_matches_individuals"],
        }
        for name in series_names:
            normalized[f"{name}_raw"] = raw_series[name][index]
            normalized[f"{name}_max_abs"] = max_abs_series[name][index]
            normalized[f"{name}_z_score"] = z_score_series[name][index]
            normalized[f"{name}_cumulative_max_abs"] = cumulative_series[name][index]
        normalized_rows.append(normalized)
    return normalized_rows


def normalized_fieldnames() -> list[str]:
    series_names = ["llm_net", *ACTUAL_INVESTOR_COLUMNS]
    fields = ["date", "llm_direction", "llm_matches_individuals"]
    for name in series_names:
        fields.extend(
            [
                f"{name}_raw",
                f"{name}_max_abs",
                f"{name}_z_score",
                f"{name}_cumulative_max_abs",
            ]
        )
    return fields


class NormalizedLineChart(Flowable):
    def __init__(self, title: str, dates: list[str], series: list[tuple[str, list[float], colors.Color]], width: float):
        super().__init__()
        self.title = title
        self.dates = dates
        self.series = series
        self.width = width
        self.height = 66 * mm

    def draw(self) -> None:
        c = self.canv
        x0, y0 = 8 * mm, 21 * mm
        w, h = self.width - 16 * mm, self.height - 32 * mm
        c.setFont("Korean", 8)
        c.setFillColor(colors.HexColor("#1f2937"))
        c.drawString(x0, y0 + h + 7 * mm, self.title)
        c.setStrokeColor(colors.HexColor("#cbd5e1"))
        c.line(x0, y0 + h / 2, x0 + w, y0 + h / 2)
        c.rect(x0, y0, w, h, stroke=1, fill=0)
        if len(self.dates) < 2:
            c.drawString(x0, y0 + h / 2 + 3 * mm, "비교 가능한 날짜가 부족합니다.")
            return
        all_values = [abs(value) for _, values, _ in self.series for value in values]
        max_abs = max(all_values or [1.0]) or 1.0
        for name, values, color in self.series:
            points = []
            for idx, value in enumerate(values):
                x = x0 + (w * idx / max(1, len(values) - 1))
                y = y0 + h / 2 + (value / max_abs) * (h * 0.42)
                points.append((x, y))
            c.setStrokeColor(color)
            c.setLineWidth(1.2)
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                c.line(x1, y1, x2, y2)
            c.setFillColor(color)
            c.circle(points[-1][0], points[-1][1], 1.4, stroke=0, fill=1)
        legend_x = x0
        legend_y = y0 + h - 5 * mm
        for name, _, color in self.series:
            c.setFillColor(color)
            c.rect(legend_x, legend_y, 3 * mm, 3 * mm, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#475569"))
            c.drawString(legend_x + 4 * mm, legend_y + 0.4 * mm, name)
            legend_x += 35 * mm
        c.setFillColor(colors.HexColor("#64748b"))
        c.setStrokeColor(colors.HexColor("#94a3b8"))
        c.setLineWidth(0.3)
        c.setFont("Korean", 5.4)
        tick_count = min(10, len(self.dates))
        tick_indices = {
            round(index * (len(self.dates) - 1) / max(1, tick_count - 1))
            for index in range(tick_count)
        }
        for idx in sorted(tick_indices):
            date = self.dates[idx]
            x = x0 + (w * idx / max(1, len(self.dates) - 1))
            c.line(x, y0, x, y0 - 1.5 * mm)
            c.saveState()
            c.translate(x - 0.8 * mm, y0 - 7.5 * mm)
            c.rotate(45)
            c.drawString(0, 0, date)
            c.restoreState()


def para(text: Any, style: ParagraphStyle) -> Paragraph:
    safe = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe.replace("\n", "<br/>"), style)


def table(data: list[list[Any]], widths: list[float] | None = None) -> Table:
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Korean"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.2),
                ("FONTSIZE", (0, 1), (-1, -1), 7.2),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#23395d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def metric_table(title: str, summary: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    rows = [["비교", "일수", "방향", "Balanced", "Buy recall", "Sell recall", "MaxAbs r", "누적 r"]]
    primary = summary["llm_vs_individuals"]
    rows.append(
        [
            "LLM vs 개인",
            primary["days"],
            pct(primary["direction_match_rate"]),
            pct(primary["balanced_accuracy"]),
            pct(primary["buy_recall"]),
            pct(primary["sell_recall"]),
            score_text(primary["max_abs_normalized"]["pearson_correlation"]),
            score_text(primary["cumulative_max_abs_normalized"]["pearson_correlation"]),
        ]
    )
    return [
        para(title, styles["KHeading2"]),
        table(
            [[para(c, styles["KSmall"]) for c in row] for row in rows],
            [39 * mm, 13 * mm, 18 * mm, 19 * mm, 19 * mm, 19 * mm, 19 * mm, 19 * mm],
        ),
    ]


def baseline_table(title: str, summary: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    rows = [["Baseline", "방향", "Balanced", "Buy recall", "Sell recall"]]
    for name, metric in summary.get("baselines_vs_individuals", {}).items():
        rows.append(
            [
                name,
                pct(metric.get("direction_match_rate")),
                pct(metric.get("balanced_accuracy")),
                pct(metric.get("buy_recall")),
                pct(metric.get("sell_recall")),
            ]
        )
    return [
        para(title, styles["KHeading2"]),
        table([[para(c, styles["KSmall"]) for c in row] for row in rows], [55 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm]),
    ]


def confusion_table(title: str, metric: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    matrix = metric.get("confusion_matrix") or {}
    rows = [["Actual \\ Pred", "Buy", "Sell", "Flat"]]
    for actual_key, label in (("actual_buy", "Actual buy"), ("actual_sell", "Actual sell"), ("actual_flat", "Actual flat")):
        row = matrix.get(actual_key) or {}
        rows.append([label, row.get("pred_buy", 0), row.get("pred_sell", 0), row.get("pred_flat", 0)])
    return [
        para(title, styles["KHeading2"]),
        table([[para(c, styles["KSmall"]) for c in row] for row in rows], [45 * mm, 25 * mm, 25 * mm, 25 * mm]),
    ]


def page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Korean", 8)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawString(18 * mm, 10 * mm, "TwinMarket Korea 거래 방향 검증 보고서")
    canvas.drawRightString(192 * mm, 10 * mm, f"{doc.page}")
    canvas.restoreState()


def build_report(
    *,
    report_path: Path,
    run_id: str,
    run_dir: Path,
    value_rows: list[dict[str, Any]],
    volume_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    font = register_font()
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="KTitle",
            parent=styles["Title"],
            fontName=font,
            fontSize=18,
            leading=24,
            alignment=TA_CENTER,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KHeading1",
            parent=styles["Heading1"],
            fontName=font,
            fontSize=13.5,
            leading=18,
            textColor=colors.HexColor("#23395d"),
            spaceBefore=12,
            spaceAfter=7,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KHeading2",
            parent=styles["Heading2"],
            fontName=font,
            fontSize=10.8,
            leading=14,
            textColor=colors.HexColor("#1f4e79"),
            spaceBefore=7,
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
            leading=10,
            alignment=TA_LEFT,
        )
    )

    story: list[Any] = []
    story.append(para("TwinMarket Korea 거래 방향 검증 보고서", styles["KTitle"]))
    story.append(
        para(
            f"실행 ID: {run_id} / information_mode={summary.get('information_mode', 'unknown')} / "
            f"limit_only={summary.get('limit_only_orders', 'unknown')} / 로그 위치: {run_dir} / "
            f"생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles["KBody"],
        )
    )
    validation_start = summary.get("validation_start_date") or "전체"
    validation_end = summary.get("validation_end_date") or "전체"
    skipped_days = int(summary.get("skip_initial_days") or 0)
    story.append(
        para(
            f"검증 구간: {validation_start} ~ {validation_end} / 초기 제외 거래일: {skipped_days}일",
            styles["KBody"],
        )
    )
    if skipped_days:
        story.append(
            para(
                f"검증 기준: 초기 {skipped_days}거래일을 제외하고 비교했다.",
                styles["KBody"],
            )
        )
    overlap_days = summary["value"]["overlap_days"]
    if overlap_days == 0:
        story.append(para("1. 검증 결과", styles["KHeading1"]))
        story.append(
            para(
                "시뮬레이션 로그 날짜와 실제 삼성전자 투자자별 순거래 CSV 날짜가 겹치지 않습니다. "
                "동일 기간으로 시뮬레이션을 다시 실행한 뒤 이 스크립트를 재실행해야 방향 일치율을 계산할 수 있습니다.",
                styles["KBody"],
            )
        )
    else:
        story.append(para("1. 핵심 검증 결과", styles["KHeading1"]))
        v_primary = summary["value"]["llm_vs_individuals"]
        q_primary = summary["volume"]["llm_vs_individuals"]
        story.append(
            para(
                "기본 검증은 LLM 에이전트 전체의 일별 순매수/순매도 sign이 실제 개인 투자자(Individuals)의 sign과 "
                "일치하는지 본다. 1차 지표는 방향 일치율, balanced accuracy, buy/sell recall이며, naive baseline과 "
                "같은 날짜 구간에서 비교한다. 상관계수와 코사인 유사도는 강도 패턴을 보는 2차 지표로 유지한다.",
                styles["KBody"],
            )
        )
        overview = [
            ["항목", "Value", "Volume"],
            ["겹치는 거래일", summary["value"]["overlap_days"], summary["volume"]["overlap_days"]],
            ["LLM vs 개인 방향 일치율", pct(v_primary["direction_match_rate"]), pct(q_primary["direction_match_rate"])],
            ["Balanced accuracy", pct(v_primary["balanced_accuracy"]), pct(q_primary["balanced_accuracy"])],
            ["Buy recall", pct(v_primary["buy_recall"]), pct(q_primary["buy_recall"])],
            ["Sell recall", pct(v_primary["sell_recall"]), pct(q_primary["sell_recall"])],
            [
                "MaxAbs 정규화 상관계수",
                score_text(v_primary["max_abs_normalized"]["pearson_correlation"]),
                score_text(q_primary["max_abs_normalized"]["pearson_correlation"]),
            ],
            [
                "MaxAbs 정규화 코사인",
                score_text(v_primary["max_abs_normalized"]["cosine_similarity"]),
                score_text(q_primary["max_abs_normalized"]["cosine_similarity"]),
            ],
            [
                "누적 정규화 상관계수",
                score_text(v_primary["cumulative_max_abs_normalized"]["pearson_correlation"]),
                score_text(q_primary["cumulative_max_abs_normalized"]["pearson_correlation"]),
            ],
        ]
        story.append(table([[para(c, styles["KSmall"]) for c in row] for row in overview], [50 * mm, 55 * mm, 55 * mm]))

        dates = [row["date"] for row in value_rows]
        value_llm = [num(row["llm_net"]) for row in value_rows]
        value_individuals = [num(row["Individuals"]) for row in value_rows]
        volume_llm = [num(row["llm_net"]) for row in volume_rows]
        volume_individuals = [num(row["Individuals"]) for row in volume_rows]
        story.append(
            NormalizedLineChart(
                "Value MaxAbs 정규화 순거래 흐름: LLM vs 개인",
                dates,
                [
                    ("LLM", max_abs_normalize(value_llm), colors.HexColor("#2563eb")),
                    ("Individuals", max_abs_normalize(value_individuals), colors.HexColor("#16a34a")),
                ],
                170 * mm,
            )
        )
        story.append(Spacer(1, 4 * mm))
        story.append(
            NormalizedLineChart(
                "Volume MaxAbs 정규화 순거래 흐름: LLM vs 개인",
                [row["date"] for row in volume_rows],
                [
                    ("LLM", max_abs_normalize(volume_llm), colors.HexColor("#2563eb")),
                    ("Individuals", max_abs_normalize(volume_individuals), colors.HexColor("#16a34a")),
                ],
                170 * mm,
            )
        )

        story.append(para("2. Sign 지표 및 Baseline", styles["KHeading1"]))
        story.extend(metric_table("Value 기준", summary["value"], styles))
        story.extend(metric_table("Volume 기준", summary["volume"], styles))
        story.extend(baseline_table("Value baseline 비교", summary["value"], styles))
        story.extend(baseline_table("Volume baseline 비교", summary["volume"], styles))
        story.extend(confusion_table("Value confusion matrix: LLM vs Individuals", v_primary, styles))
        story.extend(confusion_table("Volume confusion matrix: LLM vs Individuals", q_primary, styles))

        story.append(PageBreak())
        story.append(para("3. 일별 비교 샘플", styles["KHeading1"]))
        sample_rows = [["날짜", "LLM Value", "개인 Value", "일치", "LLM Volume", "개인 Volume", "일치"]]
        volume_by_date = {row["date"]: row for row in volume_rows}
        for row in value_rows[:30]:
            qrow = volume_by_date[row["date"]]
            sample_rows.append(
                [
                    row["date"],
                    f"{direction_label_ko(num(row['llm_net']))}\n{fmt_int(row['llm_net'])}",
                    f"{direction_label_ko(num(row['Individuals']))}\n{fmt_int(row['Individuals'])}",
                    "Y" if row["llm_matches_individuals"] else "N",
                    f"{direction_label_ko(num(qrow['llm_net']))}\n{fmt_int(qrow['llm_net'])}",
                    f"{direction_label_ko(num(qrow['Individuals']))}\n{fmt_int(qrow['Individuals'])}",
                    "Y" if qrow["llm_matches_individuals"] else "N",
                ]
            )
        story.append(
            table(
                [[para(c, styles["KSmall"]) for c in row] for row in sample_rows],
                [23 * mm, 27 * mm, 31 * mm, 12 * mm, 27 * mm, 31 * mm, 12 * mm],
            )
        )

        story.append(para("4. 해석", styles["KHeading1"]))
        story.append(para(make_interpretation(summary), styles["KBody"]))

    doc = SimpleDocTemplate(
        str(report_path),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="TwinMarket Korea 거래 방향 검증 보고서",
    )
    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)


def make_interpretation(summary: dict[str, Any]) -> str:
    value_match = summary["value"]["llm_vs_individuals"]["direction_match_rate"]
    volume_match = summary["volume"]["llm_vs_individuals"]["direction_match_rate"]
    value_balanced = summary["value"]["llm_vs_individuals"]["balanced_accuracy"]
    volume_balanced = summary["volume"]["llm_vs_individuals"]["balanced_accuracy"]
    value_sell_recall = summary["value"]["llm_vs_individuals"]["sell_recall"]
    volume_sell_recall = summary["volume"]["llm_vs_individuals"]["sell_recall"]
    value_corr = summary["value"]["llm_vs_individuals"]["max_abs_normalized"]["pearson_correlation"]
    volume_corr = summary["volume"]["llm_vs_individuals"]["max_abs_normalized"]["pearson_correlation"]
    value_cumulative_corr = summary["value"]["llm_vs_individuals"]["cumulative_max_abs_normalized"]["pearson_correlation"]
    volume_cumulative_corr = summary["volume"]["llm_vs_individuals"]["cumulative_max_abs_normalized"]["pearson_correlation"]
    return (
        f"LLM 에이전트와 실제 개인 투자자의 방향 일치율은 Value {pct(value_match)}, Volume {pct(volume_match)}이고, "
        f"balanced accuracy는 Value {pct(value_balanced)}, Volume {pct(volume_balanced)}이다. "
        f"sell recall은 Value {pct(value_sell_recall)}, Volume {pct(volume_sell_recall)}이다. "
        f"MaxAbs 정규화 상관계수는 Value {score_text(value_corr)}, Volume {score_text(volume_corr)}이며, "
        f"누적 정규화 상관계수는 Value {score_text(value_cumulative_corr)}, Volume {score_text(volume_cumulative_corr)}이다. "
        "방향 일치율이 높고 정규화 상관계수/코사인 유사도가 양수로 안정적이면 실제 개인 투자자 흐름을 시뮬레이션이 어느 정도 "
        "재현한다고 해석할 수 있다."
    )


def safe_run_name(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("_") or "run"


def main() -> None:
    args = parse_args()
    try:
        actual_value = load_actual(args.actual_value)
        actual_volume = load_actual(args.actual_volume)
        run_id, simulation = load_simulation(args.run_dir, args.stock_code)
        run_metadata = load_run_metadata(args.run_dir)

        output_dir = args.output_dir or (VALIDATION_DIR / "outputs" / safe_run_name(run_id))
        output_dir.mkdir(parents=True, exist_ok=True)

        value_rows = build_comparison_rows(label="value", actual=actual_value, simulation=simulation)
        volume_rows = build_comparison_rows(label="volume", actual=actual_volume, simulation=simulation)
        value_rows = filter_date_range(value_rows, args.start_date, args.end_date)
        volume_rows = filter_date_range(volume_rows, args.start_date, args.end_date)
        skipped_value_dates = [row["date"] for row in value_rows[: args.skip_initial_days]]
        skipped_volume_dates = [row["date"] for row in volume_rows[: args.skip_initial_days]]
        value_rows = skip_initial_rows(value_rows, args.skip_initial_days)
        volume_rows = skip_initial_rows(volume_rows, args.skip_initial_days)
        fieldnames = [
            "date",
            "llm_net",
            "llm_direction",
            "market_return",
            *[item for column in ACTUAL_INVESTOR_COLUMNS for item in (column, f"{column}_direction")],
            "llm_matches_individuals",
        ]
        write_csv(output_dir / "daily_comparison_value.csv", value_rows, fieldnames)
        write_csv(output_dir / "daily_comparison_volume.csv", volume_rows, fieldnames)
        write_csv(output_dir / "normalized_comparison_value.csv", build_normalized_rows(value_rows), normalized_fieldnames())
        write_csv(output_dir / "normalized_comparison_volume.csv", build_normalized_rows(volume_rows), normalized_fieldnames())

        summary = {
            "run_id": run_id,
            "run_dir": str(args.run_dir.resolve()),
            "actual_value": str(args.actual_value.resolve()),
            "actual_volume": str(args.actual_volume.resolve()),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "information_mode": run_metadata.get("information_mode"),
            "limit_only_orders": run_metadata.get("limit_only_orders"),
            "validation_start_date": args.start_date,
            "validation_end_date": args.end_date,
            "skip_initial_days": args.skip_initial_days,
            "skipped_value_dates": skipped_value_dates,
            "skipped_volume_dates": skipped_volume_dates,
            "value": summarize_dimension(value_rows, "value"),
            "volume": summarize_dimension(volume_rows, "volume"),
        }
        summary["primary_metrics"] = {
            "value": summary["value"]["primary_metrics"],
            "volume": summary["volume"]["primary_metrics"],
        }
        summary["reference_metrics"] = {
            "value": summary["value"]["reference_metrics"],
            "volume": summary["volume"]["reference_metrics"],
            "note": "누적 Pearson은 상승장 + 단일가 체결 구조상 구조적 역전이 발생할 수 있음",
        }
        with (output_dir / "summary_metrics.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        build_report(
            report_path=output_dir / "validation_report.pdf",
            run_id=run_id,
            run_dir=args.run_dir.resolve(),
            value_rows=value_rows,
            volume_rows=volume_rows,
            summary=summary,
        )
    except Exception as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise

    print(output_dir)
    print(output_dir / "summary_metrics.json")
    print(output_dir / "validation_report.pdf")


if __name__ == "__main__":
    main()
