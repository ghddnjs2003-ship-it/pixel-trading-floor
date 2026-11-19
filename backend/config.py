"""환경변수에서 서버 설정을 읽는다. 키는 브라우저로 내려가지 않는다.

LLM_PROVIDER 값
- openai, xai(별칭 grok), groq, gemini, openrouter, cerebras, ollama
- auto: 키가 있는 제공자를 위 순서(무료 먼저)로 고른다. 키가 하나도 없으면 ollama를 시도한다.
- LLM_BASE_URL·LLM_MODEL을 주면 제공자 기본값을 덮어쓴다(자체 호스팅·새 모델 대응).
"""

import os
from pathlib import Path

from .providers import PROVIDER_SPECS, ProviderError

BACKEND_DIR = Path(__file__).resolve().parent

# auto가 고르는 순서 — 무료 한도 제공자를 먼저, 유료는 나중에
AUTO_ORDER = ("groq", "gemini", "cerebras", "openrouter", "openai", "xai", "ollama")

ALIASES = {"grok": "xai", "google": "gemini", "local": "ollama", "llama": "ollama"}


def load_env_file(path=BACKEND_DIR / ".env"):
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _provider_config(name):
    spec = PROVIDER_SPECS[name]
    key_env = spec["key_env"]
    api_key = os.getenv(key_env, "").strip() if key_env else ""
    if key_env and not api_key:
        raise ProviderError(f"{key_env} is not set in backend/.env")
    model = (os.getenv("LLM_MODEL") or os.getenv(spec["model_env"]) or spec["model"]).strip()
    return {
        "provider": name,
        "api_key": api_key,
        "base_url": (os.getenv("LLM_BASE_URL") or spec["base_url"]).strip(),
        "model": model,
        "json_mode": spec["json_mode"],
        "timeout": spec["timeout"],
    }


def _has_key(name):
    key_env = PROVIDER_SPECS[name]["key_env"]
    return True if not key_env else bool(os.getenv(key_env, "").strip())


def get_provider_config():
    load_env_file()
    raw = os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto"
    if raw == "auto":
        for name in AUTO_ORDER:
            if _has_key(name):
                raw = name
                break
        else:
            raise ProviderError("쓸 수 있는 제공자가 없습니다 — backend/.env에 무료 키 하나를 넣거나 "
                                "Ollama(ollama serve)를 켜세요")
    name = ALIASES.get(raw, raw)
    if name not in PROVIDER_SPECS:
        raise ProviderError("LLM_PROVIDER must be one of " + ", ".join(PROVIDER_SPECS) + ", auto")
    return _provider_config(name)


def get_port():
    load_env_file()
    try:
        port = int(os.getenv("PORT", "8765"))
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return port


# ------------------------------------------------------------------ 전략·리스크 설정

def get_strategy_config():
    """백테스트·판정 파라미터. 환경변수로 덮어쓸 수 있게만 열어 둔다(기본값은 v7)."""
    from .strategy import Params
    def _f(key, default):
        try:
            return float(os.getenv(key, default))
        except (TypeError, ValueError):
            return default
    return Params(
        min_rr=_f("V7_MIN_RR", Params.min_rr),
        volume_mult=_f("V7_VOLUME_MULT", Params.volume_mult),
        cci_mode=os.getenv("V7_CCI_MODE", Params.cci_mode),
    )


def get_killswitch_rules():
    from .risk import KillSwitchRules
    def _f(key, default):
        try:
            return float(os.getenv(key, default))
        except (TypeError, ValueError):
            return default
    return KillSwitchRules(
        daily_loss_pct=_f("KILL_DAILY_LOSS_PCT", KillSwitchRules.daily_loss_pct),
        max_consecutive_losses=int(_f("KILL_MAX_CONSECUTIVE_LOSSES", KillSwitchRules.max_consecutive_losses)),
        max_daily_trades=int(_f("KILL_MAX_DAILY_TRADES", KillSwitchRules.max_daily_trades)),
        volatility_pause_atr_pct=_f("KILL_VOLATILITY_ATR_PCT", KillSwitchRules.volatility_pause_atr_pct),
        cooldown_minutes=int(_f("KILL_COOLDOWN_MINUTES", KillSwitchRules.cooldown_minutes)),
    )


# ------------------------------------------------------------------ AI 맥락 필터

def ai_filter_enabled():
    """AI_FILTER=on/off/auto. auto면 제공자 키가 있는지로 정한다(없으면 조용히 끔)."""
    load_env_file()
    raw = os.getenv("AI_FILTER", "auto").strip().lower() or "auto"
    if raw in ("off", "0", "false", "no"):
        return False
    if raw in ("on", "1", "true", "yes"):
        return True
    try:
        get_provider_config()
        return True
    except ProviderError:
        return False


def get_ai_filter_config():
    """AI 맥락 필터 설정. 키·모델은 이 디렉터리의 .env에서만 읽는다(브라우저로 내려가지 않는다)."""
    from .ai_filter import FilterConfig

    load_env_file()

    def _f(key, default):
        try:
            return float(os.getenv(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key, default):
        try:
            return int(float(os.getenv(key, default)))
        except (TypeError, ValueError):
            return default

    on_error = (os.getenv("AI_FILTER_ON_ERROR", "pass").strip().lower() or "pass")
    return FilterConfig(
        provider=os.getenv("AI_FILTER_PROVIDER", "").strip(),
        model=os.getenv("AI_FILTER_MODEL", "").strip(),
        min_interval=_f("AI_FILTER_MIN_INTERVAL", 2.0),
        cache_path=os.getenv("AI_FILTER_CACHE", str(BACKEND_DIR.parent / "data" / "ai_cache.json")).strip(),
        max_calls=_i("AI_FILTER_MAX_CALLS", 0),
        on_error="reject" if on_error == "reject" else "pass",
        loss_threshold=_f("AI_FILTER_THRESHOLD", 60.0),
        prompt_version=os.getenv("AI_FILTER_PROMPT_VERSION", "ctx-v2").strip() or "ctx-v2",
    )


def ai_filter_is_veto():
    """AI_FILTER_VETO=on이면 13인 분석의 최종 진입을 필터 REJECT가 HOLD로 내린다(기본 on)."""
    load_env_file()
    return (os.getenv("AI_FILTER_VETO", "on").strip().lower() or "on") not in ("off", "0", "false", "no")
