#!/usr/bin/env python3
"""samsung.jsonl을 삼성전자 뉴스 요약과 함께 split JSON으로 변환"""

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

def clean_text(text: str) -> str:
    """텍스트 정제"""
    text = re.sub(r'\s+', ' ', str(text or ''))
    return text.strip()

def extract_summary(body: str, limit: int = 180) -> str:
    """본문에서 요약 추출"""
    text = clean_text(body)

    # 문장 단위로 분리
    sentences = re.split(r'(?<=[.!?\n])\s+', text)

    summary = ""
    for sentence in sentences:
        if len(summary) + len(sentence) + 1 <= limit:
            if summary:
                summary += " "
            summary += sentence
        else:
            break

    # 최소 길이 확보
    if len(summary) < 80:
        summary = text[:limit].rstrip() + "..." if len(text) > limit else text

    return summary.strip()

def normalize_category(title: str, body: str) -> str:
    """삼성전자 관련 기사 카테고리 판정"""
    text = f"{title} {body}".upper()

    # 삼성전자 키워드 확인
    samsung_keywords = ("삼성전자", "SAMSUNG", "005930", "갤럭시", "DS부문", "메모리", "반도체")
    if any(keyword in text for keyword in samsung_keywords):
        return "종목"

    return "종목"  # 모두 종목 카테고리

def process_samsung_news(
    input_jsonl: Path = Path("outputs/crawl/samsung.jsonl"),
    output_dir: Path = Path("outputs/samsung_split"),
    articles_per_file: int = 100,
):
    """samsung.jsonl 처리"""

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📂 입력: {input_jsonl}")
    print(f"📂 출력: {output_dir}")
    print("")

    # JSONL 파일 읽기
    articles = []
    try:
        with open(input_jsonl, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    data = json.loads(line.strip())
                    articles.append(data)
                except json.JSONDecodeError as e:
                    print(f"⚠️ 라인 {line_num} JSON 파싱 실패: {e}")
                    continue
    except FileNotFoundError:
        print(f"❌ 파일을 찾을 수 없음: {input_jsonl}")
        return

    print(f"✅ {len(articles)}개 기사 로드 완료")
    print("")

    # 기사 처리
    processed = []
    stats = {"total": 0, "date_missing": 0, "title_missing": 0, "body_missing": 0, "valid": 0}

    for idx, article in enumerate(articles, 1):
        stats["total"] += 1

        # 필수 필드 확인
        title = clean_text(article.get('title', ''))
        body = clean_text(article.get('body', ''))
        date_str = str(article.get('date', ''))[:10]
        time_str = str(article.get('time', ''))[:5] if article.get('time') else "00:00"

        if not date_str or date_str == "None":
            stats["date_missing"] += 1
            continue
        if not title:
            stats["title_missing"] += 1
            continue
        if not body:
            stats["body_missing"] += 1
            continue

        # 요약 추출
        summary = extract_summary(body)

        # 카테고리 판정
        category = normalize_category(title, body)

        # 처리된 기사
        processed_article = {
            "제목": title,
            "작성시각": f"{date_str} {time_str}",
            "본문": body,
            "요약": summary,
            "필터링 여부": "Y"  # 모두 Y로 설정 (삼성전자 기사는 모두 필요)
        }

        processed.append(processed_article)
        stats["valid"] += 1

        if idx % 500 == 0:
            print(f"진행: {idx}/{len(articles)}")

    print(f"\n📊 처리 결과:")
    print(f"  - 총 기사: {stats['total']}")
    print(f"  - 유효한 기사: {stats['valid']}")
    print(f"  - 날짜 없음: {stats['date_missing']}")
    print(f"  - 제목 없음: {stats['title_missing']}")
    print(f"  - 본문 없음: {stats['body_missing']}")
    print("")

    # 날짜별로 정렬
    processed.sort(key=lambda x: (x['작성시각'][:10], x['작성시각'][11:]))

    # Split JSON 파일로 저장
    num_files = (len(processed) + articles_per_file - 1) // articles_per_file
    print(f"💾 {num_files}개 JSON 파일로 저장 중...")
    print("")

    for file_idx in range(num_files):
        start_idx = file_idx * articles_per_file
        end_idx = min(start_idx + articles_per_file, len(processed))

        batch = processed[start_idx:end_idx]

        # 파일명: 001.json, 002.json, ...
        file_num = file_idx + 1
        output_file = output_dir / f"{file_num:03d}.json"

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(batch, f, ensure_ascii=False, indent=2)

        print(f"  ✅ {output_file.name}: {len(batch)}개 기사")

    print("")
    print(f"✨ 완료! samsung_split 폴더 생성됨")
    print(f"📂 위치: {output_dir}")
    print(f"📊 총 {len(processed)}개 기사, {num_files}개 파일")

    # 날짜 범위 확인
    if processed:
        first_date = processed[0]['작성시각'][:10]
        last_date = processed[-1]['작성시각'][:10]
        print(f"📅 날짜 범위: {first_date} ~ {last_date}")

    return len(processed)

if __name__ == "__main__":
    process_samsung_news()
