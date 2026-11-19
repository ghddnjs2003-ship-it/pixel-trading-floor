"""OpenAI 호환 chat-completion 어댑터 — 무료 API와 로컬 Ollama를 같은 코드로 부른다.

왜 한 파일인가
- Groq·Google Gemini·OpenRouter·Cerebras는 모두 OpenAI 형식(/chat/completions)을 제공한다.
- Ollama도 /v1/chat/completions을 제공하므로, 주소만 바꾸면 같은 어댑터로 돈다.
- 그래서 '무료 API로 먼저 검증하고, 되면 로컬로 옮긴다'는 순서를 코드 한 줄(LLM_PROVIDER)로 바꿀 수 있다.

키가 필요한 곳과 필요 없는 곳
- 필요 없음: ollama(로컬, 127.0.0.1:11434)
- 무료 키 필요: groq, gemini, openrouter, cerebras (각 서비스에서 무료 발급)
- 유료: openai, xai

응답 형식
- JSON 모드(response_format)를 지원하지 않는 제공자를 위해 json_mode를 끌 수 있다.
  끄면 프롬프트로 JSON만 쓰라고 지시하고, orchestrator의 파서가 코드블록을 벗겨 읽는다.
"""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# 이름 → (기본 주소, 키 환경변수, 기본 모델, JSON 모드, 기본 타임아웃)
PROVIDER_SPECS = {
    "openai": {
        "base_url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL",
        "model": "gpt-4.1-mini", "json_mode": True, "timeout": 45,
        "label": "OpenAI (유료)",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1/chat/completions",
        "key_env": "XAI_API_KEY", "model_env": "XAI_MODEL",
        "model": "grok-3-mini", "json_mode": True, "timeout": 45,
        "label": "xAI Grok (유료)",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
        "model": "llama-3.3-70b-versatile", "json_mode": True, "timeout": 40,
        "label": "Groq (무료 한도)",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY", "model_env": "GEMINI_MODEL",
        "model": "gemini-2.0-flash", "json_mode": True, "timeout": 45,
        "label": "Google Gemini (무료 한도)",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL",
        "model": "meta-llama/llama-3.3-70b-instruct:free", "json_mode": False, "timeout": 60,
        "label": "OpenRouter (무료 모델)",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_MODEL",
        "model": "llama-3.3-70b", "json_mode": True, "timeout": 40,
        "label": "Cerebras (무료 한도)",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1/chat/completions",
        "key_env": None, "model_env": "OLLAMA_MODEL",
        "model": "qwen2.5:7b-instruct", "json_mode": True, "timeout": 120,
        "label": "로컬 Ollama (키 불필요)",
    },
}


class ProviderError(RuntimeError):
    pass


def provider_names():
    return list(PROVIDER_SPECS)


def describe_providers():
    """어떤 제공자가 지금 바로 쓸 수 있는지(키 유무) 표로 돌려준다."""
    rows = []
    for name, spec in PROVIDER_SPECS.items():
        key_env = spec["key_env"]
        has_key = True if not key_env else bool(os.getenv(key_env, "").strip())
        rows.append({
            "provider": name,
            "label": spec["label"],
            "key_env": key_env,
            "key_present": has_key,
            "default_model": spec["model"],
            "base_url": spec["base_url"],
        })
    return rows


class ChatCompletionProvider:
    """OpenAI 형식 제공자 공통 어댑터. api_key가 없으면 Authorization 헤더를 보내지 않는다(Ollama)."""

    def __init__(self, config, timeout=None):
        self.config = config
        spec = PROVIDER_SPECS.get(config.get("provider", ""), {})
        self.timeout = timeout or int(config.get("timeout") or spec.get("timeout", 45))
        self.json_mode = bool(config.get("json_mode", spec.get("json_mode", True)))
        self.retries = int(config.get("retries", 1))
        self.last_usage = None

    def complete(self, system_prompt, user_prompt):
        payload = {
            "model": self.config["model"],
            "temperature": self.config.get("temperature", 0.4),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        attempt = 0
        while True:
            try:
                return self._post(payload)
            except ProviderError as exc:
                # 429(한도) · 5xx는 잠깐 쉬고 한 번 더. 그 외는 그대로 올린다
                if attempt >= self.retries or not getattr(exc, "retryable", False):
                    raise
                attempt += 1
                time.sleep(2 * attempt)

    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        key = (self.config.get("api_key") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(self.config["base_url"], data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:200]
            except Exception:      # 본문을 못 읽어도 상태코드는 알린다
                pass
            err = ProviderError(f"{self.config.get('provider')} HTTP {exc.code}"
                                + (f" — {detail}" if detail else ""))
            err.retryable = exc.code == 429 or 500 <= exc.code < 600
            raise err from None
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", "timeout")
            err = ProviderError(f"{self.config.get('provider')} 연결 실패: {reason}")
            err.retryable = True
            raise err from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError(f"{self.config.get('provider')} 응답을 읽지 못했습니다") from None

        self.last_usage = data.get("usage") if isinstance(data, dict) else None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.config.get('provider')} 응답에 message가 없습니다") from None
        if isinstance(content, list):     # 일부 제공자는 content를 조각 배열로 준다
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(f"{self.config.get('provider')} 빈 응답")
        return content


def make_provider(config):
    """설정 dict → 어댑터. 지금은 모두 같은 OpenAI 형식이라 한 클래스로 충분하다."""
    provider = config.get("provider")
    if provider not in PROVIDER_SPECS:
        raise ProviderError(f"알 수 없는 제공자: {provider}")
    return ChatCompletionProvider(config)


def probe(config, timeout=15):
    """한 번 짧게 불러 본다 — 키·모델·주소가 실제로 도는지 확인용(분석 흐름 밖)."""
    provider = ChatCompletionProvider(config, timeout=timeout or 15)
    try:
        text = provider.complete("JSON 하나만 출력하세요.", '{"ok": true} 형식으로 답하세요.')
        return {"ok": True, "sample": text[:120], "usage": provider.last_usage}
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    # 키 없이 어떤 제공자가 준비돼 있는지 확인: python -m backend.providers
    from .config import load_env_file
    load_env_file()
    for row in describe_providers():
        mark = "키 불필요" if not row["key_env"] else ("키 있음" if row["key_present"] else "키 없음")
        print(f"{row['provider']:<11} {mark:<7} {row['default_model']:<45} {row['base_url']}")
