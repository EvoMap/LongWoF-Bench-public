"""Thin LLM client. Supports local vLLM (OpenAI-compatible) and Google Gemini."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


DEFAULT_BASE_URL = os.environ.get("LOCAL_BASE_URL", "http://localhost:8000/v1")
DEFAULT_MODEL = os.environ.get("LOCAL_MODEL", "qwen3-8b")
LOCAL_ENABLE_THINKING = os.environ.get("QWEN_ENABLE_THINKING", "0").lower() in {
    "1", "true", "yes", "on"
}

DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Empty by default: a caller who needs a proxy sets GEMINI_PROXY (and the
# usual http_proxy/https_proxy) themselves. Baking in a local address makes
# every request fail on a machine that has no proxy listening there.
DEFAULT_GEMINI_PROXY = os.environ.get("GEMINI_PROXY", "")


def _default_thinking_budget(model: str) -> Optional[int]:
    """Pick a sensible default thinking_budget per model family.

    - flash: 0 (off — saves tokens; for structured codegen we rarely need CoT)
    - pro:   2048 (Pro REQUIRES thinking >= 128; 2048 is a balanced default)
    - others: None (don't set thinkingConfig — let server defaults apply)

    Override with the GEMINI_THINKING_BUDGET env var or constructor arg.
    """
    env = os.environ.get("GEMINI_THINKING_BUDGET")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            pass
    if "flash" in model:
        return 0
    if "pro" in model:
        return 2048
    return None


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    stop_reason: str
    raw: Optional[dict] = None


class LocalVLLMClient:
    """OpenAI-compatible client against a local vLLM server.

    Mirrors gene-bench/run_gene_bench.py:call_local_vllm() so that whatever
    works in the existing eval pipeline keeps working here.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: int = 600,
    ):
        import httpx
        import openai

        self.model = model
        self.base_url = base_url
        self._http = httpx.Client(trust_env=False, timeout=timeout_s)
        self._client = openai.OpenAI(
            api_key="EMPTY",
            base_url=base_url,
            timeout=timeout_s,
            http_client=self._http,
        )

    def chat(
        self,
        user: str,
        system: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        stop: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})

        t0 = time.time()
        resp = self._client.chat.completions.create(
            model=model or self.model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": LOCAL_ENABLE_THINKING,
                }
            },
        )
        elapsed = time.time() - t0

        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_s=round(elapsed, 2),
            stop_reason=choice.finish_reason or "",
            raw=None,
        )


class GeminiClient:
    """Direct REST client against Google Generative Language API.

    Mirrors gene-bench/run_gene_bench.py:call_gemini() — same proxy handling,
    same JSON body shape, same usage parsing. Adds simple retries.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_GEMINI_MODEL,
        proxy: Optional[str] = DEFAULT_GEMINI_PROXY,
        timeout_s: int = 300,
        max_retries: int = 8,  # bumped — geo-block can need several IP rotations
        thinking_budget: Optional[int] = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            key_file = os.environ.get(
                "GEMINI_KEY_FILE", os.path.expanduser("~/.gemini_key")
            )
            try:
                with open(key_file, "r", encoding="utf-8") as fh:
                    self.api_key = fh.read().strip()
            except OSError:
                pass
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY not set. Export it, pass api_key=..., "
                "or write the key to ~/.gemini_key (or $GEMINI_KEY_FILE)."
            )
        self.model = model
        self.proxy = proxy
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.thinking_budget = (
            thinking_budget if thinking_budget is not None
            else _default_thinking_budget(model)
        )

        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            self._opener = urllib.request.build_opener(handler)
        else:
            self._opener = urllib.request.build_opener()

    @property
    def base_url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta (proxy={self.proxy})"

    def chat(
        self,
        user: str,
        system: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        stop: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        active_model = model or self.model
        active_thinking = (
            self.thinking_budget if model is None
            else _default_thinking_budget(active_model)
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{active_model}:generateContent?key={self.api_key}"
        )
        body: dict = {
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        # Both 2.5 and 3.x preview lines support thinkingConfig; 3.x-pro
        # also REQUIRES thinking >= 128 (server returns empty MAX_TOKENS
        # otherwise). Only set the field when we have a thinking budget.
        if active_thinking is not None and any(
            v in active_model for v in ("2.5", "3.1", "3.0", "-3-")
        ):
            body["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": int(active_thinking),
            }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if stop:
            body["generationConfig"]["stopSequences"] = stop
        data = json.dumps(body).encode("utf-8")

        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                resp = self._opener.open(req, timeout=self.timeout_s)
                payload = json.loads(resp.read().decode("utf-8"))
                elapsed = time.time() - t0
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                code = getattr(e, "code", None)
                body_text = ""
                if code in (400, 401, 403, 404):
                    try:
                        body_text = e.read().decode("utf-8", errors="replace")[:1500]
                    except Exception:
                        pass
                    # Geo-restriction: the upstream HTTP proxy rotated to an IP
                    # in a region Gemini blocks. This is a transient routing
                    # artifact, NOT a configuration error — retry, hoping the
                    # proxy hands us a different exit IP next time.
                    if "User location is not supported" in body_text:
                        if attempt >= self.max_retries:
                            raise RuntimeError(
                                f"Gemini geo-block persisted across "
                                f"{self.max_retries} attempts: {body_text}"
                            ) from e
                        time.sleep(min(2.0 * attempt, 8.0))
                        continue
                    raise RuntimeError(
                        f"Gemini HTTP {code} on model={active_model}: {body_text}"
                    ) from e
                if attempt >= self.max_retries:
                    raise
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                time.sleep(delay)

        text = ""
        if "candidates" in payload and payload["candidates"]:
            parts = payload["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = payload.get("usageMetadata", {})
        finish = ""
        if payload.get("candidates"):
            finish = payload["candidates"][0].get("finishReason", "") or ""
        # Defensive: Pro may return text="" with finish=MAX_TOKENS when its
        # thinking budget is exhausted. Surface it loudly so callers can fail
        # the gate immediately instead of looping forever on parse-empty.
        thoughts = usage.get("thoughtsTokenCount", 0)
        if not text and finish == "MAX_TOKENS":
            text = (
                f"<EMPTY_RESPONSE finish=MAX_TOKENS thoughts={thoughts} "
                f"out=0 in={usage.get('promptTokenCount', 0)}>"
            )
        return LLMResponse(
            text=text,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            latency_s=round(elapsed, 2),
            stop_reason=finish,
            raw=payload,
        )


def get_default_client():
    """Pick a client based on env. SYNTH_LLM = 'gemini' | 'local' (default local)."""
    backend = os.environ.get("SYNTH_LLM", "local").lower()
    if backend == "gemini":
        return GeminiClient()
    return LocalVLLMClient()
