"""내 책(모의 계좌) 문맥 + 매매 복기 메모리.

P0-1 · 포트폴리오 문맥 — AI가 '빈 책'을 보고 승인하지 않게, 지금 잔고·열린 포지션·킬스위치를
        프롬프트에 함께 넣는다. 같은 종목 중복(특히 같은 방향) 진입은 문맥과 게이트 양쪽에서 막는다.
P0-2 · 복기 메모리 — 청산 1건마다 승/패·R배수·사유를 `data/paper_memory.jsonl`에 쌓고
        사람이 읽는 `data/memory.md`를 다시 쓴다. 다음 판정에는 최근 3~5건 교훈만 넣는다.

미래 누수 금지: 백테스트처럼 과거를 재생할 때는 `as_of_ms`(그 신호 봉의 시각)를 주면
그 시점 **이전에 끝난 거래만** 읽는다. 실시간 루프는 as_of 없이 최신 기억을 쓴다.

이 모듈은 주문을 내지 않는다. 읽고 적을 뿐이다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

REASON_KO = {"STOP": "손절", "TARGET": "목표", "TIME": "시간청산",
             "강제 청산": "강제청산", "손절(수동)": "손절", "수동 청산": "수동청산"}


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _ms(value) -> Optional[int]:
    """ISO 문자열·초/밀리초 숫자를 밀리초로 접는다(형식이 다르면 None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v * 1000) if v < 1e11 else int(v)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# --------------------------------------------------------------------- 포트폴리오 문맥

def portfolio_context(state: Optional[Dict], killswitch: Optional[Dict] = None,
                      symbol: Optional[str] = None, now_ms: Optional[int] = None) -> Dict:
    """모의 계좌 상태(paper_state.json) → AI 프롬프트에 넣을 '내 책' 요약.

    없는 값은 지어내지 않는다 — 값이 없으면 None 또는 '없음'으로 남긴다.
    """
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    state = state if isinstance(state, dict) else {}
    initial = state.get("initial_equity")
    equity = state.get("equity", initial)
    closed = state.get("closed") or []
    position = state.get("position")

    today = datetime.fromtimestamp(now_ms / 1000, tz=KST).date()
    today_pnl = 0.0
    today_n = 0
    for trade in closed:
        exit_ms = _ms(trade.get("exit_time"))
        if exit_ms is None:
            continue
        if datetime.fromtimestamp(exit_ms / 1000, tz=KST).date() == today:
            today_pnl += float(trade.get("pnl") or 0.0)
            today_n += 1

    open_rows: List[Dict] = []
    if position:
        last = position.get("last_price") or position.get("entry")
        entry = float(position.get("entry") or 0.0)
        qty = float(position.get("qty") or 0.0)
        unreal = None
        if last is not None and entry and qty:
            sign = 1 if position.get("side") == "LONG" else -1
            unreal = round((float(last) - entry) * qty * sign, 2)
        open_rows.append({
            "종목": state.get("symbol") or symbol,
            "방향": position.get("side"),
            "진입가": entry,
            "현재가": last,
            "수량": qty,
            "평가손익USDT": unreal,
            "손절": position.get("stop"),
            "목표": position.get("target"),
            "보유봉": position.get("bars"),
        })

    stats = state.get("stats") or {}
    pnl_total = round(float(equity or 0) - float(initial), 2) if equity is not None and initial else None
    ctx = {
        "출처": "data/paper_state.json (모의 계좌 · 실주문 아님)",
        "자본USDT": equity,
        "시작자본USDT": initial,
        "누적손익USDT": pnl_total,
        "누적손익%": round(pnl_total / initial * 100, 2) if pnl_total is not None and initial else None,
        "오늘손익USDT": round(today_pnl, 2),
        "오늘청산": today_n,
        "체결수": stats.get("trades"),
        "승률%": stats.get("win_rate_pct"),
        "열린포지션수": len(open_rows),
        "열린포지션": open_rows,
        "보유종목": [r["종목"] for r in open_rows if r.get("종목")],
        "중복진입금지": "한 모의 계좌는 한 종목에 한 포지션만 — 같은 부호(롱/숏) 중복과 반대 방향 동시 보유를 모두 막는다",
    }
    if isinstance(killswitch, dict):
        ctx["킬스위치"] = {
            "진입가능": bool(killswitch.get("ok", killswitch.get("진입가능", True))),
            "사유": killswitch.get("reason") or killswitch.get("사유") or "정상",
        }
    return ctx


def duplicate_entry(state: Optional[Dict], symbol: Optional[str], side: Optional[str]) -> Optional[str]:
    """같은 종목에 이미 포지션이 있으면 사유 문구를 돌려준다(없으면 None).

    같은 방향은 '중복 진입 금지', 반대 방향은 '먼저 청산해야 전환' — 둘 다 한 계좌 한 종목 원칙이다.
    """
    state = state if isinstance(state, dict) else {}
    pos = state.get("position")
    if not pos:
        return None
    held_symbol = state.get("symbol")
    if symbol and held_symbol and str(symbol).upper() != str(held_symbol).upper():
        return None                     # 다른 종목이면 이 계좌에선 문제가 아니다
    held_side = pos.get("side")
    where = f"{held_symbol or '보유 종목'} {held_side}"
    if side and held_side and str(side).upper() == str(held_side).upper():
        return f"{where} 포지션 보유 중 — 같은 방향 중복 진입 금지(추가 매수 아님)"
    return f"{where} 포지션 보유 중 — 반대 방향은 청산 뒤에만"


# --------------------------------------------------------------------- 복기 메모리

def lesson_of(trade: Dict, symbol: Optional[str] = None) -> Dict:
    """청산 1건 → 복기 기록 한 줄(기계가 읽는 형태). 값은 거래 기록 그대로만 쓴다."""
    r = trade.get("r")
    r = round(float(r), 2) if isinstance(r, (int, float)) else None
    reason = trade.get("reason") or ""
    exit_ms = _ms(trade.get("exit_time"))
    return {
        "exit_ms": exit_ms,
        "exit_time": _iso(exit_ms) or trade.get("exit_time"),
        "symbol": trade.get("symbol") or symbol,
        "side": trade.get("side"),
        "reason": reason,
        "reason_ko": REASON_KO.get(reason, reason),
        "r": r,
        "pnl": trade.get("pnl"),
        "entry": trade.get("entry"),
        "exit": trade.get("exit"),
        "bars": trade.get("bars"),
        "ai_decision": trade.get("ai_decision"),
        "ai_reason": trade.get("ai_reason"),
        "win": bool(isinstance(trade.get("pnl"), (int, float)) and trade["pnl"] > 0),
    }


def _line(rec: Dict) -> str:
    r = rec.get("r")
    rtxt = f"{r:+.2f}R" if isinstance(r, (int, float)) else "—"
    side = "롱" if rec.get("side") == "LONG" else ("숏" if rec.get("side") == "SHORT" else "—")
    head = f"{rec.get('exit_time') or ''} {rec.get('symbol') or ''} {side} {rec.get('reason_ko') or ''} {rtxt}".strip()
    reason = (rec.get("ai_reason") or "").strip()
    return f"{head} · {reason[:60]}" if reason else head


def lessons_text(records: Sequence[Dict], n: int = 3) -> List[str]:
    """최근 기록 → 프롬프트에 넣을 3줄(요약 1줄 + 사례 최대 n줄 + 교훈 1줄)."""
    recs = list(records or [])
    if not recs:
        return ["아직 청산한 모의매매가 없습니다 — 복기할 실적이 없으니 성적을 지어내지 마세요."]
    wins = sum(1 for r in recs if r.get("win"))
    rs = [r["r"] for r in recs if isinstance(r.get("r"), (int, float))]
    streak = 0
    for rec in reversed(recs):
        if rec.get("win"):
            break
        streak += 1
    parts = [f"최근 {len(recs)}건 {wins}승 {len(recs) - wins}패"]
    if rs:
        parts.append(f"합 {sum(rs):+.2f}R · 평균 {sum(rs) / len(rs):+.2f}R")
    if streak:
        parts.append(f"연속 손실 {streak}건")
    out = [" · ".join(parts)]
    for rec in recs[-n:]:
        out.append("· " + _line(rec))
    long_loss = sum(1 for r in recs if not r.get("win") and r.get("side") == "LONG")
    short_loss = sum(1 for r in recs if not r.get("win") and r.get("side") == "SHORT")
    stops = sum(1 for r in recs if r.get("reason") == "STOP")
    hints = []
    if stops >= max(2, len(recs) - 1):
        hints.append("손절로 끝난 비중이 높음 — 진입 자리(1R 이내 장애물)를 더 엄격히")
    if long_loss >= 2 and long_loss >= short_loss * 2:
        hints.append("롱 손실이 몰림 — 하락 추세에서의 역추세 롱을 피함")
    if short_loss >= 2 and short_loss >= long_loss * 2:
        hints.append("숏 손실이 몰림 — 상승 추세에서의 역추세 숏을 피함")
    if any((r.get("reason") == "TIME") for r in recs):
        hints.append("시간청산 사례 있음 — 추세가 약하면 4R을 기다리지 않는다")
    if hints:
        out.append("교훈: " + " / ".join(hints))
    return out


class LessonMemory:
    """`data/paper_memory.jsonl` 에 한 줄씩 적고, `data/memory.md` 를 사람이 읽게 다시 쓴다."""

    def __init__(self, path: Optional[str] = None, md_path: Optional[str] = None,
                 data_dir: Optional[str] = None):
        base = Path(data_dir) if data_dir else DATA
        self.path = Path(path) if path else base / "paper_memory.jsonl"
        self.md_path = Path(md_path) if md_path else base / "memory.md"

    # -- 읽기
    def records(self) -> List[Dict]:
        if not self.path.is_file():
            return []
        out: List[Dict] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            return []
        out.sort(key=lambda r: r.get("exit_ms") or 0)
        return out

    def recent(self, n: int = 5, as_of_ms: Optional[int] = None) -> List[Dict]:
        """최근 n건. as_of_ms를 주면 그 시점 **이전에 청산된** 것만(백테스트 미래 누수 차단)."""
        recs = self.records()
        if as_of_ms is not None:
            recs = [r for r in recs if r.get("exit_ms") is not None and r["exit_ms"] < as_of_ms]
        return recs[-n:] if n else recs

    def prompt_lines(self, n: int = 5, as_of_ms: Optional[int] = None) -> List[str]:
        return lessons_text(self.recent(n, as_of_ms), n=3)

    # -- 쓰기
    def record(self, trade: Dict, symbol: Optional[str] = None) -> Dict:
        rec = lesson_of(trade, symbol)
        if rec.get("exit_ms") is None:
            rec["exit_ms"] = int(datetime.now(timezone.utc).timestamp() * 1000)
            rec["exit_time"] = _iso(rec["exit_ms"])
        os.makedirs(str(self.path.parent), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.write_md()
        return rec

    def write_md(self) -> None:
        recs = self.records()
        lines = ["# 모의매매 복기 메모", "",
                 "30분봉 AI 모의매매 루프가 청산마다 자동으로 남깁니다. 다음 판정의 '최근 교훈'은 이 파일의 최근 3~5건에서 만듭니다.",
                 ""]
        if not recs:
            lines.append("_아직 청산한 모의매매가 없습니다._")
        else:
            wins = sum(1 for r in recs if r.get("win"))
            rs = [r["r"] for r in recs if isinstance(r.get("r"), (int, float))]
            lines += [f"- 전체 {len(recs)}건 · {wins}승 {len(recs) - wins}패"
                      + (f" · 합 {sum(rs):+.2f}R · 평균 {sum(rs) / len(rs):+.2f}R" if rs else ""),
                      "",
                      "## 최근 교훈 (프롬프트에 들어가는 문장)", ""]
            lines += [f"- {x}" for x in lessons_text(recs[-5:], n=5)]
            lines += ["", "## 기록", "", "| 시각(UTC) | 종목 | 방향 | 결과 | R | AI 판정 | AI 근거 |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
            for rec in reversed(recs[-100:]):
                r = rec.get("r")
                lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                    rec.get("exit_time") or "", rec.get("symbol") or "", rec.get("side") or "",
                    rec.get("reason_ko") or "", f"{r:+.2f}" if isinstance(r, (int, float)) else "",
                    rec.get("ai_decision") or "", (rec.get("ai_reason") or "").replace("|", "/")[:70]))
        os.makedirs(str(self.md_path.parent), exist_ok=True)
        tmp = str(self.md_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, self.md_path)

    def reset(self) -> None:
        for p in (self.path, self.md_path):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
