# 테스트넷·데모 오토 모드 — 퇴근 후 집 PC에서 50달러로 돌려 보기

`backend/testnet_auto.py`가 30분봉 후보1(M1)을 찾고 Gemini가 PASS를 주면, **바이비트 테스트넷**
또는 **Demo Trading**에 모의 50 USDT로 시장가 주문(손절·목표 동봉)을 냅니다. 실돈은 움직이지 않습니다 —
주소가 `api-testnet.bybit.com`(테스트넷)과 `api-demo.bybit.com`(데모) 두 곳으로만 고정되어 있고,
메인넷 주소·`BYBIT_TESTNET=false`는 코드가 거부합니다.

> **한국 IP에서는 테스트넷 키 발급이 막혀(오류 10024)** Demo Trading 키를 쓸 수 있습니다. → 아래 1-1절.

## 1. 준비 (한 번만)

1. 집 PC에서 이 폴더를 최신으로:

   ```powershell
   cd C:\Users\<이름>\pixel-trading-floor
   git pull
   ```

2. `backend\.env`를 열어 아래 두 줄을 채웁니다(11-22에 발급한 테스트넷 키).

   ```
   BYBIT_API_KEY=발급받은_키
   BYBIT_API_SECRET=발급받은_시크릿
   BYBIT_TESTNET=true
   ```

   - 키 권한은 **읽기·쓰기(Read-Write) + USDT 무기한 선물(Contract)** 만. **출금 권한은 끄기.**
   - Gemini 키(`GEMINI_API_KEY`)는 이미 들어 있으니 그대로 두면 AI 심사가 함께 돕니다.
   - 키가 비어 있으면 **mock 모드**로 돌아 실제 주문이 나가지 않습니다(드라이런).

### 1-1. 테스트넷 키가 안 만들어질 때 — Demo Trading으로

한국 IP에서는 `testnet.bybit.com` 키 생성이 Bybit 규제(오류 `10024`)로 막힙니다. 이때는
[bybit.com](https://www.bybit.com)의 **Demo Trading**(모의투자) 계정에서 API 키를 발급받아 씀니다.
Demo Trading도 실돈 없는 모의 USDT만 움직이고, V5 REST 경로·서명 방식이 테스트넷과 같습니다(주소만 다름).

1. 바이비트 로그인 → 우상단 **Demo Trading**으로 전환 → 계정 설정에서 **API 키** 생성(모의투자 키).
2. `backend\.env`에 키를 넣고 **테스트넷 줄은 그대로 둔 채 한 줄만 더** 켭니다.

   ```
   BYBIT_API_KEY=데모_키
   BYBIT_API_SECRET=데모_시크릿
   BYBIT_TESTNET=true      # 그대로 둡니다(데모가 이깁니다)
   BYBIT_DEMO=true         # ← 이것만 추가 (또는 BYBIT_ENV=demo)
   ```

3. 잘 붙었는지 확인: `python -m backend.testnet_auto --status` → 첫 줄이
   `모드 demo(데모 트레이딩) · BTCUSDT · 주소 https://api-demo.bybit.com`이고 잔고가 보이면 끝입니다.
   루프를 돌릴 때는 `--demo`를 붙여도 같은 결과입니다(`python -m backend.testnet_auto --loop --demo`).

- 환경을 고르는 순서: **`--demo`/`env` 인자 > `BYBIT_DEMO` > `BYBIT_ENV` > `BYBIT_TESTNET` > 기본(테스트넷)**.
  `BYBIT_DEMO=true`와 `BYBIT_TESTNET=true`가 둘 다 켜져 있으면 **데모가 이깁니다**.
- `BYBIT_DEMO=false` + `BYBIT_TESTNET=false`처럼 둘 다 끄면 남는 것은 **실전 메인넷뿐이라 거부**합니다.
- 데모 계좌는 테스트넷과 **다른 계좌**이다 보니 상태 파일도 `.demo`가 붙어 따로 쌓입니다(5절).
- 데모 계정은 청산 손익(`/v5/position/closed-pnl`) 같은 **부가 조회가 제한**될 수 있습니다. 그때 청산은
  그대로 기록되고 손익만 '미확인'으로 남으며, 주문·포지션 관리(잔고·포지션·주문)는 계속 돕니다.

## 2. 세 번 눌러 보기 (더블클릭)

| 파일 | 하는 일 |
| --- | --- |
| `run_testnet_auto_mock.bat` | **드라이런** — 키 없이 1회 스캔. "무엇을 할지"만 보여 주고 주문 없음 |
| `run_testnet_auto_status.bat` | 계좌·포지션·킬스위치·최근 스캔 확인 |
| `run_testnet_auto.bat` | **실테스트넷·데모 루프** — 30분 봉 마감 +90초 점검 · 1분마다 상태 한 줄 · 상황 맞춤 감시(5분) (Ctrl+C로 종료) |

명령어로 직접 돌릴 때:

```powershell
python -m backend.testnet_auto --once --mock     # 1회 드라이런(주문 없음)
python -m backend.testnet_auto --once            # 1회 실제 점검(테스트넷)
python -m backend.testnet_auto --once --demo     # 1회 실제 점검(데모 트레이딩)
python -m backend.testnet_auto --loop            # 계속 자동(1분마다 상태 한 줄)
python -m backend.testnet_auto --loop --demo     # 데모로 계속 자동
python -m backend.testnet_auto --loop --adaptive # 상황 맞춤 감시(집중 5분 / 일반 30분)
python -m backend.testnet_auto --loop --no-adaptive  # 30분 고정
python -m backend.testnet_auto --status          # 상태
python -m backend.testnet_auto --stop            # 신규 진입만 정지(포지션 TP/SL은 유지)
python -m backend.testnet_auto --resume          # 다시 진입 허용
python -m backend.testnet_auto --reset           # 로컬 기록 초기화(열린 포지션 있으면 거부)
```

- `--stop`은 **돌고 있는 루프에도 바로** 먹습니다. 정지 표시 파일(`data/testnet_auto_pause*.txt`)을 쓰고,
  `--resume`이 지웁니다. (킬스위치 메모리 상태만 바꾸면 다음 저장에 덮여 사라지기 때문입니다.)
- 같은 상태 파일로 **창을 두 개 띄우면** 잠금(`*.lock`)이 두 번째를 멈춥니다 — 같은 봉에 주문이 두 번 나가는 일을 막습니다.

### 2-1. 1분 상태 줄 + 상황 맞춤 감시(Adaptive)

**① 1분마다 화면이 움직입니다.** 루프는 30분(또는 5분)을 한 번에 자지 않고, 1분마다 한 줄을 찍습니다.
창이 멈춘 것처럼 보이지 않고, `Ctrl+C`는 1초 안에 먹습니다(열린 포지션은 거래소 TP/SL이 계속 관리).

```
[testnet-auto] 21:31:30 KST · BTCUSDT 61,402.10 · 포지션 없음 · 감시 일반 30분 · 다음 점검 22:01:30 (30분 00초 남음)
[testnet-auto] 21:36:30 KST · BTCUSDT 61,388.40 · 보유 롱 0.001 @ 61,150.00 · 평가 +0.24 USDT(+0.27R) ·
                감시 집중 5분(열린 포지션) · 다음 점검 21:41:30 (5분 00초 남음) · 마지막 스캔 가격
```

- 시세는 공개 티커(바이비트 `/v5/market/tickers`)로 읽습니다. 조회가 실패하면 **마지막 스캔 가격**으로
  찍고, 그것도 없으면 `시세 조회 실패`라고만 남깁니다(루프는 계속 돕니다). mock 모드는 고정 시세라 `mock 시세`.
- `--tick-sec 30`처럼 출력 간격을 바꿀 수 있습니다(기본 60초). 스캔이 예외로 끝나면(시세 조회 실패 등)
  30분을 기다리지 않고 **2분 뒤 다시** 시도합니다.

**② 상황이 맞으면 더 자주 봅니다(집중 5분).** 다음 중 **하나라도** 걸리면 다음 대기를 30분 → **5분**으로 줄입니다.

| 조건 | 보는 법 |
| --- | --- |
| 열린 포지션 | 내 기록 또는 거래소에 포지션이 있음 — 손익이 움직이는 동안 촘촘히 |
| 변동성 급증 | 최근 확정 봉의 고가-저가 폭 ≥ 최근 20봉 평균 ATR × 1.5 |
| BSL/SSL 근접 | 현재가(스캔 시점의 마지막 확정 봉 종가)가 최근 스윙 고점(BSL)·저점(SSL) ±0.35% 안쪽 |

- **AI 호출은 늘지 않습니다.** 5분마다 돌아도 Gemini 심사는 **새 M1 후보가 처음 떴을 때만** 부릅니다
  (`signals_seen`이 같은 신호를 두 번 심사하지 않음). 포지션을 들고 있을 때 새 후보가 와도 AI는 부르지 않습니다.
- **안전장치는 그대로입니다.** 집중 감시는 '얼마나 자주 보는가'만 바꿉니다. 시드 50달러 상한·1회 손실 2~4%·
  레버리지 5배 상한·일일 킬스위치·중복 진입 금지·메인넷 차단은 한 줄도 바뀌지 않습니다.
- 지금 모드와 이유는 스캔 기록(`data/testnet_auto_log*.jsonl`)의 `watch`와 `--status` 화면에 남습니다.
- 켜는 법: `run_testnet_auto.bat`은 `--adaptive`를 붙여 돌립니다. 직접 돌릴 때는 `--loop --adaptive`,
  또는 `backend\.env`에 `BYBIT_ADAPTIVE=true`. 끄려면 `--no-adaptive`(또는 `BYBIT_ADAPTIVE=false`).
- 5분 간격은 봉 마감 +90초 기준의 5분 격자(`:01:30`, `:06:30`, `:11:30` …)입니다. 진입 창(5분)과 같은 박자라
  집중 감시 중에도 새 봉 신호 진입 시점을 놓치지 않습니다.

## 3. 한 번의 스캔에서 일어나는 일

1. 공개 API(바이비트→OKX→비트겟→바이낸스 순)로 30분봉을 받아 **확정 봉만** 씁니다. 진행 중 봉은 버립니다.
2. 거래소에서 **잔고·포지션**을 읽습니다(읽지 못하면 아무 주문도 내지 않고 `ERROR`로 멈춥니다).
3. 이미 포지션이 있으면(내 기록이든 수동 진입이든) **신규 진입을 건너뜁니다.**
4. 마지막 확정 봉에 후보1(M1)이 서면 **세션(아시아) → 킬스위치 → 급변 → 현재가 → Gemini 심사** 순으로 통과시킵니다.
5. 통과하면 다음 봉 시가를 기준으로 **손절 3ATR · 목표 4R**을 계산해 시장가 주문을 냅니다.
   수량·명목가·손실 예산은 `bybit_client`의 50달러 규칙이 정합니다(레버리지 3배, 상한 5배).
6. 청산은 거래소에 걸어 둔 TP/SL이 합니다. 모듈은 매 스캔마다 포지션이 아직 있는지만 보고,
   사라졌으면 거래소 청산 기록(`/v5/position/closed-pnl`)으로 손익을 남깁니다. 96봉(48시간)이 지나면 시간 청산합니다.

## 4. 안전 규칙 (코드에 박혀 있음)

| 항목 | 값 |
| --- | --- |
| 거래소 | `api-testnet.bybit.com`(테스트넷) 또는 `api-demo.bybit.com`(데모) 고정 · 메인넷 주문 불가 |
| 시드 상한 | 50 USDT (잔고가 커도 50까지만 계산) |
| 1회 손실 예산 | 기준자본의 2~4% (기본 3% = 1.5 USDT) — 범위 밖은 주문 거부 |
| 레버리지 | 1~5배 (기본 3배) |
| 수량 | 종목 qtyStep으로 내림, 최소 수량 손실이 예산을 넘으면 **주문하지 않음** |
| 중복 | 한 종목 한 포지션 · 같은 봉·같은 신호는 두 번 처리하지 않음 |
| 킬스위치 | 일일 손실 3%·3연속 손절·하루 20회·급변 2.5% → 30분 정지 (`.env`의 `KILL_*`로 조정) |
| 격리 | `paper_loop`의 `data/paper_*.json`은 읽지도 쓰지도 않음. mock·테스트넷·데모는 서로 다른 파일(`*.mock` / `*.demo`) |

## 5. 기록 파일

- `data/testnet_auto_state.json` — 포지션·청산·통계·킬스위치 (실테스트넷)
- `data/testnet_auto_state.demo.json` — 데모 트레이딩 기록(계좌가 달라 파일도 따로, `log`·`killswitch`도 같음)
- `data/testnet_auto_log.jsonl` — 스캔마다 한 줄: 봉·판정·사유·주문·손익
- `data/testnet_auto_killswitch.json` — 오늘의 손실·연속 손절
- 드라이런은 같은 이름에 `.mock`이 붙습니다(`testnet_auto_state.mock.json`).
- `data/testnet_auto_pause*.txt`(정지 표시)·`*.lock`(동시 실행 잠금)도 같은 폴더에 생깁니다.
- 전부 `.gitignore`에 있어 커밋되지 않습니다(PC마다 다른 로컬 상태이므로).

## 6. 알아 둘 것

- **BTCUSDT는 최소 주문이 0.001개**입니다. 30분봉 ATR이 커서 3ATR 손절이 1,500달러를 넘는 날에는
  최소 수량 손실이 1.5달러 예산을 넘어 **주문이 거부**됩니다. 이때 로그에
  `주문 거부: 최소 주문 수량 0.001로 진입하면 예상 손실 $…가 손실 예산 $1.50을 넘습니다`가 남습니다.
  거부를 줄이려면 `--loss-pct 4`(예산 2달러) 또는 변동성이 작은 `--symbol ETHUSDT`/`SOLUSDT`를 씁니다.
- 후보1은 **아시아 세션(00~08 UTC = 09~17 KST)** 진입만 잡습니다. 그 밖의 시간은 관망이 정상입니다.
- 수수료(테이커 0.055%×2)와 슬리피지 때문에 손절 1회는 예산보다 조금 더 나갈 수 있습니다(실테스트넷 기록 기준).
- 보통 30분마다, 집중 감시(포지션·급변·기준선 근접) 중에는 **5분마다** 확인하므로 TP/SL 청산 기록도 그만큼 빨라집니다
  (거래소에서는 그 전에 이미 닫혀 있습니다).
- PC가 꺼지거나 창을 닫으면 루프는 멈춥니다. **이미 열린 포지션의 손절·목표는 거래소에 남아** 계속 관리됩니다.
- 알림(카톡·문자)은 없습니다. 보고 싶으면 `run_testnet_auto_status.bat` 또는 `data/testnet_auto_log.jsonl`을 봅니다.

## 7. 자주 나는 오류

| 메시지 | 뜻 / 대처 |
| --- | --- |
| `Bybit 오류 10002` | PC 시각이 거래소와 많이 다름 — 실행할 때 자동으로 맞추지만, 계속 나면 Windows 시간 동기화 |
| `Bybit 오류 10003 / 10004` | 키가 틀렸거나 권한 없음 — `backend\.env`의 키·권한(Contract Read-Write) 확인 |
| `Bybit ConfigError: 실전(메인넷) 거래는 …` | `BYBIT_TESTNET=false`이거나 메인넷 주소 — `true`로 되돌립니다(데모는 `BYBIT_DEMO=true`) |
| `모드 demo`인데 `10003/10004` | 데모 키가 아닌 일반 키를 넣었을 수 있음 — Demo Trading에서 발급한 키인지 확인 |
| 청산 뒤 `손익 미확인` | 데모에서 청산 손익 조회가 제한된 경우 — 청산은 기록되고 포지션은 비워집니다 |
| `캔들을 받지 못했습니다` | 공개 API 일시 차단 — 잠시 뒤 다시, 또는 `--source okx` |
| `거래소 조회 실패` | 네트워크·키 문제 — 스캔을 멈췄을 뿐 주문은 나가지 않았습니다 |
| `키 없음 — mock` | `.env`에 `BYBIT_API_KEY`/`BYBIT_API_SECRET`가 비어 있음 |

## 8. 테스트

```powershell
python -m pytest backend -q
```

`backend/test_testnet_auto.py`가 이 모듈의 70가지를 고정합니다: mock에서 네트워크 주문 0회 ·
3ATR 손절·4R 목표·50달러 수량 · AI REJECT·킬스위치·세션 밖·기존 포지션 차단 · 같은 봉 중복 금지 ·
mock 손절/목표/96봉 청산 · 거래소 청산 기록으로 손익 확정 · 내 기록과 다른 포지션은 건드리지 않음 ·
paper 파일 불가침 · API 키 비노출 · 수동 정지 파일 · 한 PC에서 두 번 실행 금지(잠금).
데모 모드는 그 위에 `BYBIT_DEMO` 분기 · `.demo` 상태 파일 분리 · 데모 주문 경로 · 청산 손익 조회가
404·미지원이어도 루프가 죽지 않음 · 배너 `데모 실주문 - api-demo.bybit.com`을 고정합니다.
상황 맞춤 감시는 집중 조건 3가지(포지션·변동성·BSL/SSL 근접)와 5분/30분 결정 · 꺼져 있으면 30분 고정 ·
집중 중에도 50달러·5배 안전장치 유지 · 같은 신호를 두 번 심사하지 않음(AI 절약)을 고정하고,
1분 상태 줄은 가짜 시계로 '1분마다 한 줄 · 집중이면 5분 뒤 점검 · Ctrl+C 즉시 종료'를 확인합니다.
