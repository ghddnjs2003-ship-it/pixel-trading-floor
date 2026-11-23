# 테스트넷 오토 모드 — 퇴근 후 집 PC에서 50달러로 돌려 보기

`backend/testnet_auto.py`가 30분봉 후보1(M1)을 찾고 Gemini가 PASS를 주면, **바이비트 테스트넷**에
모의 50 USDT로 시장가 주문(손절·목표 동봉)을 냅니다. 실돈은 움직이지 않습니다 —
주소가 `api-testnet.bybit.com`으로 고정되어 있고, 메인넷 주소·`BYBIT_TESTNET=false`는 코드가 거부합니다.

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

## 2. 세 번 눌러 보기 (더블클릭)

| 파일 | 하는 일 |
| --- | --- |
| `run_testnet_auto_mock.bat` | **드라이런** — 키 없이 1회 스캔. "무엇을 할지"만 보여 주고 주문 없음 |
| `run_testnet_auto_status.bat` | 계좌·포지션·킬스위치·최근 스캔 확인 |
| `run_testnet_auto.bat` | **실테스트넷 루프** — 30분 봉 마감 +90초마다 자동 점검 (Ctrl+C로 종료) |

명령어로 직접 돌릴 때:

```powershell
python -m backend.testnet_auto --once --mock     # 1회 드라이런(주문 없음)
python -m backend.testnet_auto --once            # 1회 실제 테스트넷 점검
python -m backend.testnet_auto --loop            # 계속 자동
python -m backend.testnet_auto --status          # 상태
python -m backend.testnet_auto --stop            # 신규 진입만 정지(포지션 TP/SL은 유지)
python -m backend.testnet_auto --resume          # 다시 진입 허용
python -m backend.testnet_auto --reset           # 로컬 기록 초기화(열린 포지션 있으면 거부)
```

- `--stop`은 **돌고 있는 루프에도 바로** 먹습니다. 정지 표시 파일(`data/testnet_auto_pause*.txt`)을 쓰고,
  `--resume`이 지웁니다. (킬스위치 메모리 상태만 바꾸면 다음 저장에 덮여 사라지기 때문입니다.)
- 같은 상태 파일로 **창을 두 개 띄우면** 잠금(`*.lock`)이 두 번째를 멈춥니다 — 같은 봉에 주문이 두 번 나가는 일을 막습니다.

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
| 거래소 | `api-testnet.bybit.com` 고정 · 메인넷 주문 불가 |
| 시드 상한 | 50 USDT (잔고가 커도 50까지만 계산) |
| 1회 손실 예산 | 기준자본의 2~4% (기본 3% = 1.5 USDT) — 범위 밖은 주문 거부 |
| 레버리지 | 1~5배 (기본 3배) |
| 수량 | 종목 qtyStep으로 내림, 최소 수량 손실이 예산을 넘으면 **주문하지 않음** |
| 중복 | 한 종목 한 포지션 · 같은 봉·같은 신호는 두 번 처리하지 않음 |
| 킬스위치 | 일일 손실 3%·3연속 손절·하루 20회·급변 2.5% → 30분 정지 (`.env`의 `KILL_*`로 조정) |
| 격리 | `paper_loop`의 `data/paper_*.json`은 읽지도 쓰지도 않음. mock은 `*.mock.json`에 따로 기록 |

## 5. 기록 파일

- `data/testnet_auto_state.json` — 포지션·청산·통계·킬스위치 (실테스트넷)
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
- 30분마다만 확인하므로 TP/SL 청산은 **최대 30분 늦게** 기록됩니다(거래소에서 먼저 닫힙니다).
- PC가 꺼지거나 창을 닫으면 루프는 멈춥니다. **이미 열린 포지션의 손절·목표는 거래소에 남아** 계속 관리됩니다.
- 알림(카톡·문자)은 없습니다. 보고 싶으면 `run_testnet_auto_status.bat` 또는 `data/testnet_auto_log.jsonl`을 봅니다.

## 7. 자주 나는 오류

| 메시지 | 뜻 / 대처 |
| --- | --- |
| `Bybit 오류 10002` | PC 시각이 거래소와 많이 다름 — 실행할 때 자동으로 맞추지만, 계속 나면 Windows 시간 동기화 |
| `Bybit 오류 10003 / 10004` | 키가 틀렸거나 권한 없음 — `backend\.env`의 키·권한(Contract Read-Write) 확인 |
| `Bybit ConfigError: 실전(메인넷) 거래는 …` | `BYBIT_TESTNET=false`이거나 메인넷 주소 — `true`로 되돌립니다 |
| `캔들을 받지 못했습니다` | 공개 API 일시 차단 — 잠시 뒤 다시, 또는 `--source okx` |
| `거래소 조회 실패` | 네트워크·키 문제 — 스캔을 멈췄을 뿐 주문은 나가지 않았습니다 |
| `키 없음 — mock` | `.env`에 `BYBIT_API_KEY`/`BYBIT_API_SECRET`가 비어 있음 |

## 8. 테스트

```powershell
python -m pytest backend -q
```

`backend/test_testnet_auto.py`가 이 모듈의 38가지를 고정합니다: mock에서 네트워크 주문 0회 ·
3ATR 손절·4R 목표·50달러 수량 · AI REJECT·킬스위치·세션 밖·기존 포지션 차단 · 같은 봉 중복 금지 ·
mock 손절/목표/96봉 청산 · 거래소 청산 기록으로 손익 확정 · 내 기록과 다른 포지션은 건드리지 않음 ·
paper 파일 불가침 · API 키 비노출 · 수동 정지 파일 · 한 PC에서 두 번 실행 금지(잠금).
