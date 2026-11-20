# pixel-global Worker — 시황 중계 + ✨ Gemini AI 13인 분석

`https://pixel-global.jooyon124.workers.dev`

| 경로 | 내용 |
| --- | --- |
| `GET /global.json` | 야후 파이낸스(나스닥·S&P 선물, 달러지수, 원달러) → 트레이딩뷰 스캐너 폴백, 45초 엣지 캐시 |
| `GET /calendar.json` | ForexFactory 이번 주 일정(KST), 10분 캐시 |
| `GET /ai-analyze` | 상태 확인 — `{"ok":true,"hasKey":true}` (키 값은 절대 나오지 않음) |
| `POST /ai-analyze` | 픽셀 오피스 ◆ 정밀 분석용 — Gemini가 13인 대사·판정 JSON을 돌려준다 |

## 파일

- `pixel-global.original.js` — 2026-11-20 대시보드에서 받은 기존 중계 코드 **그대로**(시작점)
- `ai-part.js` — ✨ Gemini AI 라우트 + 새 `export default`(라우터)
- `worker.js` — 위 둘을 합친 실제 배포본 (`build.py` 산출물, 커밋해 두고 배포에 쓴다)
- `build.py` — `python3 worker/build.py`
- `patch_ai_front.py` — 2026-11-20 프론트엔드(brain·engine·script·shim·index·app·style) 1회 패치 기록

## 키 (중요)

Gemini 키는 **Worker Secret `GEMINI_API_KEY`** 에만 있다. 코드·저장소·브라우저 JS 어디에도 없다.
프론트는 같은 오리진이 아닌 `https://pixel-global.jooyon124.workers.dev/ai-analyze` 만 부른다.

## 배포

Cloudflare API 토큰(`Workers Scripts:Edit`)으로 스크립트를 올린다. 토큰은 저장소에 두지 않는다.

```bash
ACC=<account id>
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/pixel-global" \
  -H "Authorization: Bearer $CF_TOKEN" \
  -F 'metadata={"main_module":"worker.js","compatibility_date":"2026-11-19","observability":{"enabled":true}};type=application/json' \
  -F 'worker.js=@worker/worker.js;type=application/javascript+module'
```

`application/javascript+module` 이어야 한다(아니면 `Main module must be an ES module` 400).
스크립트를 새로 올리면 바인딩이 초기화되므로 **올린 뒤 시크릿을 다시 붙인다**:

```bash
GEMINI_API_KEY=... python3 -c "import json,os;print(json.dumps({'name':'GEMINI_API_KEY','text':os.environ['GEMINI_API_KEY'],'type':'secret_text'}))" \
 | curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/pixel-global/secrets" \
   -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" --data @-
```

## 남용 방지 / 한도

- `Origin` 허용: `https://ghddnjs2003-ship-it.github.io`, `http://localhost:*`, `http://127.0.0.1:*`
- IP당 10분 15회·최소 2.5초 간격(isolate 메모리 기준)
- 본문 12KB 제한, 값 범위·문자열 길이 검증
- 모델: `gemini-3.5-flash-lite` → 실패 시 `gemini-3.1-flash-lite`. 429면 `{ok:false,error:"quota"}` → 프론트가 체험 규칙으로 폴백
- 프론트도 15초 쿨다운, 시연(?kiosk=1)·`?ai=0`에서는 호출하지 않는다
