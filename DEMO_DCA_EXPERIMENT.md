# 데모 전용 리치식 1~4차 물타기(DCA) 실험 모드

`backend/demo_dca_experiment.py`는 **바이비트 Demo Trading(모의 USDT, 실돈 아님)** 계좌에서
리치 트레이딩 컨트롤러(`RICHTrading/richtrading.github.io` 커밋 `b91184e`의 `ctl/g/config.js` →
`dcaRules`·`followRules`)의 **1~4차 물타기 규칙을 그대로** 돌려 보는 실험 모드입니다.

> **실돈은 움직이지 않습니다.** 주소는 `api-demo.bybit.com` 하나로 고정되어 있고, 테스트넷·메인넷
> 클라이언트는 만들 때부터 거부합니다. 기존 안전 전략(`paper_loop`, `testnet_auto`, `demo_ai_experiment`)과
> 기록·파일·잠금이 모두 분리되어 있습니다.

## 1. 규칙 (원본 표 그대로)

선은 모두 **증거금 기준 %**(`basis: margin`)이고, 차수마다 **그 순간 평단**에서 다시 잽니다(`avgReset: true`).
5분봉 지표는 **완성봉**만 씁니다(`bars.closedOnly: true`).

| 단계 | 발동 조건 | 추가 증거금 |
|---|---|---|
| 1차 진입 | Gemini가 5분봉+4시간봉으로 방향 판단(키 없으면 기술 필터) | 가용 잔고의 **13%** |
| 익절 | 평단 대비 **증거금 +10%** = 가격 ±10/레버리지 % (50배면 0.20%) | 전량 청산 |
| 2차 | 증거금 **-25%** + 5분 **RSI(14)** 롱 <35 / 숏 >65 · 강제선 **-40%** | 가용 잔고의 **15%** |
| 3차 | 증거금 **-40%** + 5분 **볼린저(20, 2.5σ)** 이탈 · 강제선 **-60%** | **33%** |
| 4차 | 증거금 **-60%** + 볼린저 이탈 · 강제선 **-80%** | 남은 가용의 **100%** (사용 가능의 95% 상한) |
| 자동 손절 | **4차 진입 뒤에만** — 평단→추정 청산가 거리의 **90%** 지점에 reduce-only `STOP` | 1~3차에는 손절 없음 |

- 레버리지 기본 **50배**(1~50 조정). 1차 방향이 틀리면 4차까지 증거금을 더 넣는 구조라
  **50달러 시드에서는 4차에 계좌의 약 97%가 증거금으로 들어갑니다.** 손실도 그만큼 커집니다.
- 익절가는 차수가 바뀔 때마다 새 평단에서 다시 계산해 거래소 TP를 갱신합니다.
- 원본과 똑같이 맞추려면 `--reserve-lev-pct 0.48 --sl-est-dist-pct 80`을 붙입니다(아래 4절).

## 2. 집 PC에서 실행

1. 폴더를 최신으로:

   ```powershell
   cd C:\Users\<이름>\pixel-trading-floor
   git pull origin main
   ```

2. `backend\.env`에 **Demo Trading API 키**가 있는지 확인(11-23에 넣은 그 키):

   ```
   BYBIT_API_KEY=발급받은_데모_키
   BYBIT_API_SECRET=발급받은_데모_시크릿
   ```

   - 키 권한은 **읽기·쓰기 + USDT 무기한 선물**만. **출금 권한은 끄기.**
   - `GEMINI_API_KEY`가 있으면 1차 방향을 Gemini가 고릅니다. 없으면 기술 필터가 대신 고릅니다.

3. **`run_demo_dca_experiment.bat` 더블클릭.**
   - 30초마다 물타기 선·청산을 점검하고, 1분마다 상태 한 줄을 찍습니다.
   - 1차 진입은 **5분봉이 마감될 때만** 새로 판단합니다.
   - 키가 없으면 자동으로 **mock 모드** — 실제 주문 없이 흐름만 확인합니다.
   - `run_demo_dca_experiment.bat --mock`처럼 뒤에 옵션을 붙일 수 있습니다.

> **같은 데모 계좌에서 `run_testnet_auto.bat`·`run_demo_ai_experiment.bat`을 동시에 켜지 마세요.**
> 한 계좌에 두 엔진이 주문하면 서로의 포지션을 건드립니다. 이 모듈은 다른 엔진의 잠금 파일이
> 살아 있으면 실행을 거부합니다(확인 후 그 창을 끄고 다시 실행).

## 3. 명령

```powershell
python -m backend.demo_dca_experiment --once              # 1회 점검
python -m backend.demo_dca_experiment --loop              # 30초 점검 · 1분 상태 줄
python -m backend.demo_dca_experiment --status            # 계좌·포지션·차수·성적
python -m backend.demo_dca_experiment --once --mock       # 드라이런(주문 없음)
python -m backend.demo_dca_experiment --once --mock --force-entry long   # 1차 방향 고정(시험)
python -m backend.demo_dca_experiment --reset             # 기록 초기화(열린 포지션 있으면 거부)
python -m backend.demo_dca_experiment --stop / --resume    # 신규 매매 정지 / 해제
python -m backend.demo_dca_experiment --symbol SOLUSDT --leverage 25
python -m backend.demo_dca_experiment --first-pct 10 --tp-pct 15
```

자주 쓰는 옵션

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--symbol` | `ETHUSDT` | `SOLUSDT`·`BTCUSDT` 지원. BTC는 50달러·13%로 최소 수량이 안 될 수 있음 |
| `--leverage` | `50` | 1~50배 |
| `--first-pct` | `13` | 1차 증거금 비중(1~30%) |
| `--tp-pct` | `10` | 익절(증거금 %, 5~50) |
| `--entry-mode` | `auto` | `auto`(AI→기술) / `ai` / `tech` |
| `--sl-mode` | `auto` | `auto`(청산가 90%) / `pct`(증거금 %) / `off` |
| `--reserve-lev-pct` | 없음 | 원본 그대로 `0.48` — 4차에 배율×0.48만큼 증거금을 남김(50배면 76%) |
| `--max-trades` | `6` | 하루 매매(1~4차 한 묶음) 수 |
| `--daily-loss-pct` | `25` | 일일 손실 킬스위치 |
| `--source` | `auto` | 5분봉 출처(bybit→okx→bitget) |
| `--scan-sec` | `30` | 물타기 선 점검 간격(초) |

회원이 바꾸는 값은 `backend\.env`로도 고정할 수 있습니다(CLI가 우선):
`DCA_SYMBOL=SOLUSDT` · `DCA_LEVERAGE=25` · `DCA_FIRST_PCT=10` · `DCA_TP_PCT=15`.
바이비트 환경은 `BYBIT_ENV=demo`(또는 `BYBIT_DEMO=true`)를 쓰세요.

## 4. 원본과 다른 두 가지 (의도한 기본값)

1. **4차 비중** — 원본 `dcaRules`의 4차에는 `reserveLevPct: 0.48`이 있어 50배에서 실제로는
   `min(95, 100 − 50×0.48) = 76%`만 넣습니다. 이 실험의 기본은 **100%(사용 가능의 95% 상한)**로,
   요청서의 "4차 가용 잔고 100%"를 따랐습니다. 원본과 같게 하려면 `--reserve-lev-pct 0.48`.
2. **추정 청산가일 때 자동 손절 거리** — 원본은 거래소 청산가면 90%, 추정값이면 80%를 씁니다.
   이 실험의 기본은 둘 다 **90%**(요청서 기준)입니다. 원본과 같게 하려면 `--sl-est-dist-pct 80`.

## 5. 파일 (모두 `data/` 아래, git 제외)

| 파일 | 내용 |
|---|---|
| `demo_dca_state.json` | 포지션 미러(차수·평단·수량·증거금·TP/SL)·청산 기록·통계·최근 판단 |
| `demo_dca_log.jsonl` | 행동마다 한 줄(진입·물타기·청산·거부) + 대기 10분 하트비트 |
| `demo_dca_killswitch.json` | 일일 손실·연속 손절·거래 수 킬스위치 |
| `demo_dca_state*.json.lock` | 동시 실행 방지 |
| `demo_dca_state*_pause.txt` | `--stop` 표시 |

mock으로 돌리면 이름에 `.mock`이 붙어 데모 계좌 기록과 섞이지 않습니다.
**`paper_*`(서버 모의매매)·`testnet_auto_*`(오토 모드)·`demo_ai_experiment_*`(AI 실험) 파일은
읽지도 쓰지도 않습니다** — 경로가 그 이름을 가리키면 만들 때부터 거부합니다.

## 6. 알아 둘 것

- **포지션 모드**: 바이비트 데모 계좌가 **원웨이(단방향)** 여야 합니다. 헤지 모드면 `positionIdx=0`
  주문이 거부됩니다(오류 메시지에 코드가 남습니다).
- **최소 주문 수량**: 50달러·13%·50배 = 명목 325달러. ETH/SOL은 4분할이 들어가지만 BTC는
  최소 수량 미달로 1차가 거부될 수 있습니다(사유에 필요한 수량·금액을 남깁니다).
- **5분봉 출처**: 바이비트 공개 캔들이 지역 차단(403)이면 OKX → Bitget으로 자동으로 넘어갑니다.
  주문은 언제나 바이비트 데모로만 나갑니다.
- **자동 손절이 즉시 지나간 경우**: 4차 체결가가 이미 손절 자리(평단→청산가 90%)를 지나 있으면
  조건 주문을 걸 수 없으므로 다음 점검(30초)에서 시장가로 정리합니다.
- **시간 청산**: 5분봉 288개(24시간)를 넘기면 시장가로 정리합니다(`--max-hold-bars`).
- **청산 손익**: 데모 계좌는 `/v5/position/closed-pnl`이 막힐 수 있습니다. 그때는 마지막 가격으로
  계산해 `pnl_source: estimate_price`로 남기고, 못 구하면 `손익 미확인`으로 둡니다.
- 이 모드는 **실험용**입니다. 물타기는 승률은 높아 보여도 한 방향 추세에서 크게 맞는 구조이므로,
  데모에서 최소 여러 사이클(특히 4차·손절 사이클)을 쌓아 보고 판단하세요.
