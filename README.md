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
- `backend/` — 선택 사항인 Python 서버(LLM 분석 사용 시). GitHub Pages에는 쓰이지 않습니다.

## 배포

GitHub Pages는 `gh-pages` 브랜치의 내용을 그대로 올립니다. 파일을 고칠 때는 `main`과 `gh-pages` 두 브랜치를 같은 내용으로 맞춰 주세요(`git push origin main:gh-pages`).

## 참고

- 실시간 시세는 바이비트 공개 웹소켓(`wss://stream.bybit.com/v5/public/linear`)에서 받습니다.
- 스크리너는 `wss://stream.bybit.com/v5/public/linear`·`/spot`의 `tickers.{종목}`을 구독해 가격·24h 등락률·거래대금을 즉시 갱신하고, 전체 목록은 45초마다 REST로 보충합니다. 스크리너 탭을 벗어나면 소켓을 닫습니다.
- `.env.example`은 형식만 있는 빈 예시이며 실제 키는 포함되어 있지 않습니다.
- 투자 조언이 아닙니다.
