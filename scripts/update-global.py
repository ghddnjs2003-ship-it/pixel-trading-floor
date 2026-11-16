#!/usr/bin/env python3
"""글로벌 시황 스냅숏(data/global.json) 갱신 — 서버 없이 쓰는 폴백 데이터.

브라우저는 미 선물·달러 인덱스·원/달러를 직접 받을 수 없다(야후 파이낸스에 CORS 헤더가 없음).
그래서 GitHub Actions가 10분마다 이 스크립트를 돌려 data/global.json을 저장소에 커밋하고,
웹 페이지는 같은 출처의 그 JSON을 읽는다. 김프·도미넌스·공포탐욕은 페이지가 직접
공개 API에서 실시간으로 받고, 실패했을 때만 이 파일의 값으로 물러난다.

사용법:
    python3 scripts/update-global.py            # data/global.json + data/calendar.json 갱신
    python3 scripts/update-global.py --print    # 파일을 쓰지 않고 결과만 출력
    python3 scripts/update-global.py --calendar # 이번 주 경제 일정(data/calendar.json)만 갱신

출처
    · NQ=F / ES=F / DX-Y.NYB / KRW=X — 야후 파이낸스 차트 API(1일·5분봉) → 24시간 스파크라인
      (야후가 막히면 트레이딩뷰 스캐너 API로 가격·등락률만 받고 스파크는 이전 값을 지킨다)
    · BTC·ETH 달러 가격 — 코인베이스 공개 시세
    · 업비트 KRW-BTC — 업비트 공개 시세 (김프 계산)
    · 원/달러 — open.er-api.com 무료 환율
    · BTC 도미넌스·ETH·총시총 — CoinGecko /api/v3/global
    · 공포탐욕 — alternative.me/fng
    · 이번 주 미국 주요 경제 일정 — ForexFactory 주간 캘린더(nfs.faireconomy.media)
      High·Medium만 골라 한국 시간(KST)으로 바꾸고 지표명을 한국어로 옮겨 data/calendar.json에 담는다.
      (사이트가 CORS를 열지 않아 브라우저가 직접 못 받는다 — global.json과 같은 우회로를 쓴다)

이 파일은 투자 조언이 아닙니다. 값은 각 거래소·지수 제공처의 지연 시세입니다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import re as _re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'global.json')
CAL_OUT = os.path.join(ROOT, 'data', 'calendar.json')
KST = timezone(timedelta(hours=9))
FF_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# 화면에 그리는 미 증시·매크로 네 줄 (순서 고정)
MARKETS = [
    ('nq', 'NQ=F', '나스닥100 선물', 'CME'),
    ('es', 'ES=F', 'S&P500 선물', 'CME'),
    ('dxy', 'DX-Y.NYB', '달러 인덱스', 'ICE'),
    ('fx', 'KRW=X', '원/달러', 'FX'),
]
SPARK_POINTS = 36


def fetch(url, tries=3, timeout=20, headers=None):
    """JSON 받기 — 일시적인 429/5xx는 조금 기다렸다 다시 시도한다."""
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            'User-Agent': UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            **(headers or {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 + attempt * 3)
                continue
            raise
        except Exception as exc:  # noqa: BLE001 — 네트워크 오류는 값이 없다는 뜻
            last = exc
            if attempt < tries - 1:
                time.sleep(2 + attempt * 3)
                continue
            raise
    if last:
        raise last
    raise RuntimeError('no response')


def num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def round_to(value, digits):
    return None if value is None else round(value, digits)


def downsample(points, count=SPARK_POINTS):
    """24시간 흐름을 화면 폭에 맞는 점 수로 줄인다 — 양 끝은 반드시 남긴다."""
    clean = [num(p) for p in points if num(p) is not None]
    if len(clean) <= count:
        return [round_to(p, 4) for p in clean]
    step = (len(clean) - 1) / (count - 1)
    out = [clean[round(i * step)] for i in range(count)]
    out[-1] = clean[-1]
    return [round_to(p, 4) for p in out]


def yahoo_market(symbol):
    """야후 파이낸스 1일 5분봉 → 가격·전일 대비·스파크."""
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/'
           + urllib.parse.quote(symbol, safe='')
           + '?range=1d&interval=5m&includePrePost=true')
    data = fetch(url)
    result = data['chart']['result'][0]
    meta = result.get('meta') or {}
    price = num(meta.get('regularMarketPrice'))
    prev = num(meta.get('chartPreviousClose')) or num(meta.get('previousClose'))
    closes = ((result.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    if price is None and closes:
        price = num(closes[-1])
        if price is not None:
            closes = closes[:-1] + [price]
    if price is None:
        raise ValueError('no price ' + symbol)
    pct = (price / prev - 1) * 100 if prev else None
    return {
        'price': round_to(price, 4),
        'prevClose': round_to(prev, 4),
        'changePct': round_to(pct, 3),
        'spark': downsample(closes),
        'at': datetime.now(KST).isoformat(timespec='seconds'),
        'source': 'yahoo',
    }


def tradingview_market(symbol, kind='america'):
    """야후가 막혔을 때 — 트레이딩뷰 스캐너에서 가격·등락률만."""
    url = f'https://scanner.tradingview.com/{kind}/scan'
    body = json.dumps({
        'symbols': {'tickers': [symbol], 'query': {'types': []}},
        'columns': ['close', 'change', 'prev_close_price'],
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={
        'User-Agent': UA,
        'Content-Type': 'application/json',
        'Origin': 'https://www.tradingview.com',
    })
    with urllib.request.urlopen(req, timeout=20) as res:
        data = json.loads(res.read().decode('utf-8'))
    rows = data.get('data') or []
    if not rows:
        raise ValueError('not found ' + symbol)
    close, change, prev = rows[0]['d'][:3]
    return {
        'price': round_to(num(close), 4),
        'prevClose': round_to(num(prev), 4),
        'changePct': round_to(num(change), 3),
        'spark': None,
        'at': datetime.now(KST).isoformat(timespec='seconds'),
        'source': 'tradingview',
    }


TV_SYMBOLS = {'nq': ('CME_MINI:NQ1!', 'america'), 'es': ('CME_MINI:ES1!', 'america'),
              'dxy': ('TVC:DXY', 'america'), 'fx': ('FX_IDC:USDKRW', 'forex')}


def load_previous():
    try:
        with open(OUT, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — 첫 실행이면 이전 값이 없는 게 정상
        return {}
def build():
    now = datetime.now(KST)
    prev = load_previous()
    prev_markets = (prev.get('markets') or {})
    prev_crypto = (prev.get('crypto') or {})
    warnings = []

    markets = {}
    for key, symbol, _label, _tag in MARKETS:
        entry = None
        try:
            entry = yahoo_market(symbol)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'yahoo {symbol}: {exc}')
            try:
                entry = tradingview_market(*TV_SYMBOLS[key])
            except Exception as exc2:  # noqa: BLE001
                warnings.append(f'tradingview {symbol}: {exc2}')
        old = prev_markets.get(key) or {}
        if entry is None:
            entry = dict(old) if old.get('price') is not None else {'price': None}
            entry['source'] = 'stale'
        if not entry.get('spark') and old.get('spark'):
            entry['spark'] = old['spark']   # 스파크가 없으면 지난 그림을 그대로 쓴다
        markets[key] = entry

    crypto = {}
    # 원/달러 — 화면의 [원/달러] 카드와 김프 계산이 같은 값을 쓰도록 맞춘다.
    # (야후 KRW=X → 없으면 open.er-api.com 환율 → 없으면 지난 값)
    fx = (markets.get('fx') or {}).get('price')
    if not fx:
        try:
            fx_data = fetch('https://open.er-api.com/v6/latest/USD')
            fx = num(fx_data['rates'].get('KRW'))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'fx: {exc}')
    if not fx:
        fx = prev_crypto.get('usdKrw')

    btc_usd = eth_usd = btc_krw = None
    try:
        btc_usd = num(fetch('https://api.exchange.coinbase.com/products/BTC-USD/ticker')['price'])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f'coinbase btc: {exc}')
    try:
        eth_usd = num(fetch('https://api.exchange.coinbase.com/products/ETH-USD/ticker')['price'])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f'coinbase eth: {exc}')
    try:
        rows = fetch('https://api.upbit.com/v1/ticker?markets=KRW-BTC')
        btc_krw = num(rows[0]['trade_price']) if rows else None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f'upbit: {exc}')

    btc_usd = btc_usd or prev_crypto.get('btcUsd')
    eth_usd = eth_usd or prev_crypto.get('ethUsd')
    btc_krw = btc_krw or prev_crypto.get('btcKrw')
    kimchi = None
    if btc_usd and btc_krw and fx:
        kimchi = (btc_krw / (btc_usd * fx) - 1) * 100

    btc_dom = eth_dom = total_mcap = None
    try:
        cg = fetch('https://api.coingecko.com/api/v3/global')['data']
        btc_dom = num(cg['market_cap_percentage']['btc'])
        eth_dom = num(cg['market_cap_percentage']['eth'])
        total_mcap = num(cg['total_market_cap']['usd'])
    except Exception as exc:  # noqa: BLE001 — 호출 제한이면 Coinlore로 물러난다
        warnings.append(f'coingecko: {exc}')
        try:
            row = fetch('https://api.coinlore.net/api/global/')[0]
            btc_dom = num(row.get('btc_d'))
            eth_dom = num(row.get('eth_d'))
            total_mcap = num(row.get('total_mcap'))
        except Exception as exc2:  # noqa: BLE001
            warnings.append(f'coinlore: {exc2}')

    fng = fng_label = None
    try:
        row = fetch('https://api.alternative.me/fng/?limit=1')['data'][0]
        fng = int(row['value'])
        fng_label = row.get('value_classification')
    except Exception as exc:  # noqa: BLE001
        warnings.append(f'fng: {exc}')

    crypto = {
        'btcUsd': round_to(btc_usd, 2),
        'ethUsd': round_to(eth_usd, 2),
        'btcKrw': round_to(btc_krw, 0),
        'usdKrw': round_to(fx, 4),
        'kimchiPct': round_to(kimchi, 3),
        'btcDominance': round_to(btc_dom, 3),
        'ethDominance': round_to(eth_dom, 3),
        'totalMcapUsd': round_to(total_mcap, 0),
        'fng': fng if fng is not None else prev_crypto.get('fng'),
        'fngLabel': fng_label or prev_crypto.get('fngLabel'),
        'at': now.isoformat(timespec='seconds'),
    }
    for key in ('btcUsd', 'ethUsd', 'btcKrw', 'usdKrw', 'kimchiPct',
                'btcDominance', 'ethDominance', 'totalMcapUsd'):
        if crypto.get(key) is None:
            crypto[key] = prev_crypto.get(key)

    stale = [key for key, m in markets.items() if m.get('source') == 'stale']
    return {
        'updatedAt': now.isoformat(timespec='seconds'),
        'updatedAtMs': int(now.timestamp() * 1000),
        'markets': markets,
        'crypto': crypto,
        'stale': stale,
        'warnings': warnings[:8],
        'note': '지연 시세 · 참고용. 투자 조언이 아님.',
    }


# ============================================================ 이번 주 경제 일정

# 지표 이름을 한국어로 — 정확히 모르는 이름은 억지로 번역하지 않고 영어 원문을 남긴다
KO_NAMES = {
    'Warsh': '워시', 'Powell': '파월', 'Williams': '윌리엄스', 'Jefferson': '제퍼슨',
    'Bostic': '보스틱', 'Barr': '바', 'Cook': '쿡', 'Waller': '월러', 'Kashkari': '카시카리',
    'Musalem': '머살렘', 'Goolsbee': '굴스비', 'Daly': '데일리', 'Logan': '로건', 'Macklem': '맥클렘',
}

KO_RULES = [
    (r'^Fed (?:Chairman|Chair) (\w+) Speaks$', r'연준 의장 \1 연설'),
    (r'^Fed (?:Chairman|Chair) (\w+) Testifies$', r'연준 의장 \1 의회 증언'),
    (r'^FOMC Member (\w+) Speaks$', r'FOMC 위원 \1 연설'),
    (r'^FOMC Meeting Minutes$', 'FOMC 의사록'),
    (r'^FOMC Statement$', 'FOMC 성명서'),
    (r'^Federal Funds Rate$', '미 기준금리'),
    (r'^ADP Non-Farm Employment Change$', 'ADP 비농업 고용자수'),
    (r'^Non-Farm Employment Change$', '비농업 고용자수'),
    (r'^(?:Core )?Retail Sales$', None),            # 아래 일반 치환에서 처리
    (r'^(?:Prelim |Revised |Final )?UoM Consumer Sentiment$', None),
    (r'^(?:Prelim |Revised |Final )?UoM Inflation Expectations$', None),
    (r'^Philly Fed Manufacturing Index$', '필라델피아 연은 제조업지수'),
    (r'^Empire State Manufacturing Index$', '뉴욕 연은 제조업지수'),
    (r'^Richmond Manufacturing Index$', '리치먼드 연은 제조업지수'),
    (r'^Unemployment Claims$', '신규 실업수당 청구건수'),
    (r'^Unemployment Rate$', '실업률'),
    (r'^Average Hourly Earnings$', '평균 시간당 임금'),
    (r'^Average Weekly Hours$', '주당 평균 노동시간'),
    (r'^(?:Core )?PCE Price Index$', None),
    (r'^(?:Core )?CPI$', None),
    (r'^(?:Core )?PPI$', None),
    (r'^ISM Manufacturing PMI$', 'ISM 제조업 PMI'),
    (r'^ISM Services PMI$', 'ISM 서비스업 PMI'),
    (r'^ISM Manufacturing Prices$', 'ISM 제조업 물가지수'),
    (r'^JOLTS Job Openings$', 'JOLTS 구인건수'),
    (r'^CB Consumer Confidence$', 'CB 소비자신뢰지수'),
    (r'^Existing Home Sales$', '기존주택 판매'),
    (r'^New Home Sales$', '신규주택 판매'),
    (r'^Pending Home Sales$', '잠정주택 판매지수'),
    (r'^Building Permits$', '건축 허가건수'),
    (r'^Housing Starts$', '주택 착공건수'),
    (r'^Durable Goods Orders$', '내구재 주문'),
    (r'^Core Durable Goods Orders$', '근원 내구재 주문'),
    (r'^Trade Balance$', '무역수지'),
    (r'^Current Account$', '경상수지'),
    (r'^Crude Oil Inventories$', '원유 재고'),
    (r'^Natural Gas Storage$', '천연가스 재고'),
    (r'^GDP$', '국내총생산(GDP)'),
    (r'^Treasury Sec (\w+) Speaks$', r'재무장관 \1 연설'),
    (r'^Speaks$', '연설'),
    (r'^Services PMI$', '서비스업 PMI'),
    (r'^Manufacturing PMI$', '제조업 PMI'),
    (r'^Bank Holiday$', '휴장일'),
]

KO_WORDS = [
    ('Core Retail Sales', '근원 소매판매'),
    ('Retail Sales', '소매판매'),
    ('UoM Consumer Sentiment', '미시간대 소비자심리지수'),
    ('UoM Inflation Expectations', '미시간대 기대인플레이션'),
    ('Core PCE Price Index', '근원 PCE 물가'),
    ('PCE Price Index', 'PCE 물가'),
    ('Core CPI', '근원 소비자물가'),
    ('CPI', '소비자물가'),
    ('Core PPI', '근원 생산자물가'),
    ('PPI', '생산자물가'),
    ('m/m', '(전월대비)'),
    ('q/q', '(전분기대비)'),
    ('y/y', '(전년대비)'),
]

KO_LEFTOVER = _re.compile(r'[A-Za-z]{3,}')


def ko_title(en):
    """미국 지표 이름 → 한국어. 확신이 없는 이름은 영어 원문을 그대로 둔다."""
    title = en.strip()
    for pattern, repl in KO_RULES:
        match = _re.match(pattern, title)
        if not match:
            continue
        if repl is None:
            break                      # 일반 치환 규칙에 맡긴다
        out = match.expand(repl)
        for name_en, name_ko in KO_NAMES.items():
            out = out.replace(name_en, name_ko)
        return out
    out = title
    head = ''
    for word, ko in (('Prelim', '예비'), ('Revised', '수정'), ('Final', '확정'), ('Flash', '속보')):
        if out.startswith(word + ' '):
            out = out[len(word) + 1:]
            head = f' ({ko})'
            break
    for word, ko in KO_WORDS:
        out = out.replace(word, ko)
    out = ' '.join(out.split()) + head
    leftover = [tok for tok in KO_LEFTOVER.findall(out) if tok not in KO_NAMES]
    if leftover:
        return title                     # 반쯤 번역된 이름보다 원문이 낫다
    return out


def build_calendar(source=None):
    """ForexFactory 주간 캘린더 → 이번 주 미국 High·Medium 일정(KST·한국어).

    source: 이미 받아 둔 JSON 파일 경로(선택). 없으면 사이트에서 직접 받는다.
    ForexFactory는 IP당 호출 수를 묶어 두어(429) 자주 부르면 잠시 거절된다.
    """
    if source:
        with open(source, encoding='utf-8') as fh:
            rows = json.load(fh)
    else:
        rows = fetch(FF_URL)
    if not isinstance(rows, list):
        raise ValueError('calendar: unexpected response')

    events = []
    week_dates = []
    for row in rows:
        raw_date = row.get('date')
        if not raw_date:
            continue
        try:
            dt = datetime.fromisoformat(raw_date)
        except ValueError:
            continue
        et_date = dt.date()
        if dt.utcoffset() is not None:
            et_date = dt.astimezone(timezone(timedelta(hours=-5))).date()
        week_dates.append(et_date)
        if str(row.get('country', '')).upper() != 'USD':
            continue
        impact = str(row.get('impact', '')).lower()
        if impact not in ('high', 'medium'):
            continue
        kst_dt = dt.astimezone(KST)
        events.append({
            'at': kst_dt.isoformat(timespec='seconds'),
            'atMs': int(kst_dt.timestamp() * 1000),
            'date': kst_dt.strftime('%Y-%m-%d'),
            'time': kst_dt.strftime('%H:%M'),
            'impact': impact,
            'impactStars': 3 if impact == 'high' else 2,
            'title': ko_title(str(row.get('title', ''))),
            'titleEn': str(row.get('title', '')),
            'forecast': str(row.get('forecast') or ''),
            'previous': str(row.get('previous') or ''),
        })

    events.sort(key=lambda e: e['atMs'])
    week_start = min(week_dates).isoformat() if week_dates else datetime.now(KST).date().isoformat()
    week_end = (datetime.fromisoformat(week_start).date() + timedelta(days=6)).isoformat()
    now = datetime.now(KST)
    return {
        'updatedAt': now.isoformat(timespec='seconds'),
        'source': 'ForexFactory weekly calendar',
        'sourceUrl': FF_URL,
        'timezone': 'Asia/Seoul',
        'weekStart': week_start,
        'weekEnd': week_end,
        'events': events,
        'note': '미국 주요 지표 · 한국 시간(KST) · 예상/이전은 발표 전 전망치 · 투자 조언이 아님',
    }


def save_calendar(payload):
    """일정이 그대로면 파일을 건드리지 않는다 — 10분마다 커밋 소음이 쌓이지 않게."""
    os.makedirs(os.path.dirname(CAL_OUT), exist_ok=True)
    old = None
    if os.path.exists(CAL_OUT):
        try:
            with open(CAL_OUT, encoding='utf-8') as fh:
                old = json.load(fh)
        except Exception:  # noqa: BLE001
            old = None
    if old and old.get('weekStart') == payload.get('weekStart') \
            and json.dumps(old.get('events'), sort_keys=True, ensure_ascii=False) \
            == json.dumps(payload.get('events'), sort_keys=True, ensure_ascii=False):
        return False
    with open(CAL_OUT, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=1) + '\n')
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', action='store_true', help='파일을 쓰지 않고 출력만')
    parser.add_argument('--calendar', action='store_true', help='이번 주 경제 일정만 갱신')
    parser.add_argument('--ff', help='이미 받아 둔 ForexFactory JSON 파일로 일정을 만든다(사이트 호출 제한 회피)')
    args = parser.parse_args()

    if args.calendar:
        calendar = build_calendar(args.ff)
        text = json.dumps(calendar, ensure_ascii=False, indent=1) + '\n'
        if args.print:
            print(text)
            return 0
        changed = save_calendar(calendar)
        print(('저장: ' if changed else '변화 없음 — 그대로 둡니다: ') + CAL_OUT)
        print('이번 주', calendar['weekStart'], '~', calendar['weekEnd'],
              '| 미국 일정', len(calendar['events']), '건')
        for event in calendar['events']:
            print(' ', event['date'], event['time'], ('★★★' if event['impact'] == 'high' else ' ★★'), event['title'])
        return 0

    payload = build()
    text = json.dumps(payload, ensure_ascii=False, indent=1) + '\n'
    if args.print:
        print(text)
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    previous = ''
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as fh:
            previous = fh.read()
    unchanged = False
    if previous:
        try:
            old = json.loads(previous)
            # 값이 거의 그대로면 파일을 건드리지 않는다 — 커밋 소음 방지
            if json.dumps(old.get('markets'), sort_keys=True) == json.dumps(payload.get('markets'), sort_keys=True) \
                    and json.dumps(old.get('crypto'), sort_keys=True) == json.dumps(payload.get('crypto'), sort_keys=True):
                print('변화 없음 — 파일을 그대로 둡니다')
                unchanged = True
        except Exception:  # noqa: BLE001
            pass
    if not unchanged:
        with open(OUT, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print('저장:', OUT)
        print('NQ', payload['markets']['nq'].get('price'), payload['markets']['nq'].get('changePct'),
              '| 김프', payload['crypto'].get('kimchiPct'), '| F&G', payload['crypto'].get('fng'))
    if payload['warnings']:
        print('경고:', '; '.join(payload['warnings']))

    # 이번 주 경제 일정 — 실패해도 시황 스냅숏은 그대로 둔다
    try:
        calendar = build_calendar()
        changed = save_calendar(calendar)
        print(('일정 저장: ' if changed else '일정 변화 없음: ') + CAL_OUT,
              '|', calendar['weekStart'], '~', calendar['weekEnd'], '| 미국 일정', len(calendar['events']), '건')
    except Exception as exc:  # noqa: BLE001
        print('일정을 받지 못했습니다:', exc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
