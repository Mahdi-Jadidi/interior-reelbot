"""Local Sign in with ChatGPT plan-usage integration.

This is an independently authored OAuth/Responses client for the documented
open-source flow. It does not reuse ChatGPT browser cookies or the licensed
OpenAI DevKit source. Tokens stay in the operating system keyring.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

LOG = logging.getLogger(__name__)
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
RESPONSES_URL = f"{RESOURCE}/responses"
KEYRING_SERVICE = "interior-reelbot.chatgpt-plan"
KEYRING_ACCOUNT = "primary"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "Interior Reelbot"


class ChatGPTPlanError(RuntimeError):
    """A plan-usage failure. Callers must not silently switch billing paths."""


def build_authorization_url(*, client_id: str, redirect_uri: str, host_id: str,
                            state: str, nonce: str, challenge: str,
                            id_token_hint: str | None = None) -> str:
    query: dict[str, str] = {
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if client_id == "dynamic_agent_client":
        query["agent_name_hint"] = APP_NAME
    if id_token_hint:
        query["id_token_hint"] = id_token_hint
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _keyring_module():
    try:
        import keyring
    except ImportError as exc:
        raise ChatGPTPlanError("Install the optional ChatGPT connector: pip install -e '.[chatgpt]'") from exc
    backend = keyring.get_keyring()
    module = type(backend).__module__.lower()
    name = type(backend).__name__.lower()
    if backend.priority <= 0 or "fail" in module or "null" in module or "plaintext" in name:
        raise ChatGPTPlanError("No secure OS credential store is available; configure Windows Credential Manager or Linux Secret Service first")
    return keyring


def load_credentials() -> dict[str, Any] | None:
    value = _keyring_module().get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    return json.loads(value) if value else None


def save_credentials(credentials: dict[str, Any]) -> None:
    _keyring_module().set_password(
        KEYRING_SERVICE, KEYRING_ACCOUNT,
        json.dumps(credentials, ensure_ascii=False, separators=(",", ":")),
    )


def delete_credentials() -> None:
    keyring = _keyring_module()
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass


def _validate_id_token(id_token: str, *, client_id: str, nonce: str,
                       jwks_uri: str, issuer: str) -> dict[str, Any]:
    try:
        import jwt
    except ImportError as exc:
        raise ChatGPTPlanError("Install the optional ChatGPT connector dependencies first") from exc
    header = jwt.get_unverified_header(id_token)
    algorithm = header.get("alg")
    if algorithm not in {"RS256", "ES256"}:
        raise ChatGPTPlanError("OpenAI returned an unsupported ID-token signature algorithm")
    with httpx.Client(timeout=15.0) as client:
        response = client.get(jwks_uri)
        response.raise_for_status()
        jwks = response.json().get("keys", [])
    jwk = next((item for item in jwks if item.get("kid") == header.get("kid")), None)
    if jwk is None:
        raise ChatGPTPlanError("Could not find the signing key for the OpenAI ID token")
    try:
        claims = jwt.decode(
            id_token, jwt.PyJWK.from_dict(jwk).key,
            algorithms=[algorithm], audience=client_id, issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
    except Exception as exc:
        raise ChatGPTPlanError("OpenAI ID-token validation failed") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ChatGPTPlanError("OpenAI ID-token nonce did not match this sign-in")
    return claims


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    callback_path = "/auth/callback"
    result_queue: Any = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.callback_path or self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.result_queue.put({key: values[0] for key, values in params.items()})
        body = ("<meta charset='utf-8'><title>Interior Reelbot</title>"
                "<p>ورود دریافت شد. می‌توانید این پنجره را ببندید و به ترمینال برگردید.</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # OAuth codes and state values must not end up in terminal or log files.
        return


def connect_chatgpt_plan(*, timeout_seconds: int = 300) -> dict[str, Any]:
    """Authorize this local host for ChatGPT plan usage; opens a browser."""
    keyring = _keyring_module()
    try:
        import jwt  # noqa: F401 - check optional dependency before opening browser
    except ImportError as exc:
        raise ChatGPTPlanError("Install the optional ChatGPT connector dependencies first") from exc

    previous = load_credentials()
    host_path = Path.home() / ".config" / "interior-reelbot" / "chatgpt-host-id"
    host_path.parent.mkdir(parents=True, exist_ok=True)
    if host_path.exists():
        host_id = host_path.read_text(encoding="ascii").strip()
    else:
        host_id = "urn:uuid:" + str(__import__("uuid").uuid4())
        temporary = host_path.with_suffix(".tmp")
        temporary.write_text(host_id, encoding="ascii")
        os.chmod(temporary, 0o600)
        temporary.replace(host_path)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = make_pkce()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
    result_queue: Any = __import__("queue").Queue(maxsize=1)
    handler_type = type("CallbackHandler", (_CallbackHandler,), {"result_queue": result_queue})
    server = http.server.HTTPServer(("127.0.0.1", port), handler_type)
    server.timeout = timeout_seconds
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    client_id = previous.get("client_id") if previous else "dynamic_agent_client"
    auth_url = build_authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, host_id=host_id,
        state=state, nonce=nonce, challenge=challenge,
        id_token_hint=previous.get("id_token") if previous else None,
    )
    try:
        opened = webbrowser.open(auth_url, new=1, autoraise=True)
        if not opened:
            raise ChatGPTPlanError("System browser did not open; no credentials were changed. Retry on a desktop with a browser.")
        print("در صفحهٔ امن OpenAI وارد شوید و اجازهٔ استفاده از سهمیهٔ ChatGPT را تأیید کنید.")
        thread.join(timeout_seconds + 2)
        if thread.is_alive() or result_queue.empty():
            raise ChatGPTPlanError("ChatGPT sign-in timed out; no credentials were changed")
        callback = result_queue.get_nowait()
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise ChatGPTPlanError("ChatGPT sign-in state did not match; authorization was discarded")
        if callback.get("error"):
            raise ChatGPTPlanError("ChatGPT sign-in was declined or failed; no credentials were changed")
        code = callback.get("code")
        issued_client_id = callback.get("client_id") or client_id
        if not code or not issued_client_id or issued_client_id == "dynamic_agent_client":
            raise ChatGPTPlanError("OpenAI did not complete dynamic client registration")
        if previous and callback.get("client_id") and callback["client_id"] != previous["client_id"]:
            raise ChatGPTPlanError("OpenAI returned a different client registration; existing credentials were kept")

        with httpx.Client(timeout=30.0) as client:
            discovery_response = client.get(f"{ISSUER}/.well-known/openid-configuration")
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            if discovery.get("issuer") != ISSUER:
                raise ChatGPTPlanError("OpenAI issuer discovery did not match the expected issuer")
            token_response = client.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "client_id": issued_client_id,
                "code": code, "code_verifier": verifier,
                "redirect_uri": redirect_uri, "resource": RESOURCE,
            })
            if token_response.is_error:
                raise ChatGPTPlanError(f"OpenAI token exchange failed (HTTP {token_response.status_code})")
            tokens = token_response.json()

        granted_scopes = set(str(tokens.get("scope", callback.get("scope", ""))).split())
        if REQUIRED_SCOPE not in granted_scopes:
            raise ChatGPTPlanError("Sign-in succeeded, but ChatGPT plan usage was not authorized; enable the plan-usage permission and reconnect")
        id_token = tokens.get("id_token")
        if not id_token:
            raise ChatGPTPlanError("OpenAI did not return an ID token; existing credentials were kept")
        claims = _validate_id_token(
            id_token, client_id=issued_client_id, nonce=nonce,
            jwks_uri=discovery["jwks_uri"], issuer=discovery["issuer"],
        )
        if previous and claims.get("sub") != previous.get("subject"):
            raise ChatGPTPlanError("The selected ChatGPT account changed; existing credentials were kept")
        expires_in = int(tokens.get("expires_in", 3600))
        credentials = {
            "client_id": issued_client_id,
            "ext_agent_host_id": host_id,
            "issuer": discovery["issuer"],
            "subject": claims["sub"],
            "email": claims.get("email", ""),
            "id_token": id_token,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": int(time.time()) + expires_in,
            "scopes": sorted(granted_scopes),
        }
        if not credentials["refresh_token"]:
            raise ChatGPTPlanError("OpenAI did not grant offline refresh access; reconnect and approve the requested permissions")
        save_credentials(credentials)
        return {"email": credentials["email"], "plan_usage_enabled": True,
                "expires_at": credentials["expires_at"]}
    finally:
        server.server_close()
        if thread.is_alive():
            thread.join(0.1)


def connection_status() -> dict[str, Any] | None:
    credentials = load_credentials()
    if not credentials:
        return None
    return {
        "connected": True,
        "email": credentials.get("email", ""),
        "plan_usage_enabled": REQUIRED_SCOPE in credentials.get("scopes", []),
        "expires_at": credentials.get("expires_at"),
    }


def openai_auth_cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Connect or disconnect this local Reelbot from ChatGPT plan usage")
    parser.add_argument("action", choices=("connect", "status", "models", "disconnect"))
    args = parser.parse_args()
    try:
        if args.action == "connect":
            print(json.dumps(connect_chatgpt_plan(), ensure_ascii=False, indent=2))
        elif args.action == "status":
            print(json.dumps(connection_status() or {"connected": False}, ensure_ascii=False, indent=2))
        elif args.action == "models":
            print(json.dumps(asyncio.run(ChatGPTPlanCompletions().list_models()), ensure_ascii=False, indent=2))
        else:
            delete_credentials()
            print("ChatGPT credentials were removed from this device.")
    except ChatGPTPlanError as exc:
        raise SystemExit(str(exc)) from exc


def build_responses_payload(model: str, messages: list[dict[str, Any]], response_format: dict[str, Any]) -> dict[str, Any]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role in {"system", "developer"}:
            instructions.append(str(content))
            continue
        parts = [{"type": "input_text", "text": content}] if isinstance(content, str) else []
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    parts.append({"type": "input_text", "text": item.get("text", "")})
                elif item.get("type") == "image_url":
                    image = item.get("image_url", {})
                    parts.append({"type": "input_image", "image_url": image.get("url", ""),
                                  "detail": image.get("detail", "auto")})
        input_items.append({"role": role if role in {"user", "assistant"} else "user", "content": parts})
    schema_spec = response_format.get("json_schema", {})
    payload = {
        "model": model,
        "instructions": "\n\n".join(instructions),
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if schema_spec:
        payload["text"] = {"format": {
            "type": "json_schema",
            "name": schema_spec.get("name", "structured_output"),
            "strict": schema_spec.get("strict", True),
            "schema": schema_spec.get("schema", {}),
        }}
    return payload


class ChatGPTPlanCompletions:
    """Small Chat Completions-shaped adapter over the supported Responses API."""

    def __init__(self, *, credential_loader=load_credentials, credential_saver=save_credentials,
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 60.0):
        self.credential_loader = credential_loader
        self.credential_saver = credential_saver
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def _get_credentials(self) -> dict[str, Any]:
        credentials = await asyncio.to_thread(self.credential_loader)
        if not credentials:
            raise ChatGPTPlanError("Connect ChatGPT first with `reelbot-chatgpt connect`; this provider will not bill MiA as a fallback")
        if REQUIRED_SCOPE not in credentials.get("scopes", []):
            raise ChatGPTPlanError("This ChatGPT account has not authorized plan usage; reconnect and approve the plan-usage permission")
        if int(credentials.get("expires_at", 0)) <= int(time.time()) + 90:
            refresh_token = credentials.get("refresh_token")
            if not refresh_token:
                raise ChatGPTPlanError("ChatGPT session expired; reconnect with `reelbot-chatgpt connect`")
            async with httpx.AsyncClient(timeout=20.0, transport=self.transport) as client:
                response = await client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "client_id": credentials["client_id"],
                    "refresh_token": refresh_token, "resource": RESOURCE,
                })
            if response.is_error:
                raise ChatGPTPlanError(f"ChatGPT session refresh failed (HTTP {response.status_code}); reconnect")
            refreshed = response.json()
            credentials = {**credentials,
                           "access_token": refreshed["access_token"],
                           "refresh_token": refreshed.get("refresh_token", refresh_token),
                           "expires_at": int(time.time()) + int(refreshed.get("expires_in", 3600)),
                           "scopes": sorted(set(str(refreshed.get("scope", " ".join(credentials["scopes"]))).split()))}
            await asyncio.to_thread(self.credential_saver, credentials)
        return credentials

    async def create(self, *, model: str, messages: list[dict[str, Any]],
                     response_format: dict[str, Any], **_: Any) -> Any:
        credentials = await self._get_credentials()
        payload = build_responses_payload(model, messages, response_format)
        output: list[str] = []
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        completed = False
        headers = {"Authorization": "Bearer " + credentials["access_token"],
                   "Accept": "text/event-stream", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, read=self.timeout_seconds), transport=self.transport) as client:
                async with client.stream("POST", RESPONSES_URL, headers=headers, json=payload) as response:
                    if response.is_error:
                        await response.aread()
                        # Never echo upstream error bodies: gateways may include
                        # identifiers or credential-adjacent diagnostic data.
                        raise ChatGPTPlanError(f"ChatGPT Responses API HTTP {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        event = json.loads(data)
                        kind = event.get("type", "")
                        if kind == "response.output_text.delta":
                            output.append(event.get("delta", ""))
                        elif kind == "response.completed":
                            completed = True
                            final = event.get("response", {})
                            response_usage = final.get("usage", {})
                            usage = {"input_tokens": int(response_usage.get("input_tokens", 0)),
                                     "output_tokens": int(response_usage.get("output_tokens", 0))}
                            if not output:
                                for item in final.get("output", []):
                                    for part in item.get("content", []):
                                        if part.get("type") == "output_text":
                                            output.append(part.get("text", ""))
                        elif kind in {"response.failed", "response.incomplete"}:
                            detail = event.get("response", {}).get("error") or event.get("response", {}).get("incomplete_details") or {}
                            code = detail.get("code") or detail.get("reason") or kind
                            raise ChatGPTPlanError(f"ChatGPT request ended without completion ({code})")
        except httpx.TimeoutException as exc:
            raise ChatGPTPlanError(f"ChatGPT request exceeded {self.timeout_seconds:g}s") from exc
        if not completed:
            raise ChatGPTPlanError("ChatGPT stream ended without response.completed")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="".join(output)))],
            usage=SimpleNamespace(**usage),
        )

    async def list_models(self) -> list[dict[str, str]]:
        credentials = await self._get_credentials()
        async with httpx.AsyncClient(timeout=20.0, transport=self.transport) as client:
            response = await client.get(f"{RESOURCE}/models", headers={
                "Authorization": "Bearer " + credentials["access_token"],
            })
        if response.is_error:
            raise ChatGPTPlanError(f"ChatGPT model catalog HTTP {response.status_code}")
        payload = response.json()
        return [{"slug": model["slug"], "display_name": model.get("display_name", model["slug"])}
                for model in payload.get("models", []) if model.get("visibility") == "list" and model.get("slug")]


class ChatGPTPlanClient:
    def __init__(self, **kwargs: Any):
        self.chat = SimpleNamespace(completions=ChatGPTPlanCompletions(**kwargs))
