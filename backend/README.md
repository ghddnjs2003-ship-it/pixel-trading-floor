# 서버 백엔드 — AI 두뇌 · v7 룰 엔진 · 백테스트 · 킬스위치

브라우저 데모는 그대로 두고, **실전 매매 직전에 필요한 서버 쪽 부품**을 여기 모았다.
키는 모두 이 디렉터리의 `.env`에만 둔다(브라우저로 내려가지 않는다. `.env`는 git 무시).

```
backend/
  config.py        환경변수 → 제공자/전략/킬스위치 설정
  providers.py     OpenAI 호환 어댑터 (Groq·Gemini·OpenRouter·Cerebras·Ollama·OpenAI·xAI 공용)
  orchestrator.py  13인 분석 흐름 + v7 규칙 문맥 + 킬스위치 게이트
  server.py        정적 파일 서버 + /api/analyze · /api/stream(SSE)
  strategy.py      더블 S + MDS v7 룰 엔진 (파인스크립트 v7의 판정 부분 이식)
  backtest.py      과거 캔들 백테스트 (수수료·슬리피지·손절 우선 체결)
  risk.py          킬스위치 (일일 손실·연속 손절·과매매·급변·수동 정지)
  test_*.py        단위 테스트 32개 (python3 -m unittest discover backend)
```

## 1. AI 두뇌 — 무료 API 먼저, 로컬 Ollama는 나중에

`LLM_PROVIDER` 한 줄로 갈아 끼운다. 모두 OpenAI 형식(`/chat/completions`)을 쓰므로 코드는 그대로다.

| 제공자 | 필요한 키 | 무료 여부 | 기본 모델 |
|---|---|---|---|
| `groq` | `GROQ_API_KEY` | 무료 한도 | `llama-3.3-70b-versatile` |
| `gemini` | `GEMINI_API_KEY` | 무료 한도 | `gemini-2.0-flash` |
| `cerebras` | `CEREBRAS_API_KEY` | 무료 한도 | `llama-3.3-70b` |
| `openrouter` | `OPENROUTER_API_KEY` | `:free` 모델 | `meta-llama/llama-3.3-70b-instruct:free` |
| `ollama` | **없음** | 로컬 GPU | `qwen2.5:7b-instruct` |
| `openai` / `xai` | 각각의 키 | 유료 | `gpt-4.1-mini` / `grok-3-mini` |

- `LLM_PROVIDER=auto`면 무료 제공자 → 유료 → Ollama 순으로 **키가 있는 첫 곳**을 쓴다.
- `LLM_MODEL`, `LLM_BASE_URL`로 모델·주소를 덮어쓸 수 있다(새 모델·자체 서버 대응).
- 어떤 제공자가 준비됐는지 확인: `python -m backend.providers`

```bash
cp backend/.env.example backend/.env      # 무료 키 하나만 채우면 된다
python -m backend.providers               # 키 유무 표
python -m backend.server                  # http://127.0.0.1:8765/index.html?engine=llm
```

13인 흐름은 호출 1회당 13번의 LLM 요청을 낸다. 무료 한도가 작은 제공자는 하루 몇 번이면
말라붙으니, `orchestrator`에 규칙 판정(v7)을 먼저 넣어 **AI가 볼 자료를 줄이고**,
실전 판단은 규칙 엔진이 하고 AI는 해설·예외 검토를 맡는 순서가 현실적이다(현재 코드가 그 구조).

## 2. v7 룰 엔진 (`strategy.py`)

트레이딩뷰 통합 지표 `indicators/double-s-mds-v7.pine`의 **판정 부분**을 파이썬으로 옮겼다.
라벨·박스 그리기는 옮기지 않았다(화면은 파인스크립트가 한다).

- 더블 S: BSL/SSL 스윕(0.05~1.60 ATR) → 종가 복귀(거부·복귀) → **직전 봉 구조 돌파(BOS)** 확인 → 다음 봉 시가 진입
- MDS M1: 확정 매물대 존(`zone_min_age`봉 전 프로파일) → 거래량 1.15배·몸통 50% 이상 돌파 → 첫 리테스트 지지/저항 + CCI(20, hlc3) + 볼린저 확장 + 리테스트 거래량 감소
- 손절: 스윕 극점·존 경계 밖 0.15 ATR / 목표: 반대편 유동성, 1.5R 미만이면 1.5R로 대체
- 신호는 i번 봉이 끝난 뒤 확정, 진입은 i+1 시가(미래 참조 없음 — 테스트로 고정)
- 값이 없으면 `side=None`. 화면·서버 모두 지어내지 않는다.

## 3. 백테스트 (`backtest.py`)

```bash
python -m backend.backtest --symbol BTCUSDT --interval 15 --limit 1500 --source auto
python -m backend.backtest --csv data/btc15.csv --min-rr 2.0 --no-bos
```

- 데이터: 바이비트·OKX·비트겟·바이낸스 공개 API(키 불필요) 또는 CSV. `auto`면 되는 곳부터.
  서버가 있는 지역에 따라 바이비트·바이낸스가 403/451로 막힐 수 있어 OKX 폴백을 둔다.
- 체결: 다음 봉 시가, 손절·목표가 같은 봉에서 둘 다 닿으면 **손절 우선**(비관), 편도 수수료 5bp·슬리피지 2bp 기본.
- 출력: 체결 수·승률·평균 R·손익비(PF)·순손익·최대낙폭·최장 연속손실·종류별(더블S/M1).
- `--json-out`으로 전체 체결 내역을 저장해 복기에 쓸 수 있다.

**2026-11-19 기준 실제 측정 (BTCUSDT 15분봉 3,000봉 ≈ 31일, OKX 데이터, 계좌 10,000 USDT·거래당 1% 리스크)**

| 설정 | 체결 | 승률 | PF | 순손익 |
|---|---|---|---|---|
| v7 기본(BOS 포함, 1.5R) | 22 | 36.4% | 0.55 | −7.47% |
| 최소 손익비 2.0R | 20 | 30.0% | 0.51 | −8.24% |
| BOS 없이 | 37 | 32.4% | 0.77 | −7.24% |
| BOS 없이 + 2.0R | 31 | 32.3% | 0.82 | −4.95% |

→ **이 초안의 기계적 체결은 이 구간에서 마이너스다.** 표본도 작다(20~37건).
   최소 손익비 1.5R에서 손익분기 승률은 약 40%인데 승률이 그 아래다.
   실제 파인스크립트 라벨과 봉 단위로 대조해 규칙 이식이 맞는지 먼저 확인해야 하고,
   그 뒤에 손절 위치(스윕 극점 vs 존 안쪽)와 목표(반대편 유동성 vs 고정 R)를 따로 검증해야 한다.
   **이 결과를 근거로 실돈을 넣지 않는다.**

## 4. 킬스위치 (`risk.py`)

AI·규칙과 분리된 정지 장치. 주문은 내지 않고 "지금 진입해도 되나"만 답한다.

| 규칙 | 기본값 | 동작 |
|---|---|---|
| 일일 최대 손실 | 계좌의 3% | 당일 중단 |
| 연속 손절 | 3회 | 당일 중단(쿨다운 30분 뒤 재개) |
| 일일 거래 수 | 20회 | 당일 중단 |
| 급변 | ATR ≥ 가격의 2.5% | 30분 일시 중단 |
| 자본 하한 | 시작 자본의 90% | 사람 확인 전까지 중단 |
| 수동 정지 | — | 직접 풀기 전까지 진입 금지 |

- 상태는 `backend/state/killswitch.json`에 남고 KST 자정에 자동 초기화된다(수동 정지는 유지).
- `orchestrator`는 최종 판정 직전에 이 게이트를 본다 — 막혀 있으면 AI가 매수를 골라도 HOLD로 바꾸고 이유를 남긴다.
- 환경변수: `KILL_DAILY_LOSS_PCT`, `KILL_MAX_CONSECUTIVE_LOSSES`, `KILL_MAX_DAILY_TRADES`,
  `KILL_VOLATILITY_ATR_PCT`, `KILL_COOLDOWN_MINUTES`

## 5. 아직 없는 것 (일부러 만들지 않음)

- **거래소 주문 전송**: 실돈이 나가는 일이라 사용자 결정 없이 붙이지 않는다. 붙일 때는
  API 키 권한을 출금 불가·선물 거래만으로 좁히고, `risk.py` 게이트를 반드시 앞에 둔다.
- 실시간 루프(24시간 구동): 지금은 백테스트·판정까지. 루프를 돌리려면 PC 상시 구동과
  거래소 키가 먼저 필요하다.
- 백테스트의 봉 단위 라벨 대조(파인스크립트 화면 vs 엔진): 다음 단계.

## 6. 테스트

```bash
python3 -m unittest discover -s backend -t . -p "test_*.py"
```

전략(10) · 백테스트(7) · 킬스위치(11) · 설정(2) · 오케스트레이터(2) = 32개.
