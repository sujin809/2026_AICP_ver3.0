# Paper Note: From Heard to Lived Opinions

- **Title**: From Heard to Lived Opinions: Simulating Opinion Dynamics with Grounded LLM Agents in Economic Environments
- **Authors**: Ryuji Hashimoto, Masahiro Kaneko, Ryosuke Takata, Takehiro Takayanagi, Kiyoshi Izumi (Simulacra Inc. / 도쿄대 / MBZUAI)
- **Venue / status**: arXiv:2603.26701 (2026-03) → ACL 2026 Findings
- **Links**: [arXiv](https://arxiv.org/abs/2603.26701) · [HTML](https://arxiv.org/html/2603.26701) · [ACL Anthology](https://aclanthology.org/2026.findings-acl.580/)

이 노트는 결과/결론이 아니라 **방법론·실험 설계·메트릭**만 정리한다. TwinMarket Korea(RN A/B) 설계에 적용할지 판단하기 위한 자료.

---

## 연구 질문 (RQ)

- **RQ1**: 경제 환경에 그라운딩된 LLM 에이전트가 환경과 일관된 행동/의견을 보이는가?
- **RQ2**: 행동-피드백의 이력(history)이 개인 수준 의견 동학(opinion dynamics, OD)을 어떻게 형성하는가?
- **RQ3**: 개인 수준 경제적 상호작용이 어떻게 집단 수준 OD로 확장되는가?

## 전체 구조

에이전트 = 가계(household). 매 타임스텝: **① LLM이 노동/소비를 선택 + 의견 텍스트 생성 → ② 규칙 기반 시장(기업/정부)이 결과를 계산해 피드백(현금 변화) → ③ 다음 스텝에 그 결과와 동료 의견을 관찰**. LLM은 가계의 "선택+의견 생성" 지점에만 쓰이고, 기업·정부는 전부 규칙 기반(rule-based) ABM. → LLM 호출을 의사결정 지점으로만 국한한 설계.

---

## 경제 환경 다이내믹스 (수식)

**가계 현금 업데이트**

$$
M_{t,i} \leftarrow M_{t-1,i} - \bar{p}_{t,i} C_{t,i} + \bar{w}_{t,i} L_{t,i}
$$

$$
M_{t,i} \leftarrow M_{t,i} + s_{t,i} - \tau_{t,i} \quad \text{(정부 개입 후)}
$$

**기업 생산 (Cobb-Douglas)**

$$
F(K, L) = A \cdot \min(K_{max}, K)^{\alpha} \cdot L^{1-\alpha}
$$

**자본 업데이트**

$$
K_{t,i+1} \leftarrow (1-d) K_{t,i} + Y_{t,i} - C_{t,i}
$$

**가격 조정** (수요-공급 불균형 기반)

$$
\bar{p}_{t,i+1} \leftarrow \bar{p}_{t,i} \left(1 + \eta_p \cdot \frac{C_{t,i} - Y_{t,i}}{Y_{t,i}}\right)
$$

**임금 조정** (한계생산으로 수렴)

$$
\bar{w}_{t,i+1} \leftarrow \bar{w}_{t,i} + \eta_w \left(MPL_{t,i} - \bar{w}_{t,i}\right)
$$

$$
MPL_{t,i} = (1-\alpha) A \cdot \min(K_{max}, K_{t,i})^{\alpha} \cdot L_{t,i}^{-\alpha}
$$

**세금/보조금**
- 보조금: 전 가구 정액 $s_{t,i} = b$
- 세금 (재정상태 5단계 기준 누진):

$$
\tau_{t,i} =
\begin{cases}
0 & \text{if } f_{t,i} = \text{"very low"} \\
2\bar{\tau} M_{t,i} & \text{if } f_{t,i} = \text{"very high"} \\
\bar{\tau} M_{t,i} & \text{otherwise}
\end{cases}
$$

---

## LLM 프롬프트 구조 (매 타임스텝, 아래 순서)

1. 역할 지시 (가계 의사결정 과제)
2. 페르소나 (인구통계 + Big Five 성격 특성)
3. 공개 정보 (과거 임금/물가/세금/보조금 이력)
4. 타인 의견 — **직전 2 타임스텝 것만** 노출
5. 개인 경제 이력 (현금, 낸 세금, 받은 보조금, 상대적 재정상태)
6. 본인 직전 의견 (단기 기억으로 유지)
7. 의사결정 요청 → JSON 출력: {노동수준, 소비수준(범주형 low/medium/high), 의견 텍스트}

범주형 선택 후 실제 값은 구간 내 균등샘플링:

$$
L_{t,i} \sim \mathcal{U}\left(\mathcal{I}^{L}_{a^{L}_{t,i}}\right) \, , \quad C_{t,i} \sim \mathcal{U}\left(\mathcal{I}^{C}_{a^{C}_{t,i}}\right)
$$

---

## 메트릭 정의

| 메트릭 | 정의 |
|---|---|
| 재정상태 z-score | $z_{t,i} = \dfrac{M_{t,i} - \mu_t}{\sigma_t}$, 임계값 $\pm1.282 / \pm0.524$ (표준정규 10·30 백분위수)로 5단계 분류 |
| 감성점수 | FinBERT로 $\psi_{t,i} \in [-1,1]$ 산출 |
| 감성 3분류 | $\tilde{\psi}_{t,i} = \begin{cases} NEG & \psi_{t,i} < -\theta^{\psi} \\ NEU & -\theta^{\psi} \le \psi_{t,i} \le \theta^{\psi} \\ POS & \theta^{\psi} < \psi_{t,i} \end{cases}$ |
| 내부 상태공간 | $h_{t,i} = (\tilde{\psi}_{t,i}, \tilde{f}_{t,i}) \in \mathcal{H}$, $\mathcal{H} = \{NEG,NEU,POS\} \times \{low,average,high\}$, 9개 상태 / 81가지 전이 |
| 감성 변화율 | $\delta_t = \frac{1}{n}\sum_i \mathbb{1}(\tilde{\psi}_{t,i} \ne \tilde{\psi}_{t-1,i})$ ; $\delta'_t$ 는 NEU 제외 변화율 |
| 상태전이 카운트 벡터 | $N_i(h,h') = \sum_t \mathbb{1}(h_{t-1,i}=h,\, h_{t,i}=h')$, 개인별 81차원 벡터 $\boldsymbol{n}_i \in \mathbb{N}^{81}$ |
| 지니계수 | $G_t = \dfrac{1}{2n^2 \bar{M}_t} \displaystyle\sum_{i=1}^{n}\sum_{j=1}^{n} \left\lvert M_{t,i} - M_{t,j} \right\rvert$ |
| 양극화지수 | $P^{\Delta}_t(\theta^{\psi}) = 4\left\lvert \mu^{POS}_t - \mu^{NEG}_t \right\rvert \, r^{POS}_t \, r^{NEG}_t$ (그룹간 평균격차 × 두 그룹 비율의 균형도), 단 $\mu^{\tilde{\psi}}_t = \frac{1}{n^{\tilde{\psi}}_t}\sum_i \mathbb{1}(\tilde{\psi}_{t,i}=\tilde{\psi})\psi_{t,i}$, $r^{\tilde{\psi}}_t = n^{\tilde{\psi}}_t/n$ |
| 의견 임베딩 거리 | 384차원 sentence-transformer 임베딩 → $W_1(\Psi_{t-1},\Psi_t)$ Wasserstein-1 거리로 분포 변화량 (감성분포·임베딩분포 각각 계산) |

---

## 실험 설정

- **규모**: 20 에이전트 × 150 타임스텝 × 30회 독립 반복(run)
- **LLM**: Llama 3.1 8B, temperature = 0.7
- **페르소나 배정**: Nemotron-Personas-USA 데이터셋에서 성별/나이/혼인/학력/직업/도시 층화추출; Big Five는 각 차원의 반대 성향 서술어 중 랜덤 샘플링
- **초기조건**: 현금 M ~ U(0,100), 초기재고 K=20, 물가 p=1.0, 임금 w=1.5
- **하이퍼파라미터**: τ̄=0.01, 기본소득 b=5, α=0.1, d=0.005, A=0.95, η_p=0.2, η_w=0.03, K_max=50; 노동구간 [4,5)/[5,6)/[6,7), 소비구간 [5,6)/[6,7)/[7,8)
- **데이터 제외(burn-in)**: t ≤ 10 (초기 과도구간) 분석에서 제외

## 클러스터링 / 통계 방법

- **클러스터링**: 상태전이 카운트벡터 $\boldsymbol{n}_i$에 K-Means. 50개 랜덤시드로 강건성 확인, 평균 ARI = 0.716 (±0.073) → 6개 행동 군집
- **상관분석**: Pearson & Spearman, **run 단위(30 run)로 200회 부트스트랩** 리샘플링해 95% 신뢰구간 산출 (타임스텝을 독립표본처럼 쓰지 않음 — pseudo-replication 방지)
- **유의수준**: * p<0.10, ** p<0.05, *** p<0.01

## Ablation

프롬프트에서 "경제적으로 어려운 사람은 열심히 일해야 한다"는 명시적 지시문 제거 → 행동 분화 지표($\Delta^L$, $\Delta^C$)가 5~6%만 감소, 방향성은 유지 → 관찰된 패턴이 명시적 지시가 아니라 경제 피드백 메커니즘 자체에서 발생함을 확인.

---

## 우리 프로젝트 대비 눈에 띄는 설계 포인트 (논의 시작점, 미확정)

1. **run 단위 부트스트랩**: 타임스텝이 아니라 run(seed) 단위로 리샘플링 — 우리도 여러 seed/run이 있다면 참고 가능.
2. **burn-in 개념 동일** (t≤10 제외) — 우리 프로젝트의 burn-in=3일과 같은 발상.
3. **LLM 호출을 "선택+의견 생성"에만 국한**, 나머지(기업/정부)는 규칙 기반 — 비용/재현성 설계 참고.
4. **양극화지수 P^Δ**: 단순 표준편차가 아니라 "그룹간 격차 × 그룹크기 균형도" — RN_COMM_ON/OFF 간 belief 분포 비교에 유사한 지수 고려 가능.
5. **상태공간(감성×재정상태) → 전이벡터 → K-Means 클러스터링** 파이프라인 — 우리 쪽 (포지션 상태 × belief) 조합에도 적용 가능해 보임.
6. **동료 의견 노출을 "직전 2 타임스텝"으로 제한** — 우리 커뮤니티 노출 윈도우 설계와 비교할 지점.

## 적용 방안 (TODO — 다음 논의에서 채우기)

*(다음 대화에서 위 포인트들을 하나씩 검토하며 채워 넣을 예정)*
