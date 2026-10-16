"""HTTP API（内网使用，无鉴权）。"""

from __future__ import annotations

import json
import sys
import urllib.parse
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from config import MAX_BODY_BYTES, PUSH_QUOTA_PER_INBOUND, __version__, logger
from pusher import NoContextToken, PushError, StaleToken, UnknownRecipient
from service import Service
from state import NoRecipient


class _Handler(BaseHTTPRequestHandler):
    server_version = "wxpush"
    protocol_version = "HTTP/1.1"

    #: 由 build_server 注入
    service: Service

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _log_api_failure(self, status: int, payload: Any) -> None:
        """把返回给调用方的错误同步打到控制台。

        以前 4xx/5xx 只是悄悄变成响应体，调用方报「HTTP Error 502」时服务端
        控制台一片安静，只能靠猜。这里的日志专门用来消除这种空白。
        """
        if status < HTTPStatus.BAD_REQUEST:
            return
        if status == HTTPStatus.INTERNAL_SERVER_ERROR:
            # 这条路径在抛出的地方已经用 logger.exception 打过完整 traceback 了。
            return

        code, detail = "", ""
        if isinstance(payload, dict):
            code = str(payload.get("error") or "")
            detail = str(payload.get("detail") or payload.get("message") or "")
        where = f"{self.command} {self.path} 来自 {self.client_address[0]}"
        if code == "not_found":
            # 扫描器、浏览器探测未知路径都会被记成 not_found，只留 debug。
            logger.debug("%s → %d not_found", where, status)
            return

        suffix = f"：{detail}" if detail else ""
        line = f"{where} → {status} {code or 'error'}{suffix}"
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.error("%s", line)
        else:
            logger.warning("%s", line)

    def _send_json(self, status: int, payload: Any) -> None:
        # 先写日志再回包：万一对方已经断开，错误记录也不会跟着丢掉。
        self._log_api_failure(status, payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Date", formatdate(usegmt=True))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"请求体超过 {MAX_BODY_BYTES} 字节")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        # 请求体必须是 UTF-8。非法字节通常意味着调用方在发送前就把中文替换
        # 成了 '?'，或者用了非 UTF-8 编码（例如 PowerShell 的字符串 Body）。
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"请求体不是合法 UTF-8（{exc}）。请以 UTF-8 字节发送 JSON，"
                f"并确保 Content-Type: application/json"
            ) from exc
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path in ("/healthz", "/health", "/"):
                self._send_json(HTTPStatus.OK, {
                    "ok": True, "service": "wxpush", "version": __version__,
                    "endpoints": ["/api/status", "/api/contacts", "/api/send"],
                })
            elif path == "/api/status":
                self._send_json(HTTPStatus.OK, {"ok": True, **self.service.status()})
            elif path == "/api/contacts":
                self._send_json(HTTPStatus.OK, {
                    "ok": True, "contacts": self.service.list_contacts()})
            else:
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"ok": False, "error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s 失败", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "internal_error", "detail": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "bad_request", "detail": str(exc)})
            return

        if path != "/api/send":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        # 接收者由服务侧决定，调用方只给文本。
        text = body.get("text") or body.get("message")
        if not isinstance(text, str) or not text.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "error": "bad_request", "detail": "缺少 text"})
            return

        try:
            result = self.service.send(text)
        except NoRecipient as exc:
            self._send_json(HTTPStatus.CONFLICT,
                            {"ok": False, "error": "no_recipient", "detail": str(exc)})
        except UnknownRecipient as exc:
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": exc.code, "detail": str(exc)})
        except (NoContextToken, StaleToken) as exc:
            self._send_json(HTTPStatus.CONFLICT,
                            {"ok": False, "error": exc.code, "detail": str(exc)})
        except PushError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY,
                            {"ok": False, "error": exc.code, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST /api/send 失败")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "internal_error", "detail": str(exc)})
        else:
            # 成功也留一行：这样控制台能区分「没人调用」和「调用了但失败」。
            quota = result.get("quota_used")
            tail = ""
            if isinstance(quota, int):
                limit = result.get("quota_limit") or PUSH_QUOTA_PER_INBOUND
                tail = f"，本轮额度 {quota}/{limit}"
                if result.get("quota_remaining") == 0:
                    tail += "（已用满，要等对方回一条消息）"
            logger.info("POST /api/send 已推送 → %s（%d 字%s）",
                        result.get("user_id") or "?",
                        result.get("text_length") or len(text), tail)
            self._send_json(HTTPStatus.OK, result)


def build_server(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(_Handler):
        pass

    Handler.service = service

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:  # noqa: D102
            # 调用方提前断开连接是常态，不必为此打印整段 traceback。
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                BrokenPipeError, TimeoutError)):
                logger.debug("客户端断开连接：%s", client_address)
                return
            logger.exception("处理请求时出错（来自 %s）", client_address)

    return Server((host, port), Handler)
