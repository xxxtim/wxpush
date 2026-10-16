"""保活提醒：token 快过期时主动发一条微信，请对方回一句。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from client import ApiError, BusinessError, WeixinClient
from config import (
    KEEPALIVE_TICK_S,
    PREPARE_FAILED_RET,
    PUSH_QUOTA_PER_INBOUND,
    STALE_TOKEN_ERRCODE,
    logger,
)
from protocol import build_text_message
from state import Contact, State, StateStore


class KeepaliveMonitor:
    """保活提醒：token 快过期时主动发一条微信，请对方回一句。

    context_token 只在收到消息时下发/刷新，实测约 24 小时失效；一旦失效所有
    推送都会失败，而且服务自己无法恢复 —— 只能等对方再发一条消息。所以在
    token 龄达到 keepalive_after_hours 时提醒一次，对方没回应则按
    keepalive_repeat_hours 重复提醒，不刷屏。
    """

    #: 后端明确表示「凭据不可用」的返回码；这类失败重试不会成功。
    DEAD_CREDENTIAL_RETS = (STALE_TOKEN_ERRCODE, PREPARE_FAILED_RET)

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
        """列出当前需要提醒的联系人。

        三种「不该再提醒」的情况都会被跳过：
        1. token 还年轻，没到阈值；
        2. 上次提醒发出去了，重复间隔没到（不刷屏）；
        3. 凭据已经被后端判定失效 —— 除非后来又收到过消息（token 变了），
           否则重试永远不会成功，只会白刷日志。
        另外失败后的重试也按重复间隔限速，避免每个检查周期重试一次。
        还有一种是本轮推送额度已经用满：这时连提醒本身都发不出去，只能等
        对方回一条消息（额度随来信一起重置）。
        """
        if not state.keepalive_enabled:
            return []
        now = now if now is not None else time.time()
        after_s = state.keepalive_after_hours * 3600
        repeat_s = state.keepalive_repeat_hours * 3600
        due: list[Contact] = []
        for contact in state.sorted_contacts():
            if not contact.context_token or not contact.token_updated_at:
                continue
            if contact.sent_since_inbound >= PUSH_QUOTA_PER_INBOUND:
                # 额度用满：后端会直接拒绝，提醒也发不出去。第 10 条推送里
                # 已经带了「请回一条」的提示，这里只需安静等待来信。
                logger.debug("%s 本轮推送额度已用满，跳过保活提醒", contact.user_id)
                continue
            if now - contact.token_updated_at < after_s:
                continue
            if (contact.keepalive_failed_token
                    and contact.keepalive_failed_token == contact.context_token):
                continue
            if (contact.keepalive_reminded_at
                    and now - contact.keepalive_reminded_at < repeat_s):
                continue
            if (contact.keepalive_failed_at
                    and now - contact.keepalive_failed_at < repeat_s):
                continue
            due.append(contact)
        return due

    @staticmethod
    def build_reminder(contact: Contact) -> str:
        return ("【wxpush 保活提醒】凭据即将到期，"
                "给机器人随便发条消息即可继续接收推送。")

    def check_once(self, *, now: float | None = None) -> int:
        """检查一轮并按需发送提醒，返回发出的提醒条数。"""
        state = self.store.load()
        if not state.logged_in or not state.keepalive_enabled:
            return 0
        due = self.due_contacts(state, now=now)
        if not due:
            return 0

        stamp = int(now if now is not None else time.time())
        sent = 0
        for contact in due:
            body = build_text_message(contact.user_id, self.build_reminder(contact),
                                      context_token=contact.context_token)
            try:
                self._client_factory(state.base_url, state.token).send_message(body)
            except BusinessError as exc:
                # -14 / -2 都是「凭据不可用」，本地怎么重试都不会成功。
                dead = (exc.ret in self.DEAD_CREDENTIAL_RETS
                        or exc.errcode in self.DEAD_CREDENTIAL_RETS)
                if exc.ret == STALE_TOKEN_ERRCODE or exc.errcode == STALE_TOKEN_ERRCODE:
                    self.last_error = ("保活提醒发送失败：token 已失效（-14），"
                                       "请手动让对方发一条消息")
                elif exc.ret == PREPARE_FAILED_RET or exc.errcode == PREPARE_FAILED_RET:
                    self.last_error = (f"保活提醒发送失败：{exc} —— 会话 token 可能"
                                       f"已不可用，也可能是本轮推送额度用满"
                                       f"（处理方式相同），请手动让对方给机器人发一条消息")
                else:
                    self.last_error = f"保活提醒发送失败：{exc}"
                if dead:
                    self.last_error += "（已暂停对该联系人的重试，收到来信或重新登录后自动恢复）"
                logger.error("%s", self.last_error)
                self._record_failure(contact.user_id, contact.context_token,
                                     dead=dead, stamp=stamp)
                continue
            except (ApiError, OSError) as exc:
                # 传输层错误多半是暂时的：只按重复间隔限速重试，不判定凭据失效。
                self.last_error = f"保活提醒发送失败：{exc}"
                logger.error("%s", self.last_error)
                self._record_failure(contact.user_id, contact.context_token,
                                     dead=False, stamp=stamp)
                continue

            used = [contact.sent_since_inbound + 1]

            def mark(state_: State, uid=contact.user_id, ts=stamp) -> None:
                target = state_.contacts.get(uid)
                if target is not None:
                    target.keepalive_reminded_at = ts
                    target.keepalive_failed_at = 0
                    target.keepalive_failed_token = ""
                    # 提醒也是一次「发出」，但不碰 last_message_at（那是来信时间）。
                    target.last_sent_at = ts
                    # 后端按「对方每条来信后最多 10 条」限流，提醒同样占额度。
                    target.sent_since_inbound += 1
                    used[0] = target.sent_since_inbound

            self.store.update(mark)
            sent += 1
            self.reminders_sent += 1
            self.last_reminder_at = stamp
            self.last_error = ""
            logger.info("已向 %s 发出保活提醒（token 龄 %.1fh，本轮额度 %d/%d）",
                        contact.user_id, (contact.age_s or 0) / 3600,
                        used[0], PUSH_QUOTA_PER_INBOUND)
            if used[0] >= PUSH_QUOTA_PER_INBOUND:
                logger.warning("这条提醒用掉了当前凭据的最后一条推送额度，"
                               "之后的推送要等 %s 回一条消息才能继续",
                               contact.user_id)
        return sent

    def _record_failure(self, user_id: str, token: str, *, dead: bool,
                        stamp: int) -> None:
        """记下这次失败，用来给重试限速。

        `dead=True` 表示后端明确说凭据不可用：连同当时的 context_token 一起记下，
        在收到新消息（token 被换掉）之前都不再重试 —— 重试不可能成功，只会刷日志。
        """
        def apply(state: State) -> None:
            contact = state.contacts.get(user_id)
            if contact is None:
                return
            contact.keepalive_failed_at = stamp
            # 发送期间刚好收到了新消息，就当作没暂停过：那个新 token 值得试。
            if dead and contact.context_token == token:
                contact.keepalive_failed_token = token

        self.store.update(apply)

    def suspended_contacts(self) -> list[str]:
        """凭据已被判定失效、当前不会再重试的联系人。"""
        state = self.store.load()
        return sorted(c.user_id for c in state.contacts.values()
                      if c.keepalive_failed_token
                      and c.keepalive_failed_token == c.context_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "reminders_sent": self.reminders_sent,
            "last_reminder_at": int(self.last_reminder_at) if self.last_reminder_at else 0,
            "last_error": self.last_error,
            # 凭据失效、已停止重试的联系人（收到来信或重新登录后自动移出）。
            "suspended": self.suspended_contacts(),
        }
