#!/usr/bin/env python3
"""
samsung_split 기사들을 본문 기반으로 요약 & 필터링
(LLM 없이, 무료, 빠른 처리)
"""

import json
import re
from pathlib import Path
from collections import defaultdict

# 필터링 키워드
KEEP_KEYWORDS = {
    # 거시경제
    "금리", "환율", "물가", "유가", "수출", "수입", "GDP", "성장률", "경상수지",
    "증시", "코스피", "원달러", "외국인", "통화정책", "재정정책", "관세",
    # 반도체
    "반도체", "HBM", "D램", "메모리", "DRAM", "낸드", "파운드리", "EUV", "칩",
    "TSMC", "인텔", "AMD", "엔비디아", "AI칩",
    # 기업
    "삼성", "삼성전자", "현대", "SK", "LG", "포스코",
    # 기술/정책
    "AI", "데이터센터", "배터리", "원전", "에너지", "공급망", "규제",
}

DROP_KEYWORDS = {
    # 부동산
    "부동산", "아파트", "주택", "전세", "월세", "청약", "분양",
    # 개인금융
    "재테크", "펀드", "투자법", "자산관리", "카드", "보험",
    # 문화/엔터
    "문화", "예술", "스포츠", "연예", "영화", "음악", "관광", "여행",
    # 항공
    "항공", "출입국",
    # 정치인 개인사
    "정치인", "선거", "인사", "승진",
    # 사회복지
    "복지", "의료", "연금", "노인", "교육",
}

def extract_summary_from_body(body: str, limit: int = 200) -> str:
    """본문에서 요약 추출 (누가/무엇/왜/어떻게 구조)"""
    text = re.sub(r'\s+', ' ', str(body or ''))
    text = text.strip()

    if not text:
        return ""

    # 문장 분리
    sentences = []
    current_sentence = ""

    for char in text:
        current_sentence += char
        if char in '.!?\n':
            sentence = current_sentence.strip()
            if sentence and len(sentence) > 10:  # 너무 짧은 문장 제외
                sentences.append(sentence)
            current_sentence = ""

    if current_sentence.strip():
        sentences.append(current_sentence.strip())

    # 문장 조합으로 150-200자 범위 요약 생성
    summary = ""
    for sentence in sentences:
        if len(summary) + len(sentence) + 1 <= limit:
            if summary:
                summary += " "
            summary += sentence
        else:
            break

    # 길이 조정
    if len(summary) < 150:
        # 더 많은 문장 포함
        summary = " ".join(sentences)[:limit].rstrip() + "." if len(" ".join(sentences)) > limit else " ".join(sentences)

    # 최종 길이 확인
    summary = summary[:limit].rstrip()
    if summary and not summary.endswith(('.', '!', '?')):
        summary += "."

    return summary.strip()

def should_filter_as_y(title: str, body: str, summary: str) -> bool:
    """삼성전자 주가 영향 판정"""
    text = f"{title} {body} {summary}".upper()

    # KEEP 키워드 확인
    keep_count = sum(1 for keyword in KEEP_KEYWORDS if keyword in text)

    # DROP 키워드 확인
    drop_count = sum(1 for keyword in DROP_KEYWORDS if keyword in text)

    # 의심의 이익은 KEEP
    if keep_count >= 1:
        return True

    if drop_count >= 2:
        return False

    return True  # 기본: KEEP

def process_samsung_split(
    splits_dir: Path = Path("outputs/samsung_split"),
    output_file: Path = Path("outputs/samsung_split_reprocessed.json")
):
    """samsung_split 전체 처리"""

    print("📂 Samsung Split 본문 기반 요약 처리 시작")
    print("")

    splits_path = Path(splits_dir)
    if not splits_path.exists():
        print(f"❌ 경로 없음: {splits_path}")
        return

    # 모든 JSON 파일 수집
    json_files = sorted(splits_path.glob("*.json"), key=lambda x: int(x.stem))
    print(f"📋 파일 발견: {len(json_files)}개")
    print("")

    all_results = []
    stats = {
        "total": 0,
        "processed": 0,
        "keep_y": 0,
        "drop_n": 0,
        "summary_counts": defaultdict(int),
    }

    # 파일 처리
    for file_idx, json_file in enumerate(json_files, 1):
        print(f"처리 중: {json_file.name} ({file_idx}/{len(json_files)})")

        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                articles = json.load(f)

            for article_idx, article in enumerate(articles):
                if not isinstance(article, dict):
                    continue

                title = str(article.get('제목', ''))
                body = str(article.get('본문', ''))
                date_str = str(article.get('작성시각', ''))[:10]

                if not title or not body or not date_str:
                    continue

                stats["total"] += 1

                # 요약 생성
                summary = extract_summary_from_body(body, limit=200)

                # 필터링 판정
                is_keep = should_filter_as_y(title, body, summary)
                filtering = "Y" if is_keep else "N"

                if is_keep:
                    stats["keep_y"] += 1
                else:
                    stats["drop_n"] += 1

                # 길이 통계
                summary_len = len(summary)
                if 150 <= summary_len <= 200:
                    stats["summary_counts"]["150-200"] += 1
                elif summary_len < 150:
                    stats["summary_counts"]["<150"] += 1
                else:
                    stats["summary_counts"][">200"] += 1

                # 결과 저장
                result = {
                    "파일": json_file.name,
                    "인덱스": article_idx,
                    "기사제목": title,
                    "요약": summary,
                    "필터링": filtering,
                    "요약길이": summary_len,
                }
                all_results.append(result)
                stats["processed"] += 1

        except Exception as e:
            print(f"   ⚠️ 오류: {e}")
            continue

    print("")
    print("✅ 처리 완료!")
    print("")
    print(f"📊 통계:")
    print(f"  - 총 기사: {stats['total']}")
    print(f"  - 포함 (Y): {stats['keep_y']} ({stats['keep_y']/max(stats['total'], 1)*100:.1f}%)")
    print(f"  - 제외 (N): {stats['drop_n']} ({stats['drop_n']/max(stats['total'], 1)*100:.1f}%)")
    print("")
    print(f"📈 요약 길이 분포:")
    for range_label, count in sorted(stats["summary_counts"].items()):
        print(f"  - {range_label}자: {count}개")

    # 결과 저장
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print("")
    print(f"💾 결과 저장: {output_file}")
    print(f"📊 총 {len(all_results)}개 기사 처리 완료")

    return len(all_results)

if __name__ == "__main__":
    process_samsung_split()
