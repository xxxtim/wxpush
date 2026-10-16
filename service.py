"""Service：把状态、收消息轮询、保活提醒与发送组合成一件可用的东西。

HTTP 层（server）与命令行（cli）都只跟它对接口。
"""

from __future__ import annotations

from typing import Any

from client import WeixinClient
from config import PUSH_QUOTA_PER_INBOUND, TOKEN_TTL_HOURS, logger
from keepalive import KeepaliveMonitor
from poller import InboundPoller
from pusher import Pusher
from state import NoRecipient, StateStore


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
            # 后端按「对方每条来信后最多 10 条」限流（实测），这里只是把事实说清。
            "push_quota_per_inbound": PUSH_QUOTA_PER_INBOUND,
            "contacts_quota_exhausted": sorted(
                c.user_id for c in contacts
                if c.sent_since_inbound >= PUSH_QUOTA_PER_INBOUND),
            # 这些人的条数只是下界：后端可能早就用满了，别只看配额数字。
            "contacts_quota_unsynced": sorted(
                c.user_id for c in contacts if not c.quota_synced),
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
                # 实测限制：对方每来一条消息，最多只能回推 10 条。remaining 为 0
                # 时下一条多半会被后端拒绝（表现为 409/502），让对方回一句即可。
                "sent_since_inbound": contact.sent_since_inbound,
                "quota_limit": PUSH_QUOTA_PER_INBOUND,
                "quota_remaining": contact.quota_remaining,
                # False：条数只是下界（服务重启前推过，本地数不到），别当成准数。
                "quota_synced": contact.quota_synced,
            })
        return out

    def send(self, text: str, *, user_id: str | None = None) -> dict[str, Any]:
        """发送文本。`user_id` 仅供命令行临时指定，HTTP 接口不暴露。"""
        return self._pusher.send_text(text, user_id=user_id)
