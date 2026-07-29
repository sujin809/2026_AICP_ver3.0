#!/usr/bin/env python3
"""반도체·거시경제 섹션 기사의 '삼성전자 주가 관련성'을 LLM으로 분류한다.

삼성전자 버킷은 키워드 검색이라 정의상 관련이 있으므로 판단 대상이 아니다.
섹션 브라우징으로 들어온 반도체·거시경제 기사만 대상으로,
``keep`` / ``drop`` 과 한 줄 사유를 남긴다(감사 가능).

판단은 보수적으로 한다 — **애매하면 keep**.  뉴스 환경을 과도하게 깎으면
정보환경 자체가 왜곡되기 때문이다.

사용 예::

    # 먼저 소량 검증
    python News_Scraper/filter_relevance.py --in outputs/crawl/merged_news.jsonl \
        --out outputs/crawl/relevance.jsonl --limit 40
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

# 프로젝트 하드 제약: OpenRouter reasoning 은 항상 꺼둔다.
# 이 스크립트는 twinmarket_kr/llm/client.py 를 거치지 않으므로 여기서 직접 강제한다.
# effort="none" 이 생성 자체를 끄는 값이고, exclude 만으로는 숨기기에 그친다.
REASONING_OFF = {"reasoning": {"effort": "none", "exclude": True}}

SECTION_BUCKETS = ("반도체", "거시경제")

SYSTEM_PROMPT = """당신은 한국 주식시장 뉴스를 분류하는 도구입니다.
주어진 기사가 **삼성전자(005930) 주가 움직임과 관련이 있는지** 판단하십시오.

[관련 있음 = keep]
- 반도체/메모리/HBM/파운드리 산업 동향·업황·가격·수급
- 삼성전자 및 계열사(삼성전기·삼성디스플레이 등), 경쟁사(SK하이닉스·TSMC·마이크론·인텔·엔비디아) 동향
- 전자·IT·디스플레이 산업, 스마트폰·가전 시장 동향
- 거시금융 지표·정책 전반:
  · 금리, 채권·국채 금리, 한국은행·연준 등 중앙은행의 정책·발언·회의
  · 환율, 외환보유액, 통화량·유동성(M2, 화폐 증가율 등), 자금 흐름
  · 물가·인플레이션, 유가·원자재·에너지 가격
  · 수출입·무역수지·경상수지, 성장률·고용 등 주요 거시지표
  · 증시·수급·투자자 동향, 국가 신용등급, 금·달러 등 안전자산 동향
- **관세·통상·무역협상 이슈는 대상 품목(자동차·철강 등)과 무관하게 모두 keep**
  (통상 마찰은 수출 전반과 시장 심리에 파급되기 때문)
- 반도체·수출·투자·기업 관련 정부 정책·규제·세제·지원
- 공급망(해운·물류·에너지)이 산업 또는 수출에 영향을 주는 경우

[관련 없음 = drop]
- 사회·복지·인구 이슈(출산율, 다문화, 성평등, 노인복지, 청년 사회문제, 보조금 부정수급 등)
- 개인 생활물가 품목 이야기(식료품 낱개 가격 등), 생활정보·소비 팁
- 문화·예술·전시, 스포츠·연예
- **항공·여행업 기사**: 항공사 실적·노선 개설·여객 수요·항공권 가격·공항 운영·해외여행 수요
  (단, 기사의 본질이 유가·에너지 수출, 통상·관세, 지정학 리스크, 반도체·전자 업계 인사의 방한 등
   [관련 있음] 범주이고 항공/공항이 배경으로만 등장하면 **keep** 입니다)
- 개별 주택·분양·평형·세대 등 부동산 생활 정보(거시 금융안정 이슈가 아닌 경우)
- 경제정책 내용이 없는 인사·조직 개편·정치 공방
- 지역 행사, 기업 사회공헌, 인물 프로필·미담·인터뷰성 기사

[판단 원칙 — 매우 중요]
- 기본값은 **keep** 입니다. 위 [관련 없음] 범주에 **명확히** 해당할 때만 drop 하십시오.
- 조금이라도 애매하거나 판단이 갈리면 반드시 **keep** 입니다.
- 기사에 삼성전자가 직접 언급되지 않아도, 위 [관련 있음] 범주면 keep 입니다.
- 거시지표·중앙은행·환율·금리·통상 관련이면, 삼성전자와의 연결고리가 간접적이어도 keep 입니다.

출력은 아래 JSON 한 줄만:
{"decision": "keep" 또는 "drop", "reason": "20자 이내 사유"}"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify(client, *, model: str, temperature: float, title: str, summary: str, body: str) -> dict:
    content = f"제목: {title}\n\n요약/본문 발췌:\n{(summary or body)[:1200]}"
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        temperature=temperature,
        max_tokens=120,
        response_format={"type": "json_object"},
        extra_body=REASONING_OFF,
    )
    raw = (response.choices[0].message.content or "").strip()
    data = json.loads(raw)
    decision = str(data.get("decision", "")).lower()
    if decision not in {"keep", "drop"}:
        decision = "keep"  # 파싱 실패 시 보수적으로 유지
    return {"decision": decision, "reason": str(data.get("reason", ""))[:40]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="anthropic/claude-sonnet-5")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--min-body", type=int, default=150, help="이 길이 미만 본문은 판단 없이 제외 대상")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        parser.exit(2, "OPENROUTER_API_KEY 없음\n")
    client = OpenAI(api_key=key, base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))

    rows = [json.loads(l) for l in args.src.open(encoding="utf-8") if l.strip()]
    # 섹션 출처(반도체/거시경제) & 본문 충분한 기사만 판단
    targets = [
        r
        for r in rows
        if any(b in SECTION_BUCKETS for b in r.get("buckets", []))
        and len(r.get("body") or "") >= args.min_body
    ]
    done: set[str] = set()
    if args.out.exists():
        for line in args.out.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["url"])
            except Exception:
                continue
        print(f"재개: 기존 {len(done)}건 스킵")
    todo = [r for r in targets if r["url"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"판단 대상 {len(targets)}건 중 이번 실행 {len(todo)}건 · 모델 {args.model}")

    prompt_hash = _sha(SYSTEM_PROMPT)
    keep = drop = err = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        for i, row in enumerate(todo, start=1):
            try:
                verdict = classify(
                    client,
                    model=args.model,
                    temperature=args.temperature,
                    title=row.get("title", ""),
                    summary=row.get("summary", ""),
                    body=row.get("body", ""),
                )
                keep += verdict["decision"] == "keep"
                drop += verdict["decision"] == "drop"
                handle.write(
                    json.dumps(
                        {
                            "url": row["url"],
                            "date": row.get("date"),
                            "buckets": row.get("buckets"),
                            "title": row.get("title"),
                            **verdict,
                            "filter_model": args.model,
                            "filter_reasoning": "off",
                            "filter_prompt_sha256": prompt_hash,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            except Exception as exc:  # noqa: BLE001
                err += 1
                handle.write(json.dumps({"url": row.get("url"), "error": str(exc)[:200]}, ensure_ascii=False) + "\n")
            if i % 50 == 0:
                handle.flush()
                print(f"  … {i}/{len(todo)} (keep {keep}, drop {drop}, err {err})")
            time.sleep(args.delay)

    total = keep + drop
    print(f"\n완료: keep {keep} · drop {drop} ({100*drop/total:.1f}% 제외) · 오류 {err}")
    print(f"저장: {args.out} (프롬프트 sha256={prompt_hash[:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
