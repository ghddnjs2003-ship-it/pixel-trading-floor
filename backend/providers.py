"""OpenAI-compatible chat-completion adapters for OpenAI and xAI."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatCompletionProvider:
    def __init__(self, config, timeout=45):
        self.config = config
        self.timeout = timeout

    def complete(self, system_prompt, user_prompt):
        body = json.dumps({
            "model": self.config["model"],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }).encode("utf-8")
        request = Request(
            self.config["base_url"],
            data=body,
            headers={
                "Authorization": f"Bearer {self.config['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"LLM provider returned HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"LLM provider connection failed: {exc.reason if isinstance(exc, URLError) else 'timeout'}") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("LLM provider returned an invalid response") from None

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("LLM provider response is missing message content") from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM provider returned empty message content")
        return content
