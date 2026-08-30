"""Unified LLM API client (hosted gateways / Gemini direct / local vLLM).

Pruned/adapted from the legacy v1 benchmark runner. Same retry semantics
and same response schema, so results between v1 and v2 are directly
comparable.

Public surface:
    MODEL_REGISTRY              # alias -> (model_id, channel, price_tier)
    PRICE_TABLE                 # model_id -> (input_$/M, output_$/M)
    call_llm(model_key, user, system, **keys) -> dict   # with retry
    extract_python_code(text)   -> str                  # largest AST-parseable block
"""
from __future__ import annotations
import ast
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI, AzureOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False

# Global OpenAI clients (reused across all calls)
_openai_clients: dict = {}
_clients_lock = threading.Lock()
_gemini_vertex_token: str = ""
_gemini_vertex_token_expiry: float = 0.0
_gemini_vertex_lock = threading.Lock()


_LOG_URL_RE = re.compile(
    r"(?P<url>(?:https?|socks5h?|tcp)://[^\s<>\"'`]+)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = (
    r"(?:[A-Za-z0-9]+[_-])*(?:authorization|api[_-]?key|key|token|secret|"
    r"password|passwd|credentials?)(?:[_-][A-Za-z0-9]+)*"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<prefix>[\"']?{_SENSITIVE_FIELD}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<prefix>[\"']?{_SENSITIVE_FIELD}[\"']?\s*[:=]\s*)"
    r"(?P<value>[^\s,;\]}\)]+)",
    re.IGNORECASE,
)
_BEARER_SECRET_RE = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"sk-[0-9A-Za-z_-]{8,}|"
    r"gh[pousr]_[0-9A-Za-z]{10,}|"
    r"xox[baprs]-[0-9A-Za-z-]{10,}|"
    r"eyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}"
    r")\b"
)


def _redact_log_url(raw: str) -> str:
    """Remove URL credentials, query strings, and fragments from log text."""

    trailing = ""
    while raw and raw[-1] in ".,;)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
    except ValueError:
        return "<redacted-url>" + trailing
    if not parsed.scheme or not host:
        return "<redacted-url>" + trailing
    if port is not None:
        host = f"{host}:{port}"
    safe_path = parsed.path
    if re.search(r"(?:key|token|secret|password|credential)", safe_path, re.IGNORECASE):
        safe_path = "/<redacted>"
    safe = urllib.parse.urlunsplit((parsed.scheme, host, safe_path, "", ""))
    return safe + trailing


def _sanitize_log_text(value: object) -> str:
    """Best-effort credential scrubbing for human-facing API retry logs only."""

    text = str(value)
    text = _LOG_URL_RE.sub(lambda match: _redact_log_url(match.group("url")), text)
    text = _BEARER_SECRET_RE.sub(
        lambda match: match.group(0).split()[0] + " <redacted>",
        text,
    )
    text = _QUOTED_SECRET_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"<redacted>{match.group('quote')}"
        ),
        text,
    )
    text = _UNQUOTED_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        text,
    )
    return _COMMON_TOKEN_RE.sub("<redacted>", text)


def _safe_exception_text(
    exc: BaseException,
    exact_secrets: tuple[str, ...] = (),
) -> str:
    return sanitize_api_error_text(
        f"{type(exc).__name__}: {exc}",
        exact_secrets,
    )


def sanitize_api_error_text(
    value: object,
    exact_secrets: tuple[str, ...] = (),
) -> str:
    """Scrub known and heuristic credentials from persisted API diagnostics."""

    text = str(value)
    for secret in sorted(
        {str(value) for value in exact_secrets if value},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "<redacted>")
    return _sanitize_log_text(text)


def _get_openai_client(api_key: str, base_url: str, timeout: int):
    """Get or create an OpenAI client for the given credentials and base_url."""
    if not HAS_OPENAI:
        raise RuntimeError("openai SDK required. Install with: pip install openai")

    client_key = (api_key, base_url)

    # Check if client exists (double-checked locking for thread safety)
    if client_key not in _openai_clients:
        with _clients_lock:
            # Check again after acquiring lock
            if client_key not in _openai_clients:
                _openai_clients[client_key] = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=timeout,
                )

    return _openai_clients[client_key]


def _post_json(url: str, body: dict, headers: dict, timeout: Optional[int] = None) -> dict:
    """POST JSON via urllib so Gemini errors flow through the existing retry policy."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Preserve retry behavior for transient HTTP failures, but surface
        # Google/Bedrock validation messages for permanent bad requests.
        if exc.code == 429 or 500 <= exc.code < 600:
            raise
        body_text = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {exc.code} {exc.reason}: {body_text[:2000]}") from exc


# ── Model registry (synced with v1) ────────────────────────────────────
MODEL_REGISTRY: dict[str, tuple[str, str, str]] = {
    # alias        (model_id,                          channel,       price_tier)
    "opus":         ("claude-opus-4-6",                "yunwu",       "expensive"),
    "sonnet":       ("claude-sonnet-4-6",              "yunwu",       "medium"),
    "haiku":        ("claude-haiku-4-5",               "yunwu",       "cheap"),
    "gpt5_4":       ("gpt-5.4",                        "yunwu",       "medium"),
    "gpt5_mini":    ("gpt-5-mini",                     "yunwu",       "cheap"),
    "gpt5_nano":    ("gpt-5-nano",                     "yunwu",       "free"),
    "gemini_pro":   ("gemini-3.1-pro-preview",         "gemini",      "free"),
    "gemini_flash": ("gemini-3.1-flash-lite-preview",  "gemini",      "free"),
    "qwen_moe":     ("qwen3.5-397b-a17b",              "yunwu",       "cheap"),
    "qwen_coder":   ("qwen3-coder",                    "yunwu",       "expensive"),
    "ds_v3":        ("deepseek-v3.2-exp",              "yunwu",       "medium"),
    "ds_r1":        ("deepseek-r1",                    "yunwu",       "expensive"),
    "qwen_local":   ("qwen3.5-4b",                     "local",       "free"),
    "qwen_local_9b": ("qwen3.5-9b",                    "local",       "free"),
    # SiliconFlow (硅基流动) — OpenAI-compatible /v1/chat/completions.
    # Aliases / model-ids / CNY prices come from
    # https://docs.siliconflow.cn/cn/userguide/capabilities/text-generation
    # Prices in PRICE_TABLE are converted to USD/1M @ ~¥7.20=$1.
    "sf_glm47":     ("Pro/zai-org/GLM-4.7",                "siliconflow", "expensive"),
    "sf_glm51":     ("Pro/zai-org/GLM-5.1",                "siliconflow", "expensive"),
    "sf_kimi_k26":  ("Pro/moonshotai/Kimi-K2.6",           "siliconflow", "expensive"),
    "sf_minimax_m25": ("Pro/MiniMaxAI/MiniMax-M2.5",       "siliconflow", "medium"),
    "sf_qwen_moe":  ("Qwen/Qwen3.5-397B-A17B",             "siliconflow", "cheap"),
    "sf_qwen_coder30b": ("Qwen/Qwen3-Coder-30B-A3B-Instruct", "siliconflow", "cheap"),
    "sf_ds_v4_flash": ("deepseek-ai/DeepSeek-V4-Flash",    "siliconflow", "cheap"),
    "sf_ds_r1":     ("deepseek-ai/DeepSeek-R1",            "siliconflow", "expensive"),
    # Evomap LLM gateway — OpenAI-compatible /v1/chat/completions. Availability
    # depends on the endpoint policy and credentials supplied by the caller.
    "evomap_sonnet": ("evomap-claude-sonnet-4-6",          "evomap",      "medium"),
    # Optional OpenAI-compatible endpoint. Configure its URL explicitly with
    # SUB2API_BASE_URL or the --sub2api-base-url runner argument.
    "gpt5_6_sol":   ("gpt-5.6-sol",                       "sub2api",     "unpriced"),
    # AWS Bedrock Converse API via bearer token. Prefer the global model id;
    # set BEDROCK_REGION if the endpoint should use a region other than us-east-1.
    "bedrock_sonnet": ("global.anthropic.claude-sonnet-4-6", "bedrock",    "medium"),
    "bedrock_sonnet46": ("global.anthropic.claude-sonnet-4-6", "bedrock",  "medium"),
    "bedrock_opus": ("global.anthropic.claude-opus-4-8",    "bedrock",     "expensive"),
    "bedrock_opus48": ("global.anthropic.claude-opus-4-8",  "bedrock",     "expensive"),
}

# USD per 1M tokens (input, output) — used by BudgetTracker.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "global.anthropic.claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6":                (3.00,  15.00),
    "global.anthropic.claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":                 (1.00,  5.00), # nano/free tier
    # Google AI Studio prices for Gemini 3.x preview (≤200K context tier;
    # ≥200K nearly doubles, not modelled). Matches eval/estimate_cost.py.
    "gemini-3.1-pro-preview":           (2.00, 12.00),
    "gemini-3.1-flash-lite-preview":    (0.50,  3.00),
    "qwen3.5_4b":                       (0.00,  0.00),   # local
    "qwen3.5-9b":                       (0.00,  0.00),   # local
    # SiliconFlow CNY → USD @ ¥7.20=$1. Source prices (¥/1M):
    #   Pro/zai-org/GLM-4.7              6.00 / 28.00   (thinking)
    #   Pro/zai-org/GLM-5.1              6.00 / 28.00   (thinking)
    #   Pro/moonshotai/Kimi-K2.6         6.50 / 27.00   (thinking)
    #   Pro/MiniMaxAI/MiniMax-M2.5       2.10 /  8.40   (thinking)
    #   Qwen/Qwen3.5-397B-A17B           2.00 /  1.20
    #   Qwen/Qwen3-Coder-30B-A3B-Instruct 0.70 /  2.80
    #   deepseek-ai/DeepSeek-V4-Flash    1.00 /  2.00
    #   deepseek-ai/DeepSeek-R1          4.00 / 16.00   (thinking)
    "Pro/zai-org/GLM-4.7":                  (0.83,  3.89),
    "Pro/zai-org/GLM-5.1":                  (0.83,  3.89),
    "Pro/moonshotai/Kimi-K2.6":             (0.90,  3.75),
    "Pro/MiniMaxAI/MiniMax-M2.5":           (0.29,  1.17),
    "Qwen/Qwen3.5-397B-A17B":               (0.28,  0.17),
    "Qwen/Qwen3-Coder-30B-A3B-Instruct":    (0.10,  0.39),
    "deepseek-ai/DeepSeek-V4-Flash":        (0.14,  0.28),
    "deepseek-ai/DeepSeek-R1":              (0.56,  2.22),
}

# ── Backends ───────────────────────────────────────────────────────────
LOCAL_BASE_URL = os.environ.get("LOCAL_BASE_URL", "http://localhost:18080/v1")
YUNWU_BASE_URL = "https://yunwu.ai/v1"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
EVOMAP_BASE_URL = "https://llm-gateway.evomap.ai/v1"
# There is deliberately no public endpoint default for this optional channel.
# A private run records the explicitly configured (redacted) URL in its config;
# a caller that selects this model without one receives a clear error.
SUB2API_BASE_URL = os.environ.get("SUB2API_BASE_URL", "").strip()
BEDROCK_REGION = os.environ.get("AWS_BEDROCK_REGION") or os.environ.get("BEDROCK_REGION", "us-east-1")
GEMINI_VERTEX_PROJECT_ID = os.environ.get("GEMINI_VERTEX_PROJECT_ID", "gen-lang-client-0372866462")
GEMINI_VERTEX_LOCATION = os.environ.get("GEMINI_VERTEX_LOCATION", "global")
GEMINI_VERTEX_SERVICE_ACCOUNT_FILE = (
    os.environ.get("GEMINI_VERTEX_SERVICE_ACCOUNT_FILE")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or ""
)
GEMINI_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Per-HTTP-request socket-read timeout (seconds). Kept high enough for Bedrock
# Claude thinking calls, especially if a run overrides the default effort to high.
# Override per-process via env: `GENE_BENCH_API_TIMEOUT=N python -m ...`.
DEFAULT_TIMEOUT = int(os.environ.get("GENE_BENCH_API_TIMEOUT", "1200"))
# Gemini 3.x Pro is a thinking-only model where `maxOutputTokens` is the
# COMBINED cap on thinking + visible answer. The previous 8000-token cap
# was too tight: with thinking on dynamic-default, Pro routinely spent
# ~7700 tokens thinking and got truncated mid-code (observed as 10-27%
# `no_code` failures on v2.5). v1 used 32768 and worked fine — we match
# that headroom here. Combined with explicit Gemini reasoning_effort="low"
# in call_gemini, the answer side always has plenty of room.
DEFAULT_MAX_TOKENS = int(os.environ.get("GENE_BENCH_MAX_TOKENS", "32000"))
GEMINI_REASONING_EFFORT = os.environ.get("GENE_BENCH_GEMINI_REASONING_EFFORT", "low").strip().lower()
GPT_REASONING_EFFORT = os.environ.get("GENE_BENCH_GPT_REASONING_EFFORT", "low").strip().lower()
LOCAL_ENABLE_THINKING = os.environ.get("QWEN_ENABLE_THINKING", "0").lower() in {
    "1", "true", "yes", "on"
}

# Bedrock (Claude 4.x) extended/adaptive thinking. Default "low" = thinking
# ON out of the box. Set GENE_BENCH_BEDROCK_EFFORT=off (or none/0/false/no) to emit
# the byte-identical request body as before -> zero-code rollback. Opus 4.8 requires
# adaptive thinking (manual budget_tokens is rejected); effort must live in a separate
# output_config block. Thinking tokens are folded into outputTokens by Bedrock, so we
# never synthesize a thoughts_tokens value (see call_bedrock_converse).
BEDROCK_EFFORT = os.environ.get("GENE_BENCH_BEDROCK_EFFORT", "low").strip().lower()
BEDROCK_THINKING_TYPE = os.environ.get("GENE_BENCH_BEDROCK_THINKING_TYPE", "adaptive").strip().lower()
BEDROCK_BYPASS_PROXY = os.environ.get("GENE_BENCH_BEDROCK_BYPASS_PROXY", "0").strip().lower() in {
    "1", "true", "yes", "on"
}


def _first_nonempty_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


BEDROCK_PROXY = os.environ.get("GENE_BENCH_BEDROCK_PROXY", "").strip() or _first_nonempty_env(
    "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"
)
_BEDROCK_EFFORT_OFF = {"off", "none", "0", "false", "no", ""}
_BEDROCK_EFFORT_ON = {"low", "medium", "high"}


def call_openai_compatible(model_id: str, user_prompt: str, system_prompt: str,
                           api_key: str, base_url: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                           timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Call any OpenAI-compatible API (yunwu, evomap, siliconflow, local)."""
    # Get or create client (reused globally)
    client = _get_openai_client(api_key, base_url, timeout)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    resp = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""
    return {
        "response": text,
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "stop_reason": choice.finish_reason or "stop",
    }


def call_yunwu(model_id: str, user_prompt: str, system_prompt: str,
               api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """OpenAI-compatible /chat/completions on yunwu.ai proxy."""
    return call_openai_compatible(model_id, user_prompt, system_prompt, api_key, YUNWU_BASE_URL, max_tokens)


def call_evomap(model_id: str, user_prompt: str, system_prompt: str,
                api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """OpenAI-compatible /chat/completions on llm-gateway.evomap.ai."""
    # Cap max_tokens for evomap, use 400s timeout
    safe_max_tokens = min(max_tokens, 16000)
    return call_openai_compatible(model_id, user_prompt, system_prompt, api_key,
                                   EVOMAP_BASE_URL, safe_max_tokens, timeout=400)


def _openai_v1_base_url(value: str) -> str:
    """Normalize either a gateway root URL or an explicit OpenAI /v1 URL."""
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise ValueError("OpenAI-compatible base URL is required")
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


def call_sub2api(model_id: str, user_prompt: str, system_prompt: str,
                 api_key: str, base_url: str = "",
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 reasoning_effort: str | None = None) -> dict:
    """Call an explicitly configured OpenAI-compatible chat endpoint.

    There is intentionally no baked-in endpoint. Private operators must pass
    ``base_url`` or set ``SUB2API_BASE_URL`` in their environment; keeping the
    default empty prevents an internal gateway address from leaking into the
    public source or silently receiving prompts.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    effort = (
        GPT_REASONING_EFFORT
        if reasoning_effort is None
        else str(reasoning_effort).strip().lower()
    )
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if effort not in {"", "default", "auto"}:
        payload["reasoning_effort"] = effort

    configured_base_url = (
        base_url
        or os.environ.get("SUB2API_BASE_URL", "").strip()
        or SUB2API_BASE_URL
    )
    data = _post_json(
        f"{_openai_v1_base_url(configured_base_url)}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
        timeout=DEFAULT_TIMEOUT,
    )
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError(f"invalid chat completion response from {model_id}")
    choice = choices[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    return {
        "response": message.get("content") or "",
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "thoughts_tokens": 0,
        "gpt_reasoning_effort": effort or "default",
        "stop_reason": choice.get("finish_reason") or "stop",
    }


def call_bedrock_converse(model_id: str, user_prompt: str, system_prompt: str,
                          api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                          region: str = BEDROCK_REGION,
                          effort: str | None = None,
                          thinking_type: str | None = None) -> dict:
    """AWS Bedrock Converse API using AWS_BEARER_TOKEN_BEDROCK-style auth.

    `effort` controls Claude extended/adaptive thinking: None falls back to the
    module default GENE_BENCH_BEDROCK_EFFORT ("low"). Pass "off" (or low/medium/
    high) to control per call. When off, the request body is identical to the
    no-thinking baseline.
    """
    if not HAS_REQUESTS:
        raise RuntimeError("requests required. Install with: pip install requests")

    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
    payload: dict = {
        "messages": [
            {
                "role": "user",
                "content": [{"text": user_prompt}],
            }
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    # Inject adaptive thinking unless effort resolves to "off".
    eff = BEDROCK_EFFORT if effort is None else str(effort).strip().lower()
    if eff in _BEDROCK_EFFORT_OFF:
        eff = "off"
    else:
        if eff not in _BEDROCK_EFFORT_ON:
            logger.warning("Invalid Bedrock effort=%r; falling back to low", eff)
            eff = "low"
        think_type = BEDROCK_THINKING_TYPE if thinking_type is None else str(thinking_type).strip().lower()
        payload["additionalModelRequestFields"] = {
            "thinking": {"type": think_type or "adaptive"},
            "output_config": {"effort": eff},
        }

    request_kwargs = {
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        "json": payload,
        "timeout": DEFAULT_TIMEOUT,
    }
    if BEDROCK_BYPASS_PROXY:
        session = requests.Session()
        session.trust_env = False
        resp = session.post(url, **request_kwargs)
    elif BEDROCK_PROXY:
        proxies = {"http": BEDROCK_PROXY, "https": BEDROCK_PROXY}
        resp = requests.post(url, proxies=proxies, **request_kwargs)
    else:
        resp = requests.post(url, **request_kwargs)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = (resp.text or "")[:2000]
        raise requests.HTTPError(f"{exc}; response_body={body}", response=resp) from exc
    data = resp.json()

    message = ((data.get("output") or {}).get("message") or {})
    content = message.get("content") or []
    # Separate visible answer (top-level "text" blocks) from thinking
    # (reasoningContent blocks). The answer join must NOT include thinking.
    text_parts: list[str] = []
    reasoning_chars = 0
    had_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            text_parts.append(part.get("text") or "")
            continue
        rc = part.get("reasoningContent")
        if isinstance(rc, dict):
            had_reasoning = True
            rt = rc.get("reasoningText") or {}
            if isinstance(rt, dict):
                reasoning_chars += len(rt.get("text") or "")
            # rc.get("redactedContent") also means thinking fired, no measurable text.
    text = "".join(text_parts)
    usage = data.get("usage") or {}
    # thoughts_tokens MUST stay 0: Bedrock folds thinking into outputTokens, so a
    # nonzero value would double-count in _token_record. had_reasoning/reasoning_chars
    # are the evidence that thinking actually fired (adaptive high is "almost always").
    return {
        "response": text,
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "thoughts_tokens": 0,
        "had_reasoning": had_reasoning,
        "reasoning_chars": reasoning_chars,
        "bedrock_effort": eff,
        "stop_reason": data.get("stopReason", "stop"),
    }


class SiliconFlowEmptyResponse(RuntimeError):
    """SiliconFlow returned HTTP 200 but with empty `content`.

    Empirically observed under concurrent load on Qwen3.5-MoE / Kimi /
    GLM thinking models: the wire response succeeds (status 200, finish_reason
    `stop`) but `choices[0].message.content` is the empty string while
    completion_tokens is reported as 0 too. Re-issuing the same prompt a
    moment later returns the full code block. This looks like an internal
    SiliconFlow back-pressure path, so we surface it as a transient error
    that the retry policy (`_is_retryable`) should pick up.
    """


def call_siliconflow(model_id: str, user_prompt: str, system_prompt: str,
                     api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """OpenAI-compatible /chat/completions on api.siliconflow.cn."""
    if not HAS_OPENAI:
        raise RuntimeError("openai SDK required. Install with: pip install openai")

    client = OpenAI(api_key=api_key, base_url=SILICONFLOW_BASE_URL)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    resp = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""
    completion_tokens = resp.usage.completion_tokens

    # Defensive: SiliconFlow under concurrent load occasionally returns empty content.
    # Raise a transient error so the retry loop gets another shot.
    if not text.strip() and completion_tokens == 0:
        raise SiliconFlowEmptyResponse(
            f"empty content from {model_id} (finish_reason={choice.finish_reason}, "
            f"completion_tokens=0); will retry"
        )

    return {
        "response": text,
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": completion_tokens,
        "stop_reason": choice.finish_reason or "stop",
    }


def call_local(model_id: str, user_prompt: str, system_prompt: str,
               base_url: str = "", max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """vLLM/OpenAI-compatible local server."""
    if not HAS_OPENAI:
        raise RuntimeError("openai SDK required. Install with: pip install openai")

    if not base_url:
        base_url = LOCAL_BASE_URL

    client = OpenAI(api_key="dummy", base_url=base_url.rstrip('/'))
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    extra_body = {
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": LOCAL_ENABLE_THINKING},
    }

    resp = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )

    choice = resp.choices[0]
    text = choice.message.content or ""
    return {
        "response": text,
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "stop_reason": choice.finish_reason or "stop",
    }


def _get_gemini_vertex_access_token() -> str:
    """Refresh a service-account access token for Vertex AI's OpenAI endpoint."""
    global _gemini_vertex_token, _gemini_vertex_token_expiry
    now = time.time()
    if _gemini_vertex_token and now < _gemini_vertex_token_expiry - 300:
        return _gemini_vertex_token
    with _gemini_vertex_lock:
        now = time.time()
        if _gemini_vertex_token and now < _gemini_vertex_token_expiry - 300:
            return _gemini_vertex_token
        if not HAS_GOOGLE_AUTH:
            raise RuntimeError(
                "google-auth required for Gemini Vertex calls. "
                "Install google-auth and set GEMINI_VERTEX_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS."
            )
        if not GEMINI_VERTEX_SERVICE_ACCOUNT_FILE:
            raise RuntimeError(
                "Set GEMINI_VERTEX_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS "
                "for Gemini Vertex calls."
            )
        credentials = service_account.Credentials.from_service_account_file(
            GEMINI_VERTEX_SERVICE_ACCOUNT_FILE,
            scopes=GEMINI_VERTEX_SCOPES,
        )
        credentials.refresh(GoogleAuthRequest())
        _gemini_vertex_token = str(credentials.token or "")
        expiry = getattr(credentials, "expiry", None)
        _gemini_vertex_token_expiry = expiry.timestamp() if expiry is not None else now + 3300
        return _gemini_vertex_token


def call_gemini(model_id: str, user_prompt: str, system_prompt: str,
                api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Gemini via Vertex AI's OpenAI-compatible endpoint.

    This follows `tests/test_gemini.py`: service-account JSON -> Google access token
    -> `https://aiplatform.googleapis.com/.../endpoints/openapi`.
    The `api_key` parameter is kept for call-site compatibility but is not used
    by this backend.
    """
    if not HAS_OPENAI:
        raise RuntimeError("openai SDK required. Install with: pip install openai")
    access_token = _get_gemini_vertex_access_token()
    base_url = (
        "https://aiplatform.googleapis.com/v1/"
        f"projects/{GEMINI_VERTEX_PROJECT_ID}/locations/{GEMINI_VERTEX_LOCATION}/endpoints/openapi"
    )
    client = OpenAI(api_key=access_token, base_url=base_url, timeout=DEFAULT_TIMEOUT)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    vertex_model_id = model_id if model_id.startswith("google/") else f"google/{model_id}"
    request_kwargs = {
        "model": vertex_model_id,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if GEMINI_REASONING_EFFORT not in {"", "default", "auto"}:
        request_kwargs["reasoning_effort"] = GEMINI_REASONING_EFFORT
    try:
        resp = client.chat.completions.create(**request_kwargs)
    except TypeError:
        # Older OpenAI SDKs may not expose reasoning_effort as a typed keyword,
        # but still allow provider-specific request fields through extra_body.
        effort = request_kwargs.pop("reasoning_effort", None)
        if effort is None:
            raise
        extra_body = dict(request_kwargs.pop("extra_body", {}) or {})
        extra_body["reasoning_effort"] = effort
        request_kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**request_kwargs)
    choice = resp.choices[0]
    usage = resp.usage
    return {
        "response": choice.message.content or "",
        "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        "thoughts_tokens": 0,
        "gemini_backend": "vertex_openai",
        "gemini_reasoning_effort": GEMINI_REASONING_EFFORT or "default",
        "stop_reason": choice.finish_reason or "stop",
    }


# ── Retry policy ───────────────────────────────────────────────────────
# Tuned for cross-border Gemini-via-clash: the proxy occasionally drops
# Retry policy tuned for Bedrock + proxy jitter. With thinking:high, a single
# request can take 5-15 min, so retry delays need to be long enough to let the
# proxy recover without hammering it. Max delay raised from 180s -> 360s.
# Override via env var if you're on a stabler link / want to fail fast.
API_MAX_RETRIES = int(os.environ.get("GENE_BENCH_API_RETRIES", "8"))
API_RETRY_BASE_DELAY = float(os.environ.get("GENE_BENCH_API_RETRY_BASE", "5.0"))
API_RETRY_MAX_DELAY = float(os.environ.get("GENE_BENCH_API_RETRY_MAX", "360.0"))


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ValueError):
        return False
    if isinstance(exc, SiliconFlowEmptyResponse):
        return True
    if HAS_REQUESTS and isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or 500 <= status < 600
    if HAS_REQUESTS and isinstance(exc, requests.RequestException):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return True
    cls = type(exc).__name__
    if cls in {"APIConnectionError", "APITimeoutError", "RateLimitError",
               "InternalServerError", "APIStatusError", "ServiceUnavailableError",
               "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"}:
        return True
    msg = str(exc).lower()
    return ("connection refused" in msg or "connection reset" in msg
            or "temporarily unavailable" in msg or "timed out" in msg)


def call_llm(model_key: str, user_prompt: str, system_prompt: str = "",
             yunwu_key: str = "", gemini_key: str = "",
             siliconflow_key: str = "",
             evomap_key: str = "",
             sub2api_key: str = "",
             bedrock_key: str = "",
             local_base_url: str = "",
             sub2api_base_url: str = "",
             max_tokens: int = DEFAULT_MAX_TOKENS,
             effort: str | None = None,
             thinking_type: str | None = None,
             gpt_reasoning_effort: str | None = None) -> dict:
    """Unified entry with exponential-backoff retry. Raises on terminal failure.

    `effort` and `thinking_type` affect the Bedrock channel. The separate
    `gpt_reasoning_effort` field affects the sub2api GPT channel.
    """
    if model_key not in MODEL_REGISTRY:
        raise ValueError(f"unknown model_key: {model_key}; "
                         f"known: {sorted(MODEL_REGISTRY)}")
    model_id, channel, _ = MODEL_REGISTRY[model_key]

    def _do():
        if channel == "gemini":
            return call_gemini(model_id, user_prompt, system_prompt, gemini_key, max_tokens)
        elif channel == "local":
            return call_local(model_id, user_prompt, system_prompt, local_base_url, max_tokens)
        elif channel == "siliconflow":
            if not siliconflow_key:
                raise ValueError(f"siliconflow_key required for {model_key}")
            return call_siliconflow(model_id, user_prompt, system_prompt,
                                    siliconflow_key, max_tokens)
        elif channel == "evomap":
            if not evomap_key:
                raise ValueError(f"evomap_key required for {model_key}")
            return call_evomap(model_id, user_prompt, system_prompt,
                               evomap_key, max_tokens)
        elif channel == "sub2api":
            if not sub2api_key:
                raise ValueError(f"sub2api_key required for {model_key}")
            return call_sub2api(
                model_id,
                user_prompt,
                system_prompt,
                sub2api_key,
                sub2api_base_url or SUB2API_BASE_URL,
                max_tokens,
                reasoning_effort=gpt_reasoning_effort,
            )
        elif channel == "bedrock":
            if not bedrock_key:
                raise ValueError(f"bedrock_key required for {model_key}")
            return call_bedrock_converse(model_id, user_prompt, system_prompt,
                                         bedrock_key, max_tokens, effort=effort,
                                         thinking_type=thinking_type)
        else:
            if not yunwu_key:
                raise ValueError(f"yunwu_key required for {model_key}")
            return call_yunwu(model_id, user_prompt, system_prompt, yunwu_key, max_tokens)

    last_exc: Optional[BaseException] = None
    for attempt in range(API_MAX_RETRIES):
        try:
            return _do()
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == API_MAX_RETRIES - 1:
                raise
            delay = min(API_RETRY_BASE_DELAY * (2 ** attempt), API_RETRY_MAX_DELAY)
            retry_secrets = (
                yunwu_key,
                gemini_key,
                siliconflow_key,
                evomap_key,
                bedrock_key,
                _gemini_vertex_token,
            )
            print(f"    transient API error ({_safe_exception_text(e, retry_secrets)}); "
                  f"retry {attempt+1}/{API_MAX_RETRIES-1} in {delay:.1f}s")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ── Code extraction ────────────────────────────────────────────────────
def extract_python_code(text: str) -> str:
    """Extract the longest AST-parseable Python code block from an LLM reply.

    Strategy:
      1. Pull all ```python ...``` fenced blocks. If none, fall back to plain
         ```...``` blocks.
      2. AST-parse each candidate; return the LONGEST parseable one.
      3. Final fallback: longest raw block (may be syntactically broken; the
         downstream sandbox will catch and classify as `syntax_error`).
    """
    blocks = re.findall(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        blocks = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return ""
    parseable: list[str] = []
    for b in blocks:
        s = b.strip()
        try:
            ast.parse(s)
            parseable.append(s)
        except SyntaxError:
            pass
    if parseable:
        return max(parseable, key=len)
    return max(blocks, key=len).strip()
