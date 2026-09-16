# Reference Papers

> 보존 구분: 사용자 연구 참고자료. 현재 실행·정책 정본이 아니다.

Plan(실제 가격 경로를 외생적으로 고정한 LLM 리테일 투자자 시뮬레이션에서 **행동 정합성을 먼저 검증**하고, 이후 커뮤니티와 허위정보의 영향을 통제 실험으로 측정)에 맞춰 정리했다. 카테고리는 Related Work의 논리 흐름과 실험 순서를 따르며, 각 논문마다 `Summary`와 `Plan에서의 역할`을 중심으로 기술했다.

게재 상태는 2026-07-17 기준 공식 학회 프로시딩, 저널 페이지, DOI를 재확인했다. 정식 게재·accept이 확인된 논문은 그중 가장 등급이 높은 venue만 표기하며, 아래는 그 결과다.

- **Venue 확인**: TwinMarket(NeurIPS 2025 accepted, poster; ICLR 2025 워크숍 Best Paper는 별도 수상이므로 본문에는 NeurIPS만 표기), EconAgent(ACL 2024 main, long paper), SMISTS(Findings of ACL 2024 — **본회의가 아님**, 그대로 표기), Park et al. Generative Agents(UIST 2023), Zhou et al. Real Life(EMNLP 2024 main), SOTOPIA(ICLR 2024), Mou et al. 서베이(arXiv → **ACM Computing Surveys 2025**, DOI 10.1145/3800683), Ren et al. CRSEC(IJCAI 2024, DOI 10.24963/ijcai.2024/874), Liu et al. MOSAIC·FUSE(둘 다 EMNLP 2025 main), Min et al. FActScore(EMNLP 2023 main), Cont·Windrum·Choe et al.·Boehmer et al.·Argyle et al.·Vosoughi et al.·Pennycook et al.·Clarke et al.·Tetlock·Engelberg & Parsons·Brown & Warner·Zhang, Du, & Zhang은 모두 저널 페이지·DOI로 재확인. Windrum et al.은 탑 ML 학회 논문이라기보다 agent-based model 검증에서 널리 인용되는 방법론 저널 논문이다.
- **본 재확인으로 상태가 바뀐 문헌(기존 초안의 preprint/미확정 표기를 갱신)**: StockAgent(Zhang et al. 2024)는 **ACM Transactions on Intelligent Systems and Technology (TIST)**에 accept되었다(저자 GitHub 저장소 "[TIST]" 표기로 확인). Agent Market Arena(Qian et al., "When Agents Trade")는 "WWW 2026 accepted (예정)"으로 잠정 표기했던 것을 **ACM Web Conference 2026 정식 accept, DOI 10.1145/3774904.3792821**로 확정해 갱신한다.
- **여전히 preprint/working paper인 문헌과 신뢰 근거**: 정식 게재가 확인되지 않는 문헌은 accept 표기 대신 인용수와 저자 신뢰도로 보완한다. Horton(2023) NBER Working Paper는 저널 게재 전이지만(Review of Economics and Statistics 심사 중으로 알려짐) 동일 저자의 축약판이 **ACM EC 2024**에 실렸고 Semantic Scholar 기준 약 492회 인용 — 저자 John J. Horton(MIT Sloan)은 LLM-경제주체 문헌에서 이미 기초 문헌급으로 인용된다. Henning et al.(2025)은 여전히 preprint이나 교신저자 Colin F. Camerer(Caltech, MacArthur Fellow 2013)가 행동·실험경제학 최고 피인용 연구자 중 한 명이라 신뢰도가 높다. OASIS(Yang et al. 2024)는 preprint 상태에서 약 123회, S3(Gao et al. 2023)는 약 179회로 preprint 치고 이례적으로 높은 인용수를 보이며, 둘 다 Tsinghua FIB-lab(Yong Li 그룹, EconAgent·AgentSociety와 동일 랩) 소속이다. Lopez-Lira(2025)는 University of Florida 조교수로 "ChatGPT가 주가를 예측한다" 연구로 이미 널리 인용된 저자다. Shachi(Kuroki et al. 2025)는 Sakana AI 공동창업자 Takuya Akiba와 도쿄대 Takashi Ikegami(인공생명 분야 저명 교수)가 저자다. ASFM(약 36회 인용)과 Li et al.(2026) Behavioral Consistency Validation, Project Sid는 각각 인용 기반이 아직 충분히 쌓이지 않았거나(2026년 2월 공개) 산업 기술보고서(비심사)이므로 별도 신뢰 근거 없이 preprint임을 그대로 명시한다.

인용의 중심축은 NeurIPS·EMNLP main·ICLR·UIST와 Journal of Finance·Journal of Financial Economics·Science·Nature·Information Systems Research·Production and Operations Management·Political Analysis의 archival paper로 둔다. Quantitative Finance, Findings of ACL, JASSS 논문은 각각 stylized fact의 정의, misinformation simulation, ABM 검증 방법을 보완하는 문헌으로 사용한다.

---

## 1. 시장 재현이 아니라 무엇을 검증할 것인가

**Plan에서의 역할:**“개인 LLM 에이전트만으로 실제 시장 전체를 재현할 수 없다”는 직관을 논문에서 방어 가능한 명제로 바꾼다. 핵심 주장은 시장 재현이 절대적으로 불가능하다는 것이 아니라, **가격의 stylized fact를 맞추는 것만으로는 에이전트의 개별 판단이 실제 개인 투자자와 닮았다고 식별할 수 없다**는 것이다. 따라서 기존 연구를 부정하기보다, 거시적 시장 타당성과 미시적 행동 타당성이 서로 다른 검증 대상임을 밝힌다.
( + LLM 에이전트 시장 시뮬레이션의 검증은 통상 stylized facts와 집단 수준 패턴에서 멈춰왔다. 페르소나가 에이전트 수준에서 설계대로 발현되는지는 최근에야 검증되기 시작했고 (Li et al. 2026), LLM 에이전트는 인간 시장 참여자의 행동적 편차(버블, 역행 매수)를 체계적으로 덜 재현하며 교과서적 합리성으로 수렴한다(Henning et al. 2025). 허위정보의 금융 효과에 대한 기준 증거는 자연발생 사건의 관찰(역인과 교란), 또는 TwinMarket처럼 **내생 가격 시장에서 단일 루머 사례를 처치-대조로 비교**한 사후 관찰(TwinMarket도 고증심성 유저에게 과장된 헤드라인을 주입하고 중립 뉴스 대조군과 비교하는 처치 실험을 이미 수행했다)뿐이었다. 다수의 허위정보 처치를 **요인설계로 교차**하고 반복 통계 검정으로 식별한 사례는 아직 없다. 이 절의 네 편은 "정렬 수준이 계측되고(E1) 페르소나 발현이 점검된(E2-E3) 에이전트 집단에서, 조직적 허위정보와 커뮤니티 채널의 효과를 2*2 처치로 분리 식렬하는 것(E4)" 이라는 본 연구의 위치를 세우는 직접 방법론 선행이다. 주장 범위는 "허위정보가 시장을 움직인다"가 아니라 "modeled investor agents의 반응"으로 한정한다(외생 가격). )

### Yang et al. (2025) — TwinMarket

- **Title**: [TwinMarket: A Scalable Behavioral and Social Simulation for Financial Markets](https://papers.nips.cc/paper_files/paper/2025/file/5bf234ecf83cd77bc5b77a24ba9338b0-Paper-Conference.pdf)
- **Authors**: Yuzhe Yang, Yifei Zhang, Minghao Wu, Kaidi Zhang, Yunmiao Zhang, Honghai Yu, Yan Hu, Benyou Wang
- **Venue / status**: Advances in Neural Information Processing Systems 38 (NeurIPS 2025) — accepted main conference paper
- **Summary**: 대규모 투자자 데이터를 이용해 BDI 기반 투자자 agent, 사회적 관계, 거래 환경을 구축하고, 생성된 가격 경로를 실제 가격과 비교한다. 가격 RMSE·MAE와 fat tail, volatility clustering, bubbles/recessions 등의 거시 검증뿐 아니라 wealth inequality와 turnover–return 관계를 이용한 미시 수준 검증도 수행한다.
- **Plan에서의 역할**: 가장 가까운 비교대상 중 하나인 논문. 본 연구는 TwinMarket의 목표가 잘못되었다고 주장하지 않고, **다른 construct를 검증한다**고 정리한다. TwinMarket이 가격 형성과 거시적 시장 동학을 묻는다면, 본 연구는 실제 가격을 고정한 뒤 리테일 투자자의 순거래 방향과 정보환경 개입 효과를 묻는다. TwinMarket은 micro- and macro-level validation을 모두 수행한다. 그러나 그 micro validation은 agent 집단에서 나타나는 wealth inequality와 turnover–return 관계 등 행동 패턴의 재현에 초점을 둔다. 본 연구는 이와 별도로, 고정된 실제 가격 경로에서 simulation 집단의 일별 순매매 방향이 실제 개인 투자자 순매매와 얼마나 정합적인지를 검증한다.(at Baseline)

### Cont (2001)

- **Title**: [Empirical Properties of Asset Returns: Stylized Facts and Statistical Issues](https://doi.org/10.1080/713665670)
- **Authors**: Rama Cont
- **Venue / status**: Quantitative Finance, 1(2), 223–236 — published journal article
- **Summary**: 다양한 시장과 자산에서 반복적으로 관찰되는 fat tails, volatility clustering, absence of linear autocorrelation 등의 경험적 규칙을 정리한다. 이러한 규칙은 개별 시장의 세부 원인보다 여러 시장에 공통된 통계적 특성을 요약한다.
- **Plan에서의 역할**: stylized fact의 정의와 범위를 인용하는 기준 문헌이다. 이 논문이 “stylized fact는 쓸모없다”거나 “개별 판단을 식별할 수 없다”고 직접 말하는 것은 아니므로 그렇게 인용하지 않는다. 대신 stylized fact가 시장 수익률의 집계 특성을 요약한다는 점을 분명히 하고, 본 연구는 그와 별도로 개인의 매수·매도 방향 분석이 필요하다고 설계 선택을 제시한다. 즉, stylized fact만 이용하는 것이 아니라 개인의 매수·매도 방향 분석의 필요성을 보여줄 때 사용한다.


### Windrum, Fagiolo, and Moneta (2007)

- **Title**: [Empirical Validation of Agent-Based Models: Alternatives and Prospects](https://www.jasss.org/10/2/8.html)
- **Authors**: Paul Windrum, Giorgio Fagiolo, Alessio Moneta
- **Venue / status**: Journal of Artificial Societies and Social Simulation, 10(2), 8 — published peer-reviewed methodology article
- **Summary**: agent-based model의 경험적 검증이 단일 output matching으로 끝나지 않으며, 입력·미시 규칙·거시 결과·calibration을 함께 고려해야 한다고 정리한다. 검증 대상과 수준을 명시하지 않으면 서로 다른 모델이 같은 거시 결과를 설명하는 문제가 남는다.
- **Plan에서의 역할**: 본 연구의 **다층 검증 구조**를 정당화한다. Stage 1에서 실제 개인 순거래와 aggregate behavioral alignment를 확인하고, Stage 2에서 개입 효과를 측정하며, Stage 3에서 언어·노출·커뮤니티 메커니즘을 분석하는 순서를 뒷받침한다. 탑 ML 근거라기보다 Methods의 validation framework를 위한 중심 방법론 문헌으로 사용한다.

---

## 2. 시뮬레이션 검증을 계측 대상으로 삼아야하는 이유

### Li et al. (2026) — Behavioral Consistency Validation

- **Title**: [Behavioral Consistency Validation for LLM Agents: An Analysis of Trading-Style Switching through Stock-Market Simulation](https://arxiv.org/abs/2602.07023)
- **Authors**: Zeping Li, Guancheng Wan, Keyang Chen, Yu Chen, Yiwen Zhao, Philip Torr, Guangnan Ye, Zhenfei Yin, Hongfeng Chai
- **Venue / status**: arXiv:2602.07023 (Fudan / UCLA / BNP Paribas / Oxford) — preprint, 학회·저널 게재 여부 미확인
- **Summary**: 손실회피·군집성향·부 차별화 민감도·가격괴리 민감도 4개 행동재무 동인을 2⁵ 완전요인설계로 32개 에이전트에 프롬프트 주입하고, S&P500 5종목·253거래일에서 10거래일마다 스타일 전환(기술적↔기본적) 결정을 관찰. counterfactual ledger로 대안 전략 성과 추적. 특성별 정렬 점수를 정의해 특성 보유 16 vs 미보유 16 코호트를 Mann-Whitney U + rank-biserial/Cliff's δ/CLES로 비교. 결과: 손실회피는 4개 모델 전부 유의, 군집은 Qwen만, 부 차별화는 GPT-4o-mini만, 가격괴리는 전무 — 프롬프트로 심은 특성은 부분적으로만 발현.
- **Plan에서의 역할**: 페르소나 발현을 당연시할 수 없다는 직접 근거이자 E2(조작 점검)의 존재 이유. 인공 요인설계 페르소나·전환 행동을 검증한 원논문을 본 연구는 실데이터 파생(TwinMarket 차용) 페르소나·특성 **발현** 검증으로 번안한다 — 전략이 고정된 설계라 "전환"이 아닌 "발현"이 검증 대상이다. E2의 통계 절차 전체(코호트 비교, 단측 MW-U, δ/CLES, 부분 발현의 정직한 보고)와 E5의 rationale-centric case study 형식(부록 C), E6 다중 모델 비교(단일 백엔드 한계 근거)의 선례다. 4개 동인은 본 연구의 페르소나 필드가 아니므로 직접 코호트 검증은 불가능하고 이론 매핑(처분효과↔손실회피 등)으로만 연결한다. counterfactual ledger가 없는 본 연구 설계에서는 전환·반사실 기반 지표를 계산하지 않는다.

### Li et al. (2024) — EconAgent

- **Title**: [EconAgent: Large Language Model-Empowered Agents for Simulating Macroeconomic Activities](https://arxiv.org/abs/2310.10436)
- **Authors**: Nian Li, Chen Gao, Mingyu Li, Yong Li, Qingmin Liao
- **Venue / status**: Proceedings of ACL 2024 — accepted main conference paper (Tsinghua; [코드 공개](https://github.com/tsinghua-fib-lab/ACL24-EconAgent))
- **Summary**: perception·memory·action 모듈을 갖춘 100 에이전트가 노동·소비를 결정하는 거시경제 시뮬레이션(20년, GPT-3.5). 검증: (a) 규칙(LEN/CATS/Composite)·RL(AI-Economist) 베이스라인 대비 지표 안정성과 필립스 곡선·오쿤 법칙의 올바른 재현(Pearson −0.619, −0.918; 규칙 베이스라인은 필립스 곡선 방향이 역전), (b) 모듈 ablation(perception 제거 시 "too stable", reflection 제거 시 초기 인플레 이상), (c) 에이전트별 회귀 — 240개 결정을 프롬프트 투입 변수에 회귀해 "변수가 유의한 에이전트 수" 집계(Table 1), (d) 대화 재투입 사례 분석, (e) COVID 외부 충격의 질적 재현, (f) robustness: 모델별 5회 반복(Fig. 12), N=100 vs 300 민감도, 비용($30/2h) 보고.
- **Plan에서의 역할**: LLM 에이전트가 규칙 기반 대비 현실적 규칙성을 재현한다는 분야 표준 근거. E3의 원칙 전체 — "회귀 독립변수 = 프롬프트에 실제로 들어간 변수", 에이전트별 회귀 + 유의 에이전트 수 집계(관측 60여 일 확보로 가능), 보수적 대안은 풀링+고정효과. E1의 규칙 더미(always-buy/momentum/contrarian) 비교 구조와 E6의 ablation(페르소나 제거)·시드 반복·구성 민감도·비용 보고 관행의 선례. 다만 EconAgent의 대표 검증(필립스·오쿤)은 행동이 거시 지표를 만들어내는 내생 구조에서만 가능하다 — 외생 가격인 본 연구에는 시스템 수준 검증 경로가 닫혀 있음을 명시한다.

### Henning et al. (2025) — LLM Agents Do Not Replicate Human Market Traders

- **Title**: [LLM Agents Do Not Replicate Human Market Traders: Evidence From Experimental Finance](https://arxiv.org/abs/2502.15800)
- **Authors**: Thomas Henning, Siddhartha M. Ojha, Ross Spoon, Jiatong Han, Colin F. Camerer
- **Venue / status**: arXiv:2502.15800v3 (Caltech / Virginia Tech / Zhejiang) — preprint, 학회·저널 게재 여부 미확인. 2025-02 초판 제목 "LLM Trading: Analysis of LLM Agent Behavior in Experimental Asset Markets", 2025-10 개정판(v3) 제목 기준으로 인용.
- **Summary**: 인간 피험자에게서 버블·붕괴를 안정적으로 유발하는 고전 실험금융 패러다임(기본가치가 알려진 위험자산, SSW 계열)에 LLM 에이전트를 투입(단일 모델 시장 + 혼합 "battle royale"). LLM은 기본가치 근처의 "교과서적 합리성"으로 수렴, 버블 형성은 미미, 거래 전략 분산도 인간보다 작음. 결론: 창발적 대형 버블 같은 인간 행동 특징을 LLM 전용 데이터로 재현하는 것에 의존하면 위험.
- **Plan에서의 역할**: "LLM 에이전트 ≠ 인간 투자자"가 이미 알려진 현상이라는 근거 — E1의 정렬 계측이 필요한 이유. E1의 전쟁 충격 구간 불일치(에이전트 추세 매도 vs 실제 리테일 역행 매수)를 이 논문의 한국 리테일·실제 가격 경로 버전으로 위치 부여 — balanced accuracy 천장(~76.5%)을 "시뮬레이션의 부족"이 아니라 "알려진 한계의 정량화"로 서술하는 근거(약점의 무기화). 저쪽은 인간의 버블(비합리)을 LLM이 안 만들고, 본 연구는 인간의 역행 매수(행동적)를 LLM이 안 함 — 현상은 다르나 상위 명제("too rational")가 같아 독립 수렴 증거다. "LLM 전략 분산 < 인간" 발견은 E6 ablation의 예측 근거(페르소나는 부족한 행동 분산을 인위적으로 복원하는 장치라는 해석)로도 연결된다.
  - **방법론 주의**: 이 논문은 MSE(기본가치 대비)·PCC(피어슨 상관계수, 인간 평균 가격 경로와의 유사도)·포트폴리오 분산을 Table 1의 헤드라인 지표로 쓴다. PCC는 "라운드별 내생 가격 경로(연속형)"끼리의 유사도를 재는 것으로, 본 연구 E1(외생 가격 + 이진 매수/매도 방향, Balanced Accuracy)과는 비교 대상의 성격이 다르다. 차용하는 것은 PCC라는 통계 기법이 아니라 "too rational" 해석 결론뿐임을 본문에 명시한다.
  - **해석 경계**: 실험실 인공 자산·기본가치 공지 설계다. 실제 가격 경로·실제 투자자 데이터와의 대조는 본 연구가 추가하는 부분이며, 이들의 결과를 "모든 시장 맥락에서 LLM이 합리적"으로 일반화하지 않는다.

---

## 3. 실제 가격 경로 위에서 행동적 정합성을 검증하는 근거

**Plan에서의 역할:** 실제 삼성전자 가격을 외생적으로 주고 agent가 buy/sell만 선택하는 설계의 검증 대상을 정한다. 여기서 확인하는 것은 개별 인간 투자자의 심리적 복제가 아니라, agent 집단이 만든 일별 순거래 방향이 실제 개인 투자자 집계 흐름을 얼마나 근사하는가라는 **aggregate behavioral alignment**이다.

### Choe, Kho, and Stulz (1999)

- **Title**: [Do Foreign Investors Destabilize Stock Markets? The Korean Experience in 1997](https://doi.org/10.1016/S0304-405X(99)00037-9)
- **Authors**: Hyuk Choe, Bong-Chan Kho, René M. Stulz
- **Venue / status**: Journal of Financial Economics, 54(2), 227–264 — published journal article
- **Summary**: 한국 주식시장에서 외국인 투자자의 herding, positive feedback trading, 거래 시점과 가격 영향을 분석한다. 투자자 유형별 주문 흐름을 분리하고 장중 거래를 세분해 본다는 점이 중요하다.
- **Plan에서의 역할**: 실제 시장 가격은 개인뿐 아니라 외국인·기관 등 여러 투자자 유형의 상호작용으로 형성된다는 근거다. 따라서 본 연구에서 실제 가격을 고정하고 개인 투자자 행동만 비교하는 것은 전체 시장 재현 주장을 피하는 구조적 선택이다. 다만 이 논문이 1997년 한국시장 자료를 사용하므로 현재 삼성전자 기간의 효과 크기를 직접 이전하지 않는다.

### Boehmer et al. (2021)

- **Title**: [Tracking Retail Investor Activity](https://doi.org/10.1111/jofi.13033)
- **Authors**: Ekkehart Boehmer, Charles M. Jones, Xiaoyan Zhang, Xinran Zhang
- **Venue / status**: Journal of Finance, 76(5), 2249–2305 — published journal article
- **Summary**: 거래 데이터에서 retail activity를 식별하고 retail buy/sell imbalance를 구성해 시장 결과와 연결한다. retail order imbalance를 방향과 크기의 집계 지표로 다루는 대표적 금융 실증 연구다.
- **Plan에서의 역할**: 실제 개인 투자자의 순매수·순매도 흐름을 behavioral benchmark로 두는 근거다. 본 연구에서는 실제 `Individuals` 순거래대금 또는 순거래량과 simulation의 일별 AM+PM signed fills를 비교한다. 단위가 다르면 방향 일치가 주지표가 되고, 크기 상관은 표준화 후 보조지표로만 사용한다.

### Argyle et al. (2023)

- **Title**: [Out of One, Many: Using Language Models to Simulate Human Samples](https://doi.org/10.1017/pan.2023.2)
- **Authors**: Lisa P. Argyle, Ethan C. Busby, Nancy Fulda, Joshua R. Gubler, Christopher Rytting, David Wingate
- **Venue / status**: Political Analysis, 31(3), 337–351 — published journal article
- **Summary**: 인구통계·사회적 배경을 conditioning한 language model이 집단별 응답 패턴을 어느 정도 재현할 수 있는지를 algorithmic fidelity 관점에서 평가한다. 평균 응답뿐 아니라 집단 간 차이와 분포적 정합성을 함께 본다.
- **Plan에서의 역할**: persona를 사용하는 이론적 근거이자 해석의 경계다. 연령·투자성향·depth별 행동 차이를 분석할 수 있지만, 사전에 정의된 persona가 실제 한국 투자자 분포를 대표한다는 근거가 없으면 **modeled persona heterogeneity**로만 사용. depth가 무작위 배정되지 않고 정보 접근·커뮤니티 권한과 함께 변하면 인과적 “depth 효과”로 해석하지 않는다.

### Ma et al. (2024) — SMISTS

- **Title**: [Simulated Misinformation Susceptibility (SMISTS): Enhancing Misinformation Research with Large Language Model Simulations](https://aclanthology.org/2024.findings-acl.162/)
- **Authors**: Weicheng Ma, Chunyuan Deng, Aram Moossavi, Lili Wang, Soroush Vosoughi, Diyi Yang
- **Venue / status**: Findings of the Association for Computational Linguistics: ACL 2024 — accepted peer-reviewed Findings paper
- **Summary**: persona-conditioned LLM을 사용해 허위정보의 정확성 판단과 공유 의도를 시뮬레이션하고, 실제 인간 응답과의 관계 및 demographic heterogeneity를 평가한다. 판단(judgment)과 공유 의도(action intention)가 같은 결과가 아님을 보여주는 데 유용하다.
- **Plan에서의 역할**: belief, sharing, trading을 구분하는 근거다. 본 연구의 기사 열람·검색·좋아요·매매는 서로 다른 단계로 기록하며, `selected/read`를 belief로 부르지 않는다. SMISTS의 정확성 판단 문항을 그대로 복제할 필요는 없지만, 별도의 사후 stance/confidence rubric을 두어 언어적 판단과 실제 주문을 분리하는 원리를 차용할 수 있다.

---

## 4. LLM 사회 시뮬레이션의 설계와 타당성 경계

**Plan에서의 역할:** persona, memory, 상호작용을 사용하는 기술적 선례와 동시에, 그럴듯한 대화가 인간 사회의 재현을 뜻하지 않는다는 한계를 정리한다. 이 절은 모델 내부 결과와 인간 투자자에 대한 외적 일반화를 구분하는 역할을 한다.

### Park et al. (2023) — Generative Agents

- **Title**: [Generative Agents: Interactive Simulacra of Human Behavior](https://doi.org/10.1145/3586183.3606763)
- **Authors**: Joon Sung Park, Joseph C. O'Brien, Carrie J. Cai, Meredith Ringel Morris, Percy Liang, Michael S. Bernstein
- **Venue / status**: ACM Symposium on User Interface Software and Technology (UIST 2023) — accepted conference paper
- **Summary**: observation, memory, reflection, planning 구조를 가진 생성형 agent가 장기간 상호작용하고 사회적 행동을 형성할 수 있음을 보인다. memory retrieval와 reflection ablation을 통해 architecture 구성요소의 역할을 평가한다.
- **Plan에서의 역할**: 과거 뉴스·거래·커뮤니티가 다음 turn의 판단에 들어가는 memory 기반 설계의 선례다. 본 연구에서는 PM 커뮤니티가 PM 주문 뒤에 발생하므로, 같은 PM 주문이 아니라 **다음 AM 이후 행동**에 연결해야 한다는 시간 순서 검증에 사용한다.

### Zhou et al. (2024) — Is this the real life?

- **Title**: [Is this the real life? Is this just fantasy? The Misleading Success of Simulating Social Interactions With LLMs](https://aclanthology.org/2024.emnlp-main.1208/)
- **Authors**: Xuhui Zhou, Zhe Su, Tiwalayo Eisape, Hyunwoo Kim, Maarten Sap
- **Venue / status**: EMNLP 2024 — accepted main conference paper
- **Summary**: LLM끼리 상호작용할 때 인간 실험과 다른 정보 비대칭이 사라져, 표면적으로 높은 social success가 과장될 수 있음을 보인다. agent가 사람보다 상대의 정보를 더 직접적으로 공유받는 설정은 평가를 쉽게 만든다.
- **Plan에서의 역할**: 정보 누출과 환경 설계 audit의 핵심 근거다. agent에게 `fake` 라벨, 미래 가격, 다른 agent의 비공개 state가 노출되지 않았는지 확인하고, visible candidate·read/search·selected를 구분해 기록한다. simulation 내부에서 유의한 효과가 나와도 인간 투자자 효과로 바로 일반화하지 않는 Limitations의 근거다.

### Zhou et al. (2024) — SOTOPIA

- **Title**: [SOTOPIA: Interactive Evaluation for Social Intelligence in Language Agents](https://proceedings.iclr.cc/paper_files/paper/2024/hash/b3075b88e583a0e98d8b24338a613060-Abstract-Conference.html)
- **Authors**: Xuhui Zhou, Hao Zhu, Leena Mathur, Ruohong Zhang, Haofei Yu, Zhengyang Qi, Louis-Philippe Morency, Yonatan Bisk, Daniel Fried, Graham Neubig, Maarten Sap
- **Venue / status**: International Conference on Learning Representations (ICLR 2024) — accepted conference paper
- **Summary**: 사회적 목표가 있는 agent–agent 및 human–agent 상호작용을 구성하고, goal completion과 social intelligence의 여러 차원을 평가한다. 강한 LLM도 복잡한 사회적 상황에서는 안정적으로 높은 성능을 보이지 못한다.
- **Plan에서의 역할**: community interaction을 단순 engagement 수치 하나로 환원하지 않는 근거다. 그대로 SOTOPIA-Eval을 복제하기보다, 본 연구의 community 기능에 맞게 정보공유, 동조, 반박, 질문, 근거제시 등 **관찰 가능한 게시글 기능 rubric**을 수정 차용할 수 있다. `like/Best`를 social intelligence score로 부르지는 않는다.

**Plan에서의 역할:** Related Work 도입부의 분류 틀로 쓴다. 본 연구 결과의 직접 근거로는 사용하지 않는다.

### Mou et al. (2024) — From Individual to Society

- **Title**: [From Individual to Society: A Survey on Social Simulation Driven by Large Language Model-based Agents](https://arxiv.org/abs/2412.03563)
- **Venue / status**: arXiv:2412.03563 (2024) → **ACM Computing Surveys** 게재(2025, DOI 10.1145/3800683) — accepted peer-reviewed journal article
- **Summary**: LLM 사회 시뮬레이션을 개인/시나리오/사회 시뮬레이션으로 분류한 포괄 서베이다. 분류 틀은 원문 대조로 확인된다.
- **Plan에서의 역할**: Related Work 도입부의 분류 틀이다.

---

## 5. 금융시장 LLM 시뮬레이션 선행연구

**Plan에서의 역할:** Related Work의 "분야 지형" 서술용이다. 대부분 배경·비교 인용이며, 본 연구의 중심 주장을 직접 뒷받침하지는 않는다.

### Zhang et al. (2024) — StockAgent

- **Title**: When AI Meets Finance (StockAgent): Large Language Model-based Stock Trading in Simulated Real-world Environments
- **Authors**: C. Zhang, X. Liu, Z. Zhang, M. Jin, L. Li, Z. Wang, Y. Zhang, et al.
- **Venue / status**: arXiv:2407.18957 (2024) → **ACM Transactions on Intelligent Systems and Technology (TIST)** accepted (저자 GitHub 저장소 "[TIST]" 표기로 확인, 2026-07-17 재확인)
- **Summary**: 이벤트 기반 멀티에이전트 주식거래 시뮬레이션. 외부 요인(금리 등)·자산 규모·전략이 거래에 미치는 영향을 조건별로 평가하고, GPT-3.5와 Gemini 등 백엔드 간 뚜렷이 다른 거래 패턴을 보고. 모델 사전지식의 시장 예측 누출(test-set leakage)을 최소화하는 설계를 강조.
- **Plan에서의 역할**: 금융 LLM 에이전트 시뮬레이션의 대표 선행 중 하나이나, 조건별 행동 비교는 있어도 실제 투자자 데이터와의 정렬 검증·허위정보 처치는 없다. 백엔드 간 행동 차이 보고는 단일 백엔드 한계(E6·Limitations)의 인용 근거로 쓴다.

### Horton (2023) — Homo Silicus

- **Title**: [Large Language Models as Simulated Economic Agents: What Can We Learn from Homo Silicus?](https://www.nber.org/papers/w31122)
- **Authors**: John J. Horton
- **Venue / status**: NBER Working Paper w31122 (2023) — working paper, 정식 학회·저널 게재 아님
- **Summary**: LLM을 인간의 암묵적 계산 모델로 취급해 모사 대상 집단의 대리자(proxy)로 사용할 수 있다는 경제학적 논증. 고전 행동경제학 실험의 LLM 재현을 시연.
- **Plan에서의 역할**: "에이전트로 인간 대상 불가능한 실험을 대행한다"는 프레임의 이론적 근거. proxy 주장은 Henning et al.의 반증적 발견과 함께 균형 있게 인용한다 — 대행 가능성과 충실도 한계를 한 쌍으로 다룬다.

### 기타 인접 선행 (한 줄 정리)

- **ASFM (Gao et al., 2024)**: "Simulating Financial Market via Large Language Model based Agents," arXiv:2406.19966 — preprint. 현실적 주문 체결 + 뉴스 반응 에이전트의 주식시장 시뮬레이션 — 분야 지형 각주용. *(유사 제목의 별개 논문 arXiv:2510.12189(2025)와 혼동 주의.)*
- **Agent Market Arena (Qian et al., 2025)**: 실제 논문 제목은 "When Agents Trade: Live Multi-Market Trading Benchmark for LLM Agents," arXiv:2510.11695 — "Agent Market Arena"는 벤치마크 별명. **ACM Web Conference (WWW) 2026 accepted 확정**(DOI 10.1145/3774904.3792821, 2026-07-17 재확인). 에이전트 아키텍처 4종(InvestorAgent·TradeAgent·HedgeFundAgent·DeepFundAgent) × 백본 5종 실거래 비교. "아키텍처가 행동 패턴을 좌우, 백본 기여는 상대적으로 작음" — 단일 백엔드 한계 논의의 보조.
- **Shachi (Kuroki et al., 2025)**: "Shachi: A Modular, Controllable Framework for LLM-Based Agent-Based Modeling of Emergent Collective Behavior," arXiv:2509.21862 — preprint. "Shachi"는 프레임워크명(저자 아님). EconAgent 다중 백엔드 재실행 결과는 부록(Appendix C.6) 실험이며 논문의 핵심 기여가 아님 — 인용 시 부록 실험임을 명시. 규칙성 재현은 공통이나 절편·기울기가 모델별 상이. 백엔드 민감도 인용 보조.
- **Lopez-Lira (2025)**: "Can Large Language Models Trade? Testing Financial Theories with LLM Agents in Market Simulations," arXiv:2504.10789 — preprint(SSRN 동시 게재). 시장 시뮬레이션 내 금융이론 검증(가치·모멘텀·마켓메이커 전략 에이전트) — 인접. *(같은 저자가 공저한 별개 논문 Li et al. 2026과 혼동 주의.)*

---

## 6. 사회·허위정보(가짜뉴스) 시뮬레이션

**Plan에서의 역할:** MOSAIC·FUSE·SMISTS(준영님 문서 담당) 이외에 금융 도메인 밖에서 이뤄진 대규모 소셜/허위정보 시뮬레이션의 지형을 세운다. 대부분 "확산은 있으나 거래가 없다"는 대비 축으로만 쓴다.

### Yang et al. (2024) — OASIS

- **Title**: OASIS: Open Agent Social Interaction Simulations with One Million Agents
- **Authors**: Ziyi Yang 외 22인(23인 공저)
- **Venue / status**: arXiv:2411.11581 — preprint, 학회·저널 게재 없음
- **Summary**: X·Reddit을 본뜬 범용 소셜미디어 시뮬레이터, 최대 100만 에이전트. 정보 확산·집단 양극화·군집 효과 재현.
- **Plan에서의 역할**: 소셜 플랫폼 동학은 있으나 거래 행동이 없음 — "확산 연구와 거래 연구 사이의 빈자리"를 세우는 대비 축.

### Gao et al. (2023) — S3

- **Title**: [S3: Social-network Simulation System with Large Language Model-Empowered Agents](https://arxiv.org/abs/2307.14984)
- **Venue / status**: arXiv:2307.14984 — preprint (EconAgent 참고문헌에서 서지 확인)
- **Summary**: 실제 소셜 네트워크에서 감정·태도·상호작용 행동을 재현하는 시뮬레이션 프레임워크.
- **Plan에서의 역할**: 사회 시뮬레이션 지형 각주. 현재 결과의 근거로 쓰지 않음.

### 기타 (한 줄 정리)

- **AgentSociety (Piao et al., 2025)**: arXiv:2502.08691 — preprint. 실제 규모는 1만+ 에이전트·500만 상호작용, 5개 시나리오(양극화·선동성 메시지 확산·UBI 정책·재난 대응·도시 지속가능성) — 지형 각주.
- **CRSEC (Ren et al., 2024)**: 실제 제목 "Emergence of Social Norms in Generative Agent Societies: Principles and Architecture," arXiv:2403.08251 — **IJCAI 2024 accepted peer-reviewed conference paper**(DOI 10.24963/ijcai.2024/874). CRSEC = Creation&Representation/Spreading/Evaluation/Compliance — 지형 각주. *(피어리뷰 통과 논문이므로 인용 강도 재검토 여지 있음.)*
- **Project Sid (Altera.AL, 2024)**: arXiv:2411.00114 — **비심사 industry technical report**(학회·저널 게재 없음). Minecraft 문명 수준 창발 — 지형 각주, 인용 시 preprint/기술보고서임을 명시.

---

## 7. 커뮤니티와 허위정보의 사회적 영향

**Plan에서의 역할:** RQ1과 RQ4를 뒷받침한다. 커뮤니티 ON/OFF는 정보 채널의 존재가 modeled retail agent의 stance와 매매를 어떻게 바꾸는지 보는 개입이며, 허위정보와 커뮤니티의 결합은 amplification 또는 buffering을 검정하는 interaction이다. 현재 구조만으로 통계적 mediation이나 실제 네트워크 cascade를 주장하지 않는다.

### Liu et al. (2025) — MOSAIC

- **Title**: [MOSAIC: Modeling Social AI for Content Dissemination and Regulation in Multi-Agent Simulations](https://aclanthology.org/2025.emnlp-main.325/)
- **Authors**: Genglin Liu, Vivian T. Le, Salman Rahman, Elisa Kreiss, Marzyeh Ghassemi, Saadia Gabriel
- **Venue / status**: EMNLP 2025 — accepted main conference paper
- **Summary**: 다수의 LLM agent가 뉴스와 user-generated content에 반응하고, 공유·댓글·moderation을 수행하는 social-media simulation을 제시한다. reasoning trace의 표현과 실제 action이 항상 일치하지 않을 수 있음을 보이며, 별도 popularity study에서 BERTopic으로 게시글 topic과 engagement를 사후 분석한다.
- **Plan에서의 역할**:
  - **그대로 차용 가능한 원리**: 조건별 커뮤니티 활동을 분리하고, 표현된 이유·정서와 실제 행동을 따로 평가한다. 본 연구에서는 `belief_summary`·뉴스 해석·게시글의 bullish/bearish stance와 실제 매수/매도 방향의 일치율을 계산한다.
  - **수정 차용 가능한 분석**: 본 연구의 처치 단위는 topic이 아니라 개별 fake article/claim이다. 따라서 article별로 노출 전후의 `belief_summary`, 뉴스 해석, community post, 다음 turn 매매를 연결하고, rubric과 embedding으로 반응을 분석한다. BERTopic은 source가 연결되지 않은 일반 커뮤니티 담론을 탐색할 때만 선택적으로 사용한다.
  - **현재 구조에서 불가능한 주장**: 현재 community가 directed follower graph나 repost cascade를 저장하지 않으므로 centrality, homophily, cascade depth를 MOSAIC처럼 분석할 수 없다. 또한 embedding 또는 BERTopic만으로 특정 허위 claim의 수용을 판정할 수 없다.

#### MOSAIC에서 차용할 분석: rubric과 embedding

- MOSAIC은 agent의 reasoning trace와 실제 action을 분리해 본다는 원리를 제공한다.
- 본 연구의 단위는 topic이 아니라 `agent × fake article × 최초 노출 turn`이다.
- 노출 전 belief(`t−1`), 노출 후 belief·거래(`t`), 다음 turn belief·거래(`t+1`)를 연결한다.
- PM 커뮤니티는 PM 거래 뒤에 발생하므로, 효과는 다음 AM 이후에 본다.
- `belief_summary`와 뉴스 해석을 blind rubric으로 coding하며, 이는 latent belief가 아닌 expressed belief다.
- rubric은 stance(−2~+2), fake claim 관계(반박~강화), confidence(0~4)를 분리한다.
- primary는 fake 방향으로의 stance 변화, secondary는 claim 수용·confidence 변화다.
- embedding은 fake·factual anchor·belief·게시글의 의미상 상대적 위치와 이동을 시각화한다.
- embedding 유사도만으로 취약성을 판정하지 않고, claim 수용 rubric과의 정합성을 확인한다.
- fake별·persona별 effect size는 forest plot/heatmap, embedding 이동은 UMAP/trajectory로 보인다.
- `source_fake_id`, `claim_id`, `event_id`가 있어야 기사별 belief·post·trade를 직접 연결할 수 있다.
- BERTopic은 source가 없는 일반 게시글을 보조적으로 요약할 때만 사용한다.
- 주검정은 stance·signed trade의 condition contrast와 `misinformation × community` interaction이다.

### Vosoughi, Roy, and Aral (2018)

- **Title**: [The Spread of True and False News Online](https://doi.org/10.1126/science.aap9559)
- **Authors**: Soroush Vosoughi, Deb Roy, Sinan Aral
- **Venue / status**: Science, 359(6380), 1146–1151 — published journal article
- **Summary**: Twitter diffusion cascade에서 false news가 true news보다 더 멀리, 빠르게, 깊게 확산되는 경향을 대규모 자료로 보인다. novelty와 감정 반응이 확산 차이와 관련됨을 제시한다.
- **Plan에서의 역할**: 사회적 채널이 허위정보의 영향을 증폭할 수 있다는 RQ4의 배경 근거다. 다만 현재 연구에는 실제 social graph와 repost cascade가 없으므로 이 논문의 depth·breadth·velocity metric을 그대로 사용하지 않는다. 향후 provenance와 repost edge를 저장할 때 확장 가능한 선례로 둔다.

### Pennycook et al. (2021)

- **Title**: [Shifting Attention to Accuracy Can Reduce Misinformation Online](https://doi.org/10.1038/s41586-021-03344-2)
- **Authors**: Gordon Pennycook, Ziv Epstein, Mohsen Mosleh, Antonio A. Arechar, Dean Eckles, David G. Rand
- **Venue / status**: Nature, 592, 590–595 — published journal article
- **Summary**: 정확성에 주의를 환기하는 간단한 intervention이 misinformation sharing intention과 실제 공유 행동을 개선할 수 있음을 여러 실험에서 보인다. 허위정보 공유가 반드시 강한 믿음만으로 설명되지는 않고, attention의 방향도 중요하다.
- **Plan에서의 역할**: 기사 후보 노출, 실제 열람, 선택, 커뮤니티 endorsement, 주문을 분리하는 근거다. 본 연구에서 `read/search/selected`는 attention·정보접촉 지표이며 belief의 직접 측정치가 아니다. community가 fake effect를 완충할 경우, 반박·근거제시·정확성 언급 비율을 exploratory mechanism으로 분석할 수 있다.

---

## 8. 뉴스가 투자자 행동에 영향을 주는 금융 실증 근거

**Plan에서의 역할:** 뉴스와 매체 노출이 투자자 attention, sentiment, 거래에 영향을 줄 수 있음을 보여준다. 다만 본 연구에서는 실제 가격 경로가 모든 조건에 동일하므로, treatment가 가격을 변화시켰다는 event-study 주장은 할 수 없다. 금융 문헌은 **행동 결과변수와 event window를 설계하는 근거**로 차용한다.

### Clarke et al. (2021)

- **Title**: [Fake News, Investor Attention, and Market Reaction](https://doi.org/10.1287/isre.2019.0910)
- **Authors**: Jonathan Clarke, Hailiang Chen, Ding Du, Yu (Jeffrey) Hu
- **Venue / status**: Information Systems Research, 32(1), 35–52 — published journal article
- **Summary**: 금융 관련 fake news가 investor attention과 시장 반응에 어떤 관계를 갖는지 실증적으로 분석한다. 허위정보의 영향이 정보의 존재뿐 아니라 투자자의 attention과 연결되어 있음을 보여준다.
- **Plan에서의 역할**: fake article의 `visible → read/search → selected → trade` 경로를 분리하는 근거다. 본 연구에서는 attention과 constrained trading을 직접 관찰할 수 있다는 차별점이 있다. 단, 실제 주가의 abnormal return은 결과변수로 사용하지 않음.

### Tetlock (2007)

- **Title**: [Giving Content to Investor Sentiment: The Role of Media in the Stock Market](https://doi.org/10.1111/j.1540-6261.2007.01232.x)
- **Authors**: Paul C. Tetlock
- **Venue / status**: Journal of Finance, 62(3), 1139–1168 — published journal article
- **Summary**: 언론의 비관적 언어가 투자자 sentiment와 거래·수익률 동학에 연결됨을 텍스트 분석으로 보인다. 뉴스 tone이 투자자의 정보 환경을 측정하는 중요한 변수임을 제시한다.
- **Plan에서의 역할**: bullish/bearish fake condition의 방향성을 사전 audit하고, agent의 표현된 stance와 주문 방향을 연결하는 근거다. polarity는 사후 LLM 판단에 맡기지 않고 독립 annotator 또는 검증된 classifier로 stimulus 단계에서 확인해야 한다.

### Engelberg and Parsons (2011)

- **Title**: [The Causal Impact of Media in Financial Markets](https://doi.org/10.1111/j.1540-6261.2010.01626.x)
- **Authors**: Joseph E. Engelberg, Christopher A. Parsons
- **Venue / status**: Journal of Finance, 66(1), 67–97 — published journal article
- **Summary**: 동일한 정보에 대한 지역 언론 노출 차이를 이용해 media coverage가 투자자의 거래에 미치는 인과적 영향을 분석한다. 정보 내용과 전달 채널을 구분하는 연구 설계가 핵심이다.
- **Plan에서의 역할**: 동일한 실제 뉴스·가격 경로를 유지하고 community와 fake article만 바꾸는 controlled information environment의 근거다. 다만 본 연구의 인과효과는 **simulation 안의 agent behavior에 대한 효과**이며, 실제 한국 투자자에 대한 causal effect로 일반화하지 않는다.

### Brown and Warner (1985)

- **Title**: [Using Daily Stock Returns: The Case of Event Studies](https://doi.org/10.1016/0304-405X(85)90042-X)
- **Authors**: Stephen J. Brown, Jerold B. Warner
- **Venue / status**: Journal of Financial Economics, 14(1), 3–31 — published journal article
- **Summary**: 일별 수익률 자료를 이용한 event-study의 통계적 성질과 abnormal return 검정 절차를 체계적으로 평가한다.
- **Plan에서의 역할**: 본 연구에서 직접 복제할 분석이라기보다 **경계 설정용 문헌**이다. 가격이 모든 condition에서 외생적으로 동일하므로 AR/CAR을 treatment outcome으로 계산하면 조건 간 차이가 구조적으로 생기지 않는다. 대신 D−2~D+2 event-time 정렬 원리만 차용해 belief·attention·signed trade의 동적 변화를 보여준다.

---

## 9. 허위 자극물과 생성 텍스트를 어떻게 감사할 것인가

**Plan에서의 역할:** RQ2·RQ3의 처치가 실제로 의도한 bullish/bearish misinformation인지 확인하고, agent가 원문을 어떻게 해석·변형했는지 보조적으로 분석한다. content-level deviation, expressed stance, confidence(별도 schema 또는 rubric을 추가한 경우), trading action은 서로 다른 층으로 유지한다.

### Liu et al. (2025) — FUSE

- **Title**: [The Stepwise Deception: Simulating the Evolution from True News to Fake News with LLM Agents](https://aclanthology.org/2025.emnlp-main.1330/)
- **Authors**: Yuhan Liu, Zirui Song, Juntian Zhang, Xiaoqing Zhang, Xiuying Chen, Rui Yan
- **Venue / status**: EMNLP 2025 — accepted main conference paper
- **Summary**: true news가 partially false, fake news로 단계적으로 변형되는 과정을 spreader, commentator, verifier, stander 역할과 memory 구조를 가진 agent simulation으로 모델링한다. FUSE-EVAL은 original text와 evolved text의 내용·표현 이탈을 여러 차원으로 평가한다.
- **Plan에서의 역할**:
  - **그대로 차용 가능한 원리**: 원 factual anchor와 주입 article, 그리고 주입 article와 agent-generated interpretation/post 사이의 변형을 다차원 rubric으로 분리해 측정한다.
  - **수정 차용 가능한 분석**: 본 연구에 중요한 `Sentiment Shift`, `New Information Introduced`, `Certainty Shift`, `Perspective/Paraphrasing Deviation`을 우선 사용하고, 한국어 금융 문장에 맞춘 예시와 anchor를 새로 만든다. 즉 rubric을 우리가 설계해야함
  - **현재 구조에서 불가능한 주장**: FUSE는 내생적 stepwise content evolution을 연구하지만 본 연구의 fake article은 외생적으로 주입된다. 따라서 content evolution 속도, cascade, spreader role 효과를 그대로 보고하지 않는다. FUSE-EVAL은 agent belief나 confidence 측정기가 아니다.

#### FUSE-EVAL을 본 연구에 차용하는 방법

FUSE-EVAL은 원문과 변형문 사이의 `Sentiment Shift (SS)`, `New Information Introduced (NII)`, `Certainty Shift (CS)`, `Stylistic Shift (STS)`, `Temporal Shift (TS)`, `PD`를 평가하고, 그 평균을 Total Deviation으로 요약한다. 원문은 PD를 core-dimension 목록과 appendix에서는 `Perspective Deviation`, 세부 설명에서는 `Paraphrasing Degree`로 다르게 표기하며, 본문은 1–10 scale을 쓰지만 appendix prompt에는 0–10을 지시한다. 따라서 본 연구에서는 PD의 의미, scale, anchor를 자체 rubric에서 명시적으로 고정해야 한다.

본 연구에서는 FUSE-EVAL 전체를 동일하게 복제하지 않고 다음 두 곳에 차용한다.

1. **Stimulus audit**: factual anchor와 bullish/bearish fake article을 비교한다. SS는 방향성, NII는 새 주장 수, CS는 단정성 변화, PD는 관점 또는 의미 변화로 사용한다. NII가 높다고 자동으로 false인 것은 아니므로 각 atomic claim의 근거 유무를 별도로 확인한다.
2. **Reception/deviation audit**: fake article과 `belief_summary`, 뉴스 해석, community post를 비교해 agent가 내용을 강화·완화·반박했는지 본다. 이 점수는 expressed stance와 trading outcome을 설명하는 보조변수로만 쓴다.

권장 rubric은 0–4 ordinal scale이다. `0=변화 없음`, `1=미미`, `2=부분적`, `3=명확`, `4=강한 변화`처럼 anchor를 고정하고, 조건을 모르는 두 명의 annotator가 표본을 blind coding한다. FUSE 원문도 50개 뉴스에 대해 3명의 human annotator와 LLM judge를 비교하고 Fleiss' κ 및 차원별 Pearson correlation을 보고했다. 본 연구도 weighted Cohen's κ 또는 ICC와 차원별 분포를 먼저 확인한 뒤 LLM-as-a-judge를 전체 자료에 적용한다. CS는 문장의 단정성이다. 별도 self-reported confidence를 쓰려면 belief 또는 news-interpretation schema에 새 field를 추가해 수집해야 하며, 현재 belief schema에는 구조화된 confidence field가 없다.

### Zhang, Du, and Zhang (2022)

- **Title**: [A Theory-Driven Machine Learning System for Financial Disinformation Detection](https://doi.org/10.1111/poms.13743)
- **Authors**: Xiaohui Zhang, Qianzhou Du, Zhongju Zhang
- **Venue / status**: Production and Operations Management, 31(8), 3160–3179 — published journal article
- **Summary**: 금융 disinformation의 내용·언어적 특성을 이론적으로 구조화하고 machine-learning detection으로 연결한다. 금융 문맥에서 허위정보를 일반 fake-news dataset과 동일하게 처리하면 안 된다는 점을 보여준다.
- **Plan에서의 역할**: synthetic article의 금융적 타당성, source impersonation, 과장·누락·근거 없는 수치, 방향성을 점검하는 stimulus audit 근거다. bullish와 bearish article은 동일 event anchor, 길이, 형식, source style, 정보량을 맞추고 방향성만 달라지도록 설계한다.

### Min et al. (2023) — FActScore

- **Title**: [FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation](https://aclanthology.org/2023.emnlp-main.741/)
- **Authors**: Sewon Min, Kalpesh Krishna, Xinxi Lyu, Mike Lewis, Wen-tau Yih, Pang Wei Koh, Mohit Iyyer, Luke Zettlemoyer, Hannaneh Hajishirzi
- **Venue / status**: EMNLP 2023 — accepted main conference paper
- **Summary**: 긴 생성문을 atomic fact로 분해하고, 신뢰 가능한 source에서 각 fact가 지지되는지를 평가해 factual precision을 계산한다.
- **Plan에서의 역할**: 기사와 community post를 claim 단위로 분해해 source evidence를 연결하는 원리를 차용한다. 전체 FActScore pipeline을 그대로 적용할 필요는 없지만 `claim_id`, `source span`, `supported/unsupported/contradicted` ledger를 만들면 FUSE의 NII를 실제 허위 claim과 구분할 수 있다. 이는 treatment manipulation check와 정성 사례 선정에 사용한다.

---

## Summary Table

| Paper | Venue / status | Plan 카테고리 | Plan에서의 역할 |
|-------|----------------|--------------|----------------|
| Yang et al. (2025) — TwinMarket | NeurIPS 2025 | 검증 대상 | 가격·거시 검증과 본 연구의 일별 개인 순거래 방향 검증을 구분하는 직접 비교 대상 |
| Cont (2001) | Quantitative Finance | 검증 대상 | stylized fact가 무엇을 요약하는지 정의하고, 개별 매수·매도 방향 분석의 필요성을 제시 |
| Windrum et al. (2007) | JASSS | 검증 방법론 | 입력·미시 규칙·거시 결과를 구분하는 다층 ABM validation framework |
| Li et al. (2026) — Behavioral Consistency Validation | arXiv preprint | 정렬 계측 | 프롬프트로 심은 페르소나·특성이 부분적으로만 발현됨을 코호트 비교로 보인 선례, E2 조작점검의 직접 근거 |
| Li et al. (2024) — EconAgent | ACL 2024 | 정렬 계측 | 규칙 기반 대비 필립스곡선·오쿤법칙 재현, 에이전트별 회귀·ablation·시드 반복 관행의 선례(E3·E6) |
| Henning et al. (2025) | arXiv preprint | 정렬 계측 | LLM이 인간의 버블·행동적 편차를 재현하지 못하고 교과서적 합리성에 수렴한다는 "too rational" 근거, E1 정렬 계측의 필요성 |
| Choe et al. (1999) | Journal of Financial Economics | 행동 정합성 | 한국 시장 가격 형성에 개인 외 여러 투자자 유형이 관여한다는 맥락 |
| Boehmer et al. (2021) | Journal of Finance | 행동 정합성 | retail buy/sell imbalance를 집계 행동 benchmark로 다루는 선례 |
| Argyle et al. (2023) | Political Analysis | Persona | persona-conditioned simulation의 가능성과 외적 일반화의 경계 |
| Ma et al. (2024) — SMISTS | Findings of ACL 2024 | Persona·허위정보 | belief, sharing, trading을 분리하고 persona 차이를 해석하는 근거 |
| Park et al. (2023) | UIST 2023 | LLM 사회 시뮬레이션 | memory·reflection 구조와 PM community 이후 시간 순서의 선례 |
| Zhou et al. (2024) — Real Life | EMNLP 2024 | LLM 사회 시뮬레이션 | 정보 누출과 외적 타당성 과장에 대한 경고 |
| Zhou et al. (2024) — SOTOPIA | ICLR 2024 | LLM 사회 시뮬레이션 | 커뮤니티 글의 정보공유·동조·반박 등 기능 rubric 설계의 선례 |
| Mou et al. (2024) — From Individual to Society | ACM Computing Surveys (2025) | 분류 틀 | Related Work 도입부에서 개인/시나리오/사회 시뮬레이션을 나누는 분류 틀 |
| Zhang et al. (2024) — StockAgent | ACM TIST (accepted) | 금융 LLM 시뮬레이션 지형 | 조건별 행동 비교는 있으나 실제 투자자 정렬 검증·허위정보 처치는 없음, 백엔드 간 행동 차이는 단일 백엔드 한계(E6)의 인용 근거 |
| Horton (2023) — Homo Silicus | NBER Working Paper | 금융 LLM 시뮬레이션 지형 | "에이전트로 인간 대상 불가능한 실험을 대행한다"는 프레임의 이론적 근거, Henning et al.과 균형 인용 |
| Yang et al. (2024) — OASIS | arXiv preprint | 사회·허위정보 시뮬레이션 지형 | 정보 확산·양극화는 재현하나 거래 행동이 없어 "확산 연구와 거래 연구 사이의 빈자리"를 세우는 대비 축 |
| Gao et al. (2023) — S3 | arXiv preprint | 사회·허위정보 시뮬레이션 지형 | 실제 소셜 네트워크의 감정·태도 재현 지형 각주, 현재 결과의 직접 근거로는 미사용 |
| Liu et al. (2025) — MOSAIC | EMNLP 2025 | Community·허위정보 | expressed belief/action 분리, fake별 rubric·embedding 반응 분석의 출발점 |
| Vosoughi et al. (2018) | Science | Community·허위정보 | 허위정보의 사회적 확산 위험과 향후 network 분석의 배경 |
| Pennycook et al. (2021) | Nature | Community·허위정보 | attention, belief, sharing/action을 구분하는 근거 |
| Clarke et al. (2021) | Information Systems Research | 금융 뉴스·행동 | financial fake news의 attention → reaction 경로 설계 근거 |
| Tetlock (2007) | Journal of Finance | 금융 뉴스·행동 | 뉴스 tone, 투자자 sentiment, 거래의 연결 및 polarity audit 근거 |
| Engelberg & Parsons (2011) | Journal of Finance | 금융 뉴스·행동 | 정보 내용과 전달 채널을 분리하는 controlled information-environment 설계 선례 |
| Brown & Warner (1985) | Journal of Financial Economics | 금융 뉴스·행동 | 가격 결과 대신 D−2~D+2 event-time 정렬만 차용하는 경계 설정 |
| Liu et al. (2025) — FUSE | EMNLP 2025 | 자극물·텍스트 감사 | factual anchor–fake–agent text 사이 변형을 보는 수정 rubric의 출발점 |
| Zhang, Du, & Zhang (2022) | Production and Operations Management | 자극물·텍스트 감사 | 금융 disinformation의 방향성·근거·형식을 사전 audit하는 근거 |
| Min et al. (2023) — FActScore | EMNLP 2023 | 자극물·텍스트 감사 | claim 단위 evidence ledger로 fake claim과 생성문을 연결하는 원리 |

표에는 각 절의 독립된 `###` 논문만 포함했다. ASFM, Agent Market Arena, Shachi, Lopez-Lira(5절), AgentSociety, CRSEC, Project Sid(6절)는 "기타(한 줄 정리)" 지형 각주로만 인용되므로 표에서 제외했다.

---
