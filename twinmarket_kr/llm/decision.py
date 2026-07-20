from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.llm.belief import load_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed


DECISION_KEYS = ("action", "quantity", "reason", "risk_control")


def build_trading_constraints(
    *,
    available_cash: float,
    current_quantity: int,
    current_price: float,
    min_order_unit: int = config.MIN_ORDER_UNIT,
    max_single_trade_cash_ratio: float = config.MAX_SINGLE_TRADE_CASH_RATIO,
    allow_hold: bool = False,
    price_label: str = "공시가",
) -> dict[str, Any]:
    usable_cash = max(0.0, available_cash * max_single_trade_cash_ratio)
    max_buy_quantity = int(usable_cash // current_price) if current_price > 0 else 0
    allowed_actions = []
    if max_buy_quantity >= min_order_unit:
        allowed_actions.append("buy")
    if current_quantity >= min_order_unit:
        allowed_actions.append("sell")
    if allow_hold:
        allowed_actions.append("hold")
    return {
        "available_cash": available_cash,
        "max_single_trade_cash_ratio": max_single_trade_cash_ratio,
        "max_single_trade_cash": usable_cash,
        "current_quantity": current_quantity,
        "current_price": current_price,
        "announced_price": current_price,
        "price_label": price_label,
        "min_order_unit": min_order_unit,
        "max_affordable_quantity": max_buy_quantity,
        "max_buy_quantity": max_buy_quantity,
        "max_sell_quantity": current_quantity,
        "allow_hold": allow_hold,
        "allowed_actions": allowed_actions,
    }


def parse_decision_json(content: str, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, default in {
        "action": "",
        "quantity": 0,
        "order_type": "announced_price",
        "price": 0,
        "reason": "",
        "risk_control": "",
    }.items():
        data.setdefault(key, default)
    action = str(data["action"]).strip().lower()
    corrections: list[str] = []
    validation_errors: list[str] = []
    raw_quantity = data.get("quantity")
    try:
        if isinstance(raw_quantity, bool):
            raise ValueError("boolean is not an order quantity")
        numeric_quantity = float(raw_quantity)
        if not numeric_quantity.is_integer():
            raise ValueError("fractional share quantity")
        quantity = int(numeric_quantity)
    except (TypeError, ValueError, OverflowError):
        quantity = 0
        validation_errors.append("invalid_quantity_format")
    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError, OverflowError):
        price = 0.0
        validation_errors.append("invalid_price_format")
    order_type = "announced_price"
    if constraints:
        allow_hold = bool(constraints.get("allow_hold", False))
        reference_price = float(constraints.get("current_price") or 0)
        min_order_unit = int(constraints["min_order_unit"])
        max_buy_quantity = int(constraints.get("max_buy_quantity") or 0)
        max_sell_quantity = int(constraints["max_sell_quantity"])
        allowed_actions = set(constraints.get("allowed_actions") or [])
        if action not in allowed_actions:
            validation_errors.append(f"action_not_allowed:{action}")
        if action == "hold" and not allow_hold:
            validation_errors.append("hold_not_allowed")
        if action not in {"buy", "sell"} and not (allow_hold and action == "hold"):
            validation_errors.append(f"invalid_action:{action}")
        if quantity < min_order_unit:
            validation_errors.append("quantity_below_min")
        if action == "buy" and quantity > max_buy_quantity:
            validation_errors.append(f"buy_quantity_exceeds_max:{quantity}>{max_buy_quantity}")
        if action == "sell" and quantity > max_sell_quantity:
            validation_errors.append(f"sell_quantity_exceeds_holding:{quantity}>{max_sell_quantity}")
        price = reference_price if action in {"buy", "sell"} else 0
        if not allowed_actions:
            validation_errors.append("no_allowed_actions")
    if not str(data.get("reason") or "").strip():
        validation_errors.append("missing_reason")
    if not str(data.get("risk_control") or "").strip():
        validation_errors.append("missing_risk_control")
    return {
        "action": action,
        "quantity": quantity,
        "order_type": order_type,
        "price": price,
        "reason": str(data.get("reason") or ""),
        "risk_control": str(data.get("risk_control") or ""),
        "order_corrections": corrections,
        "validation_errors": validation_errors,
        "valid": not validation_errors,
    }


class DecisionValidationError(RuntimeError):
    pass


def _decision_diagnostics(
    decision: dict[str, Any],
    constraints: dict[str, Any],
    *,
    attempts: int,
) -> dict[str, Any]:
    action = str(decision.get("action") or "")
    quantity = int(decision.get("quantity") or 0)
    max_quantity = int(
        constraints.get("max_buy_quantity" if action == "buy" else "max_sell_quantity") or 0
    )
    if quantity != 1:
        one_share_reason = "not_one_share"
    elif max_quantity == 1:
        one_share_reason = "constraint_maximum_is_one"
    elif max_quantity > 1:
        one_share_reason = "llm_selected_one_with_larger_capacity"
    else:
        one_share_reason = "unknown"
    return {
        "decision_attempts": attempts,
        "decision_retry_count": attempts - 1,
        "decision_origin": "llm_first_response" if attempts == 1 else "llm_validation_retry",
        "available_cash": float(constraints.get("available_cash") or 0),
        "current_quantity": int(constraints.get("current_quantity") or 0),
        "max_buy_quantity": int(constraints.get("max_buy_quantity") or 0),
        "max_sell_quantity": int(constraints.get("max_sell_quantity") or 0),
        "selected_action_max_quantity": max_quantity,
        "one_share_reason": one_share_reason,
        "deterministic_fallback_used": False,
    }


async def make_decision(
    agent: dict[str, Any],
    today_belief: dict[str, Any],
    market_analysis: dict[str, Any],
    portfolio_summary: str,
    order_history: str,
    trading_constraints: dict[str, Any],
    *,
    allow_hold: bool = False,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
    validation_attempts: int = 4,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    decision_space_instruction = (
        '이번 실행에서는 action이 반드시 "buy" 또는 "sell"이어야 합니다. "hold"는 선택할 수 없습니다.'
        if not allow_hold
        else '이번 실행에서는 action으로 "buy", "sell", "hold" 중 하나를 선택할 수 있습니다.'
    )
    prompt = load_prompt("make_decision.txt").format(
        persona_prompt=agent["persona_prompt"],
        today_belief=json.dumps(today_belief, ensure_ascii=False, indent=2),
        market_analysis=json.dumps(market_analysis, ensure_ascii=False, indent=2),
        portfolio_summary=portfolio_summary,
        order_history=order_history,
        trading_constraints=json.dumps(trading_constraints, ensure_ascii=False, indent=2),
        decision_space_instruction=decision_space_instruction,
    )
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    invalid_history: list[list[str]] = []
    current_prompt = prompt
    for attempt in range(1, validation_attempts + 1):
        response = await client.chat(
            [{"role": "user", "content": current_prompt}],
            response_format={"type": "json_object"},
            temperature=0.2 if attempt == 1 else 0.1,
            seed=stable_llm_seed(seed or 0, "decision_validation", attempt),
            audit_label="trading_decision",
        )
        decision = parse_decision_json(response_content(response) or "{}", trading_constraints)
        if decision.get("valid"):
            decision.update(_decision_diagnostics(decision, trading_constraints, attempts=attempt))
            decision["invalid_attempt_errors"] = invalid_history
            return decision
        invalid_history.append(list(decision.get("validation_errors") or []))
        current_prompt = (
            prompt
            + "\n\n이전 응답은 거래 제약을 위반했습니다. "
            + f"위반 항목: {decision.get('validation_errors')}. "
            + "판단 자체는 유지하거나 다시 판단해도 되지만, 설명하지 말고 JSON만 출력하세요. "
            + "action은 반드시 allowed_actions 안의 buy 또는 sell이어야 합니다. "
            + "quantity는 반드시 1 이상의 정수이고 선택한 action의 최대 수량 이하여야 합니다."
        )
    raise DecisionValidationError(
        "LLM did not produce a valid buy/sell decision after "
        f"{validation_attempts} validation attempts: {invalid_history}"
    )
