"""웹 화면의 ◆ 정밀 분석 버튼이 '지금 고른 모드'로 분석하는지 지키는 회귀 테스트.

2026-11-26 홍원 님 지적: 상단에서 ⚡스캘핑을 고르고 ◆ 정밀 분석을 누르면, 버튼 핸들러가
모드를 알고리즘으로 되돌려(setMode('algo')·analyze('algo')) 스캘핑 분석이 시작되지 않고
알고리즘 13인 파이프라인이 돌았다. 브라우저를 띄우지 않는 pytest에서는 app.js 소스를 읽어
그 회귀를 막는다(모드 분기·프롬프트·엔진은 lite/brain.js·engine.js·gemini.js가 이미 모드별로
나뉘어 있고, 화면이 모드를 잘못 넘기지만 않으면 스캘핑 5인 흐름이 그대로 돈다).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def handler(source, selector):
    """`qs('#analyze-btn').addEventListener('click', () => {` 부터 짝이 맞는 `});` 까지."""
    start = source.index("qs('%s').addEventListener('click'" % selector)
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("핸들러 끝을 찾지 못했습니다: %s" % selector)


class AnalyzeButtonModeTest(unittest.TestCase):
    def setUp(self):
        self.app = read("app.js")

    def test_analyze_button_uses_selected_mode(self):
        body = handler(self.app, "#analyze-btn")
        self.assertIn("analyze(currentMode)", body,
                      "◆ 정밀 분석은 지금 고른 모드로 분석해야 합니다")

    def test_analyze_button_does_not_force_algo(self):
        body = handler(self.app, "#analyze-btn")
        self.assertNotIn("analyze('algo')", body,
                         "스캘핑을 고른 상태에서 알고리즘으로 되돌리면 안 됩니다")
        self.assertNotIn('analyze("algo")', body)
        self.assertNotIn("setMode('algo')", body,
                         "◆ 정밀 분석이 모드 선택을 바꾸면 스캘핑 선택이 사라집니다")

    def test_mode_toggle_still_selects_scalp(self):
        self.assertIn("if (scalpBtn) scalpBtn.addEventListener('click', () => { setMode('scalp'); applyModeDim('scalp'); });",
                      self.app)

    def test_analyze_defaults_to_current_mode(self):
        # 입력칸 Enter·다른 호출부도 고른 모드를 그대로 쓴다
        self.assertRegex(self.app, r"async function analyze\(mode = currentMode\)")

    def test_page_wires_mode_buttons_and_bumps_cache(self):
        html = read("index.html")
        for sel in ('id="mode-algo"', 'id="mode-scalp"', 'id="analyze-btn"'):
            self.assertIn(sel, html)
        # 모드를 바꾼 app.js가 브라우저 캐시에 남지 않게 버전 쿼리가 붙어 있어야 한다
        self.assertRegex(html, r'src="app\.js\?v=\d+"')


if __name__ == "__main__":
    unittest.main()
