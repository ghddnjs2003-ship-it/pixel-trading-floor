#!/usr/bin/env python3
"""2026-11-20 픽셀 오피스 ✨ Gemini AI 연동 패치 (CRLF 파일 대응). 한 번만 실행."""
import io, os, sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
BASE = os.path.abspath(BASE)


def patch(name, pairs):
    path = os.path.join(BASE, name)
    with open(path, 'r', encoding='utf-8', newline='') as f:
        src = f.read()
    for old, new in pairs:
        if old not in src:
            print(f'!! {name}: 패턴 없음 -> {old[:70]!r}')
            sys.exit(1)
        if src.count(old) != 1:
            print(f'!! {name}: 패턴 {src.count(old)}회 -> {old[:70]!r}')
            sys.exit(1)
        src = src.replace(old, new)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(src)
    print(f'ok {name}')


# ── brain.js: Gemini 판정 반영 함수 ──────────────────────────────────────────
AI_FN = '''  // ✨ Gemini AI 판정 반영 — 방향·확신도는 AI가 정하고, 진입·손절·목표·청산 숫자는
  //   규칙이 다시 계산한다. 그래서 리스크 게이트(손절이 청산가 안쪽인지)와 화면 스키마가 그대로 유지된다.
  function applyAi(an, ai) {
    const lev = an.lev;
    const side = ai && ai.action === 'BUY' ? 'LONG' : ai && ai.action === 'SELL' ? 'SHORT' : null;
    const conf = ai && Number.isFinite(ai.confidence)
      ? Math.max(0, Math.min(100, Math.round(ai.confidence))) : an.plan.confidence;
    if (!side) {
      const lv = levels(an.plan.candidate, an.sig.price, lev);
      const plan = {
        ...an.plan, ...lv, side: null, action: 'HOLD', bias: 'PASS', confidence: conf,
        reason: 'AI_HOLD', hold: true, trial: false, rsiHot: false, pending: { side: an.plan.candidate, ...lv },
      };
      const gate = riskGate(plan);
      const pm = { verdict: 'APPROVE', code: 'HOLD_OK', marginPct: 0 };
      return { ...an, plan, gate, pm, final: finalize(plan, gate, pm, an.mode), ai: true };
    }
    const lv = levels(side, an.sig.price, lev);
    const plan = {
      ...an.plan, ...lv, side, action: side === 'LONG' ? 'BUY' : 'SELL', bias: side, candidate: side,
      confidence: conf, reason: 'AI', hold: false, trial: false, rsiHot: false, pending: null,
    };
    const gate = riskGate(plan);
    const pm = pmReview(plan, an.sig, gate);
    return { ...an, plan, gate, pm, final: finalize(plan, gate, pm, an.mode), ai: true };
  }

'''.replace('\n', '\r\n')


patch('lite/brain.js', [
    ('  function analyze(snap, opts = {}) {\r\n', AI_FN + '  function analyze(snap, opts = {}) {\r\n'),
    ('return { RULES, clampLev, fmtPrice, signals, levels, decide, riskGate, pmReview, finalize, analyze };',
     'return { RULES, clampLev, fmtPrice, signals, levels, decide, riskGate, pmReview, finalize, analyze, applyAi };'),
])

# ── engine.js: Gemini 호출 + 대사 덮어쓰기 ───────────────────────────────────
AI_BLOCK = '''        // ✨ Gemini AI 13인 분석 — Worker(GEMINI_API_KEY Secret)가 13인 대사와 판정을 돌려준다.
        //   응답 실패·무료 한도(429)·연타는 여기서 조용히 체험 규칙(B안)으로 넘어간다(화면은 멈추지 않는다).
        if (deps.gemini && !kiosk && opts.ai !== false) {
          log('> ✨ Gemini AI 13인 팀에 분석을 요청합니다…');
          const ai = await deps.gemini.analyze(base, snap, { lev, mode });
          if (ai && ai.ok) {
            an = Brain.applyAi(base, ai);
            T = deps.gemini.texts(Script.texts(an, snap), ai, mode);
            emit({ type: 'ai', ok: true, model: ai.model, ms: ai.ms });
            log(`> ✨ Gemini AI(${ai.model}) 응답 ${(Math.round((ai.ms || 0) / 100) / 10).toFixed(1)}초 — 13인 팀 판정 ${an.final.action} ${an.plan.confidence}%`);
          } else {
            emit({ type: 'ai', ok: false, code: ai && ai.code });
            const why = ai && ai.code && ai.code !== 'cooldown' ? ` (${ai.code})` : '';
            log(`> ⚠ Gemini AI 응답 없음${why} — 체험 규칙(B안)으로 판정합니다`);
          }
        }
        if (!T) T = Script.texts(an, snap);

'''.replace('\n', '\r\n')

patch('lite/engine.js', [
    ('        const an = Brain.analyze(snap, { lev, mode });\r\n        const T = Script.texts(an, snap);\r\n',
     '        const base = Brain.analyze(snap, { lev, mode });\r\n        let an = base;\r\n        let T = null;\r\n'),
    ('        await wait(600);\r\n\r\n        log(\'── 1/5 애널리스트 브리핑',
     '        await wait(600);\r\n\r\n' + AI_BLOCK + '        log(\'── 1/5 애널리스트 브리핑'),
])

# ── shim.js: 엔진에 Gemini 클라이언트 주입 ───────────────────────────────────
patch('lite/shim.js', [
    ("      data: { resolveSymbol: win.LiteData.resolveSymbol, fetchSnapshot: (sym) => win.LiteData.fetchSnapshot(sym, { fetch: ctx.fetchImpl }) },\r\n",
     "      data: { resolveSymbol: win.LiteData.resolveSymbol, fetchSnapshot: (sym) => win.LiteData.fetchSnapshot(sym, { fetch: ctx.fetchImpl }) },\r\n"
     "      // ?ai=0 이거나 LITE_CONFIG.gemini === false 면 규칙만 쓴다(시연 화면은 아래에서 kiosk로 꺼진다)\r\n"
     "      gemini: win.LiteGemini && (win.LITE_CONFIG || {}).gemini !== false\r\n"
     "        && new URLSearchParams(win.location.search).get('ai') !== '0' ? win.LiteGemini.create() : null,\r\n"),
])

# ── index.html: 스크립트 추가 + 판정 패널 배지 + 캐시 버스터 ──────────────────
patch('index.html', [
    ('  <script src="lite/brain.js?v=12"></script>', '  <script src="lite/brain.js?v=13"></script>'),
    ('  <script src="lite/script.js?v=12"></script>', '  <script src="lite/script.js?v=13"></script>'),
    ('  <script src="lite/engine.js"></script>\n  <script src="lite/shim.js"></script>',
     '  <script src="lite/engine.js?v=13"></script>\n  <script src="lite/gemini.js?v=13"></script>\n  <script src="lite/shim.js"></script>'),
    ('  <link rel="stylesheet" href="style.css">', '  <link rel="stylesheet" href="style.css?v=13">'),
    ('<div class="dp-head"><span>최종 판정</span><button id="dp-card-btn"',
     '<div class="dp-head"><span>최종 판정</span><span id="dp-source" class="dp-source hidden" title="판정을 만든 엔진"></span><button id="dp-card-btn"'),
])

# ── app.js: ai 이벤트 배지 + 초기화 ─────────────────────────────────────────
patch('app.js', [
    ("  const rb = qs('#dp-risk');\r\n  if (rb) rb.className = 'hidden';\r\n",
     "  const rb = qs('#dp-risk');\r\n  if (rb) rb.className = 'hidden';\r\n"
     "  const src = qs('#dp-source');\r\n  if (src) { src.textContent = ''; src.classList.add('hidden'); src.classList.remove('live'); }\r\n"),
    ("    case 'decision':   onDecision(ev); break;",
     "    case 'decision':   onDecision(ev); break;\r\n    case 'ai':         onAi(ev); break;"),
    ("function onDecision(ev) {",
     "// ✨ Gemini AI / 체험 규칙 — 이번 판정을 누가 만들었는지 패널 머리에 표시\r\n"
     "function onAi(ev) {\r\n"
     "  const el = qs('#dp-source');\r\n"
     "  if (!el) return;\r\n"
     "  if (ev && ev.ok) {\r\n"
     "    const label = String(ev.model || 'gemini').replace(/^gemini-/, '').replace(/-/g, ' ');\r\n"
     "    el.textContent = `✨ Gemini AI · ${label}`;\r\n"
     "    el.classList.add('live');\r\n"
     "  } else {\r\n"
     "    el.textContent = '체험 규칙 B안';\r\n"
     "    el.classList.remove('live');\r\n"
     "  }\r\n"
     "  el.classList.remove('hidden');\r\n"
     "}\r\n\r\n"
     "function onDecision(ev) {"),
])

print('done')
