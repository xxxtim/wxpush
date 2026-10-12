#!/usr/bin/env python3
"""wxpush —— 轻量级微信消息推送服务。

把微信当成你自己程序的告警 / 通知出口：单文件、纯标准库、内网无鉴权。

用法：

    python wxpush.py                 # 没登录过就自动扫码登录，登录过就直接启动服务
    python wxpush.py status          # 查看状态、接收者与联系人
    python wxpush.py login           # 重新扫码登录
    python wxpush.py send "你好"     # 直接发送（不经 HTTP）

关键约束（协议决定，绕不过去）
------------------------------
发送消息必须携带 `context_token`，而后端只在**收到的消息**里下发该 token。
所以必须先扫码登录拿到 bot token，并且每个接收者都必须先给这个机器人发过
消息，才会有可用的 context_token。token 实测约 24 小时有效，对方每发一条
消息就刷新一次。

因此本服务在后台长轮询 getupdates，记住每个来信用户的 context_token，发送时
原样回传。HTTP 接口只在内网使用，**不做鉴权**。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

from qr import encode_matrix, render_terminal

__version__ = "1.0.0"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

#: 微信后端入口（扫码登录用的固定域名）。
API_BASE_URL = "https://ilinkai.weixin.qq.com"

ILINK_APP_ID = "bot"
CLIENT_VERSION = "2.4.9"
DEFAULT_BOT_TYPE = "3"
BOT_AGENT = f"wxpush/{__version__}"

#: 后端在 bot token 陈旧时返回的错误码。
STALE_TOKEN_ERRCODE = -14
#: 后端「prepare failed」的 ret。实测会话 token 不可用时会返回它而不是 -14，
#: 所以不能只靠 -14 判断 token 失效。
PREPARE_FAILED_RET = -2

LONG_POLL_TIMEOUT_S = 35.0
API_TIMEOUT_S = 15.0
CONFIG_TIMEOUT_S = 10.0
QR_POLL_TIMEOUT_S = 35.0

#: 实测 context_token 在对方最后一次来信后约 24 小时内可用，仅用于「疑似过期」
#: 提示，**不用于拦截发送**（后端才是权威）。
TOKEN_TTL_HOURS = 24.0

#: 保活提醒：token 龄达到这个值（留出缓冲，保证提醒本身还发得出去）时提醒
#: 对方回一条；对方没回应就每 DEFAULT_KEEPALIVE_REPEAT_HOURS 再提醒一次。
DEFAULT_KEEPALIVE_AFTER_HOURS = 22.0
DEFAULT_KEEPALIVE_REPEAT_HOURS = 2.0
KEEPALIVE_TICK_S = 60.0

MAX_TEXT_CHARS = 4000
MAX_BODY_BYTES = 256 * 1024

#: 状态文件（含凭据，已加入 .gitignore）。
STATE_FILENAME = "wxpush.json"

logger = logging.getLogger("wxpush")


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------


class MessageType:
    NONE = 0
    USER = 1
    BOT = 2


class MessageItemType:
    NONE = 0
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5
    TOOL_CALL_START = 11
    TOOL_CALL_RESULT = 12


class MessageState:
    NEW = 0
    GENERATING = 1
    FINISH = 2


#: 非文本条目在正文里的占位标签。
MEDIA_ITEM_LABELS: dict[int, str] = {
    MessageItemType.IMAGE: "[图片]",
    MessageItemType.VOICE: "[语音]",
    MessageItemType.FILE: "[文件]",
    MessageItemType.VIDEO: "[视频]",
}


def client_version_int(version: str = CLIENT_VERSION) -> int:
    """把 "2.4.9" 编码成 iLink-App-ClientVersion 需要的 0x00MMNNPP。"""
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0, 0, 0]
    return ((parts[0] & 0xFF) << 16) | ((parts[1] & 0xFF) << 8) | (parts[2] & 0xFF)


def random_wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再做 base64。"""
    return base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()


def common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(client_version_int()),
    }


def post_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
        **common_headers(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def build_base_info() -> dict[str, str]:
    return {"channel_version": CLIENT_VERSION, "bot_agent": BOT_AGENT}


def new_client_id() -> str:
    return f"wxpush-{uuid.uuid4().hex}"


def build_text_message(
    to_user_id: str,
    text: str,
    *,
    context_token: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """构造一条文本 SendMessageReq。"""
    msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id or new_client_id(),
        "message_type": MessageType.BOT,
        "message_state": MessageState.FINISH,
        "item_list": [{"type": MessageItemType.TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    return {"msg": msg}


def _coerce_id(value: Any) -> str:
    """消息/用户 id 在线路上是 uint64，一律按字符串处理以免丢精度。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value))
    return str(value)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_inbound_message(raw: dict[str, Any]) -> dict[str, Any]:
    """把 msgs[] 里的一项规范化（TEXT 取正文，VOICE 取转写，其余给占位标签）。

    **枚举与时间字段必须用 `_as_int` 重新取整**：为了让 uint64 的 id 不丢精度，
    JSON 是带 `parse_int=str` 解析的，于是连 `message_type`、`item.type`、
    `create_time_ms` 也全都变成了字符串。直接拿它们和 int 常量比较，
    `"1" == 1` 恒为 False。
    """
    item_types: list[int] = []
    text_parts: list[str] = []
    for item in raw.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = _as_int(item.get("type"))
        if item_type is not None:
            item_types.append(item_type)
        if item_type == MessageItemType.TEXT:
            text = (item.get("text_item") or {}).get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif item_type == MessageItemType.VOICE:
            transcript = (item.get("voice_item") or {}).get("text")
            if isinstance(transcript, str) and transcript:
                text_parts.append(transcript)
        else:
            label = MEDIA_ITEM_LABELS.get(item_type) if item_type is not None else None
            if label:
                text_parts.append(label)
    return {
        "message_id": _coerce_id(raw.get("message_id")),
        "from_user_id": _coerce_id(raw.get("from_user_id")),
        "to_user_id": _coerce_id(raw.get("to_user_id")),
        "session_id": _coerce_id(raw.get("session_id")),
        "context_token": raw.get("context_token")
        if isinstance(raw.get("context_token"), str)
        else "",
        "create_time_ms": _as_int(raw.get("create_time_ms")) or 0,
        "message_type": _as_int(raw.get("message_type")) or 0,
        "text": "\n".join(text_parts),
        "item_types": item_types,
        "raw": raw,
    }


def parse_json(raw: str) -> Any:
    """解析后端 JSON，把整数 id 保留为字符串（uint64 会超出浮点精度）。"""
    return json.loads(raw, parse_int=str)


# ---------------------------------------------------------------------------
# 微信后端 HTTP 客户端
# ---------------------------------------------------------------------------


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
        ret = _as_int(response.get("ret"))
        if ret not in (0, None):
            raise BusinessError(
                f"sendMessage ret={ret} errmsg={response.get('errmsg', '')}",
                ret=ret, errmsg=str(response.get("errmsg") or ""),
                errcode=_as_int(response.get("errcode")) or 0,
            )
        return response

    def notify_start(self) -> dict[str, Any]:
        return self._post("ilink/bot/msg/notifystart", {}, timeout=CONFIG_TIMEOUT_S)

    def notify_stop(self) -> dict[str, Any]:
        return self._post("ilink/bot/msg/notifystop", {}, timeout=CONFIG_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 状态存储（单账号、单文件）
# ---------------------------------------------------------------------------


class NoRecipient(RuntimeError):
    """无法确定该发给谁（没有联系人，或有多个且未绑定）。"""


def default_state_path() -> Path:
    env = os.environ.get("WXPUSH_HOME")
    if env:
        return Path(env).expanduser() / STATE_FILENAME
    return Path(__file__).resolve().parent / STATE_FILENAME


@dataclass
class Contact:
    """一个给机器人发过消息的微信用户。"""

    user_id: str
    context_token: str = ""
    token_updated_at: int = 0
    #: 最近一次**收到**该用户消息的时间（只由来信写入）。
    last_message_at: int = 0
    #: 最近一次**发出**消息给该用户的时间。
    last_sent_at: int = 0
    message_count: int = 0
    #: 上次发出保活提醒的时间；收到新消息（换发新 token）时清零。
    keepalive_reminded_at: int = 0

    @property
    def age_s(self) -> int | None:
        return int(time.time() - self.token_updated_at) if self.token_updated_at else None

    @property
    def token_issued_at(self) -> int | None:
        """从 token 内嵌时间戳解析出的签发时间（尽力而为，仅供显示）。

        实测结构：base64 解码后 102 字节，偏移 24 处是一个 u32 小端秒级时间戳，
        等于该 token 所伴随那条消息的 create_time_ms // 1000。这是逆向观察到的
        内部布局，不是公开协议，因此**只用于展示**，绝不参与任何判断。
        """
        return _token_issued_at(self.context_token)


#: token 内嵌时间戳的可接受下界（2020-01-01 起），用于排除误解析。
_TOKEN_TS_MIN = 1577836800


def _token_issued_at(token: str) -> int | None:
    if not token or len(token) < 40:
        return None
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except (ValueError, TypeError):
        return None
    if len(raw) < 28:
        return None
    stamp = int.from_bytes(raw[24:28], "little")
    if _TOKEN_TS_MIN <= stamp <= int(time.time()) + 86400:
        return stamp
    return None


@dataclass
class State:
    """全部持久化状态：凭据 + 联系人 + 轮询游标。"""

    token: str = ""
    base_url: str = API_BASE_URL
    user_id: str = ""
    account_id: str = ""
    saved_at: int = 0
    sync_buf: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    recipient: str = ""
    keepalive_enabled: bool = True
    keepalive_after_hours: float = DEFAULT_KEEPALIVE_AFTER_HOURS
    keepalive_repeat_hours: float = DEFAULT_KEEPALIVE_REPEAT_HOURS
    contacts: dict[str, Contact] = field(default_factory=dict)

    @property
    def logged_in(self) -> bool:
        return bool(self.token.strip())

    def sorted_contacts(self) -> list[Contact]:
        return sorted(self.contacts.values(),
                      key=lambda c: c.last_message_at, reverse=True)

    def resolve_recipient(self, override: str | None = None) -> str:
        """确定消息发给谁。

        优先级：调用方显式指定的 `override`（命令行用）→ 已绑定的接收者 →
        唯一的联系人；有多个联系人又没有绑定则明确报错，而不是猜。
        """
        if override and override.strip():
            return override.strip()
        if self.recipient and self.recipient in self.contacts:
            return self.recipient
        if len(self.contacts) == 1:
            return next(iter(self.contacts))
        if not self.contacts:
            raise NoRecipient(
                "还没有任何联系人：请先让绑定的微信号给机器人发一条消息，"
                "这样才会拿到 context_token")
        candidates = "、".join(c.user_id for c in self.sorted_contacts()[:5])
        raise NoRecipient(
            f"有多个联系人（{candidates}），请先绑定一个接收者："
            f"python wxpush.py bind <user_id>")

    def auto_bind_first_contact(self, user_id: str) -> bool:
        """尚未绑定接收者时，把第一个来信用户自动绑定上。"""
        if self.recipient or not user_id:
            return False
        self.recipient = user_id
        return True

    def record_inbound(self, user_id: str, context_token: str, *,
                       create_time_ms: int = 0) -> Contact:
        """记录一条来信并刷新该用户的 context_token。

        正文**不落盘** —— 对方的消息内容属于隐私，只保留时间戳与计数。
        """
        if not user_id:
            raise ValueError("user_id 不能为空")
        contact = self.contacts.get(user_id) or Contact(user_id=user_id)
        if context_token:
            contact.context_token = context_token
            contact.token_updated_at = int(time.time())
            contact.keepalive_reminded_at = 0
        contact.last_message_at = int(time.time())
        if create_time_ms:
            contact.last_message_at = max(contact.last_message_at,
                                          create_time_ms // 1000)
        contact.message_count += 1
        self.contacts[user_id] = contact
        self.auto_bind_first_contact(user_id)
        return contact

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "State":
        contacts: dict[str, Contact] = {}
        for user_id, value in (raw.get("contacts") or {}).items():
            if not isinstance(value, dict):
                continue
            contacts[user_id] = Contact(
                user_id=user_id,
                context_token=str(value.get("context_token") or ""),
                token_updated_at=int(value.get("token_updated_at") or 0),
                last_message_at=int(value.get("last_message_at") or 0),
                last_sent_at=int(value.get("last_sent_at") or 0),
                message_count=int(value.get("message_count") or 0),
                keepalive_reminded_at=int(value.get("keepalive_reminded_at") or 0),
            )
        return cls(
            token=str(raw.get("token") or ""),
            base_url=str(raw.get("base_url") or API_BASE_URL),
            user_id=str(raw.get("user_id") or ""),
            account_id=str(raw.get("account_id") or ""),
            saved_at=int(raw.get("saved_at") or 0),
            sync_buf=str(raw.get("sync_buf") or ""),
            host=str(raw.get("host") or DEFAULT_HOST),
            port=int(raw.get("port") or DEFAULT_PORT),
            recipient=str(raw.get("recipient") or ""),
            keepalive_enabled=bool(raw.get("keepalive_enabled", True)),
            keepalive_after_hours=float(raw.get("keepalive_after_hours")
                                        or DEFAULT_KEEPALIVE_AFTER_HOURS),
            keepalive_repeat_hours=float(raw.get("keepalive_repeat_hours")
                                         or DEFAULT_KEEPALIVE_REPEAT_HOURS),
            contacts=contacts,
        )


class StateStore:
    """单文件状态的读写：原子写入，内容没变就不写盘。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_state_path()
        self._lock = threading.RLock()
        self._state: State | None = None
        self._last_payload: str | None = None

    @staticmethod
    def _serialize(state: State) -> str:
        return json.dumps(state.to_json(), ensure_ascii=False, indent=2)

    def load(self) -> State:
        with self._lock:
            if self._state is not None:
                return self._state
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                self._state = State.from_json(raw if isinstance(raw, dict) else {})
            except FileNotFoundError:
                self._state = State()
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                logger.warning("状态文件无法解析，按空状态处理：%s", exc)
                self._state = State()
            self._last_payload = self._serialize(self._state)
            return self._state

    def save(self, state: State | None = None, *, force: bool = False) -> State:
        """写盘。内容未变化时直接返回，不做 IO。

        轮询每轮都会拿到 get_updates_buf，无脑写盘就是每 30 多秒写一次内容
        完全相同的文件。
        """
        with self._lock:
            state = state or self._state or self.load()
            self._state = state
            payload = self._serialize(state)
            if not force and payload == self._last_payload and self.path.exists():
                return state

            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._last_payload = payload
            return state

    def update(self, mutator: Callable[[State], Any]) -> State:
        """在锁内读取-修改-写回，避免并发覆盖。"""
        with self._lock:
            state = self.load()
            mutator(state)
            return self.save(state)


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------

MAX_QR_REFRESHES = 3
LOGIN_TIMEOUT_S = 480.0

_QR_CACHE: dict[str, str] = {}


def render_qr_cached(url: str) -> str:
    """渲染登录二维码（缓存上一次结果，避免重复编码）。"""
    cached = _QR_CACHE.get(url)
    if cached:
        return cached
    rendered = render_terminal(encode_matrix(url, "L"))
    _QR_CACHE.clear()
    _QR_CACHE[url] = rendered
    return rendered


@dataclass
class LoginOutcome:
    connected: bool = False
    bot_token: str = ""
    account_id: str = ""
    base_url: str = ""
    user_id: str = ""
    already_connected: bool = False
    message: str = ""


def prompt_verify_code() -> str:
    try:
        return input("请输入手机微信上显示的数字：").strip()
    except EOFError:
        return ""


def do_login(*, bot_type: str = DEFAULT_BOT_TYPE,
             on_status: Callable[[str], None] = lambda _m: None,
             prompt: Callable[[], str] | None = prompt_verify_code,
             timeout_s: float = LOGIN_TIMEOUT_S,
             client_factory: Callable[[], WeixinClient] | None = None) -> LoginOutcome:
    """执行一次扫码登录。成功时返回凭据（由调用方落盘）。"""
    make_client = client_factory or (lambda: WeixinClient(API_BASE_URL))
    client = make_client()
    try:
        response = client.fetch_qr_code(bot_type)
    except ApiError as exc:
        return LoginOutcome(message=f"获取二维码失败：{exc}")

    qrcode = response.get("qrcode")
    url = response.get("qrcode_img_content")
    if not isinstance(qrcode, str) or not qrcode:
        return LoginOutcome(message=f"二维码响应缺少 qrcode：{response}")
    if not isinstance(url, str) or not url:
        return LoginOutcome(message=f"二维码响应缺少 qrcode_img_content：{response}")

    print()
    try:
        print(render_qr_cached(url))
    except Exception as exc:  # noqa: BLE001 - 渲染失败不该阻断登录
        print(f"（二维码渲染失败：{exc}）", file=sys.stderr)
    print("请用微信扫描上方二维码完成登录。")
    print("如果扫不出来，也可以在手机上打开这个链接：")
    print(f"  {url}")
    print()

    poll_base = API_BASE_URL
    pending_code = ""
    refreshes = 0
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        status = client.poll_qr_status(qrcode, verify_code=pending_code or None,
                                       base_url=poll_base)
        state = str(status.get("status") or "wait")

        if state == "wait":
            time.sleep(1.0)
        elif state == "scaned":
            if pending_code:
                pending_code = ""
            on_status("已扫描，请在手机上确认")
            time.sleep(1.0)
        elif state == "scaned_but_redirect":
            host = status.get("redirect_host")
            if isinstance(host, str) and host:
                poll_base = f"https://{host}"
                on_status(f"已切换到 {host}")
            time.sleep(1.0)
        elif state == "need_verifycode":
            if prompt is None:
                return LoginOutcome(message="后端要求输入手机上的验证码，但没有可用的输入方式。")
            on_status("请输入手机微信上显示的数字")
            pending_code = prompt().strip()
        elif state in ("verify_code_blocked", "expired"):
            pending_code = ""
            if refreshes >= MAX_QR_REFRESHES:
                return LoginOutcome(message="二维码多次失效或验证码多次错误，请稍后重试。")
            refreshes += 1
            on_status(f"二维码失效，正在刷新（{refreshes}/{MAX_QR_REFRESHES}）")
            try:
                response = client.fetch_qr_code(bot_type)
                qrcode = str(response.get("qrcode") or "")
                url = str(response.get("qrcode_img_content") or "")
                poll_base = API_BASE_URL
                if qrcode and url:
                    print(render_qr_cached(url))
            except ApiError as exc:
                return LoginOutcome(message=f"刷新二维码失败：{exc}")
        elif state == "binded_redirect":
            return LoginOutcome(already_connected=True,
                                message="这个微信号已经绑定过，无需重复登录。")
        elif state == "confirmed":
            account_id = status.get("ilink_bot_id")
            if not isinstance(account_id, str) or not account_id:
                return LoginOutcome(message="登录已确认，但后端没有返回 ilink_bot_id。")
            base_url = status.get("baseurl")
            user_id = status.get("ilink_user_id")
            token = status.get("bot_token")
            return LoginOutcome(
                connected=True,
                bot_token=token if isinstance(token, str) else "",
                account_id=account_id,
                base_url=base_url if isinstance(base_url, str) and base_url
                else API_BASE_URL,
                user_id=user_id if isinstance(user_id, str) else "",
                message="登录成功",
            )
        else:
            on_status(f"未处理的二维码状态 {state!r}，继续等待")
            time.sleep(1.0)

    return LoginOutcome(message="等待扫码超时。")


# ---------------------------------------------------------------------------
# 收消息轮询（捕获 context_token）
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_S = 30.0
RETRY_S = 2.0
STALE_TOKEN_PAUSE_S = 3600.0


@dataclass
class PollerStats:
    started_at: float = 0.0
    last_poll_at: float = 0.0
    last_message_at: float = 0.0
    messages_seen: int = 0
    messages_recorded: int = 0
    polls_ok: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    paused_until: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "started_at": int(self.started_at) if self.started_at else 0,
            "last_poll_at": int(self.last_poll_at) if self.last_poll_at else 0,
            "last_message_at": int(self.last_message_at) if self.last_message_at else 0,
            "messages_seen": self.messages_seen,
            "messages_recorded": self.messages_recorded,
            "polls_ok": self.polls_ok,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "paused": now < self.paused_until,
            "paused_remaining_s": max(0, int(self.paused_until - now)),
        }


class InboundPoller:
    """后台长轮询 getupdates，记录来信用户的 context_token。"""

    def __init__(self, store: StateStore,
                 client_factory: Callable[[str, str], WeixinClient] | None = None):
        self.store = store
        self.stats = PollerStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_factory = client_factory or WeixinClient

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wxpush-poller",
                                       daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        state = self.store.load()
        client = self._client_factory(state.base_url, state.token)
        self.stats.started_at = time.time()
        buf = state.sync_buf

        while not self._stop.is_set():
            paused = self.stats.paused_until - time.time()
            if paused > 0:
                # 不覆盖 last_error：暂停的「原因」比剩余时间重要。
                self._stop.wait(min(paused, 1.0))
                continue
            try:
                response = client.get_updates(buf, timeout=LONG_POLL_TIMEOUT_S)
            except ApiError as exc:
                self._fail(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - 线程绝不能崩
                self._fail(f"{type(exc).__name__}: {exc}")
                continue

            self.stats.last_poll_at = time.time()
            ret = _as_int(response.get("ret"))
            errcode = _as_int(response.get("errcode"))
            # 后端会话过期时返回 {"errcode":"-14","errmsg":"session timeout"}，
            # 这种响应**没有 ret 字段**，所以不能只看 ret。
            if (ret not in (0, None)) or (errcode not in (0, None)):
                if ret == STALE_TOKEN_ERRCODE or errcode == STALE_TOKEN_ERRCODE:
                    self.stats.paused_until = time.time() + STALE_TOKEN_PAUSE_S
                    self.stats.last_error = (
                        f"bot token 陈旧（errcode {STALE_TOKEN_ERRCODE}），暂停 "
                        f"{int(STALE_TOKEN_PAUSE_S)}s；请重新运行 "
                        f"python wxpush.py login")
                    logger.error("轮询被后端拒绝：%s", self.stats.last_error)
                else:
                    self.stats.last_error = (
                        f"getUpdates ret={ret} errcode={errcode} "
                        f"errmsg={response.get('errmsg', '')}")
                    logger.warning("轮询失败：%s", self.stats.last_error)
                    self._backoff()
                continue

            self.stats.consecutive_failures = 0
            new_buf = response.get("get_updates_buf")
            if isinstance(new_buf, str) and new_buf and new_buf != buf:
                buf = new_buf
                self.store.update(lambda s: setattr(s, "sync_buf", new_buf))
            messages = response.get("msgs") or []
            count = len(messages) if isinstance(messages, list) else 0

            self.stats.polls_ok += 1
            if self.stats.polls_ok == 1:
                logger.info("轮询已连通后端")
            if count:
                logger.info("本轮收到 %d 条消息", count)
            self._handle(messages)

    def _handle(self, messages: Any) -> None:
        if not isinstance(messages, list):
            return
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            self.stats.messages_seen += 1
            message = parse_inbound_message(raw)
            # 判定来信只看 from_user_id，不按 message_type 过滤：真实后端给用户
            # 来信的 message_type 是 0（不是 1），而机器人自己发出的消息
            # from_user_id 为空串。
            if not message["from_user_id"]:
                continue
            if not message["context_token"]:
                self.stats.last_error = (
                    f"来自 {message['from_user_id']} 的消息没有 context_token")
            self.store.update(
                lambda s, m=message: s.record_inbound(
                    m["from_user_id"], m["context_token"],
                    create_time_ms=m["create_time_ms"],
                )
            )
            self.stats.messages_recorded += 1
            self.stats.last_message_at = time.time()
            logger.info("收到来信：%s  text=%r", message["from_user_id"],
                        message["text"][:40])

    def _fail(self, error: str) -> None:
        self.stats.consecutive_failures += 1
        self.stats.last_error = error
        self._backoff()

    def _backoff(self) -> None:
        delay = (BACKOFF_S if self.stats.consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                 else RETRY_S)
        self._stop.wait(delay)


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


class PushError(RuntimeError):
    code = "push_failed"


class UnknownRecipient(PushError):
    code = "unknown_recipient"


class NoContextToken(PushError):
    code = "no_context_token"


class StaleToken(PushError):
    code = "stale_token"


class DeliveryFailed(PushError):
    code = "delivery_failed"


class KeepaliveMonitor:
    """保活提醒：token 快过期时主动发一条微信，请对方回一句。

    context_token 只在收到消息时下发/刷新，实测约 24 小时失效；一旦失效所有
    推送都会失败，而且服务自己无法恢复 —— 只能等对方再发一条消息。所以在
    token 龄达到 keepalive_after_hours 时提醒一次，对方没回应则按
    keepalive_repeat_hours 重复提醒，不刷屏。
    """

    def __init__(self, store: StateStore,
                 client_factory: Callable[[str, str], WeixinClient] | None = None):
        self.store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_factory = client_factory or WeixinClient
        self.reminders_sent = 0
        self.last_reminder_at = 0.0
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wxpush-keepalive",
                                       daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - 线程绝不能崩
                self.last_error = f"{type(exc).__name__}: {exc}"
            if self._stop.wait(KEEPALIVE_TICK_S):
                return

    def due_contacts(self, state: State, *, now: float | None = None) -> list[Contact]:
        """列出当前需要提醒的联系人。"""
        if not state.keepalive_enabled:
            return []
        now = now if now is not None else time.time()
        after_s = state.keepalive_after_hours * 3600
        repeat_s = state.keepalive_repeat_hours * 3600
        due: list[Contact] = []
        for contact in state.sorted_contacts():
            if not contact.context_token or not contact.token_updated_at:
                continue
            if now - contact.token_updated_at < after_s:
                continue
            if (contact.keepalive_reminded_at
                    and now - contact.keepalive_reminded_at < repeat_s):
                continue
            due.append(contact)
        return due

    @staticmethod
    def build_reminder(contact: Contact) -> str:
        return ("【wxpush 保活提醒】推送凭据即将到期，请回复本条消息任意内容"
                "以刷新凭据、继续接收推送。")

    def check_once(self, *, now: float | None = None) -> int:
        """检查一轮并按需发送提醒，返回发出的提醒条数。"""
        state = self.store.load()
        if not state.logged_in or not state.keepalive_enabled:
            return 0
        due = self.due_contacts(state, now=now)
        if not due:
            return 0

        sent = 0
        for contact in due:
            body = build_text_message(contact.user_id, self.build_reminder(contact),
                                      context_token=contact.context_token)
            try:
                self._client_factory(state.base_url, state.token).send_message(body)
            except BusinessError as exc:
                if exc.ret == STALE_TOKEN_ERRCODE or exc.errcode == STALE_TOKEN_ERRCODE:
                    self.last_error = ("保活提醒发送失败：token 已失效（-14），"
                                       "请手动让对方发一条消息")
                elif exc.ret == PREPARE_FAILED_RET:
                    self.last_error = (f"保活提醒发送失败：{exc} —— 会话 token 可能"
                                       f"已不可用，请手动让对方给机器人发一条消息")
                else:
                    self.last_error = f"保活提醒发送失败：{exc}"
                logger.error("%s", self.last_error)
                continue
            except (ApiError, OSError) as exc:
                self.last_error = f"保活提醒发送失败：{exc}"
                logger.error("%s", self.last_error)
                continue

            stamp = int(now if now is not None else time.time())

            def mark(state_: State, uid=contact.user_id, ts=stamp) -> None:
                target = state_.contacts.get(uid)
                if target is not None:
                    target.keepalive_reminded_at = ts
                    # 提醒也是一次「发出」，但不碰 last_message_at（那是来信时间）。
                    target.last_sent_at = ts

            self.store.update(mark)
            sent += 1
            self.reminders_sent += 1
            self.last_reminder_at = stamp
            self.last_error = ""
            logger.info("已向 %s 发出保活提醒（token 龄 %.1fh）",
                        contact.user_id, (contact.age_s or 0) / 3600)
        return sent

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "reminders_sent": self.reminders_sent,
            "last_reminder_at": int(self.last_reminder_at) if self.last_reminder_at else 0,
            "last_error": self.last_error,
        }


class Pusher:
    """把接收者解析成 context_token 并发送文本。"""

    def __init__(self, store: StateStore):
        self.store = store
        self._client: WeixinClient | None = None

    def client(self) -> WeixinClient:
        if self._client is None:
            state = self.store.load()
            self._client = WeixinClient(state.base_url, state.token)
        return self._client

    def send_text(self, text: str, *, user_id: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise DeliveryFailed("text 不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise DeliveryFailed(f"text 超过 {MAX_TEXT_CHARS} 字")

        state = self.store.load()
        if not state.logged_in:
            raise DeliveryFailed("尚未登录，请先运行 python wxpush.py login")

        target = state.resolve_recipient(user_id)
        contact = state.contacts.get(target)
        if contact is None:
            raise UnknownRecipient(
                f"没有联系人 {target!r}：对方必须先给机器人发一条消息")
        if not contact.context_token:
            raise NoContextToken(
                f"联系人 {target!r} 还没有 context_token，请让 TA 给机器人发条消息")

        # 刻意不检查 token 年龄：约 24 小时只是提示，权威是后端。
        body = build_text_message(target, text, context_token=contact.context_token)
        client_id = str(body["msg"]["client_id"])
        try:
            response = self.client().send_message(body)
        except BusinessError as exc:
            if exc.ret == STALE_TOKEN_ERRCODE or exc.errcode == STALE_TOKEN_ERRCODE:
                raise StaleToken(
                    "后端认为该会话 token 已失效，请让对方再给机器人发一条消息") from exc
            # 实测：会话 token 不可用时后端不一定返回 -14，更常见的是
            # `ret=-2 prepare failed`（bot token 陈旧也会落到这里），
            # 所以不猜原因，带上原始返回并给出两种可能。
            hint = ""
            if exc.ret == PREPARE_FAILED_RET:
                hint = ("（后端 prepare 失败：可能该会话 token 已不可用，请让对方发"
                        "一条消息；也可能是 bot token 已陈旧 —— 若所有发送都失败，"
                        "运行 python wxpush.py login 重新登录）")
            raise DeliveryFailed(f"发送失败：{exc}{hint}") from exc
        except ApiError as exc:
            raise DeliveryFailed(f"发送传输错误：{exc}") from exc

        server_id = response.get("message_id")

        def touch(state_: State) -> None:
            target_contact = state_.contacts.setdefault(target, Contact(user_id=target))
            # 只记「发出」时间，绝不写 last_message_at（那是来信时间）。
            target_contact.last_sent_at = int(time.time())

        self.store.update(touch)
        return {
            "ok": True,
            "user_id": target,
            "client_id": client_id,
            "server_message_id": str(server_id) if server_id else "",
            "text_length": len(text),
        }


# ---------------------------------------------------------------------------
# HTTP API（内网使用，无鉴权）
# ---------------------------------------------------------------------------


class Service:
    """持有状态、轮询器与发送器。"""

    def __init__(self, store: StateStore):
        self.store = store
        self.poller: InboundPoller | None = None
        self.keepalive: KeepaliveMonitor | None = None
        self._pusher = Pusher(store)

    def start(self) -> None:
        state = self.store.load()
        if not state.logged_in:
            raise LookupError("尚未登录，请先运行 python wxpush.py login")
        if self.poller is None or not self.poller.running:
            self.poller = InboundPoller(self.store)
            self.poller.start()
            logger.info("收消息轮询已启动")
        if state.keepalive_enabled and (self.keepalive is None
                                        or not self.keepalive.running):
            self.keepalive = KeepaliveMonitor(self.store)
            self.keepalive.start()
            logger.info("保活提醒已启用（token 龄达 %.0fh 时提醒）",
                        state.keepalive_after_hours)
        try:
            WeixinClient(state.base_url, state.token).notify_start()
        except Exception:  # noqa: BLE001 - 纯通知性质
            logger.debug("notifyStart 失败", exc_info=True)

    def stop(self) -> None:
        if self.poller:
            self.poller.stop()
            self.poller = None
        if self.keepalive:
            self.keepalive.stop()
            self.keepalive = None
        state = self.store.load()
        try:
            WeixinClient(state.base_url, state.token).notify_stop()
        except Exception:  # noqa: BLE001
            logger.debug("notifyStop 失败", exc_info=True)

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        contacts = state.sorted_contacts()
        try:
            recipient = state.resolve_recipient()
            recipient_error = ""
        except NoRecipient as exc:
            recipient, recipient_error = "", str(exc)

        return {
            "logged_in": state.logged_in,
            "account_id": state.account_id,
            "base_url": state.base_url,
            "recipient": recipient,
            "recipient_bound": state.recipient,
            "recipient_error": recipient_error,
            "poller_running": bool(self.poller and self.poller.running),
            "poller": self.poller.stats.to_dict() if self.poller else {},
            "keepalive": self.keepalive.to_dict() if self.keepalive else {
                "running": False, "reminders_sent": 0,
                "last_reminder_at": 0, "last_error": "",
            },
            "keepalive_enabled": state.keepalive_enabled,
            "keepalive_after_hours": state.keepalive_after_hours,
            "contacts_total": len(contacts),
            # 不做年龄过滤：含义是「我们持有 token」，是否还有效只有后端知道。
            "contacts_pushable": sum(1 for c in contacts if c.context_token),
        }

    def list_contacts(self) -> list[dict[str, Any]]:
        state = self.store.load()
        ttl_s = TOKEN_TTL_HOURS * 3600
        out = []
        for contact in state.sorted_contacts():
            age = contact.age_s
            out.append({
                "user_id": contact.user_id,
                "pushable": bool(contact.context_token),
                "token_age_s": age,
                # 仅提示：约 24 小时未互动，建议先让对方发条消息。
                "stale_suspected": age is not None and age > ttl_s,
                "token_issued_at": contact.token_issued_at,
                "last_message_at": contact.last_message_at,
                "last_sent_at": contact.last_sent_at,
                "message_count": contact.message_count,
            })
        return out

    def send(self, text: str, *, user_id: str | None = None) -> dict[str, Any]:
        """发送文本。`user_id` 仅供命令行临时指定，HTTP 接口不暴露。"""
        return self._pusher.send_text(text, user_id=user_id)


class _Handler(BaseHTTPRequestHandler):
    server_version = "wxpush"
    protocol_version = "HTTP/1.1"

    #: 由 build_server 注入
    service: Service

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Any) -> None:
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
            self._send_json(HTTPStatus.OK, self.service.send(text))
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


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _state_path(args: argparse.Namespace) -> str | None:
    """--state 的默认值是 SUPPRESS，未提供时属性不存在，这里统一兜底。"""
    return getattr(args, "state", None)


def perform_login(store: StateStore, *, quiet: bool = False) -> int:
    """扫码登录并把凭据写入状态文件。"""
    outcome = do_login(
        on_status=(lambda _m: None) if quiet else (lambda m: print(f"  ... {m}")),
    )
    if outcome.already_connected:
        print(outcome.message)
        return 0
    if not outcome.connected:
        print(f"登录失败：{outcome.message}", file=sys.stderr)
        return 1

    def apply(state: State) -> None:
        state.token = outcome.bot_token
        state.base_url = outcome.base_url
        state.account_id = outcome.account_id
        state.user_id = outcome.user_id
        state.saved_at = int(time.time())
        state.sync_buf = ""  # 新会话，游标重置

    store.update(apply)
    print("登录成功，凭据已保存。")
    print("\n下一步：运行 python wxpush.py 启动服务")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """默认命令：没登录就登录，登录了就启动服务。"""
    store = StateStore(_state_path(args))
    state = store.load()

    if getattr(args, "force_login", False):
        if perform_login(store) != 0:
            return 1
        state = store.load()

    if not state.logged_in:
        print("=" * 60)
        print("首次运行：需要先用微信扫码登录。")
        print("=" * 60)
        if perform_login(store) != 0:
            return 1
        state = store.load()

    host = getattr(args, "host", None) or state.host or DEFAULT_HOST
    port = getattr(args, "port", None) or state.port or DEFAULT_PORT

    service = Service(store)
    try:
        service.start()
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"启动收消息轮询失败：{exc}", file=sys.stderr)
        return 1

    # 先绑定端口，成功后再报「轮询在跑」，避免误导。
    try:
        server = build_server(service, host, port)
    except OSError as exc:
        service.stop()
        print(f"无法监听 {host}:{port}：{exc}", file=sys.stderr)
        return 1

    contacts = store.load().sorted_contacts()
    # 绑定 0.0.0.0 只是「监听所有网卡」，它不是一个可连接的地址，所以对外展示
    # 一律用服务器实际拿到的地址：本机监听就写 127.0.0.1，否则写真实 IP。
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    shown_host = "127.0.0.1" if bound_host in ("0.0.0.0", "::") else bound_host
    base = f"http://{shown_host}:{bound_port}"
    pushable = sum(1 for c in contacts if c.context_token)
    print(f"wxpush {__version__} 已启动 —— {base}")
    print(f"  账号      : {state.account_id or '(未知)'}")
    print(f"  联系人    : {len(contacts)} 个（其中 {pushable} 个可以推送）")
    print(f"  状态文件  : {store.path}")
    print()
    print("收消息轮询已在运行，收到消息会实时打印在下面。")
    if bound_host in ("0.0.0.0", "::"):
        print(f"服务监听所有网卡（{bound_host}:{bound_port}），"
              f"内网其他机器把 {shown_host} 换成这台机器的内网 IP 即可。")
    print()
    print("接口（内网直接调用，无需鉴权）：")
    print("  GET  /api/status    服务状态")
    print("  GET  /api/contacts  可以推给谁")
    print('  POST /api/send      发送，请求体 {"text":"部署完成"}')
    print()
    print(f'  curl -X POST {base}/api/send '
          f'-H "Content-Type: application/json" -d \'{{"text":"部署完成"}}\'')
    print()
    print("─" * 60)
    if contacts:
        print("现在就可以推送了。")
    else:
        print("还差一步才能推送：请用接收推送的那个微信号，给这个机器人随便发一句话。")
        print("消息一到，这里会打印「收到来信」，之后就能推送了。")
    print("\n按 Ctrl+C 停止。")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        server.server_close()
        service.stop()
    return 0


def _contact_state_text(contact: Contact) -> str:
    if not contact.context_token:
        return "无 token"
    age = contact.age_s
    if age is not None and age > TOKEN_TTL_HOURS * 3600:
        return "疑似过期"
    return "有效"


def cmd_status(args: argparse.Namespace) -> int:
    store = StateStore(_state_path(args))
    state = store.load()
    print(f"状态文件   : {store.path}")
    if not state.logged_in:
        print("登录状态   : 未登录（直接运行 python wxpush.py 即可扫码登录）")
        return 1
    print("登录状态   : 已登录")
    print(f"账号       : {state.account_id or '(未知)'}")
    print(f"base_url   : {state.base_url}")
    print(f"监听配置   : {state.host}:{state.port}")
    print(f"保活提醒   : {'开启' if state.keepalive_enabled else '关闭'}"
          f"（token 龄达 {state.keepalive_after_hours:g}h 时提醒）")

    contacts = state.sorted_contacts()
    try:
        bound = state.resolve_recipient()
        note = "" if state.recipient else "（未显式绑定，按唯一联系人自动选定）"
        print(f"接收者     : {bound} {note}".rstrip())
    except NoRecipient as exc:
        print(f"接收者     : 未确定 —— {exc}")
    print(f"联系人     : {len(contacts)} 个")

    if contacts:
        print()
        print(f"  {'user_id':<30} {'状态':<10} {'msgs':>5}  {'最近来信':<17} "
              f"{'最近发出':<17}")
        for contact in contacts:
            received = (time.strftime("%m-%d %H:%M:%S",
                                      time.localtime(contact.last_message_at))
                        if contact.last_message_at else "-")
            sent_at = (time.strftime("%m-%d %H:%M:%S",
                                     time.localtime(contact.last_sent_at))
                       if contact.last_sent_at else "-")
            print(f"  {contact.user_id[:29]:<30} "
                  f"{_contact_state_text(contact):<10} "
                  f"{contact.message_count:>5}  "
                  f"{received:<17} {sent_at:<17}")
    else:
        print("\n还没有联系人。请让目标微信号给机器人发一条消息。")

    print("\n提示：context_token 实测约 24 小时有效，对方每发一条消息都会刷新；"
          "标为「疑似过期」时仍会尝试发送，只有后端返回 -14 才代表真的失效。")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    store = StateStore(_state_path(args))

    def apply(state: State) -> None:
        if args.host:
            state.host = args.host
        if args.port:
            state.port = args.port
        if args.keepalive_on:
            state.keepalive_enabled = True
        if args.keepalive_off:
            state.keepalive_enabled = False

    store.update(apply)
    state = store.load()
    print(f"已更新：host={state.host} port={state.port} "
          f"保活={'开启' if state.keepalive_enabled else '关闭'}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """直接发送。接收者默认由服务侧决定，`--to` 可临时覆盖。"""
    store = StateStore(_state_path(args))
    try:
        result = Service(store).send(" ".join(args.text), user_id=args.to)
    except NoRecipient as exc:
        print(f"无法确定接收者：{exc}", file=sys.stderr)
        return 1
    except PushError as exc:
        print(f"发送失败（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"发送失败：{exc}", file=sys.stderr)
        return 1
    print(f"已发送给 {result['user_id']}"
          f"（message_id={result['server_message_id'] or '无'}）")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    """绑定接收者。不传参数则显示当前绑定，或自动绑定唯一的联系人。"""
    store = StateStore(_state_path(args))
    state = store.load()

    if args.user_id:
        target = args.user_id.strip()
        if target not in state.contacts:
            print(f"警告：{target} 还不在联系人里（对方尚未给机器人发过消息）。",
                  file=sys.stderr)
            print("已绑定，但需要 TA 发一条消息后才能实际送达。", file=sys.stderr)
        store.update(lambda s: setattr(s, "recipient", target))
        print(f"已绑定接收者：{target}")
        return 0

    if args.clear:
        store.update(lambda s: setattr(s, "recipient", ""))
        print("已解除绑定；之后会按「唯一联系人」自动选定接收者。")
        return 0

    if state.recipient:
        print(f"当前绑定的接收者：{state.recipient}")
        return 0

    if len(state.contacts) == 1:
        target = next(iter(state.contacts))
        store.update(lambda s: setattr(s, "recipient", target))
        print(f"已自动绑定唯一的联系人：{target}")
        return 0
    if not state.contacts:
        print("还没有任何联系人。请先让目标微信号给机器人发一条消息。")
        return 1
    print("有多个联系人，请显式指定：python wxpush.py bind <user_id>")
    for contact in state.sorted_contacts():
        print(f"  {contact.user_id}")
    return 1


def cmd_keepalive(args: argparse.Namespace) -> int:
    """查看保活状态；`--now` 立即检查一次并发送提醒（用于验证链路）。"""
    store = StateStore(_state_path(args))
    state = store.load()

    print(f"保活提醒   : {'开启' if state.keepalive_enabled else '关闭'}")
    print(f"提醒阈值   : token 龄 {state.keepalive_after_hours:g} 小时")
    print(f"重复间隔   : 对方没回应时每 {state.keepalive_repeat_hours:g} 小时再提醒")
    print(f"检查频率   : 每 {KEEPALIVE_TICK_S:.0f} 秒一次")

    if args.now:
        if not state.logged_in:
            print("\n尚未登录，无法发送提醒。", file=sys.stderr)
            return 1
        if not state.contacts:
            print("\n还没有联系人（没有 token 可保活）。", file=sys.stderr)
            return 1
        # 把阈值临时降到 0，强制触发一条，便于确认链路可用。
        original = state.keepalive_after_hours

        def force(s: State) -> None:
            s.keepalive_after_hours = 0.0
            for contact in s.contacts.values():
                contact.keepalive_reminded_at = 0

        store.update(force)
        monitor = KeepaliveMonitor(store)
        try:
            sent = monitor.check_once()
        finally:
            store.update(lambda s: setattr(s, "keepalive_after_hours", original))
        print(f"\n已强制发送 {sent} 条保活提醒。")
        if monitor.last_error:
            print(f"错误：{monitor.last_error}", file=sys.stderr)
        return 0 if sent else 1

    if not state.logged_in:
        print("\n尚未登录。")
        return 1

    print()
    if not state.contacts:
        print("还没有联系人（没有 token 可保活）。")
        return 0
    print(f"  {'user_id':<34} {'token 龄':>10}  {'距提醒':>10}  上次提醒")
    for contact in state.sorted_contacts():
        age = contact.age_s
        age_text = f"{age / 3600:.1f}h" if age is not None else "-"
        if age is None:
            remain = "-"
        else:
            remain_s = state.keepalive_after_hours * 3600 - age
            remain = f"{remain_s / 3600:.1f}h" if remain_s > 0 else "已到期"
        reminded = (time.strftime("%m-%d %H:%M", time.localtime(
            contact.keepalive_reminded_at)) if contact.keepalive_reminded_at else "从未")
        print(f"  {contact.user_id[:33]:<34} {age_text:>10}  {remain:>10}  {reminded}")
    print()
    print("用 `python wxpush.py keepalive --now` 可立即发一条提醒来验证链路。")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _add_state_arg(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    parser.add_argument(
        "--state", default=argparse.SUPPRESS,
        help=argparse.SUPPRESS if suppress
        else f"状态文件路径（默认 {STATE_FILENAME}）",
    )


def _add_run_args(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """仅「启动服务」分支需要的参数。

    默认值用 SUPPRESS：这样参数只写在外层时（`--port 9000 run`），子命令不会用
    None 把它覆盖掉。
    """
    h = argparse.SUPPRESS if suppress else None
    parser.add_argument("--host", default=argparse.SUPPRESS, help=h or "监听地址")
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help=h or "监听端口")
    parser.add_argument("--force-login", action="store_true",
                        default=argparse.SUPPRESS,
                        help=h or "先强制重新扫码登录")


def build_parser() -> argparse.ArgumentParser:
    # 共享父解析器，让全局参数在子命令前后都能写。
    state_parent = argparse.ArgumentParser(add_help=False)
    _add_state_arg(state_parent)
    run_parent = argparse.ArgumentParser(add_help=False)
    _add_state_arg(run_parent)
    _add_run_args(run_parent)

    parser = argparse.ArgumentParser(
        prog="wxpush.py",
        description="轻量级微信消息推送服务（单文件、纯标准库、内网无鉴权）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "不带子命令运行即可：未登录会自动扫码登录，已登录则直接启动 HTTP 服务。\n"
            "接收者由服务侧决定（绑定一个微信号），HTTP 调用方不需要传 user_id。\n"
            "示例：\n"
            "  python wxpush.py                      启动（自动判断）\n"
            "  python wxpush.py --port 9000          换端口启动\n"
            "  python wxpush.py login                重新扫码登录\n"
            "  python wxpush.py status               查看状态、接收者与联系人\n"
            "  python wxpush.py bind <user_id>       指定接收者\n"
            '  python wxpush.py send "你好"          直接发送\n'
        ),
    )
    parser.add_argument("--version", action="version", version=f"wxpush {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    _add_state_arg(parser)

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="启动服务（与不带子命令相同）",
                   parents=[run_parent]).set_defaults(func=cmd_run)
    sub.add_parser("login", help="扫码登录（覆盖已有凭据）",
                   parents=[state_parent]).set_defaults(func=None)
    sub.add_parser("status", help="查看状态、接收者与联系人",
                   parents=[state_parent]).set_defaults(func=cmd_status)

    p_set = sub.add_parser("set", help="修改监听地址与端口等配置",
                           parents=[state_parent])
    p_set.add_argument("--host")
    p_set.add_argument("--port", type=int)
    p_set.add_argument("--keepalive-on", action="store_true", help="开启保活提醒（默认开启）")
    p_set.add_argument("--keepalive-off", action="store_true", help="关闭保活提醒")
    p_set.set_defaults(func=cmd_set)

    p_bind = sub.add_parser("bind", help="绑定/查看接收者（本服务只发给一个人）",
                            parents=[state_parent])
    p_bind.add_argument("user_id", nargs="?", default=None,
                        help="要绑定的 user_id；省略则显示当前绑定")
    p_bind.add_argument("--clear", action="store_true", help="解除绑定")
    p_bind.set_defaults(func=cmd_bind)

    p_send = sub.add_parser("send", help="直接发送（不经 HTTP，接收者由服务侧决定）",
                            parents=[state_parent])
    p_send.add_argument("text", nargs="+")
    p_send.add_argument("--to", default=None, help="临时指定接收者（覆盖绑定）")
    p_send.set_defaults(func=cmd_send)

    p_ka = sub.add_parser("keepalive", help="查看保活提醒状态；--now 立即发一条验证",
                          parents=[state_parent])
    p_ka.add_argument("--now", action="store_true",
                      help="立即强制发送一条保活提醒（验证用）")
    p_ka.set_defaults(func=cmd_keepalive)

    # 顶层也注册启动参数，使 `python wxpush.py --port 9000` 可用。
    top = argparse.ArgumentParser(add_help=False)
    _add_run_args(top, suppress=True)
    for action in top._actions:  # noqa: SLF001 - 有意复用同一组参数定义
        parser._add_action(action)  # noqa: SLF001
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    if args.command is None:
        return cmd_run(args)
    if args.command == "login":
        store = StateStore(_state_path(args))
        return perform_login(store)
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(130)
