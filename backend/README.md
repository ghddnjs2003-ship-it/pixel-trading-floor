# Optional server-side LLM analysis

The default page keeps using the local demo rules. The optional server engine
keeps provider credentials and model calls out of browser JavaScript.

1. Copy `.env.example` to `.env` in this directory and set `LLM_PROVIDER` plus
   the matching `OPENAI_API_KEY` or `XAI_API_KEY`. `.env` is ignored by git.
2. From the project root, run `python -m backend.server`.
3. Open `http://127.0.0.1:8765/index.html?engine=llm` and select **정밀 분석**.

The backend fetches public Bybit linear-perpetual candles, then runs the four
analysts, both BULL/BEAR debate rounds, ACE, three risk reviewers, and PM as
separate provider requests. It streams the same events consumed by the UI over
`/api/stream`. News, fundamentals, and social feeds are not configured here;
agents are instructed to disclose missing evidence instead of inventing it.

The implementation uses only the Python standard library and OpenAI-compatible
chat-completions endpoints. Provider configuration and orchestration are
separate modules so another provider or an independently hosted model can be
added without moving keys into the frontend.
