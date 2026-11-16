# PIXEL TRADING FLOOR · 데모 버전

13명의 픽셀 트레이더가 사무실에서 일하는 모습과, 실시간 바이비트 시세 기반의 모의 트레이딩 화면을 담은 정적 웹 데모입니다.

## 바로 열기

- 웹: https://ghddnjs2003-ship-it.github.io/pixel-trading-floor/
- 휴대폰은 세로 화면(9:16) 레이아웃으로 자동 전환됩니다. 강제로 켜려면 `?vertical=1`, 끄려면 `?vertical=0`.

## 구성

- `index.html` — 사무실(13인 데스크) 화면과 진입점
- `app.js`, `hq.js`, `stage.js`, `qr.js` — 사무실 연출·UI
- `style.css`, `hq.css` — 사무실 화면 스타일, `lite/lite.css` — 모바일·데모 레이어
- `lite/` — 서버 없이 도는 데모 엔진(모의 매매·차트·지표, `shim.js`가 `/api/*`를 가로챕니다)
- `lite/screener.js`, `lite/screener.css` — `[스크리너]` 탭(바이비트 무기한·현물 USDT 종목, 실시간 웹소켓 시세)
- `lite/global.js`, `lite/global.css` — 글로벌 시황: `[사무실]`·`[트레이딩]` 탭의 상단 슬림 전광판(36px)과 `[스크리너]` 탭의 7칸 요약 카드
- `data/global.json` — 미 선물·달러 인덱스·원/달러 스냅숏(24시간 스파크 포함). `scripts/update-global.py`가 만들고 `.github/workflows/update-global.yml`이 10분마다 갱신합니다
- `backend/` — 선택 사항인 Python 서버(LLM 분석 사용 시). GitHub Pages에는 쓰이지 않습니다.

## 글로벌 시황

- `[사무실]`·`[트레이딩]` 탭 — 화면 맨 위에 한 줄 전광판: NQ 선물 · ES 선물 · DXY · 원/달러 · 김프 · BTC 도미넌스 · 공포탐욕
- `[스크리너]` 탭 — 필터 줄 아래 7칸 요약 카드: 미 선물은 24시간 스파크라인, 도미넌스는 BTC/ETH 비중 막대, 공포탐욕은 게이지
- 값은 서버 없이 브라우저가 직접 받습니다: 업비트·코인베이스(김프 30초), CoinGecko `/api/v3/global`(3분, 429면 Coinlore로 자동 전환), alternative.me 공포탐욕(10분).
- 미 선물(NQ·ES)·달러 인덱스·원/달러는 야후 파이낸스에 CORS 헤더가 없어 브라우저가 직접 못 받습니다. 대신 GitHub Actions가 10분마다 `data/global.json`에 담아 같은 출처로 건네고, 페이지는 그 값을 읽습니다(없으면 지난 값 유지).
- 값이 30분보다 낡으면 SYNC 표시가 노랗게 바뀝니다.

## 배포

GitHub Pages는 `gh-pages` 브랜치의 내용을 그대로 올립니다. 파일을 고칠 때는 `main`과 `gh-pages` 두 브랜치를 같은 내용으로 맞춰 주세요(`git push origin main:gh-pages`).

## 참고

- 실시간 시세는 바이비트 공개 웹소켓(`wss://stream.bybit.com/v5/public/linear`)에서 받습니다.
- 스크리너는 `wss://stream.bybit.com/v5/public/linear`·`/spot`의 `tickers.{종목}`을 구독해 가격·24h 등락률·거래대금을 즉시 갱신하고, 전체 목록은 45초마다 REST로 보충합니다. 스크리너 탭을 벗어나면 소켓을 닫습니다.
- `.env.example`은 형식만 있는 빈 예시이며 실제 키는 포함되어 있지 않습니다.
- 글로벌 시황 값은 각 거래소·지수 제공처의 지연 시세입니다. 투자 조언이 아닙니다.
