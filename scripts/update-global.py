#!/usr/bin/env python3
"""글로벌 시황 스냅숏(data/global.json) 갱신 — 서버 없이 쓰는 폴백 데이터.

브라우저는 미 선물·달러 인덱스·원/달러를 직접 받을 수 없다(야후 파이낸스에 CORS 헤더가 없음).
그래서 GitHub Actions가 10분마다 이 스크립트를 돌려 data/global.json을 저장소에 커밋하고,
웹 페이지는 같은 출처의 그 JSON을 읽는다. 김프·도미넌스·공포탐욕은 페이지가 직접
공개 API에서 실시간으로 받고, 실패했을 때만 이 파일의 값으로 물러난다.

사용법:
    python3 scripts/update-global.py            # data/global.json 갱신
    python3 scripts/update-global.py --print    # 파일을 쓰지 않고 결과만 출력

출처
    · NQ=F / ES=F / DX-Y.NYB / KRW=X — 야후 파이낸스 차트 API(1일·5분봉) → 24시간 스파크라인
      (야후가 막히면 트레이딩뷰 스캐너 API로 가격·등락률만 받고 스파크는 이전 값을 지킨다)
    · BTC·ETH 달러 가격 — 코인베이스 공개 시세
    · 업비트 KRW-BTC — 업비트 공개 시세 (김프 계산)
    · 원/달러 — open.er-api.com 무료 환율
    · BTC 도미넌스·ETH·총시총 — CoinGecko /api/v3/global
    · 공포탐욕 — alternative.me/fng

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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'global.json')
KST = timezone(timedelta(hours=9))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', action='store_true', help='파일을 쓰지 않고 출력만')
    args = parser.parse_args()

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
    if previous:
        try:
            old = json.loads(previous)
            # 값이 거의 그대로면 파일을 건드리지 않는다 — 커밋 소음 방지
            if json.dumps(old.get('markets'), sort_keys=True) == json.dumps(payload.get('markets'), sort_keys=True) \
                    and json.dumps(old.get('crypto'), sort_keys=True) == json.dumps(payload.get('crypto'), sort_keys=True):
                print('변화 없음 — 파일을 그대로 둡니다')
                return 0
        except Exception:  # noqa: BLE001
            pass
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write(text)
    print('저장:', OUT)
    print('NQ', payload['markets']['nq'].get('price'), payload['markets']['nq'].get('changePct'),
          '| 김프', payload['crypto'].get('kimchiPct'), '| F&G', payload['crypto'].get('fng'))
    if payload['warnings']:
        print('경고:', '; '.join(payload['warnings']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
