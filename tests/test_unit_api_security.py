from __future__ import annotations

from pathlib import Path

import pytest

import api


def test_api_log_sanitizer_removes_urls_credentials_and_common_tokens() -> None:
    secrets = (
        "alice",
        "hunter2",
        "query-secret",
        "bearer-secret",
        "key-secret",
        "pass-secret",
        "token-secret",
        "aws-secret-value",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "sk-abcdefghijk",
    )
    raw = (
        "proxy=https://alice:hunter2@proxy.example:8443/v1?token=query-secret#frag "
        "Authorization: Bearer bearer-secret "
        "api_key='key-secret' password=pass-secret token: token-secret "
        "AWS_SECRET_ACCESS_KEY=aws-secret-value "
        "AKIA" + "ABCDEFGHIJKLMNOP sk-abcdefghijk"
    )

    safe = api._sanitize_log_text(raw)

    assert "https://proxy.example:8443/v1" in safe
    assert "?" not in safe
    assert all(secret not in safe for secret in secrets)


def test_retry_log_is_sanitized_without_changing_retry_or_return_logic(
    monkeypatch, capsys
) -> None:
    expected = {
        "response": "ok",
        "input_tokens": 1,
        "output_tokens": 2,
        "stop_reason": "stop",
    }
    calls = 0
    sleeps: list[float] = []

    def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError(
                "proxy https://alice:hunter2@proxy.example/v1?token=query-secret "
                "api_key=retry-secret Bearer bearer-secret "
                "opaque credential provider-key-not-logged"
            )
        return expected

    monkeypatch.setattr(api, "call_yunwu", fake_call)
    monkeypatch.setattr(api, "API_MAX_RETRIES", 2)
    monkeypatch.setattr(api, "API_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(api, "API_RETRY_MAX_DELAY", 0.0)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    result = api.call_llm(
        "opus",
        "user",
        "system",
        yunwu_key="provider-key-not-logged",
    )
    output = capsys.readouterr().out

    assert result is expected
    assert calls == 2
    assert sleeps == [0.0]
    assert "retry 1/1" in output
    assert "https://proxy.example/v1" in output
    assert all(
        secret not in output
        for secret in (
            "alice",
            "hunter2",
            "query-secret",
            "retry-secret",
            "bearer-secret",
            "provider-key-not-logged",
        )
    )


def test_persisted_api_error_sanitizer_removes_exact_and_heuristic_secrets() -> None:
    raw = (
        "RuntimeError: https://alice:hunter2@example.com/v1?token=query-secret "
        "Authorization: Bearer bearer-secret api_key=field-secret "
        "opaque exact-provider-secret sk-abcdefghijk"
    )
    safe = api.sanitize_api_error_text(raw, ("exact-provider-secret",))
    assert "https://example.com/v1" in safe
    for secret in (
        "alice",
        "hunter2",
        "query-secret",
        "bearer-secret",
        "field-secret",
        "exact-provider-secret",
        "sk-abcdefghijk",
    ):
        assert secret not in safe


def test_sub2api_has_no_baked_in_internal_endpoint() -> None:
    source = Path(api.__file__).read_text(encoding="utf-8")

    assert ("sub2api-api" + ".evomap.work") not in source


def test_sub2api_requires_an_explicit_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(api, "SUB2API_BASE_URL", "")
    monkeypatch.delenv("SUB2API_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="OpenAI-compatible base URL is required"):
        api.call_sub2api(
            "gpt-5.6-sol",
            "user",
            "system",
            "test-key",
        )
