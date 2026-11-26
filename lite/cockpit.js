/* PIXEL TRADING FLOOR — 오토 모드 조종석 + ⚙️ 설정 (2026-11-24 홍원 님 승인 시안 · 웹 v27)
   · 데모 트레이딩 탭 맨 위에 조종석: AUTO 토글 · 모의 잔고 · 현재 포지션 · 리스크 · 최대 레버리지 ·
     최근 AI 판단 · BSL/SSL 기준선 · 상태 새로고침 / 리스크 설정 / 긴급 정지 · 최근 실행 로그
   · 상단 4탭 오른쪽 끝 ⚙️: 연결 상태(웹 v27 · Cloudflare Worker AI · 백엔드 30분봉 AI · 집 PC 데모) /
     오토 모드 기본값(시드·1회 리스크·최대 레버리지·가동 세션) / 빠른 관리(긴급 정지·데모→실전 순서·새로고침)
   · 트레이딩뷰 위젯은 조종석 아래에 그대로 둔다 (2026-11-20 홍원 님 "트레이딩뷰만 사용할거야 전부다")

   값의 출처 — 화면이 숫자를 지어내지 않는다
     · data/paper_state.json   백엔드 30분봉 AI 모의매매의 자본·포지션·마지막 스캔·킬스위치 (30분마다 루프가 커밋)
     · data/paper_log.jsonl    봉별 판단 로그 → 최근 실행 로그
     · data/paper_killswitch.json  오늘 손실·연속 손절·수동 정지
     · Bybit 15분봉 공개 API  최근 스윙 고점·저점 → BSL(위) / SSL(아래) 기준선
     · lite/paper.js           이 브라우저의 로컬 모의 계좌 (모의 진입·청산)
     · localStorage            AUTO 토글 · 긴급 정지 잠금 · 오토 모드 기본값
   집 PC의 테스트넷·데모 오토 모드 상태 파일은 저장소에 커밋하지 않는다(.gitignore) — 웹은 그 사실만 안내한다.
   투자 조언이 아니다. */
(function () {
  'use strict';

  const LITE = window.LITE;
  const page = document.getElementById('trade-page');
  const topbar = document.getElementById('topbar');
  if (!page || !topbar) return;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const safe = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const VERSION = 'v31';
  const KEY = 'pixel-auto-cockpit-v1';
  const DECISION_KEY = 'pixel-lite-latest-decision-v1';
  const WORKER_URL = 'https://pixel-global.jooyon124.workers.dev/global.json';
  const KLINE_URL = (window.LiteLive && window.LiteLive.URL_KLINE) || 'https://api.bybit.com/v5/market/kline';
  const STATE_URL = 'data/paper_state.json';
  const LOG_URL = 'data/paper_log.jsonl';
  const KILL_URL = 'data/paper_killswitch.json';
  const LOG_ROWS = 7;

  const SESSIONS = {
    asia: '아시아 09:00~17:00 KST',
    london: '런던 17:00~01:00 KST',
    ny: '뉴욕 22:00~07:00 KST',
    all: '24시간 상시',
  };
  // 설정 상자의 옵션은 좁은 칸에 들어가게 KST를 빼고 적는다(고른 뒤 요약 줄에는 KST까지 나온다)
  const SESSION_OPTS = {
    asia: '아시아 09:00~17:00',
    london: '런던 17:00~01:00',
    ny: '뉴욕 22:00~07:00',
    all: '24시간 상시',
  };
  const LEVS = [1, 2, 3, 5, 10];
  const DEFAULTS = { auto: null, kill: null, seed: 50, riskPct: 3, maxLev: 3, session: 'asia', collapsed: false };
  const ACTION_KO = { HOLD: '관망', OPEN: '진입', CLOSE: '청산', SKIP: '건너뜀', REJECT: '거절' };

  /* ---------------------------------------------------------------- 값 도우미 */
  const isNum = (v) => v != null && v !== '' && Number.isFinite(Number(v));
  const num = (v, dp = 0) => (isNum(v)
    ? Number(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp }) : '—');
  const pct = (v, dp = 2) => (isNum(v) ? `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(dp)}%` : '—');
  const priceText = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return '—';
    const dp = n < 0.01 ? 8 : n < 1 ? 5 : n < 100 ? 4 : 2;
    return n.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
  };
  function kstParts(iso, withDate) {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    try {
      const parts = new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).formatToParts(d);
      const get = (t) => (parts.find((p) => p.type === t) || {}).value || '';
      return { clock: `${get('hour')}:${get('minute')}`, day: `${get('month')}-${get('day')}` };
    } catch (_) {
      const s = d.toISOString();
      return { clock: s.slice(11, 16), day: s.slice(5, 10) };
    }
  }
  const clockOf = (iso) => (kstParts(iso) || {}).clock || '—';
  // '2026-11-24 11:00'(KST 문자열)은 그대로 시:분만, ISO·ms는 한국 시간으로
  function logClock(rec) {
    const raw = rec && rec.bar_kst;
    if (typeof raw === 'string' && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(raw)) return raw.slice(11, 16);
    return clockOf(rec && (rec.bar || rec.ts));
  }
  function ageMin(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return null;
    return Math.max(0, (Date.now() - t) / 60000);
  }
  function ageText(min) {
    if (min == null) return '';
    if (min < 1) return '방금';
    if (min < 60) return `${Math.round(min)}분 전`;
    if (min < 1440) return `${Math.floor(min / 60)}시간 ${Math.round(min % 60)}분 전`;
    return `${Math.floor(min / 1440)}일 전`;
  }

  /* ---------------------------------------------------------------- 상태 */
  function loadCfg() {
    let raw = null;
    try { raw = localStorage.getItem(KEY); } catch (_) { raw = null; }
    let cfg = Object.assign({}, DEFAULTS);
    if (raw) {
      try { cfg = Object.assign(cfg, JSON.parse(raw) || {}); } catch (_) { /* 깨진 값은 기본값 */ }
    }
    if (!isNum(cfg.seed) || Number(cfg.seed) <= 0) cfg.seed = DEFAULTS.seed;
    if (!isNum(cfg.riskPct) || Number(cfg.riskPct) <= 0) cfg.riskPct = DEFAULTS.riskPct;
    if (!LEVS.includes(Number(cfg.maxLev))) cfg.maxLev = DEFAULTS.maxLev;
    if (!SESSIONS[cfg.session]) cfg.session = DEFAULTS.session;
    return cfg;
  }
  function saveCfg() {
    try { localStorage.setItem(KEY, JSON.stringify(S.cfg)); } catch (_) { /* 저장 못 해도 이번 화면은 동작 */ }
  }

  const S = {
    cfg: loadCfg(),
    paper: null,
    paperError: null,
    log: [],
    backendKill: null,
    logAt: 0,
    local: null,
    decision: null,
    levels: null,
    levelsError: null,
    worker: { ok: null, at: 0, error: null },
    busy: false,
    autoDefaulted: false,
  };

  /* ---------------------------------------------------------------- 자료 받기 */
  async function getText(url, ms) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), ms || 8000) : 0;
    try {
      const res = await fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  async function tryJson(url) {
    try { return { ok: true, data: JSON.parse(await getText(url)) }; }
    catch (e) { return { ok: false, error: e && e.message ? e.message : String(e) }; }
  }
  async function tryText(url) {
    try { return { ok: true, text: await getText(url) }; }
    catch (e) { return { ok: false, error: e && e.message ? e.message : String(e) }; }
  }

  function readDecision() {
    try {
      const raw = localStorage.getItem(DECISION_KEY);
      if (!raw) return null;
      const d = JSON.parse(raw);
      return d && ['LONG', 'SHORT', 'HOLD'].includes(d.action) ? d : null;
    } catch (_) { return null; }
  }

  function readLocalPaper() {
    try {
      if (!LITE || !LITE.paper || typeof LITE.paper.snapshot !== 'function') return null;
      return LITE.paper.snapshot();
    } catch (_) { return null; }
  }

  // BSL / SSL — 최근 15분봉 200개의 스윙(2봉 프랙털) 중 지금 가격 바로 위 고점·바로 아래 저점
  function calcLiquidity(candles, last) {
    const win = candles.slice(-200);
    if (win.length < 10) return null;
    const highs = [];
    const lows = [];
    for (let i = 2; i < win.length - 2; i += 1) {
      const c = win[i];
      if (c.h >= win[i - 1].h && c.h >= win[i - 2].h && c.h >= win[i + 1].h && c.h >= win[i + 2].h) highs.push(c.h);
      if (c.l <= win[i - 1].l && c.l <= win[i - 2].l && c.l <= win[i + 1].l && c.l <= win[i + 2].l) lows.push(c.l);
    }
    const above = highs.filter((h) => h > last);
    const below = lows.filter((l) => l < last);
    const bsl = above.length ? Math.min(...above) : Math.max(...win.map((c) => c.h));
    const ssl = below.length ? Math.max(...below) : Math.min(...win.map((c) => c.l));
    return { bsl, ssl, basis: above.length && below.length ? '15분봉 스윙' : '15분봉 고저' };
  }

  async function loadLevels() {
    const base = (($('#trade-coin') || {}).value || 'BTC').toUpperCase();
    const symbol = `${base}USDT`;
    try {
      const res = await fetch(`${KLINE_URL}?category=linear&symbol=${symbol}&interval=15&limit=200`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const rows = (body && body.result && body.result.list) || [];
      const candles = rows
        .map((k) => ({ t: Number(k[0]), o: Number(k[1]), h: Number(k[2]), l: Number(k[3]), c: Number(k[4]) }))
        .filter((c) => [c.h, c.l, c.c].every(Number.isFinite))
        .sort((a, b) => a.t - b.t);
      if (!candles.length) throw new Error('캔들이 비어 있습니다');
      const last = candles[candles.length - 1].c;
      const levels = calcLiquidity(candles, last);
      if (!levels) throw new Error('캔들이 짧습니다');
      S.levels = Object.assign(levels, { symbol, last });
      S.levelsError = null;
    } catch (e) {
      S.levels = null;
      S.levelsError = e && e.message ? e.message : String(e);
    }
  }

  async function checkWorker(force) {
    if (!force && S.worker.at && Date.now() - S.worker.at < 60000) return;
    try {
      const res = await fetch(`${WORKER_URL}?t=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json().catch(() => null);
      S.worker = { ok: true, at: Date.now(), updatedAt: body && (body.updatedAt || body.updated_at), error: null };
    } catch (e) {
      S.worker = { ok: false, at: Date.now(), updatedAt: null, error: e && e.message ? e.message : String(e) };
    }
  }

  async function refresh(force) {
    if (S.busy && !force) return;
    S.busy = true;
    paintBusy(true);
    const bust = `?t=${Date.now()}`;
    try {
      const [st, lg, ks] = await Promise.all([
        tryJson(STATE_URL + bust),
        tryText(LOG_URL + bust),
        tryJson(KILL_URL + bust),
      ]);
      if (st.ok && st.data) { S.paper = st.data; S.paperError = null; }
      else { S.paperError = st.error || '상태 파일을 받지 못했습니다'; }
      if (lg.ok) {
        S.log = String(lg.text || '').split('\n').filter((l) => l.trim()).slice(-40)
          .map((l) => { try { return JSON.parse(l); } catch (_) { return null; } })
          .filter(Boolean).reverse();
        S.logAt = Date.now();
      } else if (!S.log.length) {
        S.log = [];
      }
      S.backendKill = ks.ok && ks.data ? ks.data : (S.paper && S.paper.killswitch) || null;
      S.local = readLocalPaper();
      S.decision = readDecision();
      await Promise.all([loadLevels(), checkWorker(force)]);
    } finally {
      S.busy = false;
      paintBusy(false);
      defaultAutoOnce();
      render();
    }
  }

  /* ---------------------------------------------------------------- 상태 판단 */
  function backendAge() {
    if (!S.paper) return null;
    return ageMin(S.paper.updated_at || (S.paper.last_scan && S.paper.last_scan.ts));
  }
  function backendFresh() {
    const a = backendAge();
    return a != null && a < 60;
  }
  function killState() {
    // 프론트 잠금(내가 누른 긴급 정지)과 백엔드 킬스위치(루프가 기록한 발동)를 함께 본다
    if (S.cfg.kill) return { on: true, front: true, reason: S.cfg.kill.reason || '조종석 긴급 정지' };
    const bk = S.backendKill || (S.paper && S.paper.killswitch);
    if (bk && (bk.manual_stop || bk.tripped)) {
      return { on: true, front: false, reason: bk.trip_reason || bk.reason || '백엔드 킬스위치' };
    }
    return { on: false, front: false, reason: '' };
  }
  // 처음 연 사람에게는 백엔드 기록의 상태로 AUTO를 정한다 — 기록이 살아 있으면 켜 둔 채로 시작
  function defaultAutoOnce() {
    if (S.cfg.auto !== null) return;
    S.cfg.auto = backendFresh();
    saveCfg();
  }

  /* ---------------------------------------------------------------- 조종석 DOM */
  const section = document.createElement('section');
  section.id = 'trade-auto';
  section.className = 'trade-auto';
  section.dataset.pane = 'auto';
  section.setAttribute('aria-label', '오토 모드 조종석');
  section.innerHTML = `
    <div class="ta-head">
      <div class="ta-title"><b>오토 모드 조종석</b><span id="ta-mode">—</span></div>
      <span class="ta-badge off" id="ta-badge">연결 확인 중</span>
      <button class="ta-fold" id="ta-fold" type="button" title="조종석 접기·펴기" aria-label="조종석 접기">▾</button>
      <label class="ta-switch" id="ta-switch">
        <input type="checkbox" id="ta-toggle" role="switch" aria-label="오토 모드"><span class="ta-track"><i></i></span><b id="ta-toggle-text">AUTO OFF</b>
      </label>
    </div>
    <div class="ta-grid">
      <div class="ta-card"><span class="ta-lbl">모의 계좌 잔고<i>30분봉 AI · 실주문 없음</i></span><b id="ta-balance">—</b><small id="ta-balance-sub">—</small></div>
      <div class="ta-card"><span class="ta-lbl">현재 포지션 / 상태</span><b id="ta-position">—</b><small id="ta-position-sub">—</small></div>
      <div class="ta-card"><span class="ta-lbl">실전 리스크 기준</span><b id="ta-risk">—</b><small id="ta-risk-sub">—</small></div>
      <div class="ta-card"><span class="ta-lbl">최대 레버리지</span><b id="ta-lev">—</b><small id="ta-lev-sub">—</small></div>
      <div class="ta-card"><span class="ta-lbl">최근 AI 판단<i id="ta-ai-when"></i></span><b id="ta-ai">—</b><small id="ta-ai-sub">—</small></div>
      <div class="ta-card"><span class="ta-lbl">BSL / SSL 기준선</span><b id="ta-levels">—</b><small id="ta-levels-sub">—</small></div>
    </div>
    <div class="ta-actions">
      <button id="ta-refresh" type="button">상태 새로고침</button>
      <button id="ta-risk-btn" type="button">리스크 설정</button>
      <button id="ta-kill" class="ta-kill" type="button">긴급 정지</button>
    </div>
    <div class="ta-log">
      <div class="ta-log-head"><b>최근 실행 로그</b><span id="ta-log-sub">data/paper_log.jsonl</span></div>
      <ul id="ta-log-list"><li class="ta-empty">로그를 불러오는 중…</li></ul>
    </div>
    <div class="ta-note">모의 계좌 · 실제 시세 · 실주문 없음 · 긴급 정지는 이 화면을 즉시 멈추고 잠급니다(백엔드 정지는 ⚙️ 설정 안내)</div>`;

  const marketbar = page.querySelector('.trade-marketbar');
  if (marketbar) marketbar.after(section);
  else page.insertBefore(section, page.firstChild);

  /* ---------------------------------------------------------------- 그리기 */
  let flash = null;
  const BASE_LABEL = { '#ta-refresh': '상태 새로고침', '#cs-save': '저장', '#cs-reset': '기본값', '#cs-refresh': '데이터 새로고침' };
  function paintBusy(busy) {
    const btn = $('#ta-refresh');
    if (btn) btn.textContent = busy ? '불러오는 중…' : BASE_LABEL['#ta-refresh'];
  }
  function flashBtn(sel, text) {
    const btn = $(sel);
    if (!btn) return;
    btn.textContent = text;
    clearTimeout(flash);
    flash = setTimeout(() => {
      if (sel === '#ta-kill' || sel === '#ta-refresh') { render(); return; }
      btn.textContent = BASE_LABEL[sel] || text;
    }, 1600);
  }

  function render() {
    const kill = killState();
    const autoOn = !!S.cfg.auto && !kill.on;

    // 모드 줄 — 데모/실전, 백엔드 두뇌, 가동 세션
    const mode = $('#ta-mode');
    if (mode) {
      mode.textContent = `Demo Trading (모의) · 백엔드 30분봉 M1 + Gemini AI · ${SESSIONS[S.cfg.session]}`;
    }

    // 배지 — 백엔드 기록의 나이로 연결 상태를 말한다
    const badge = $('#ta-badge');
    if (badge) {
      badge.className = 'ta-badge';
      if (kill.on) {
        badge.classList.add('kill');
        badge.textContent = kill.front ? '안전 잠금 · 프론트 정지' : '킬스위치 발동';
      } else if (!S.paper) {
        badge.classList.add('off');
        badge.textContent = S.paperError ? '백엔드 기록 없음' : '연결 확인 중';
      } else if (backendFresh()) {
        badge.textContent = `DEMO 연결 · ${ageText(backendAge())} 갱신`;
      } else {
        badge.classList.add('warn');
        badge.textContent = `스냅숏 ${ageText(backendAge())}`;
      }
    }

    // AUTO 토글
    const toggle = $('#ta-toggle');
    const sw = $('#ta-switch');
    const tx = $('#ta-toggle-text');
    if (toggle) toggle.checked = autoOn;
    if (sw) sw.classList.toggle('locked', kill.on);
    if (tx) tx.textContent = kill.on ? 'LOCKED' : autoOn ? 'AUTO ON' : 'AUTO OFF';

    // 잔고 — 백엔드 모의 자본(기준) + 이 브라우저 로컬 모의 계좌
    const bal = $('#ta-balance');
    const balSub = $('#ta-balance-sub');
    const eq = S.paper && isNum(S.paper.equity) ? Number(S.paper.equity) : null;
    const start = S.paper && isNum(S.paper.initial_equity) ? Number(S.paper.initial_equity) : null;
    const localEq = S.local && S.local.account ? Number(S.local.account.equity) : null;
    if (bal) {
      const v = eq != null ? eq : localEq;
      bal.textContent = v == null ? '—' : `${num(v, 2)} USDT`;
      bal.className = v == null ? '' : (start != null && eq != null ? (eq > start ? 'up' : eq < start ? 'down' : '') : '');
    }
    if (balSub) {
      const parts = [];
      if (eq != null && start != null) parts.push(`시작 ${num(start)} → ${num(eq, 2)}`);
      if (localEq != null) parts.push(`로컬 <b>${num(localEq, 2)}</b>`);
      balSub.innerHTML = parts.length ? parts.join(' · ') : '백엔드 기록을 기다리는 중 — 로컬 모의 계좌로 보입니다';
    }

    // 포지션 / 상태
    const pos = S.paper && S.paper.position ? S.paper.position : null;
    const posEl = $('#ta-position');
    const posSub = $('#ta-position-sub');
    const localOpen = S.local && Array.isArray(S.local.open) ? S.local.open : [];
    if (posEl) {
      if (kill.on) { posEl.textContent = '정지 · 잠금'; posEl.className = 'down'; }
      else if (pos) { posEl.textContent = `${String(pos.side || '').toUpperCase()} ${priceText(pos.entry)}`; posEl.className = String(pos.side).toUpperCase() === 'SHORT' ? 'down' : 'up'; }
      else if (localOpen.length) { posEl.textContent = `로컬 ${localOpen.length}건 보유`; posEl.className = 'up'; }
      else { posEl.textContent = 'HOLD · 진입 자리 대기'; posEl.className = 'hold'; }
    }
    if (posSub) {
      if (kill.on) {
        posSub.innerHTML = kill.front
          ? '긴급 정지 — 새 스캔·진입을 멈췄습니다 · 잠금을 풀면 다시 따라갑니다'
          : `백엔드 킬스위치 발동 — ${safe(kill.reason)} · 집 PC에서 해제합니다`;
      } else if (pos) {
        posSub.innerHTML = `손절 <b>${priceText(pos.stop)}</b> · 목표 <b>${priceText(pos.target)}</b> · 위험 <b>$${num(pos.risk_usd, 2)}</b>`;
      } else if (localOpen.length) {
        const p = localOpen[0];
        posSub.innerHTML = `${safe(p.display || p.symbol)} ${safe(p.side)} ${safe(p.leverage)}x · 진입 <b>${priceText(p.entry)}</b> · ROE <b>${pct(p.roePct, 1)}</b>`;
      } else {
        posSub.innerHTML = `신호 조건을 기다리는 중 · 30분봉 확정마다 스캔`;
      }
    }

    // 리스크 기준 (설정값 → 조종석 카드로 바로)
    const seed = Number(S.cfg.seed);
    const riskPct = Number(S.cfg.riskPct);
    const riskUsd = seed * riskPct / 100;
    const riskEl = $('#ta-risk');
    const riskSub = $('#ta-risk-sub');
    if (riskEl) riskEl.textContent = `$${num(seed)} · ${riskPct}%`;
    if (riskSub) riskSub.innerHTML = `1회 손실 한도 <b>$${num(riskUsd, 2)}</b> · 실전 기준(모의 계좌와 별도)`;

    const levEl = $('#ta-lev');
    const levSub = $('#ta-lev-sub');
    if (levEl) levEl.textContent = `${Number(S.cfg.maxLev)}x`;
    if (levSub) levSub.innerHTML = `실전 상한 · 격리 고정 · 손절이 청산가 안쪽`;

    // 최근 AI 판단 — 백엔드 마지막 스캔이 기준, 내 화면 판정은 아래 줄
    const scan = (S.paper && S.paper.last_scan) || null;
    const ai = $('#ta-ai');
    const aiSub = $('#ta-ai-sub');
    const when = $('#ta-ai-when');
    const scanAction = scan ? String(scan.action || 'HOLD').toUpperCase() : null;
    const dAct = S.decision ? String(S.decision.action || '').toUpperCase() : null;
    const stamp = scanAction || dAct || '—';
    if (ai) {
      ai.textContent = stamp === '—' ? '—' : `${stamp} · ${ACTION_KO[stamp] || stamp}`;
      ai.className = stamp === 'OPEN' ? 'up' : stamp === 'CLOSE' ? 'down' : stamp === '—' ? '' : 'hold';
    }
    if (when) when.textContent = scan ? clockOf(scan.bar || scan.ts) : '';
    if (aiSub) {
      if (scan) {
        const bits = [`스캔 <b>${clockOf(scan.bar || scan.ts)}</b> · <b>${priceText(scan.price)}</b>`];
        if (scan.regime) bits.push(safe(scan.regime));
        if (dAct && dAct !== scanAction) bits.push(`내 화면 ${safe(dAct)}`);
        aiSub.innerHTML = bits.join(' · ');
      } else if (S.decision) {
        aiSub.innerHTML = `내 화면 ${safe(dAct)} · <b>${clockOf(S.decision.timestamp)}</b> · ${S.decision.entry ? `진입 ${priceText(S.decision.entry)}` : '진입가 없음'}`;
      } else {
        aiSub.textContent = '아직 판단 기록이 없습니다 — ◆ 정밀 분석 또는 30분봉 스캔을 기다립니다';
      }
    }

    // BSL / SSL
    const lv = $('#ta-levels');
    const lvSub = $('#ta-levels-sub');
    if (lv) lv.textContent = S.levels ? `${priceText(S.levels.bsl)} / ${priceText(S.levels.ssl)}` : '—';
    if (lvSub) {
      if (S.levels) {
        const d = (p) => (S.levels.last ? ((p - S.levels.last) / S.levels.last) * 100 : null);
        lvSub.innerHTML = `BSL 위 · SSL 아래 · ${safe(S.levels.basis)} · <b>${pct(d(S.levels.bsl), 1)}</b> / <b>${pct(d(S.levels.ssl), 1)}</b>`;
      } else {
        lvSub.textContent = S.levelsError ? `기준선을 받지 못했습니다 (${S.levelsError})` : '15분봉 스윙을 계산하는 중…';
      }
    }

    // 긴급 정지 버튼
    const killBtn = $('#ta-kill');
    if (killBtn) {
      killBtn.classList.toggle('on', kill.on);
      killBtn.textContent = kill.on ? (kill.front ? '🔒 잠금 해제' : '🔒 백엔드 킬스위치 발동') : '긴급 정지';
      killBtn.title = kill.on ? `${kill.reason} — 누르면 프론트 잠금을 풉니다` : '오토 모드를 즉시 멈추고 이 화면을 잠급니다';
    }

    // 실행 로그
    const list = $('#ta-log-list');
    const logSub = $('#ta-log-sub');
    if (logSub) {
      logSub.textContent = S.logAt
        ? `최근 ${Math.min(S.log.length, LOG_ROWS)}건 · ${ageText(ageMin(new Date(S.logAt).toISOString()))} 갱신`
        : 'data/paper_log.jsonl';
    }
    if (list) {
      const rows = S.log.slice(0, LOG_ROWS);
      list.innerHTML = rows.length
        ? rows.map((rec) => {
            const act = String(rec.action || 'HOLD').toUpperCase();
            const events = Array.isArray(rec.events) ? rec.events.filter(Boolean) : [];
            const filter = rec.filter || {};
            let note = events.join(' · ');
            if (!note && filter.reason) note = String(filter.reason);
            if (!note && rec.signal) note = `${rec.signal.kind || ''} ${rec.signal.label || ''}`.trim();
            if (!note) note = rec.regime ? String(rec.regime) : (act === 'HOLD' ? 'M1 신호 없음' : '');
            const time = logClock(rec);
            const pl = isNum(rec.pnl) ? ` · ${num(rec.pnl, 2)} USDT` : '';
            return `<li><time>${safe(time)}</time><span class="ta-act ${safe(act.toLowerCase())}">${safe(ACTION_KO[act] || act)}</span>`
              + `<span class="ta-note" title="${safe(note)}">${safe(priceText(rec.price))} · ${safe(note)}${safe(pl)}</span></li>`;
          }).join('')
        : `<li class="ta-empty">${S.logAt ? '아직 판단 기록이 없습니다' : '로그를 불러오는 중…'}</li>`;
    }

    // 설정 창이 열려 있으면 연결 상태·기본값도 함께 새로 그린다
    if (!$('#cockpit-settings') || !$('#cockpit-settings').classList.contains('hidden')) paintSettings();
  }

  /* ---------------------------------------------------------------- 상호작용 */
  function setAuto(on) {
    if (killState().on && on) {
      render();
      return;
    }
    S.cfg.auto = !!on;
    saveCfg();
    render();
    if (on) refresh(true);
  }

  function tripKill() {
    S.cfg.auto = false;
    S.cfg.kill = { at: new Date().toISOString(), reason: '조종석 긴급 정지' };
    saveCfg();
    render();
    flashBtn('#ta-kill', '정지 · 잠금됨');
  }

  function releaseKill() {
    S.cfg.kill = null;
    saveCfg();
    render();
    flashBtn('#ta-kill', '잠금 해제');
  }

  function toggleKill() {
    if (S.cfg.kill) releaseKill();
    else tripKill();
  }

  $('#ta-toggle').addEventListener('change', (e) => setAuto(e.target.checked));
  $('#ta-kill').addEventListener('click', () => {
    if (killState().on && !S.cfg.kill) {
      // 백엔드가 발동한 킬스위치는 웹에서 풀 수 없다 — 방법만 안내한다
      flashBtn('#ta-kill', '집 PC에서 해제');
      return;
    }
    toggleKill();
  });
  $('#ta-refresh').addEventListener('click', () => { refresh(true); flashBtn('#ta-refresh', '새로고침 중…'); });
  $('#ta-risk-btn').addEventListener('click', () => openSettings('risk'));
  $('#ta-fold').addEventListener('click', () => {
    S.cfg.collapsed = !section.classList.contains('collapsed');
    saveCfg();
    applyCollapsed();
  });
  function applyCollapsed() {
    section.classList.toggle('collapsed', !!S.cfg.collapsed);
    const b = $('#ta-fold');
    if (b) {
      b.textContent = S.cfg.collapsed ? '▸' : '▾';
      b.setAttribute('aria-label', S.cfg.collapsed ? '조종석 펴기' : '조종석 접기');
    }
  }

  /* ---------------------------------------------------------------- ⚙️ 버튼 + 설정 창 */
  const nav = $('#view-switch');
  if (nav) {
    const gear = document.createElement('button');
    gear.type = 'button';
    gear.id = 'view-settings';
    gear.className = 'view-gear';
    gear.textContent = '⚙️';
    gear.title = '설정 — 연결 상태 · 오토 모드 기본값 · 빠른 관리';
    gear.setAttribute('aria-label', '설정');
    gear.setAttribute('aria-expanded', 'false');
    gear.addEventListener('click', (e) => {
      e.preventDefault();
      openSettings('all');
    });
    nav.appendChild(gear);
  }

  const modal = document.createElement('div');
  modal.id = 'cockpit-settings';
  modal.className = 'ta-modal hidden';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', '설정');
  modal.innerHTML = `
    <div class="ta-modal-box">
      <button class="ta-x" id="cs-close" type="button" aria-label="닫기">✕</button>
      <h2>⚙️ 설정 <span>픽셀 오피스 · 트레이딩</span></h2>

      <section class="ta-sec">
        <h3>연결 상태</h3>
        <dl class="ta-conn">
          <div><dt>웹 버전<i>GitHub Pages · main·gh-pages</i></dt><dd><b>${VERSION}</b></dd></div>
          <div><dt>Cloudflare Worker AI<i>pixel-global.jooyon124.workers.dev</i></dt><dd id="cs-worker">확인 중…</dd></div>
          <div><dt>백엔드 30분봉 AI 모의매매<i>data/paper_state.json</i></dt><dd id="cs-backend">확인 중…</dd></div>
          <div><dt>집 PC 데모 · 테스트넷 오토<i>data/testnet_auto_state.demo.json</i></dt><dd id="cs-pc">—</dd></div>
        </dl>
      </section>

      <section class="ta-sec">
        <h3>오토 모드 기본값</h3>
        <div class="ta-form">
          <label>실전 시드 (USDT)<input id="cs-seed" type="number" min="1" step="1" inputmode="decimal" aria-label="실전 시드"></label>
          <label>1회 리스크 (%)<input id="cs-risk" type="number" min="0.1" max="20" step="0.1" inputmode="decimal" aria-label="1회 리스크"></label>
          <label>최대 레버리지<select id="cs-lev">${LEVS.map((v) => `<option value="${v}">${v}x</option>`).join('')}</select></label>
          <label>가동 세션<select id="cs-session">${Object.keys(SESSIONS).map((k) => `<option value="${k}">${SESSION_OPTS[k]}</option>`).join('')}</select></label>
        </div>
        <p class="ta-hint" id="cs-risk-line">—</p>
        <div class="ta-sec-actions"><button id="cs-reset" type="button">기본값</button><button id="cs-save" class="primary" type="button">저장</button></div>
      </section>

      <section class="ta-sec">
        <h3>빠른 관리</h3>
        <div class="ta-quick">
          <button id="cs-kill" class="kill" type="button">긴급 정지</button>
          <button id="cs-refresh" type="button">데이터 새로고침</button>
        </div>
        <ol class="ta-guide">
          <li><b>데모에서 먼저</b> — 이 탭의 모의 계좌로 신호·체결을 확인합니다(실돈 없음).</li>
          <li><b>30분봉 AI 모의매매</b> — 집 PC에서 <code>python -m backend.paper_loop --loop</code> (30분봉 후보1 + Gemini 심사, 주문 없음).</li>
          <li><b>데모 트레이딩(바이비트 Demo)</b> — <code>backend/.env</code>에 데모 키 · <code>BYBIT_DEMO=true</code> → <code>python -m backend.testnet_auto --loop --demo</code>.</li>
          <li><b>실전 전환은 마지막에</b> — 출금 권한 없는 키, 시드 $50 · 1회 3% · 3x. 긴급 정지는 <code>data/paper_killswitch.json</code>의 <code>manual_stop</code>.</li>
        </ol>
      </section>
      <p class="ta-foot">모의 계좌 · 실제 시세 · 실주문 없음 · 투자 조언이 아님. 집 PC의 테스트넷 상태 파일은 저장소에 올리지 않습니다(로컬 전용).</p>
    </div>`;
  document.body.appendChild(modal);

  function paintSettings() {
    const seed = Number(S.cfg.seed);
    const riskPct = Number(S.cfg.riskPct);
    const seedEl = $('#cs-seed');
    const riskEl = $('#cs-risk');
    const levEl = $('#cs-lev');
    const sesEl = $('#cs-session');
    // 사용자가 입력 중인 값은 새로고침·저장 밖에서 덮어쓰지 않는다
    if (!S.settingsDirty) {
      if (seedEl) seedEl.value = String(seed);
      if (riskEl) riskEl.value = String(riskPct);
      if (levEl) levEl.value = String(S.cfg.maxLev);
      if (sesEl) sesEl.value = S.cfg.session;
    }
    paintHint();

    const worker = $('#cs-worker');
    if (worker) {
      if (S.worker.ok === null) worker.innerHTML = '<b class="dim">확인 중…</b>';
      else if (S.worker.ok) worker.innerHTML = `<b>응답 정상</b> · ${ageText(ageMin(new Date(S.worker.at).toISOString()))} 확인${S.worker.updatedAt ? ` · 값 ${ageText(ageMin(S.worker.updatedAt))}` : ''}`;
      else worker.innerHTML = `<b class="bad">응답 없음</b> · ${safe(S.worker.error || '')}`;
    }

    const backend = $('#cs-backend');
    if (backend) {
      if (S.paper) {
        const a = backendAge();
        const ok = a != null && a < 60;
        backend.innerHTML = `<b class="${ok ? '' : 'dim'}">${ok ? '연결' : '지연'}</b> · ${ageText(a) || '시각 없음'} · ${num(S.paper.scan_count)}회 스캔`;
      } else {
        backend.innerHTML = `<b class="bad">기록 없음</b> · ${safe(S.paperError || '아직 받지 못했습니다')}`;
      }
    }

    const pc = $('#cs-pc');
    if (pc) {
      const local = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)/.test(location.hostname);
      pc.innerHTML = local
        ? '<b class="dim">이 PC에서 열림</b> · 백엔드 상태는 <code>run_testnet_auto_status.bat</code>으로 확인'
        : '<b class="dim">집 PC 로컬 전용</b> · 웹은 이 파일을 읽지 않습니다';
    }

    const killBtn = $('#cs-kill');
    if (killBtn) {
      const k = killState();
      killBtn.classList.toggle('on', k.on);
      killBtn.textContent = k.on ? (k.front ? '🔒 잠금 해제' : '🔒 백엔드 발동') : '긴급 정지';
    }
  }

  // 입력하는 동안 기본값 요약 줄이 바로 바뀐다 (저장 전에도 결과가 보이게)
  function paintHint() {
    const line = $('#cs-risk-line');
    if (!line) return;
    const seed = Number(($('#cs-seed') || {}).value);
    const riskPct = Number(($('#cs-risk') || {}).value);
    const lev = Number(($('#cs-lev') || {}).value) || Number(S.cfg.maxLev);
    const ses = ($('#cs-session') || {}).value || S.cfg.session;
    if (!(seed > 0) || !(riskPct > 0)) {
      line.textContent = '실전 시드와 1회 리스크를 숫자로 입력하세요';
      return;
    }
    line.innerHTML = `실전 기준 <b>시드 $${num(seed)}</b> · 1회 손실 <b>${riskPct}% ($${num(seed * riskPct / 100, 2)})</b> · 최대 <b>${lev}x</b> · <b>${safe(SESSIONS[ses] || '')}</b> — 저장하면 조종석 카드에 바로 반영됩니다`;
  }

  ['#cs-seed', '#cs-risk', '#cs-lev', '#cs-session'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('input', () => { S.settingsDirty = true; paintHint(); });
    el.addEventListener('change', () => { S.settingsDirty = true; paintHint(); });
  });

  function openSettings(focus) {
    document.activeElement && document.activeElement.blur && document.activeElement.blur();
    S.settingsDirty = false;
    paintSettings();
    modal.classList.remove('hidden');
    const gear = $('#view-settings');
    if (gear) gear.setAttribute('aria-expanded', 'true');
    if (focus === 'risk') {
      const el = $('#cs-seed');
      if (el) setTimeout(() => el.focus(), 30);
    }
    if (S.worker.ok === null || Date.now() - S.worker.at > 60000) checkWorker(true).then(paintSettings);
  }

  function closeSettings() {
    modal.classList.add('hidden');
    const gear = $('#view-settings');
    if (gear) gear.setAttribute('aria-expanded', 'false');
  }

  $('#cs-close').addEventListener('click', closeSettings);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeSettings(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeSettings();
  });

  $('#cs-save').addEventListener('click', () => {
    const seed = Number($('#cs-seed').value);
    const risk = Number($('#cs-risk').value);
    const lev = Number($('#cs-lev').value);
    const ses = $('#cs-session').value;
    if (!(seed > 0)) { $('#cs-seed').focus(); return; }
    if (!(risk > 0 && risk <= 20)) { $('#cs-risk').focus(); return; }
    S.cfg.seed = seed;
    S.cfg.riskPct = risk;
    S.cfg.maxLev = LEVS.includes(lev) ? lev : S.cfg.maxLev;
    if (SESSIONS[ses]) S.cfg.session = ses;
    saveCfg();
    S.settingsDirty = false;
    render();
    flashBtn('#cs-save', '저장됨 ✓');
  });
  $('#cs-reset').addEventListener('click', () => {
    S.cfg.seed = DEFAULTS.seed;
    S.cfg.riskPct = DEFAULTS.riskPct;
    S.cfg.maxLev = DEFAULTS.maxLev;
    S.cfg.session = DEFAULTS.session;
    saveCfg();
    S.settingsDirty = false;
    paintSettings();
    render();
    flashBtn('#cs-reset', '기본값 ✓');
  });
  $('#cs-kill').addEventListener('click', () => {
    if (killState().on && !S.cfg.kill) { flashBtn('#cs-kill', '집 PC에서 해제'); return; }
    toggleKill();
    paintSettings();
  });
  $('#cs-refresh').addEventListener('click', () => {
    refresh(true).then(paintSettings);
    flashBtn('#cs-refresh', '새로고침 중…');
  });

  /* ---------------------------------------------------------------- 휴대폰 [오토] 화면 탭 */
  const msv = $('#trade-msv');
  if (msv) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.dataset.tview = 'auto';
    tab.setAttribute('aria-selected', 'false');
    tab.textContent = '오토';
    msv.appendChild(tab);
    tab.addEventListener('click', () => {
      page.dataset.tview = 'auto';
      $$('#trade-msv [data-tview]').forEach((b) => {
        const on = b === tab;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', String(on));
      });
      // 화면이 보이면 지금 값을 다시 받는다(오래된 스냅숏을 보여 주지 않게)
      if (Date.now() - S.logAt > 30000) refresh(true);
    });
  }

  /* ---------------------------------------------------------------- 시작 */
  applyCollapsed();
  render();
  refresh(true);
  // 오토 모드·트레이딩 화면이 보이는 동안 1분마다 상태를 다시 받는다(탭이 가려져 있으면 쉰다)
  setInterval(() => {
    if (document.hidden) return;
    if (!document.documentElement.classList.contains('trading-view')) return;
    if (!S.cfg.auto || killState().on) return;
    if (Date.now() - S.logAt < 55000) return;
    refresh(false);
  }, 15000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (document.documentElement.classList.contains('trading-view') && Date.now() - S.logAt > 55000) refresh(false);
  });
  // 상단 탭을 눌러 데모 트레이딩으로 돌아오면 잠금·배지가 지금 상태와 맞는지 다시 그린다
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (t && t.closest && t.closest('#view-switch [data-view="trading"]')) {
      render();
      if (Date.now() - S.logAt > 55000) refresh(false);
    }
  });

  window.LiteCockpit = {
    refresh: () => refresh(true),
    open: () => openSettings('all'),
    state: S,
  };
})();
