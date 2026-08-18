# sys_100_ko_ver5.db — 100명 에이전트 코호트 (momentum/contrarian 축 포함)

삼성전자 단일 자산 실험용 에이전트 100명의 페르소나 데이터셋. 각 에이전트의
프롬프트 전문이 DB 안에 들어 있어, 시뮬레이터가 이 파일만 읽으면 코호트를 그대로
재현할 수 있다.

- 파일: `data/sys_100_ko_ver5.db` (SQLite, 220KB)
- 생성: `TwinMarket_analysis/build_ver5_cohort.py` (ver4 → ver5)
- 계보: TwinMarket `sys_1000.db` → 100명 샘플링 → ko 번역 → ver3 → ver4(gender 복원)
  → **ver5(momentum/contrarian 축 추가)**

## 테이블

### `agents` (100행, 20컬럼)

| 컬럼 | 값 |
|---|---|
| `agent_id` | `A001`~`A100` (PK) |
| `source_user_id` | TwinMarket `Profiles.user_id` |
| `user_type` | ordinary 91 / small_influencer 8 / big_influencer 1 |
| `gender` | 남성 / 여성 |
| `strategy` | technical 60 / value 40 |
| **`momentum_contrarian`** | **momentum 56 / contrarian 44** ← ver5 신규 |
| `bh_*_category` | 처분효과 · 복권선호 · 총수익률 · 과소분산 |
| `ini_cash`, `fol_ind`, `news_depth`, `trad_pro` | 시뮬레이션 초기값 |
| `persona_prompt` | 프롬프트 전문 (축 문장이 모두 반영된 최종본) |
| `age`, `age_group`, `location`, `segment_key`, `match_score` | 미사용 슬롯 (`0` / `미사용`) |

### `meta` (8행)

`momentum_contrarian` 축의 출처·배정 방법·시드를 담은 provenance 테이블.

## momentum / contrarian 축

**이 축은 TwinMarket에서 추출한 값이 아니라 우리가 무작위 배정한 처치변수다.**
TwinMarket `Profiles`에는 대응 컬럼이 없고, `strategy`(技术面/基本面)는 *어떤 정보를
읽는가*를 나타내지 *어느 방향의 최근 가격 흐름에 진입하는가*를 나타내지 않는다.
따라서 이 축을 TwinMarket 유래 특성으로 보고하면 안 된다. (`CLAUDE.md` 절대 규칙)

- 배정: 에이전트별 Bernoulli(0.5) 독립 추첨, `agent_id` 순, **그룹 크기 고정 안 함**
- 시드: `20260818` (재실행 시 동일 배정 재현)
- 결과: momentum 56 / contrarian 44
- `strategy`와 독립 배정 → technical×momentum 32, technical×contrarian 28,
  value×momentum 24, value×contrarian 16

### 프롬프트에 삽입된 문장

전략 문장("기술적/기본적 분석 투자자로서…") **바로 다음 줄**에 한 줄이 들어간다.

**momentum**
> 당신은 신규 매수를 결정할 때 최근 가격이 꾸준히 상승한 국면을 선호합니다. 상승 추세가
> 단기간 이어질 가능성이 높다고 보아 그런 국면에서 신규 매수하거나 비중을 확대합니다.
> 반대로 최근 가격이 하락하는 국면에서는 신규 매수를 피합니다.

**contrarian**
> 당신은 신규 매수를 결정할 때 최근 가격이 하락한 국면을 선호합니다. 가격 하락이 과도해
> 향후 반등할 가능성이 있다고 판단되면 신규 매수합니다. 반대로 최근 가격이 크게 상승한
> 국면은 과열되었다고 보아 추격 매수를 피합니다.

문구를 "신규 **매수**를 결정할 때"로 한정한 이유: value 에이전트의 "저평가되었을 때
매수" 문장과 층위를 분리하기 위해서다. 무엇이 싼지는 내재가치로 판단하고, 신규 진입
타이밍만 최근 가격 흐름에 연동된다. 이 한정이 없으면 value×momentum 조합이 "비싸졌으니
산다"로 읽혀 모순이 된다.

## 사용법

```python
import sqlite3
con = sqlite3.connect("data/sys_100_ko_ver5.db")
con.row_factory = sqlite3.Row
agents = [dict(r) for r in con.execute("SELECT * FROM agents ORDER BY agent_id")]

# 축별 분할
momentum = [a for a in agents if a["momentum_contrarian"] == "momentum"]

# 프롬프트는 그대로 시스템 프롬프트로 사용 가능
prompt = agents[0]["persona_prompt"]
```

```sql
-- 축 교차 확인
SELECT strategy, momentum_contrarian, COUNT(*)
FROM agents GROUP BY 1, 2;
```

## ver4 대비 변경점

`persona_prompt` 한 컬럼만 바뀌었고(문장 한 줄 삽입), `momentum_contrarian` 컬럼이
추가됐다. 나머지 18개 컬럼은 ver4와 값이 동일하다. 삽입 문장을 제거하면 ver4
프롬프트와 줄 단위로 완전히 일치하며, 이는 생성 스크립트에서 100명 전원에 대해
assert로 검증한다.

## 함께 보기

`TwinMarket_analysis/agents_100_personas_ver5.json` — 같은 100명의 한/영 프롬프트와
축 값(이 저장소에는 포함되지 않음). 배정은 이 DB에서 읽어가므로 두 파일의
`momentum_contrarian` 값은 100명 전원 일치한다.
