#!/usr/bin/env python3
"""wxpush —— 轻量级微信消息推送服务（入口脚本）。

把微信当成你自己程序的告警 / 通知出口：纯标准库、内网无鉴权。
实现在同目录的若干模块里，本文件只负责转交给 `cli`。

用法（与原单文件版本完全一致）：

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

模块划分（都在本文件同目录，按职责分工）
----------------------------------------
    config     常量、超时与路径
    protocol   协议枚举、请求头与消息编解码
    client     微信后端 HTTP 客户端
    state      状态文件（凭据 + 联系人 + 轮询游标）
    login      扫码登录
    poller     收消息轮询
    pusher     发送
    keepalive  保活提醒
    service    把轮询、保活、发送组合成一个服务
    server     HTTP 接口
    cli        命令行
    qr         终端二维码编码器
"""

from cli import run

if __name__ == "__main__":
    raise SystemExit(run())
