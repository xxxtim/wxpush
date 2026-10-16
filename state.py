"""状态存储（单账号、单文件）。

状态文件里只有凭据、时间戳与计数 —— 对方发来的**正文不落盘**。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import (
    API_BASE_URL,
    DEFAULT_HOST,
    DEFAULT_KEEPALIVE_AFTER_HOURS,
    DEFAULT_KEEPALIVE_REPEAT_HOURS,
    DEFAULT_PORT,
    PROJECT_ROOT,
    PUSH_QUOTA_PER_INBOUND,
    STATE_FILENAME,
    logger,
)


class NoRecipient(RuntimeError):
    """无法确定该发给谁（没有联系人，或有多个且未绑定）。"""


def default_state_path() -> Path:
    env = os.environ.get("WXPUSH_HOME")
    if env:
        return Path(env).expanduser() / STATE_FILENAME
    return PROJECT_ROOT / STATE_FILENAME


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
    #: 本轮额度内**成功**发出的条数：对方每发来一条消息就清零，之后重新计到
    #: PUSH_QUOTA_PER_INBOUND 为止（保活提醒也算一条）。用来在第 10 条上附带
    #: 提醒，也用来解释「token 没过期却发不出去」。
    sent_since_inbound: int = 0
    #: 本轮这个计数是否可信。收到来信后清零重算，所以那时一定可信；但旧版本的
    #: 状态文件里没有这个计数，只要本轮已经推过消息，本地从 0 数就一定**偏小**
    #: （后端已经放行了几条只有它自己知道）。传输层出错时也会标成不可信 ——
    #: 消息可能其实已经送达并占掉一条额度。
    quota_synced: bool = True
    #: 本轮是否还欠对方一句额度提醒。计数不可信时（刚升级、传输层出错）要在
    #: 下一条推送里就请对方回一句，趁还发得出去的时候把额度归零；提醒一旦发出去
    #: 就清掉，避免同一个窗口里反复提醒。
    quota_note_pending: bool = False
    #: 上次**成功**发出保活提醒的时间；收到新消息（换发新 token）时清零。
    keepalive_reminded_at: int = 0
    #: 上次保活提醒**发送失败**的时间。用来给失败后的重试限速：没有它，
    #: 检查周期（60 秒）一到就会拿同一个失效的 token 反复重试、反复刷日志。
    keepalive_failed_at: int = 0
    #: 判定「凭据已不可用」时该联系人的 context_token。非空且与当前 token
    #: 相同时，说明这期间没收到过新消息 —— 再提醒也不可能成功，直接跳过。
    keepalive_failed_token: str = ""

    @property
    def age_s(self) -> int | None:
        return int(time.time() - self.token_updated_at) if self.token_updated_at else None

    @property
    def quota_remaining(self) -> int:
        """本轮额度还剩几条。0 表示下一条多半会被后端拒绝，得让对方回一句。"""
        return max(0, PUSH_QUOTA_PER_INBOUND - self.sent_since_inbound)

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
            # 换了新凭据，之前判定「发不出去」的结论作废，重新开始计时。
            contact.keepalive_failed_at = 0
            contact.keepalive_failed_token = ""
        contact.last_message_at = int(time.time())
        if create_time_ms:
            contact.last_message_at = max(contact.last_message_at,
                                          create_time_ms // 1000)
        contact.message_count += 1
        # 后端的推送额度是按「对方这条来信」重新计的：一条来信换 10 条推送。
        # 不管这条消息有没有带新 token，额度都重新开始算。
        contact.sent_since_inbound = 0
        contact.quota_synced = True
        contact.quota_note_pending = False
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
            last_message_at = int(value.get("last_message_at") or 0)
            last_sent_at = int(value.get("last_sent_at") or 0)
            if "quota_synced" in value:
                # 新版本写的状态文件：这两个判断本身也要原样恢复，重启后不能又当成
                # 可信 —— 重启并不会让漏数的那些推送回到计数里。
                quota_synced = bool(value.get("quota_synced"))
                quota_note_pending = bool(value.get("quota_note_pending"))
            elif "sent_since_inbound" in value:
                quota_synced = True
                quota_note_pending = False
            else:
                # 旧版本写下的状态文件没有这个计数。本轮还没推过消息（最近一次
                # 发出早于最近一次来信）时 0 就是对的；已经推过就不对了 —— 后端
                # 放行了几条只有它自己知道，标成不可信，下一条会附提醒请对方回一句。
                quota_synced = not (last_sent_at and last_sent_at >= last_message_at)
                quota_note_pending = not quota_synced
            contacts[user_id] = Contact(
                user_id=user_id,
                context_token=str(value.get("context_token") or ""),
                token_updated_at=int(value.get("token_updated_at") or 0),
                last_message_at=last_message_at,
                last_sent_at=last_sent_at,
                message_count=int(value.get("message_count") or 0),
                sent_since_inbound=int(value.get("sent_since_inbound") or 0),
                quota_synced=quota_synced,
                quota_note_pending=quota_note_pending,
                keepalive_reminded_at=int(value.get("keepalive_reminded_at") or 0),
                keepalive_failed_at=int(value.get("keepalive_failed_at") or 0),
                keepalive_failed_token=str(value.get("keepalive_failed_token") or ""),
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
