#!/usr/bin/env python3
"""pixel-global Worker 빌드 — 기존 중계 코드(pixel-global.original.js)는 그대로 두고
ai-part.js(✨ Gemini AI 13인 분석)를 뒤에 붙여 worker.js를 만든다.

  python3 worker/build.py     # worker.js 생성
배포는 worker/deploy.py 참고(API 토큰은 저장소에 두지 않는다).
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    orig = open(os.path.join(BASE, 'pixel-global.original.js'), encoding='utf-8').read()
    cut = orig.index('export default{')
    head = orig[:cut].rstrip()          # /global.json · /calendar.json 헬퍼 전부 (그대로)
    ai = open(os.path.join(BASE, 'ai-part.js'), encoding='utf-8').read().strip()
    out = head + '\n\n' + ai + '\n'
    path = os.path.join(BASE, 'worker.js')
    open(path, 'w', encoding='utf-8').write(out)
    print(f'worker.js {len(out)} bytes (원본 헤더 {len(head)}B + AI {len(ai)}B)')


if __name__ == '__main__':
    main()
