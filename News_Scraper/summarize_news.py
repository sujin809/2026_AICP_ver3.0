#!/usr/bin/env python3
"""병합된 뉴스 전문에 요약을 생성한다 (기본 Sonnet 5, 공백 포함 150자 이상 200자 미만).

요약문은 D1/D2 에이전트가 **실제로 읽는 텍스트**이므로, 모델·프롬프트·파라미터는
입력 provenance 로 취급한다.  이 스크립트는 사용한 모델/프롬프트 해시/파라미터를
각 레코드와 리포트에 함께 남긴다.

길이 규칙(확정):
* 공백 포함 **150자 이상 200자 미만**, 목표 170자 내외
* 범위를 벗어나면 최대 N회 재시도(짧으면 "더 상세히", 길면 "더 간결히")
* 그래도 실패하면 **기사를 버리지 않고** ``summary_length_ok=false`` 플래그만 남긴다

사용 예::

    python News_Scraper/summarize_news.py \
        --in outputs/crawl/merged_news.jsonl \
        --out outputs/crawl/summaries.jsonl \
        --limit 20            # 먼저 소량으로 품질·비용 확인 권장
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    def load_dotenv(*_a, **_k):
        return False

MIN_CHARS = 150
MAX_CHARS = 200  # 미만

# 프로젝트 하드 제약: OpenRouter reasoning 은 항상 꺼둔다.
# 이 스크립트는 twinmarket_kr/llm/client.py 를 거치지 않으므로 여기서 직접 강제한다.
REASONING_OFF = {"reasoning": {"effort": "none", "exclude": True}}

SYSTEM_PROMPT = """당신은 한국 경제·산업 뉴스를 요약하는 도구입니다.
주어진 기사 원문을 읽고, 아래 규칙을 정확히 지켜 한국어 요약문 하나만 출력하십시오.

[길이]
- 공백을 포함하여 150자 이상 200자 미만.
- 170자 내외를 목표로 하되, 150자 미만이거나 200자 이상이 되어서는 안 됩니다.

[근거 — 가장 중요]
- **오직 아래에 주어진 이 기사 본문만을 근거로** 요약하십시오.
- 다른 기사, 당신이 이전에 본 내용, 사전 학습된 배경지식, 인터넷 검색 결과를
  **절대 참조하거나 섞지 마십시오.**
- 본문에 없는 사실·수치·인물·기업·날짜를 **만들어내지 마십시오.**
  본문에서 확인되지 않는 내용은 아무리 그럴듯해도 쓰지 않습니다.
- 본문이 짧거나 정보가 적으면, 없는 내용을 채우지 말고 있는 사실을 더 구체적으로 서술하십시오.

[내용 — 무엇을 우선 담을 것인가]
- 이 기사에서 **주가에 영향을 줄 수 있는 정보를 최대한 우선 포함**하십시오. 예:
  · 실적·매출·영업이익 등 재무 수치, 전년/전분기 대비 증감
  · 가격·수급·점유율·수주·계약·투자·증설 규모
  · 금리·환율·물가·수출입 등 거시지표 수치와 방향
  · 정부 정책·규제·관세 등 제도 변화와 적용 시점
  · 기업·산업이 직면한 리스크, 공급망 차질
  · 원문에 이미 실려 있는 전망·가이던스(출처가 본문에 명시된 것)
- 원문에 등장하는 수치(가격, 지수, 금액, 비율, 날짜)는 **그대로 보존**합니다. 반올림하거나 바꾸지 마십시오.
- 지면을 채우기 위한 배경 설명·일반론보다, 위와 같은 **구체적 사실과 수치**를 우선합니다.

[금지]
- 투자 권유, 매수/매도 의견, 주관적 전망·평가를 쓰지 마십시오.
- 원문에 있는 전망은 사실로서 전달할 수 있으나, 당신이 새로 만들어내지 마십시오.
- 요약문 외의 머리말·설명·따옴표·목록·마크다운을 출력하지 마십시오.

[문체]
- 중립적인 평서형 뉴스체(~다).

출력: 요약문 텍스트 한 단락만."""


def _prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    """모델이 붙이기 쉬운 따옴표/머리말을 제거."""
    out = (text or "").strip()
    for prefix in ("요약:", "요약문:", "다음은", "-"):
        if out.startswith(prefix):
            out = out[len(prefix) :].strip()
    return out.strip().strip('"').strip("'").strip()


def summarize_one(client, *, model: str, temperature: float, title: str, body: str, max_retries: int, body_limit: int):
    """Return (summary, char_count, length_ok, attempts)."""
    article = f"제목: {title}\n\n본문:\n{body[:body_limit]}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": article},
    ]
    summary, count = "", 0
    for attempt in range(1, max_retries + 2):
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=600,
            extra_body=REASONING_OFF,
        )
        summary = _clean(response.choices[0].message.content or "")
        count = len(summary)  # 공백 포함
        if MIN_CHARS <= count < MAX_CHARS:
            return summary, count, True, attempt
        if attempt > max_retries:
            break
        hint = (
            f"직전 요약이 {count}자로 짧습니다. 같은 규칙을 지키되 원문의 사실을 더 포함해 "
            f"150자 이상 200자 미만으로 다시 작성하십시오."
            if count < MIN_CHARS
            else f"직전 요약이 {count}자로 깁니다. 같은 규칙을 지키되 핵심만 남겨 "
            f"150자 이상 200자 미만으로 다시 작성하십시오."
        )
        messages = messages + [
            {"role": "assistant", "content": summary},
            {"role": "user", "content": hint},
        ]
    return summary, count, False, attempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="src", type=Path, required=True, help="병합된 뉴스 JSONL")
    parser.add_argument("--out", type=Path, required=True, help="요약 JSONL (재개 지원)")
    parser.add_argument("--model", default="anthropic/claude-sonnet-5", help="모델 ID (확정: Sonnet 5)")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--body-limit", type=int, default=6000, help="본문 입력 상한(문자)")
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 건수(소량 테스트용)")
    parser.add_argument("--delay", type=float, default=0.2)
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        parser.exit(2, "OPENROUTER_API_KEY 가 .env 에 없습니다.\n")
    client = OpenAI(api_key=api_key, base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))

    rows = [json.loads(line) for line in args.src.open(encoding="utf-8") if line.strip()]
    done: set[str] = set()
    if args.out.exists():
        for line in args.out.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["url"])
            except Exception:
                continue
        print(f"재개: 기존 {len(done)}건 스킵")
    todo = [r for r in rows if r.get("url") not in done and (r.get("body") or "").strip()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"대상 {len(todo)}건 · 모델 {args.model} · temp {args.temperature}")

    prompt_hash = _prompt_sha256(SYSTEM_PROMPT)
    ok = failed_len = errors = 0
    lengths: list[int] = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        for i, row in enumerate(todo, start=1):
            try:
                summary, count, length_ok, attempts = summarize_one(
                    client,
                    model=args.model,
                    temperature=args.temperature,
                    title=row.get("title", ""),
                    body=row.get("body", ""),
                    max_retries=args.max_retries,
                    body_limit=args.body_limit,
                )
                lengths.append(count)
                ok += length_ok
                failed_len += (not length_ok)
                handle.write(
                    json.dumps(
                        {
                            "url": row["url"],
                            "date": row.get("date"),
                            "bucket": row.get("bucket"),
                            "title": row.get("title"),
                            "summary": summary,
                            "summary_chars": count,
                            "summary_length_ok": length_ok,
                            "summary_attempts": attempts,
                            "summary_model": args.model,
                            "summary_temperature": args.temperature,
                            "summary_reasoning": "off",
                            "summary_prompt_sha256": prompt_hash,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                handle.write(
                    json.dumps({"url": row.get("url"), "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
                    + "\n"
                )
            if i % 25 == 0:
                handle.flush()
                print(f"  … {i}/{len(todo)} (범위내 {ok}, 범위밖 {failed_len}, 오류 {errors})")
            time.sleep(args.delay)

    if lengths:
        lengths.sort()
        print(
            f"\n완료: 범위내 {ok} · 범위밖 {failed_len} · 오류 {errors}\n"
            f"길이(공백포함) 최소 {lengths[0]} / 중앙 {lengths[len(lengths)//2]} / 최대 {lengths[-1]}"
        )
    print(f"저장: {args.out}  (프롬프트 sha256={prompt_hash[:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
