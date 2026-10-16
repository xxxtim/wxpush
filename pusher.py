"""发送：把接收者解析成 context_token，发一条文本，并维护本轮推送额度。"""

from __future__ import annotations

import time
from typing import Any

from client import ApiError, BusinessError, WeixinClient
from config import (
    MAX_TEXT_CHARS,
    PREPARE_FAILED_RET,
    PUSH_QUOTA_NOTE,
    PUSH_QUOTA_NOTE_UNSYNCED,
    PUSH_QUOTA_PER_INBOUND,
    STALE_TOKEN_ERRCODE,
    logger,
)
from protocol import build_text_message
from state import Contact, State, StateStore


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

        # 本轮额度里这是第几条。第 PUSH_QUOTA_PER_INBOUND 条是最后一条能发出去
        # 的，所以在它的正文底部附一句提醒，让对方回一条把额度续上；本地计数不
        # 可信时提前到第一条就提醒（见 _attach_quota_note）。
        quota_seq = contact.sent_since_inbound + 1
        body_text, note = self._attach_quota_note(text, contact, quota_seq)

        # 刻意不检查 token 年龄：约 24 小时只是提示，权威是后端。
        body = build_text_message(target, body_text, context_token=contact.context_token)
        client_id = str(body["msg"]["client_id"])
        try:
            response = self.client().send_message(body)
        except BusinessError as exc:
            if exc.ret == STALE_TOKEN_ERRCODE or exc.errcode == STALE_TOKEN_ERRCODE:
                raise StaleToken(
                    "后端认为该会话 token 已失效，请让对方再给机器人发一条消息") from exc
            # 实测：会话 token 不可用时后端不一定返回 -14，更常见的是
            # `ret=-2 prepare failed`。**推送额度用满返回的是同一个 -2**（线上
            # 事故就是这么发生的：token 还在 24 小时内，第 11 条被拒）。所以
            # 不猜原因，但把「额度用满」当成首要嫌疑，重新登录放到最后再说。
            hint = ""
            if exc.ret == PREPARE_FAILED_RET:
                if quota_seq >= PUSH_QUOTA_PER_INBOUND or not contact.quota_synced:
                    local = ("（本地计数不可信，本窗口只数到 "
                             f"{quota_seq - 1} 条）" if not contact.quota_synced else "")
                    hint = (f"（后端 prepare 失败：首要嫌疑是本轮推送额度已用满 —— "
                            f"微信对每条来信最多放行 {PUSH_QUOTA_PER_INBOUND} 条，"
                            f"让对方回一条消息即可继续{local}；若对方刚回过消息仍然"
                            f"失败，再考虑运行 python wxpush.py login 重新登录）")
                else:
                    hint = (f"（后端 prepare 失败：可能是该会话 token 已不可用，或 "
                            f"bot token 已陈旧；也可能是本轮额度用满（本地数到 "
                            f"{quota_seq - 1}/{PUSH_QUOTA_PER_INBOUND} 条）—— "
                            f"先让对方回一条消息，始终失败再运行 "
                            f"python wxpush.py login 重新登录）")
            raise DeliveryFailed(f"发送失败：{exc}{hint}") from exc
        except ApiError as exc:
            # 传输层出错时消息**可能**已经送达后端（比如响应在回程丢了），那样它会
            # 占掉一条额度而我们没记上。把这轮标成「计数不可信 + 欠一句提醒」，下一条
            # 推送就会请对方回一句，免得不知不觉撞上 10 条上限。
            try:
                self._mark_quota_unsynced(target)
            except Exception:  # noqa: BLE001 - 记账失败不能盖掉真正的发送错误
                logger.debug("标记额度计数不可信失败", exc_info=True)
            raise DeliveryFailed(f"发送传输错误：{exc}") from exc

        server_id = response.get("message_id")
        used = [quota_seq]

        def touch(state_: State) -> None:
            target_contact = state_.contacts.setdefault(target, Contact(user_id=target))
            # 只记「发出」时间，绝不写 last_message_at（那是来信时间）。
            target_contact.last_sent_at = int(time.time())
            target_contact.sent_since_inbound += 1
            if note:
                # 提醒已经随着这条发出去了，本轮不再重复提醒。
                target_contact.quota_note_pending = False
            used[0] = target_contact.sent_since_inbound

        self.store.update(touch)
        if note:
            logger.info("已在正文底部附上额度提醒（本轮第 %d 条，上限 %d%s）",
                        used[0], PUSH_QUOTA_PER_INBOUND,
                        "" if contact.quota_synced else "，本地计数不可信")
        return {
            "ok": True,
            "user_id": target,
            "client_id": client_id,
            "server_message_id": str(server_id) if server_id else "",
            "text_length": len(body_text),
            "quota_used": used[0],
            "quota_limit": PUSH_QUOTA_PER_INBOUND,
            "quota_remaining": max(0, PUSH_QUOTA_PER_INBOUND - used[0]),
            # False 表示本地条数偏小、只是下界（见 Contact.quota_synced）。
            "quota_synced": contact.quota_synced,
            "quota_note_appended": bool(note),
        }

    @staticmethod
    def _attach_quota_note(text: str, contact: Contact,
                           quota_seq: int) -> tuple[str, str]:
        """给这条推送拼上额度提醒，返回 (正文, 实际附加的提醒)。

        两种情况要提醒对方回一句：
        1. 这正是额度内最后一条（第 10 条）；
        2. 本轮欠着一句提醒（`quota_note_pending`）：计数不可信 —— 旧状态文件 /
           服务重启前推过 / 上次传输层出错可能已经送出去了。后端可能早就用掉了几
           条，等本地数到 10 就太晚了，所以趁现在还能发就先请对方把额度归零。
           发出去一次就清掉标记，不会反复提醒。

        正文本身就接近长度上限时不拼 —— 宁可少一句提醒，也不能让一次本来发得出去
        的推送被自己顶掉（标记不清，下一条再试）。
        """
        if quota_seq >= PUSH_QUOTA_PER_INBOUND:
            note = PUSH_QUOTA_NOTE
        elif contact.quota_note_pending:
            note = PUSH_QUOTA_NOTE_UNSYNCED
        else:
            return text, ""

        if len(text) + 2 + len(note) > MAX_TEXT_CHARS:
            logger.warning("正文已 %d 字，本条（第 %d/%d 条）不追加额度提醒",
                           len(text), quota_seq, PUSH_QUOTA_PER_INBOUND)
            return text, ""
        return f"{text}\n\n{note}", note

    def _mark_quota_unsynced(self, target: str) -> None:
        """把该联系人的本轮额度计数标成「不可信」，并要求下一条推送提醒对方。"""
        def apply(state_: State) -> None:
            contact = state_.contacts.get(target)
            if contact is not None:
                contact.quota_synced = False
                contact.quota_note_pending = True
        self.store.update(apply)
