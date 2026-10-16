"""收消息轮询（捕获 context_token）。

后台长轮询 getupdates：来信里带着 context_token，推送时必须原样回传。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from client import ApiError, WeixinClient
from config import (
    BACKOFF_S,
    LONG_POLL_TIMEOUT_S,
    MAX_CONSECUTIVE_FAILURES,
    RETRY_S,
    STALE_TOKEN_ERRCODE,
    STALE_TOKEN_PAUSE_S,
    logger,
)
from protocol import as_int, parse_inbound_message
from state import StateStore


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
            ret = as_int(response.get("ret"))
            errcode = as_int(response.get("errcode"))
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
