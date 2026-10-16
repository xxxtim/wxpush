"""扫码登录：拿二维码、等待确认、返回凭据（由调用方落盘）。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable

from client import ApiError, WeixinClient
from config import API_BASE_URL, DEFAULT_BOT_TYPE, LOGIN_TIMEOUT_S, MAX_QR_REFRESHES
from qr import encode_matrix, render_terminal

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
