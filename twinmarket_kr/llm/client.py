from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:
    AsyncOpenAI = None  # type: ignore[assignment]

import config


class UnexpectedModelError(RuntimeError):
    pass


def stable_llm_seed(base_seed: int, *parts: object) -> int:
    payload = "|".join([str(base_seed), *(str(part) for part in parts)])
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = base_url or config.OPENROUTER_BASE_URL
        self.model = model or config.OPENROUTER_MODEL
        self.max_retries = config.OPENROUTER_MAX_RETRIES if max_retries is None else max_retries
        self.timeout = timeout
        self.offline = os.getenv("TWINMARKET_OFFLINE_LLM", "").strip().lower() in {"1", "true", "yes"}
        if self.offline:
            self.client = None
            return
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set.")
        if AsyncOpenAI is None:
            raise RuntimeError("openai package is not installed. Run pip install -r requirements.txt.")
        # Disable the SDK's hidden retry layer so every physical retry is governed
        # by this client, counted in the audit log, and covered by the global slot.
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        seed: int | None = None,
        audit_label: str = "unspecified",
    ) -> Any:
        if os.getenv("TWINMARKET_OFFLINE_LLM", "").strip().lower() in {"1", "true", "yes"}:
            return _offline_response(messages)

        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        if seed is not None:
            kwargs["seed"] = int(seed)
        provider_options: dict[str, Any] = {
            "require_parameters": config.OPENROUTER_REQUIRE_PARAMETERS,
            "allow_fallbacks": config.OPENROUTER_ALLOW_FALLBACKS,
        }
        if config.OPENROUTER_PROVIDER_ORDER:
            provider_options["order"] = config.OPENROUTER_PROVIDER_ORDER
        kwargs["extra_body"] = {"provider": provider_options}

        delay = 1.0
        last_error: Exception | None = None
        prompt_sha256 = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            response = None
            try:
                async with _global_openrouter_slot(config.OPENROUTER_GLOBAL_CONCURRENCY):
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs),
                        timeout=self.timeout + 5,
                    )
                returned_model = getattr(response, "model", None)
                if returned_model and str(returned_model) != str(kwargs["model"]):
                    raise UnexpectedModelError(
                        f"OpenRouter returned {returned_model!r} for requested model "
                        f"{kwargs['model']!r}"
                    )
                _record_api_audit(
                    label=audit_label,
                    requested_model=str(kwargs["model"]),
                    seed=seed,
                    attempt=attempt,
                    latency_seconds=time.perf_counter() - started,
                    prompt_sha256=prompt_sha256,
                    response=response,
                )
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _record_api_audit(
                    label=audit_label,
                    requested_model=str(kwargs["model"]),
                    seed=seed,
                    attempt=attempt,
                    latency_seconds=time.perf_counter() - started,
                    prompt_sha256=prompt_sha256,
                    response=response,
                    error=exc,
                )
                if attempt >= self.max_retries or not _is_retryable_error(exc):
                    break
                retry_after = _retry_after_seconds(exc)
                wait_seconds = retry_after if retry_after is not None else delay
                await asyncio.sleep(min(config.OPENROUTER_RETRY_MAX_DELAY, max(0.0, wait_seconds)))
                delay = min(config.OPENROUTER_RETRY_MAX_DELAY, delay * 2)
        raise RuntimeError(
            f"OpenRouter chat failed after {self.max_retries} attempts: {last_error}"
        ) from last_error

    async def ping(self) -> str:
        response = await self.chat(
            [{"role": "user", "content": "Reply with pong."}],
            temperature=0,
        )
        return response_content(response)


def response_content(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "")
        return ""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def _retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _is_retryable_error(error: BaseException) -> bool:
    if isinstance(error, UnexpectedModelError):
        return False
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code is None:
        # Only known network/transport failures are safe to retry. A local
        # ValueError/TypeError or an unknown SDK failure will not improve by
        # replaying the same request six times.
        retryable_names = {
            "APIConnectionError",
            "APITimeoutError",
            "ConnectError",
            "ConnectTimeout",
            "ConnectionError",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "TimeoutError",
        }
        return (
            isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in retryable_names
        )
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False
    return code in {408, 409, 429} or 500 <= code <= 599


def _record_api_audit(
    *,
    label: str,
    requested_model: str,
    seed: int | None,
    attempt: int,
    latency_seconds: float,
    prompt_sha256: str,
    response: Any | None = None,
    error: BaseException | None = None,
) -> None:
    path = Path(config.OPENROUTER_AUDIT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = getattr(response, "usage", None) if response is not None else None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif usage is not None and not isinstance(usage, dict):
        usage = {
            key: getattr(usage, key, None)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "label": label,
        "status": "error" if error is not None else "success",
        "requested_model": requested_model,
        "returned_model": getattr(response, "model", None) if response is not None else None,
        "provider": getattr(response, "provider", None) if response is not None else None,
        "request_id": getattr(response, "id", None) if response is not None else None,
        "seed": seed,
        "attempt": attempt,
        "latency_seconds": round(latency_seconds, 6),
        "prompt_sha256": prompt_sha256,
        "usage": usage,
        "error_type": type(error).__name__ if error is not None else None,
        "error": str(error) if error is not None else None,
    }
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@asynccontextmanager
async def _global_openrouter_slot(limit: int):
    """Cross-process semaphore shared by all six condition runners on one machine."""
    if limit < 1:
        yield
        return
    slot_dir = Path(config.OPENROUTER_SLOT_DIR)
    slot_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    while handle is None:
        for index in range(limit):
            candidate = (slot_dir / f"slot_{index:03d}.lock").open("a+", encoding="utf-8")
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate.close()
                continue
            handle = candidate
            break
        if handle is None:
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _offline_response(messages: list[dict[str, str]]) -> str:
    prompt = messages[-1].get("content", "") if messages else ""
    if "[모드: react]" in prompt:
        post_ids = [int(value) for value in re.findall(r"post_id=(\d+)", prompt)]
        reactions = [
            {"post_id": post_id, "reaction": ("like" if index % 3 == 0 else "none")}
            for index, post_id in enumerate(post_ids)
        ]
        return json.dumps({"reactions": reactions}, ensure_ascii=False)
    if "[모드: select]" in prompt:
        limit_match = re.search(r"최대\s+(\d+)개", prompt)
        limit = int(limit_match.group(1)) if limit_match else 3
        post_ids = [int(value) for value in re.findall(r"post_id=(\d+)", prompt)]
        return json.dumps({"selected_post_ids": post_ids[:limit]}, ensure_ascii=False)
    if "will_post" in prompt or "게시글 타입 6종" in prompt:
        return json.dumps(
            {
                "will_post": True,
                "post_type": "impression",
                "title": "오늘 삼성전자 흐름 메모",
                "content": "가격 흐름과 뉴스가 엇갈려 보입니다. 무리하지 않고 수량을 제한해 대응하겠습니다.",
            },
            ensure_ascii=False,
        )
    if "[어제 커뮤니티 Best 게시글]" in prompt and "자유 형식 텍스트" in prompt:
        return "커뮤니티는 혼조 분위기입니다. 가격 리스크와 장기 성장성을 함께 보겠습니다."
    if "거래 제약:" in prompt and "최종 결정을 출력" in prompt:
        constraints = _extract_json_after_label(prompt, "거래 제약:")
        allowed = constraints.get("allowed_actions") or ["buy"]
        action = "buy" if "buy" in allowed else "sell"
        max_quantity = int(constraints.get("max_buy_quantity" if action == "buy" else "max_sell_quantity") or 1)
        quantity = max(1, min(max_quantity, 10))
        return json.dumps(
            {
                "action": action,
                "quantity": quantity,
                "reason": "Offline smoke run: mixed signals justify a small constrained trade.",
                "risk_control": "Keep position size small and preserve cash for later turns.",
            },
            ensure_ascii=False,
        )
    if "거래 전 시장 분석" in prompt:
        return json.dumps(
            {
                "market_view": "Mixed short-term setup with both price risk and recovery potential.",
                "valuation_view": "Valuation is not decisive in this offline smoke run.",
                "technical_view": "Price and volume signals are treated as mixed.",
                "news_view": "News impact is mixed and requires cautious sizing.",
                "portfolio_view": "Portfolio risk is managed through small order size.",
                "key_risks": ["volatility", "news uncertainty"],
                "opportunity": ["limited entry after weakness"],
                "caution": ["avoid excessive concentration"],
                "confidence": "medium",
            },
            ensure_ascii=False,
        )
    if (
        "Belief를 JSON" in prompt
        or "투자 Belief를 JSON" in prompt
        or ("belief_summary" in prompt and "dim_1" in prompt)
    ):
        return json.dumps(
            {
                "dim_1": "Short-term direction is mixed, so I will respond cautiously.",
                "dim_2": "Valuation needs confirmation from market data.",
                "dim_3": "Macro and semiconductor cycle signals remain important.",
                "dim_4": "Investor sentiment looks mixed rather than one-sided.",
                "dim_5": "News flow supports caution and selective action.",
                "dim_6": "I should avoid overconfidence and keep trades small.",
                "belief_summary": "Samsung Electronics has mixed signals today. I will trade conservatively within constraints.",
                "view_change": "Maintained a cautious view based on mixed information.",
            },
            ensure_ascii=False,
        )
    if "작업 모드:\npost_search" in prompt:
        return json.dumps(
            {
                "new_findings": [],
                "view_change": "유지",
                "view_change_detail": "Offline smoke run did not add search results.",
                "unresolved_questions": [],
            },
            ensure_ascii=False,
        )
    if "작업 모드:\npre_search" in prompt:
        return json.dumps(
            {
                "key_findings": ["Offline smoke run checks the trailing news window."],
                "curiosity_points": ["HBM demand", "exchange rate", "foreign flows"],
                "search_rationale": "Verify the seven-day cutoff and search audit path.",
                "search_keywords": ["HBM", "환율", "외국인"],
            },
            ensure_ascii=False,
        )
    if "뉴스" in prompt:
        return json.dumps(
            {
                "selected_news": [],
                "news_sentiment": "mixed",
                "short_term_impact": "Short-term impact is mixed.",
                "long_term_impact": "Long-term impact depends on earnings and semiconductor demand.",
                "persona_interpretation": "The investor stays cautious and avoids oversized trades.",
                "confidence": "medium",
                "reason": "Offline smoke run summary.",
            },
            ensure_ascii=False,
        )
    return "pong"


def _extract_json_after_label(prompt: str, label: str) -> dict[str, Any]:
    start = prompt.find(label)
    if start < 0:
        return {}
    start = prompt.find("{", start)
    if start < 0:
        return {}
    depth = 0
    for index in range(start, len(prompt)):
        char = prompt[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(prompt[start : index + 1])
                except json.JSONDecodeError:
                    return {}
                return data if isinstance(data, dict) else {}
    return {}
