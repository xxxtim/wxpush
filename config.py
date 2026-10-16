"""常量、路径与日志器。

这里只放不依赖其他 wxpush 模块的东西，任何模块都可以直接引用，不会形成环路。
"""

from __future__ import annotations

import logging
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

#: 微信后端入口（扫码登录用的固定域名）。
API_BASE_URL = "https://ilinkai.weixin.qq.com"

ILINK_APP_ID = "bot"
CLIENT_VERSION = "2.4.9"
DEFAULT_BOT_TYPE = "3"
BOT_AGENT = f"wxpush/{__version__}"

#: 项目根目录：入口 `wxpush.py`、状态文件与 `qr.py` 都在这一层。
PROJECT_ROOT = Path(__file__).resolve().parent

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

#: 实测（微信没有公开说明）：对方每发来一条消息，机器人最多只能回推 10 条。
#: 第 11 条会失败 —— 哪怕 context_token 还在 24 小时窗口内。所以第 10 条
#: （额度内最后一条能发出去的）要在正文底部带一句提醒，让对方回一条消息。
PUSH_QUOTA_PER_INBOUND = 10

#: 额度内最后一条推送自动追加的提示。**对方随便发一条消息**即可（不必回复这条），
#: 额度与 context_token 一起重置。
PUSH_QUOTA_NOTE = (
    f"【wxpush】本次额度已满（{PUSH_QUOTA_PER_INBOUND} 条），"
    f"给机器人随便发条消息即可继续接收推送。"
)

#: 本地计数不可信时用的提示（见 Contact.quota_synced）。这种情况下不知道后端
#: 已经放行了几条，所以不能断言「已用满」，但一样要请对方发一句，趁还发得出去
#: 的时候把额度归零。
PUSH_QUOTA_NOTE_UNSYNCED = (
    f"【wxpush】额度可能已满（每条来信限 {PUSH_QUOTA_PER_INBOUND} 条），"
    f"给机器人随便发条消息即可继续接收推送。"
)

#: 保活提醒：token 龄达到这个值（留出缓冲，保证提醒本身还发得出去）时提醒
#: 对方回一条；对方没回应就每 DEFAULT_KEEPALIVE_REPEAT_HOURS 再提醒一次。
DEFAULT_KEEPALIVE_AFTER_HOURS = 22.0
DEFAULT_KEEPALIVE_REPEAT_HOURS = 2.0
KEEPALIVE_TICK_S = 60.0

MAX_TEXT_CHARS = 4000
MAX_BODY_BYTES = 256 * 1024

#: 状态文件（含凭据，已加入 .gitignore）。
STATE_FILENAME = "wxpush.json"

#: 扫码登录：二维码最多刷新几次、整体等待多久。
MAX_QR_REFRESHES = 3
LOGIN_TIMEOUT_S = 480.0

#: 收消息轮询：连续失败到几次转长退避、退避与重试间隔、token 陈旧后的暂停时长。
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_S = 30.0
RETRY_S = 2.0
STALE_TOKEN_PAUSE_S = 3600.0

logger = logging.getLogger("wxpush")
