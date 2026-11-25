# 데모 전용 AI 자율 매매 실험 모드 — AI가 스스로 진입·홀딩·청산까지

`backend/demo_ai_experiment.py`는 **바이비트 Demo Trading(모의 USDT, 실돈 아님)** 계좌에서
AI가 차트를 읽고 **스스로 판단**해 진입·유지·조기 청산까지 해 보는 실험 모드입니다.
기존 `testnet_auto`(후보1 규칙 + AI 심사)와 달니 **기계 규칙 없이 AI만** 방향을 정합니다.

> **실돈은 움직이지 않습니다.** 주소가 `api-demo.bybit.com`(데모) 또는
> `api-testnet.bybit.com`(테스트넷) 두 곳으로만 고정되어 있고, 메인넷 클라이언트는 만들 때부터 거부합니다.

## 1. 집 PC에서 실행 (한 번만 준비)

1. 폴더를 최신으로:

   ```powershell
   cd C:\Users\<이름>\pixel-trading-floor
   git pull
   ```

2. `backend\.env`에 **Demo Trading API 키**가 있는지 확인(11-23에 넣은 그 키):

   ```
   BYBIT_API_KEY=발급받은_데모_키
   BYBIT_API_SECRET=발급받은_데모_시크릿
   ```

   - 키 권한은 **읽기·쓰기 + USDT 무기한 선물**만. **출금 권한은 끄기.**
   - `GEMINI_API_KEY`가 있으면 Gemini가 판단합니다. 없으면 **HOLD만 하며 사유를 남깁니다**(주문 없음).

3. **`run_demo_ai_experiment.bat` 더블클릭.** (첫 실행은 `python -m backend.demo_ai_experiment --loop`)

   - 15분 봉이 마감되고 60초 뒤마다 판단하고, 대기 중 **1분마다 상태 한 줄**을 찍습니다.
   - 키가 없으면 자동으로 **mock 모드** — 실제 주문 없이 흐름만 확인합니다.
   - `run_demo_ai_experiment.bat --mock`처럼 뒤에 옵션을 붙일 수 있습니다.

## 2. 무엇을 보는가

- **미보유**: OKX/Bitget 공개 15분봉·4시간봉 → EMA20/50 · ATR14 · RSI14 · CCI20 · 볼린저 %B ·
  최근 20봉 스윙 고저(BSL/SSL) · 직전 12봉 흐름을 AI에게 주고 `LONG`/`SHORT`/`HOLD`를 받습니다.
  `LONG`/`SHORT`면 AI가 고른 `stop_atr`(1.5~3.0)·`target_r`(1.5~3.0)로 **즉시 시장가 진입 + TP/SL**.
- **보유 중**: 거래소 포지션의 평가손익(R)·보유 봉 수·차트 흐름을 보여 주고 `HOLD`/`CLOSE`를 받습니다.
  `CLOSE`면 시장가 청산. 손절·목표는 거래소에 걸려 있으므로 AI가 조용해도 위험은 제한됩니다.
- **안전장치**: 기준 시드 50 USDT · 1회 손실 3% · 레버리지 ≤ 3배 · **하루 최대 4회 진입** ·
  **일일 손실 10% 킬스위치** · 최대 96봉(15분봉 24시간) 시간 청산 · 청산한 봉 재진입 금지 ·
  메인넷 차단 · `--stop`/`--resume` 수동 정지.

## 3. 명령

```powershell
python -m backend.demo_ai_experiment --once            # 1회 판단
python -m backend.demo_ai_experiment --loop            # 계속(15분 봉 마감 +60초)
python -m backend.demo_ai_experiment --status          # 계좌·포지션·최근 AI 판단·성적
python -m backend.demo_ai_experiment --once --mock     # 드라이런(주문 없음)
python -m backend.demo_ai_experiment --reset           # 기록 초기화(열린 포지션 있으면 거부)
python -m backend.demo_ai_experiment --stop / --resume  # 신규 진입 정지 / 해제
python -m backend.demo_ai_experiment --interval 30     # 30분봉으로 판단
python -m backend.demo_ai_experiment --testnet         # 데모 대신 테스트넷 계좌
```

## 4. 파일 (모두 `data/` 아래, git 제외)

| 파일 | 내용 |
|---|---|
| `demo_ai_experiment_state.json` | 포지션 미러·청산 기록·통계·최근 AI 판단·오늘 진입 수 |
| `demo_ai_experiment_log.jsonl` | 스캔마다 한 줄(판단·사유·문맥·주문·킬스위치) |
| `demo_ai_experiment_killswitch.json` | 일일 손실·거래 수 킬스위치 상태 |
| `demo_ai_experiment_state*.json.lock` | 동시 실행 방지 |
| `demo_ai_experiment_state*_pause.txt` | `--stop` 표시 |

mock으로 돌리면 이름에 `.mock`이 붙어 데모 계좌 기록과 섞이지 않습니다.
**`paper_*`(서버 모의매매)·`testnet_auto_*`(기존 오토 모드) 파일은 읽지도 쓰지도 않습니다.**

## 5. 알아 둘 것

- Demo Trading은 `/v5/position/closed-pnl` 조회가 막힐 수 있습니다. 그때는 확정 봉의 고가·저가로
  손절/목표를 추정해 `pnl_source: bar_simulation`으로 남기고, 그것도 안 되면 `손익 미확인`으로 둡니다.
- 무료 Gemini 한도에 걸리면 그 스캔은 **HOLD(관망)** 로 남고 다음 봉에서 다시 시도합니다.
- 이 모드는 **실험용**입니다. 실제 돈으로 옮기기 전에 데모에서 최소 며칠의 판단·손익을 쌓아 보세요.
