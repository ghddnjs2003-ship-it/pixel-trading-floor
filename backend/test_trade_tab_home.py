"""트레이딩 탭 홈 화면(2026-11-26 시안) 회귀 테스트.

홍원 님 지시(2026-11-26):
- "시장대시보드는 나두고 그옆에 대모트레이딩 버튼을 바꿔야지"
  → 첫 탭은 '시장 대시보드' 그대로, 두 번째 탭 이름만 '데모 트레이딩' → '트레이딩'.
- 두 번째 탭(data-view="trading")을 시안(downloads/trading_tab_mockup_2026-11-26.png)대로:
  상단 상태 바(미연결 · 가이드 · 실전 안내) → 1 판정 → ⚡ 오토 모드(바이비트 연결하기 · 데모 모드로 체험)
  → 코인(BTC·ETH·SOL·XRP·DOGE) + 타임프레임(15m·30m·1H·4H) + 트레이딩뷰 실시간 차트
     → 최근 익절 · 청산 신호(실제 data/paper_state.json · data/paper_log.jsonl).
- 기존 데모 조종석·수동 주문은 사라지지 않고 '데모 모드로 체험'에서 같은 탭 안에 열린다.
- 가짜 수익률·가짜 데이터 금지, 캐시 버스터 ?v=31.

브라우저를 띄우지 않는 pytest에서는 소스를 읽어 구조·배선을 지킨다.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class TabNameTest(unittest.TestCase):
    """첫 탭은 시장 대시보드 그대로, 두 번째 탭만 '트레이딩'."""

    def setUp(self):
        self.js = read("lite/trading.js")

    def test_first_tab_is_market_dashboard(self):
        self.assertIn('data-view="dash">시장 대시보드</button>', self.js,
                      "첫 탭 이름은 '시장 대시보드'여야 합니다")

    def test_second_tab_is_trading(self):
        self.assertIn('data-view="trading">트레이딩</button>', self.js,
                      "두 번째 탭 이름은 '트레이닝'이 아니라 '트레이딩'이어야 합니다")

    def test_old_demo_trading_tab_label_is_gone(self):
        self.assertNotIn(">데모 트레이딩</button>", self.js,
                         "상단 탭의 '데모 트레이딩' 이름은 '트레이딩'으로 바뀌어야 합니다")

    def test_cockpit_settings_labels_match_v31(self):
        cockpit = read("lite/cockpit.js")
        self.assertIn("const VERSION = 'v31';", cockpit, "⚙️ 설정의 웹 버전 표시")
        self.assertIn("픽셀 오피스 · 트레이딩", cockpit, "설정 창 부제도 탭 이름과 맞춘다")

    def test_page_label_is_trading(self):
        self.assertIn("page.setAttribute('aria-label', '트레이딩');", self.js)

    def test_market_dashboard_screen_is_untouched(self):
        dash = read("lite/dashboard.js")
        self.assertIn("page.setAttribute('aria-label', '시장 대시보드');", dash,
                      "시장 대시보드 화면은 원래대로 두어야 합니다")
        self.assertIn("page.id = 'dash-page';", dash)
        self.assertNotIn("trade-home", dash,
                         "새 시안 UI는 시장 대시보드가 아니라 트레이딩 탭에 들어가야 합니다")


class TradeHomeMarkupTest(unittest.TestCase):
    """시안의 각 칸이 실제로 있고 문구가 맞는지."""

    def setUp(self):
        self.js = read("lite/trading.js")

    def test_status_bar(self):
        self.assertIn('id="th-conn"', self.js)
        self.assertIn('>미연결</span>', self.js)
        self.assertIn('id="th-guide">가이드</button>', self.js)
        self.assertIn('id="th-live-guide">실전 안내</button>', self.js)

    def test_verdict_card(self):
        self.assertIn('<span class="th-num">1</span><h2>판정</h2>', self.js)
        self.assertIn("바이비트 연결 후 AI 최신 분석과 주문 칸이 열립니다.", self.js)
        # 30분봉 AI 판정 요약은 실제 기록의 보조 줄로만 붙는다
        self.assertIn('id="th-verdict-line"', self.js)

    def test_auto_mode_card(self):
        self.assertIn("⚡", self.js)
        self.assertIn("오토 모드</h2>", self.js)
        self.assertIn("AI 오토 모드 상태", self.js)
        self.assertIn("○ 지금 열린 포지션 없음 (연결 대기)", self.js)
        self.assertIn("연결 후 실전·데모 포지션과 가격 정보가 열립니다.", self.js)
        self.assertIn('id="th-connect">바이비트 연결하기</button>', self.js)
        self.assertIn('id="th-demo">데모 모드로 체험</button>', self.js)

    def test_coin_and_frame_buttons(self):
        self.assertIn("코인 3×", self.js)
        self.assertIn("const HOME_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];", self.js)
        self.assertIn("data-coin=\"' + coin + '\"", self.js)
        self.assertIn("const HOME_FRAMES = [['15', '15m'], ['30', '30m'], ['60', '1H'], ['240', '4H']];", self.js)
        self.assertIn("data-frame=\"' + frame[0] + '\"", self.js)
        self.assertIn("${HOME_COIN_BUTTONS}", self.js)
        self.assertIn("${HOME_FRAME_BUTTONS}", self.js)

    def test_tradingview_chart_is_real_widget(self):
        self.assertIn("BYBIT:BTCUSDT.P", self.js)
        self.assertIn("TradingView 실시간 차트", self.js)
        self.assertIn("new window.TradingView.widget({", self.js)
        self.assertIn("container_id: 'th-tv-chart'", self.js)
        # 코인·타임프레임을 바꾸면 위젯을 그 값으로 다시 만든다
        self.assertIn("`BYBIT:${String(coin || 'BTC').toUpperCase()}USDT.P`", self.js)
        self.assertIn("interval: home.frame", self.js)
        self.assertNotIn("<canvas id=\"th-tv-chart\"", self.js,
                         "가짜(캔버스) 차트가 아니라 트레이딩뷰 위젯이어야 합니다")

    def test_signal_card(self):
        self.assertIn("최근 익절 · 청산 신호</h2>", self.js)
        self.assertIn("최근 30분봉 AI · 데모 실제 기록 기준", self.js)
        self.assertIn("% = 증거금 기준 · 거래 비용 반영", self.js)


class TradeHomeDataTest(unittest.TestCase):
    """기록은 실제 파일에서만 읽고, 지어낸 수익률을 두지 않는다."""

    def setUp(self):
        self.js = read("lite/trading.js")

    def test_reads_real_paper_files(self):
        self.assertIn("const HOME_STATE_URL = 'data/paper_state.json';", self.js)
        self.assertIn("const HOME_LOG_URL = 'data/paper_log.jsonl';", self.js)
        self.assertIn("fetch(HOME_STATE_URL + bust", self.js)
        self.assertIn("fetch(HOME_LOG_URL + bust", self.js)

    def test_no_fake_profit_numbers(self):
        # 시안의 예시 숫자(+15.0% · 실전 7$ · 50,000 USDT)를 그대로 박아 두면 가짜 기록이 된다
        for fake in ("+15.0%", "실전 7$", "가동 중 (50,000"):
            self.assertFalse(fake in self.js, "지어낸 수치 '%s'가 남아 있습니다" % fake)

    def test_closed_trades_are_rendered_from_state(self):
        self.assertIn("closed.slice(-3).reverse()", self.js)
        self.assertIn("HOME_REASON_KO", self.js)
        self.assertIn("trade.r", self.js)

    def test_open_paper_position_is_shown(self):
        self.assertIn("paper.position", self.js)
        self.assertIn("보유 중", self.js)
        self.assertIn("(연결 대기)", self.js)

    def test_backtest_reference_is_labelled(self):
        self.assertIn("HOME_BACKTEST", self.js)
        self.assertIn("백테스트", self.js)
        self.assertIn("PF 2.02", self.js)


class TradeModeSwitchTest(unittest.TestCase):
    """데모 조종석·수동 주문은 같은 탭 안에서 열린다."""

    def setUp(self):
        self.js = read("lite/trading.js")

    def test_modes_toggle_on_same_page(self):
        self.assertIn("page.dataset.mode = next", self.js)
        self.assertIn("function setTradeMode(mode)", self.js)
        self.assertIn("page.dataset.mode = 'home'", self.js)

    def test_demo_button_opens_terminal_in_place(self):
        self.assertIn("$('#th-demo').addEventListener('click', () => setTradeMode('demo'))", self.js)
        self.assertIn("if ($('#trade-home-back')) $('#trade-home-back').addEventListener('click', () => setTradeMode('home'))", self.js)

    def test_cockpit_and_order_panel_still_exist(self):
        self.assertIn('<div class="trade-layout">', self.js)
        self.assertIn('class="trade-ticket"', self.js)
        self.assertTrue((ROOT / "lite" / "cockpit.js").exists())
        self.assertIn("section.id = 'trade-auto'", read("lite/cockpit.js"),
                      "오토 모드 조종석은 계속 있어야 합니다")

    def test_connect_button_opens_settings_panel(self):
        self.assertIn("window.LiteCockpit && typeof window.LiteCockpit.open === 'function'", self.js)
        self.assertIn("window.LiteCockpit.open()", self.js)
        self.assertIn("openConnectSettings", self.js)

    def test_guide_modals(self):
        self.assertIn("바이비트 연결 · 이용 가이드", self.js)
        self.assertIn("실전 안내 · 소액 원칙", self.js)
        self.assertIn("50 USDT", self.js)
        self.assertIn("3배", self.js)

    def test_other_entry_points_open_demo_mode(self):
        self.assertIn("show: () => { setTradeMode('demo'); setView('trading'); }", self.js)


class CacheBustTest(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_version_is_31(self):
        self.assertRegex(self.html, r'src="lite/trading\.js\?v=31"')
        self.assertRegex(self.html, r'href="lite/lite\.css\?v=31"')
        self.assertIn("lite/tradehome.css?v=31", self.html)

    def test_no_stale_30(self):
        self.assertNotIn("?v=30", self.html, "캐시 버스터가 v30으로 남아 있습니다")
        self.assertNotIn("?v=29", self.html)

    def test_every_versioned_asset_is_31(self):
        versions = set(re.findall(r"\?v=(\d+)", self.html))
        self.assertEqual(versions, {"31"}, "index.html의 버전 쿼리는 모두 ?v=31이어야 합니다")


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.css = read("lite/tradehome.css")

    def test_home_and_demo_are_mutually_exclusive(self):
        self.assertIn('#trade-page[data-mode="home"] > *:not(.trade-home)', self.css)
        self.assertIn('#trade-page[data-mode="demo"] > .trade-home', self.css)

    def test_global_chip_strip_hidden_on_trade_tab(self):
        self.assertIn("html.trading-view #dash-chips { display: none !important; }", self.css,
                      "트레이딩 탭은 시안처럼 자기 머리말만 보여야 합니다(대시보드 칩 숨김)")

    def test_mobile_hides_ticker_tape(self):
        self.assertIn("html.trading-view #tape { display: none !important; }", self.css,
                      "휴대폰 트레이딩 화면에서 잘리던 시황 테이프를 접는다")
        self.assertIn("html.trading-view #trade-page { inset: 48px 0 0; }", self.css)

    def test_mobile_home_scrolls(self):
        self.assertIn('html.trading-view #trade-page[data-mode="home"] { overflow-y: auto;', self.css,
                      "휴대폰 390px에서 트레이딩 홈이 위아래로 스크롤되어야 합니다")


if __name__ == "__main__":
    unittest.main()
