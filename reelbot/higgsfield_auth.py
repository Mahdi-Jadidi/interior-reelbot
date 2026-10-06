"""OAuth connection and read-only discovery for Higgsfield MCP.

Connecting/discovering tools never invokes a generation tool or spends credits.
Generation stays disabled until the live account's schemas and costs are reviewed.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import logging
import queue
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

from .chatgpt_plan import _keyring_module
from .config import Settings
from .higgsfield import MCP_URL

LOG = logging.getLogger(__name__)
SERVICE = "interior-reelbot.higgsfield-mcp"
ACCOUNT = "oauth"
APP_NAME = "Interior Reelbot"


def _mcp_types():
    try:
        from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
        from pydantic import AnyUrl
    except ImportError as exc:
        raise RuntimeError("Install the Higgsfield connector with `pip install -e '.[higgsfield]'`") from exc
    return AuthorizationCodeResult, OAuthClientProvider, TokenStorage, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken, AnyUrl


class KeyringStorage:
    """OAuthClientProvider storage that persists tokens in the OS keyring."""

    def __init__(self):
        self.keyring = _keyring_module()

    def _read(self, suffix: str, model: Any) -> Any:
        value = self.keyring.get_password(SERVICE, suffix)
        return model.model_validate_json(value) if value else None

    def _write(self, suffix: str, value: Any) -> None:
        if value is None:
            try:
                self.keyring.delete_password(SERVICE, suffix)
            except self.keyring.errors.PasswordDeleteError:
                pass
        else:
            self.keyring.set_password(SERVICE, suffix, value.model_dump_json())

    async def get_tokens(self):
        *_, OAuthToken, _AnyUrl = _mcp_types()
        return self._read("tokens", OAuthToken)

    async def set_tokens(self, tokens):
        self._write("tokens", tokens)

    async def get_client_info(self):
        *_, OAuthClientInformationFull, _OAuthClientMetadata, _OAuthToken, _AnyUrl = _mcp_types()
        return self._read("client_info", OAuthClientInformationFull)

    async def set_client_info(self, client_info):
        self._write("client_info", client_info)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    callback_path = "/oauth/callback"
    result_queue: queue.Queue

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.callback_path or self.client_address[0] != "127.0.0.1":
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.result_queue.put({key: value[0] for key, value in values.items()})
        body = "<meta charset='utf-8'><p>Higgsfield authorization returned. Close this tab and return to Reelbot.</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _callback_pair(AuthorizationCodeResult: Any, *, port: int, result_queue: queue.Queue):
    handler = type("HiggsfieldCallbackHandler", (_CallbackHandler,), {"result_queue": result_queue})
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    server.timeout = 300
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    async def redirect(auth_url: str) -> None:
        if not webbrowser.open(auth_url, new=1, autoraise=True):
            raise RuntimeError("Could not open the system browser for Higgsfield authorization")

    async def callback():
        await asyncio.to_thread(thread.join, 302)
        if thread.is_alive() or result_queue.empty():
            raise TimeoutError("Higgsfield OAuth timed out; no access was saved")
        params = result_queue.get_nowait()
        return AuthorizationCodeResult(
            code=params.get("code", ""), state=params.get("state"), iss=params.get("iss"),
        )

    return server, thread, redirect, callback


async def discover_higgsfield_tools() -> list[dict[str, Any]]:
    (AuthorizationCodeResult, OAuthClientProvider, TokenStorage,
     _OAuthClientInformationFull, OAuthClientMetadata, _OAuthToken, AnyUrl) = _mcp_types()
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise RuntimeError("Install the Higgsfield connector with `pip install -e '.[higgsfield]'`") from exc

    storage = KeyringStorage()
    # Bind the socket first to obtain the exact loopback redirect URI used for
    # dynamic OAuth registration; preserve the /oauth/callback path.
    import socket
    reserve = socket.socket()
    reserve.bind(("127.0.0.1", 0))
    port = reserve.getsockname()[1]
    reserve.close()
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    server, thread, redirect, callback = _callback_pair(
        AuthorizationCodeResult, port=port, result_queue=result_queue,
    )
    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name=APP_NAME,
            redirect_uris=[AnyUrl(f"http://127.0.0.1:{port}/oauth/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
    )
    try:
        async with httpx2.AsyncClient(auth=provider, timeout=httpx2.Timeout(30, read=300)) as http_client:
            async with streamable_http_client(MCP_URL, http_client=http_client) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    return [{
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema,
                    } for tool in response.tools]
    finally:
        server.server_close()
        if thread.is_alive():
            thread.join(0.1)


def connection_status() -> dict[str, bool]:
    keyring = _keyring_module()
    return {
        "oauth_client_registered": bool(keyring.get_password(SERVICE, "client_info")),
        "oauth_tokens_saved": bool(keyring.get_password(SERVICE, "tokens")),
        "tool_map_saved": (Settings.from_env().data_dir / "higgsfield_tools.json").is_file(),
    }


def higgsfield_auth_cli() -> None:
    parser = argparse.ArgumentParser(description="Connect to Higgsfield MCP and discover its current tools")
    parser.add_argument("action", choices=("connect", "status", "disconnect"))
    args = parser.parse_args()
    try:
        if args.action == "status":
            print(json.dumps(connection_status(), indent=2))
            return
        keyring = _keyring_module()
        if args.action == "disconnect":
            for suffix in ("tokens", "client_info"):
                try:
                    keyring.delete_password(SERVICE, suffix)
                except keyring.errors.PasswordDeleteError:
                    pass
            print("Higgsfield MCP credentials were removed from this device.")
            return
        tools = asyncio.run(discover_higgsfield_tools())
        destination = Settings.from_env().data_dir / "higgsfield_tools.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        print(json.dumps({"connected": True, "tools_discovered": len(tools),
                          "tool_names": [tool["name"] for tool in tools],
                          "tool_map": str(destination)}, ensure_ascii=False, indent=2))
        print("No generation was started and no credits were spent.")
    except Exception as exc:
        LOG.exception("Higgsfield MCP connection failed")
        raise SystemExit(f"Higgsfield connection failed: {type(exc).__name__}: {exc}") from exc
