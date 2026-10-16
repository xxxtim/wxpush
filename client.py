"""微信后端 HTTP 客户端：绑定某个 base_url 与 token 的 JSON-over-HTTPS 封装。"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import (
    API_BASE_URL,
    API_TIMEOUT_S,
    CONFIG_TIMEOUT_S,
    DEFAULT_BOT_TYPE,
    LONG_POLL_TIMEOUT_S,
    QR_POLL_TIMEOUT_S,
)
from protocol import as_int, build_base_info, common_headers, parse_json, post_headers


class ApiError(RuntimeError):
    """传输层或 HTTP 状态错误。"""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class BusinessError(RuntimeError):
    """HTTP 成功但业务返回码非 0。"""

    def __init__(self, message: str, *, ret: int, errmsg: str = "", errcode: int = 0):
        super().__init__(message)
        self.ret = ret
        self.errmsg = errmsg
        self.errcode = errcode


def _ssl_context() -> ssl.SSLContext:
    # 用系统的信任库。不引入 certifi 之类的第三方根证书包：那些包只含 Mozilla
    # 的公开根列表，会忽略机器上（尤其是企业网络里）安装的自定义根证书。
    return ssl.create_default_context()


class WeixinClient:
    """绑定到某个 base_url 与 token 的 JSON-over-HTTPS 客户端。"""

    def __init__(self, base_url: str = API_BASE_URL, token: str | None = None,
                 *, timeout: float = API_TIMEOUT_S):
        self.base_url = (base_url or API_BASE_URL).rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl = _ssl_context()

    def _request(self, method: str, endpoint: str, *, body: dict | None,
                 headers: dict[str, str], timeout: float,
                 base_url: str | None = None) -> str:
        base = base_url or self.base_url
        if not base.endswith("/"):
            base += "/"
        url = urllib.parse.urljoin(base, endpoint)
        # ensure_ascii=False：发送**原始 UTF-8** 中文，而不是 \uXXXX 转义。
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            raise ApiError(f"{method} {endpoint} HTTP {exc.code}", status=exc.code,
                           body=detail) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {endpoint} 失败：{exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ApiError(f"{method} {endpoint} 超时（{timeout}s）") from exc
        except OSError as exc:
            raise ApiError(f"{method} {endpoint} 失败：{exc}") from exc

    def _post(self, endpoint: str, body: dict, *, authenticated: bool = True,
              timeout: float | None = None) -> Any:
        payload = dict(body)
        if authenticated:
            payload["base_info"] = build_base_info()
        raw = self._request(
            "POST", endpoint, body=payload,
            headers=post_headers(self.token if authenticated else None),
            timeout=timeout if timeout is not None else self.timeout,
        )
        return parse_json(raw)

    def fetch_qr_code(self, bot_type: str = DEFAULT_BOT_TYPE) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(bot_type)}"
        raw = self._request("POST", endpoint, body={"local_token_list": []},
                            headers=post_headers(None), timeout=CONFIG_TIMEOUT_S,
                            base_url=API_BASE_URL)
        return parse_json(raw)

    def poll_qr_status(self, qrcode: str, *, verify_code: str | None = None,
                       base_url: str | None = None) -> dict[str, Any]:
        query = {"qrcode": qrcode}
        if verify_code:
            query["verify_code"] = verify_code
        endpoint = "ilink/bot/get_qrcode_status?" + urllib.parse.urlencode(query)
        try:
            raw = self._request("GET", endpoint, body=None,
                                headers=common_headers(), timeout=QR_POLL_TIMEOUT_S,
                                base_url=base_url or API_BASE_URL)
        except ApiError:
            return {"status": "wait"}  # 长轮询超时或网关抖动都视为「继续等」
        return parse_json(raw)

    def get_updates(self, get_updates_buf: str = "",
                    *, timeout: float = LONG_POLL_TIMEOUT_S) -> dict[str, Any]:
        try:
            return self._post("ilink/bot/getupdates",
                              {"get_updates_buf": get_updates_buf or ""}, timeout=timeout)
        except ApiError as exc:
            if "超时" in str(exc):  # 长轮询超时是正常控制流
                return {"ret": "0", "msgs": [], "get_updates_buf": get_updates_buf}
            raise

    def send_message(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("ilink/bot/sendmessage", body)
        ret = as_int(response.get("ret"))
        if ret not in (0, None):
            raise BusinessError(
                f"sendMessage ret={ret} errmsg={response.get('errmsg', '')}",
                ret=ret, errmsg=str(response.get("errmsg") or ""),
                errcode=as_int(response.get("errcode")) or 0,
            )
        return response

    def notify_start(self) -> dict[str, Any]:
        return self._post("ilink/bot/msg/notifystart", {}, timeout=CONFIG_TIMEOUT_S)

    def notify_stop(self) -> dict[str, Any]:
        return self._post("ilink/bot/msg/notifystop", {}, timeout=CONFIG_TIMEOUT_S)
