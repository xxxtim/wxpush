"""协议层：枚举、请求头，以及消息的编解码。

这一层只依赖标准库与 config，不碰状态、不碰网络。
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

from config import BOT_AGENT, CLIENT_VERSION, ILINK_APP_ID


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


def coerce_id(value: Any) -> str:
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


def as_int(value: Any) -> int | None:
    """把线路上可能是字符串的数值取回 int（枚举、错误码、时间戳都用它）。"""
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

    **枚举与时间字段必须用 `as_int` 重新取整**：为了让 uint64 的 id 不丢精度，
    JSON 是带 `parse_int=str` 解析的，于是连 `message_type`、`item.type`、
    `create_time_ms` 也全都变成了字符串。直接拿它们和 int 常量比较，
    `"1" == 1` 恒为 False。
    """
    item_types: list[int] = []
    text_parts: list[str] = []
    for item in raw.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = as_int(item.get("type"))
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
        "message_id": coerce_id(raw.get("message_id")),
        "from_user_id": coerce_id(raw.get("from_user_id")),
        "to_user_id": coerce_id(raw.get("to_user_id")),
        "session_id": coerce_id(raw.get("session_id")),
        "context_token": raw.get("context_token")
        if isinstance(raw.get("context_token"), str)
        else "",
        "create_time_ms": as_int(raw.get("create_time_ms")) or 0,
        "message_type": as_int(raw.get("message_type")) or 0,
        "text": "\n".join(text_parts),
        "item_types": item_types,
        "raw": raw,
    }


def parse_json(raw: str) -> Any:
    """解析后端 JSON，把整数 id 保留为字符串（uint64 会超出浮点精度）。"""
    return json.loads(raw, parse_int=str)
