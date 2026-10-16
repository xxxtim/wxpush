"""命令行：`python wxpush.py ...`（或 `python -m wxpush ...`）。

不带子命令运行就是默认命令 cmd_run：没登录先扫码登录，登录了就启动服务。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Sequence

from config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    KEEPALIVE_TICK_S,
    PUSH_QUOTA_PER_INBOUND,
    STATE_FILENAME,
    TOKEN_TTL_HOURS,
    __version__,
)
from keepalive import KeepaliveMonitor
from login import do_login
from pusher import PushError
from server import build_server
from service import Service
from state import Contact, NoRecipient, State, StateStore


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
        # 换了登录凭据：之前判定「发不出去」的会话值得再试一次。
        for contact in state.contacts.values():
            contact.keepalive_failed_at = 0
            contact.keepalive_failed_token = ""

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
    print(f"  推送额度  : 对方每发一条消息可推 {PUSH_QUOTA_PER_INBOUND} 条"
          f"（实测限流；第 {PUSH_QUOTA_PER_INBOUND} 条会自动附上提醒，让对方发条消息续额度）")
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
        print(f"  {'user_id':<30} {'状态':<10} {'msgs':>5}  {'本轮额度':>8}  "
              f"{'最近来信':<17} {'最近发出':<17}")
        for contact in contacts:
            received = (time.strftime("%m-%d %H:%M:%S",
                                      time.localtime(contact.last_message_at))
                        if contact.last_message_at else "-")
            sent_at = (time.strftime("%m-%d %H:%M:%S",
                                     time.localtime(contact.last_sent_at))
                       if contact.last_sent_at else "-")
            used = min(contact.sent_since_inbound, PUSH_QUOTA_PER_INBOUND)
            quota = f"{used}/{PUSH_QUOTA_PER_INBOUND}"
            if not contact.quota_synced:
                quota += "?"  # 只数到这么多，后端可能已经用掉更多
            elif contact.quota_remaining == 0:
                quota += "!"  # 用满了：下一条要先让对方回一句
            print(f"  {contact.user_id[:29]:<30} "
                  f"{_contact_state_text(contact):<10} "
                  f"{contact.message_count:>5}  "
                  f"{quota:>8}  "
                  f"{received:<17} {sent_at:<17}")
    else:
        print("\n还没有联系人。请让目标微信号给机器人发一条消息。")

    print("\n提示：context_token 实测约 24 小时有效，对方每发一条消息都会刷新；"
          "标为「疑似过期」时仍会尝试发送，只有后端返回 -14 才代表真的失效。")
    print(f"      「本轮额度」是对方最后一条来信之后已推的条数，上限 "
          f"{PUSH_QUOTA_PER_INBOUND} 条（实测，微信没有明说）。带 ! 表示已用满，"
          f"下一条会失败；带 ? 表示这个数只是下界（服务重启前的推送、或上次传输层"
          f"出错的没被数到），真实剩几条只有后端知道 —— 两种都让对方回一条消息即可。")
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
    quota_used = result.get("quota_used")
    if isinstance(quota_used, int):
        limit = result.get("quota_limit") or PUSH_QUOTA_PER_INBOUND
        tail = ""
        if result.get("quota_synced") is False:
            tail = "（这个数只是下界：服务重启前推过的没被数到）"
        elif result.get("quota_remaining") == 0:
            tail = "（已用满，请让对方回一条消息）"
        print(f"本轮额度   : 第 {quota_used}/{limit} 条{tail}")
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
                # 手动强制：把「已暂停重试」和失败限速一并清掉，真的再试一次。
                contact.keepalive_failed_at = 0
                contact.keepalive_failed_token = ""

        store.update(force)
        monitor = KeepaliveMonitor(store)
        try:
            sent = monitor.check_once()
        finally:
            store.update(lambda s: setattr(s, "keepalive_after_hours", original))
        print(f"\n已强制发送 {sent} 条保活提醒。")
        if not sent:
            blocked = [c.user_id for c in store.load().contacts.values()
                       if c.quota_remaining == 0]
            if blocked:
                print(f"没有发出去：{'、'.join(blocked)} 的本轮推送额度（"
                      f"{PUSH_QUOTA_PER_INBOUND} 条）已用满，提醒也会被后端拒绝，"
                      f"需要对方先回一条消息。", file=sys.stderr)
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
    print(f"  {'user_id':<34} {'token 龄':>10}  {'距提醒':>10}  {'上次提醒':<16} 状态")
    suspended: list[str] = []
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
        if contact.quota_remaining == 0:
            note = "本轮额度已用满，提醒也发不出去（等对方来信）"
        elif (contact.keepalive_failed_token
                and contact.keepalive_failed_token == contact.context_token):
            note = "已暂停重试（凭据失效，等对方来信）"
            suspended.append(contact.user_id)
        elif contact.keepalive_failed_at:
            note = ("上次失败 "
                    + time.strftime("%m-%d %H:%M",
                                    time.localtime(contact.keepalive_failed_at)))
        else:
            note = ""
        print(f"  {contact.user_id[:33]:<34} {age_text:>10}  {remain:>10}  "
              f"{reminded:<16} {note}".rstrip())
    if suspended:
        print()
        print("「已暂停重试」表示后端已明确拒绝发送（凭据不可用），服务不会再每隔"
              "一分钟重试。恢复方式：")
        print("  1. 让对方给机器人发一条消息（换发新凭据，自动恢复）；")
        print("  2. 或者 `python wxpush.py login` 重新登录；")
        print("  3. 想立刻强制再试一次：`python wxpush.py keepalive --now`。")
    print()
    print("用 `python wxpush.py keepalive --now` 可立即发一条提醒来验证链路。")
    return 0


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
        description="轻量级微信消息推送服务（纯标准库、零依赖、内网无鉴权）。",
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


def run(argv: Sequence[str] | None = None) -> int:
    """脚本入口：解析参数并执行，把 Ctrl+C 统一成退出码 130。"""
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
