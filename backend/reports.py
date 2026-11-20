"""판정 리포트 아카이브 — 분석 1회를 파일 한 장으로 남긴다.

왜: 판정이 화면에서 사라지면 '무엇을 보고 왜 그렇게 했는지'가 없어진다. tauric의 evidence·inputs
(read/absent/failed) 표기를 참고해, **입력 상태·모델·근거·게이트**를 같은 장에 적는다.

저장 위치(기본): `data/reports/{YYYY-MM-DD-HHMMSS}-{SYMBOL}.md` + 같은 이름 `.json`
- .md  : 사람이 읽는 브리핑(복사·공유용)
- .json: 프로그램이 다시 읽는 원본(테스트·복기용)

키·계좌 비밀은 담지 않는다. 모의 계좌 숫자와 판정 문장만 남는다.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SAFE = re.compile(r"[^A-Za-z0-9가-힣_.-]+")


def _safe(text: str, fallback: str = "report") -> str:
    s = SAFE.sub("-", str(text or "")).strip("-")
    return (s[:40] or fallback)


def write_report(run: Dict, out_dir: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
    """분석 1회 → (md 경로, json 경로). 쓰기 실패는 예외를 올리지 않고 error를 돌려준다."""
    now = now or datetime.now(KST)
    base = Path(out_dir) if out_dir else DATA / "reports"
    symbol = _safe(run.get("display") or run.get("symbol") or "SYMBOL", "SYMBOL")
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    md_path = base / f"{stamp}-{symbol}.md"
    json_path = base / f"{stamp}-{symbol}.json"
    payload = {**run, "archived_at": now.isoformat(timespec="seconds")}
    try:
        os.makedirs(str(base), exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(payload, now))
        return {"ok": True, "md": str(md_path), "json": str(json_path)}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "md": None, "json": None}


def render_markdown(run: Dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(KST)
    action = run.get("action") or "HOLD"
    lines: List[str] = [
        f"# {run.get('display') or run.get('symbol')} 판정 리포트 · {action}"
        + (f" {run.get('confidence')}%" if run.get("confidence") is not None else ""),
        "",
        f"- 시각: {now.strftime('%Y-%m-%d %H:%M KST')}",
        f"- 모델: {run.get('model') or '규칙(LLM 없음)'}",
        f"- 종목: {run.get('symbol') or '-'} · 모드: {run.get('mode') or 'algo'}",
    ]
    if run.get("price") is not None:
        lines.append(f"- 기준가: {run.get('price')}")
    gate = run.get("killswitch") or {}
    if gate:
        lines.append(f"- 킬스위치: {'통과' if gate.get('ok') else '차단'} — {gate.get('reason') or '-'}")
    lines += ["", "## 입력 상태", "", "| 입력 | 상태 |", "| --- | --- |"]
    for key, value in (run.get("inputs") or {}).items():
        lines.append(f"| {key} | {value} |")
    if run.get("portfolio"):
        p = run["portfolio"]
        lines += ["", "## 내 책(모의 계좌)", "",
                  f"- 자본 {p.get('자본USDT')} USDT · 누적 {p.get('누적손익USDT')} USDT({p.get('누적손익%')}%)",
                  f"- 오늘 {p.get('오늘손익USDT')} USDT · 체결 {p.get('체결수')}건 · 승률 {p.get('승률%')}%"]
        for pos in p.get("열린포지션") or []:
            lines.append(f"- 보유: {pos.get('종목')} {pos.get('방향')} @ {pos.get('진입가')} · "
                         f"평가손익 {pos.get('평가손익USDT')} USDT")
        if not (p.get("열린포지션") or []):
            lines.append("- 보유 포지션 없음")
    if run.get("regime"):
        r = run["regime"]
        lines += ["", "## 시장 국면", "", f"- 국면: {r.get('국면')}"]
        for group in ("변동성", "추세", "심리"):
            vals = r.get(group) or {}
            if vals:
                lines.append(f"- {group}: " + " · ".join(f"{k} {v}" for k, v in vals.items() if v is not None))
        for ev in r.get("이벤트") or []:
            lines.append(f"- 일정: {ev.get('시각KST')} {ev.get('제목')} ({ev.get('중요도')})")
        for h in r.get("헤드라인") or []:
            lines.append(f"- 헤드라인: {h}")
    if run.get("lessons"):
        lines += ["", "## 최근 복기 교훈", ""] + [f"- {x}" for x in run["lessons"]]
    if run.get("rule"):
        rule = run["rule"]
        lines += ["", "## 규칙(v7)", "",
                  f"- 방향 {rule.get('side')} · {rule.get('kind')} {rule.get('label')} · 등급 {rule.get('grade')}"
                  f" · 손익비 {rule.get('rr')}",
                  f"- 이유: {', '.join(rule.get('reasons') or []) or '-'}"]
    if run.get("ai_filter"):
        ai = run["ai_filter"]
        lines += ["", "## AI 맥락 필터", "",
                  f"- 판정 {ai.get('decision')} · 손절확률 {ai.get('loss_prob')} · {ai.get('reason') or '-'}"]
    if run.get("risk_turns"):
        lines += ["", "## 리스크 3인 교차 심사", ""]
        for turn in run["risk_turns"]:
            lines.append(f"- **{turn.get('role')}**: {turn.get('report') or turn.get('bubble')}")
    lines += ["", "## 최종 판정", "",
              f"- {action}" + (f" · 확신도 {run.get('confidence')}%" if run.get("confidence") is not None else ""),
              f"- 근거: {run.get('rationale') or '-'}"]
    if run.get("blocked"):
        lines.append(f"- 차단: {run['blocked']}")
    lines += ["", "---", "_모의 판정입니다. 주문은 나가지 않았고, 투자 권유가 아닙니다._", ""]
    return "\n".join(lines)


def list_reports(out_dir: Optional[str] = None, limit: int = 10) -> List[Dict]:
    base = Path(out_dir) if out_dir else DATA / "reports"
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.glob("*.md"), reverse=True)[:limit]:
        rows.append({"name": path.name, "path": str(path), "size": path.stat().st_size})
    return rows
