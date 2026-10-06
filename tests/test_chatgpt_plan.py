import asyncio
import json
import time
import urllib.parse

import httpx
import pytest

from reelbot.chatgpt_plan import (
    ChatGPTPlanCompletions,
    ChatGPTPlanError,
    REQUIRED_SCOPE,
    build_authorization_url,
    build_responses_payload,
    make_pkce,
)


def credentials(**overrides):
    result = {
        "client_id": "client", "access_token": "secret-access-token",
        "refresh_token": "secret-refresh-token", "expires_at": int(time.time()) + 3600,
        "scopes": [REQUIRED_SCOPE],
    }
    result.update(overrides)
    return result


def test_authorization_url_contains_pkce_and_scopes_without_tokens():
    verifier, challenge = make_pkce()
    url = build_authorization_url(
        client_id="dynamic_agent_client", redirect_uri="http://127.0.0.1:1234/auth/callback",
        host_id="urn:uuid:host", state="state", nonce="nonce", challenge=challenge,
        id_token_hint="prior-id-token",
    )
    parsed = httpx.URL(url)
    assert parsed.host == "auth.openai.com"
    assert parsed.params["code_challenge_method"] == "S256"
    assert parsed.params["code_challenge"] == challenge
    assert REQUIRED_SCOPE in parsed.params["scope"]
    assert parsed.params["id_token_hint"] == "prior-id-token"
    assert verifier not in url


def test_responses_payload_preserves_schema_and_disables_storage():
    payload = build_responses_payload("gpt-model", [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": [
            {"type": "text", "text": "Plan"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc", "detail": "low"}},
        ]},
    ], {"json_schema": {"name": "plan", "strict": True, "schema": {"type": "object"}}})
    assert payload["store"] is False and payload["stream"] is True
    assert payload["instructions"] == "Rules"
    assert payload["input"][0]["content"][1]["detail"] == "low"
    assert payload["text"]["format"]["schema"] == {"type": "object"}


def test_stream_adapter_returns_only_completed_response_and_does_not_log_token():
    events = [
        {"type": "response.output_text.delta", "delta": "{\"ok\":"},
        {"type": "response.output_text.delta", "delta": "true}"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 12, "output_tokens": 3}}},
    ]

    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret-access-token"
        payload = json.loads(request.content)
        assert payload["store"] is False and payload["stream"] is True
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    adapter = ChatGPTPlanCompletions(
        credential_loader=lambda: credentials(), transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(adapter.create(
        model="gpt-model", messages=[{"role": "user", "content": "hello"}],
        response_format={"json_schema": {"name": "x", "schema": {"type": "object"}}},
    ))
    assert result.choices[0].message.content == '{"ok":true}'
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 3


@pytest.mark.parametrize("event", [
    {"type": "response.failed", "response": {"error": {"code": "failed"}}},
    {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "limit"}}},
])
def test_incomplete_stream_is_rejected(event):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(event) + "\n\n")

    adapter = ChatGPTPlanCompletions(
        credential_loader=lambda: credentials(), transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatGPTPlanError, match="without completion"):
        asyncio.run(adapter.create(model="m", messages=[], response_format={}))


def test_plan_provider_does_not_fallback_without_authorized_account():
    adapter = ChatGPTPlanCompletions(credential_loader=lambda: None)
    with pytest.raises(ChatGPTPlanError, match="will not bill MiA"):
        asyncio.run(adapter.create(model="m", messages=[], response_format={}))


def test_refresh_rotates_and_persists_refresh_token():
    saved = []

    def handler(request):
        payload = urllib.parse.parse_qs(request.content.decode())
        assert payload["grant_type"] == ["refresh_token"]
        assert payload["refresh_token"] == ["secret-refresh-token"]
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    expired = credentials(expires_at=0)
    adapter = ChatGPTPlanCompletions(
        credential_loader=lambda: expired, credential_saver=lambda value: saved.append(value),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(adapter._get_credentials())
    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "new-refresh"
    assert saved == [result]
