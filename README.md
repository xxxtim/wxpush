# wxpush

一个给自己用的微信推送服务：把微信当成你其他程序的告警和通知出口。任何语言的程序发一个 HTTP 请求过来，你手机上就收到一条微信。

代码就两个文件，`wxpush.py` 是服务本体，`qr.py` 负责在终端画登录二维码。仓库里没有第三方依赖。

需要 Python 3.9 或更高版本。

## 它做什么，不做什么

这是一个**单向**的通知下发工具：你的程序往里推，你手机上收到微信。刻意保持极简，因此下面这些事它不做：

- **不处理微信侧发来的消息。** 收到的消息只用来取出其中的 `context_token`，好让推送凭据继续有效（见下文「保活提醒」），正文不会被回复、转发或保存。
- **不落盘记录消息内容。** 状态文件里只有时间戳和计数，对方说过什么不会被写下来。
- **只支持纯文本。** 发不了图片、语音、文件、视频。

如果你需要**双向对话、多媒体消息、多账号**这类能力，请用微信官方的项目：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)

## 这个方案的取舍

**优点**

- **零依赖、零配置。** 只要机器上有 Python 3.9+，`python wxpush.py` 就能跑，不用装任何包。
- **不需要申请任何权限。** 用自己平时的微信号扫码就能用，不用注册公众号、不用企业认证、不用申请模板消息或其他接口权限，也不需要服务器和域名，内网跑就行。
- **谁都能用。** 整个流程只有一次扫码，之后调用方在任意语言、任意机器上发个 HTTP 请求即可。

**缺点**

- **每 24 小时要续一次凭证。** 推送用的 `context_token` 由微信后端在收到消息时下发，实测约 24 小时失效，而且**只能靠对方再发一条消息来续**，没法用代码刷新。本服务会在凭据快过期时主动发一条保活提醒（默认 22 小时），对方回一句即可；但对方长期不理的话，推送就会失效。
- **服务得一直开着。** 只有一直在线长轮询，才能及时收到消息、拿到新凭证。进程停了，续期也就停了。
- **只能推给一个人。** 收件人绑定在服务端，没有多用户的概念。

---


## 微信的token机制

发消息必须带一个叫 `context_token` 的东西，而这个 token 只会在**收到消息**的时候由后端下发。也就是说你造不出来它，也推不了一个从没跟你的机器人说过话的人。

所以这个服务一边在后台长轮询 `getupdates`，把每个给机器人发过消息的人、以及随那条消息一起下发的 `context_token` 记下来；一边开一个很小的 HTTP 接口，你发消息的时候它把存下来的 token 原样带上。

结论就一句：**你想推给谁，得先让谁给机器人发条消息。** `GET /api/contacts` 能看出当前谁可以推。

---

## 跑起来

不需要安装，直接跑：

```bash
python wxpush.py
```

第一次运行会先让你扫码登录，登录成功后提示下一步，再启动服务：

```
首次运行：需要先用微信扫码登录。
（二维码）
登录成功，凭据已保存。

下一步：运行 python wxpush.py 启动服务

wxpush 已启动 —— http://127.0.0.1:8765
  账号      : xxxx@im.bot
  联系人    : 0 个（其中 0 个可以推送）
  状态文件  : /path/to/wxpush.json

收消息轮询已在运行，收到消息会实时打印在下面。
服务监听所有网卡（0.0.0.0:8765），内网其他机器把 127.0.0.1 换成这台机器的内网 IP 即可。

接口（内网直接调用，无需鉴权）：
  GET  /api/status    服务状态
  GET  /api/contacts  可以推给谁
  POST /api/send      发送，请求体 {"text":"部署完成"}

  curl -X POST http://127.0.0.1:8765/api/send -H "Content-Type: application/json" -d '{"text":"部署完成"}'

────────────────────────────────────────────────────────────
还差一步才能推送：请用接收推送的那个微信号，给这个机器人随便发一句话。
消息一到，这里会打印「收到来信」，之后就能推送了。

按 Ctrl+C 停止。
```

凭据、联系人、轮询游标都存在 `wxpush.py` 同目录的 `wxpush.json` 里。这里面有你的 bot 凭据，别提交到仓库（`.gitignore` 已经忽略了）。想放别处就用 `WXPUSH_HOME` 环境变量或者 `--state`。

---

## 发消息

收件人是服务端定好的，调用方不用也不该传 user_id。

```bash
curl -s -X POST http://<内网IP>:8765/api/send \
  -H "Content-Type: application/json" \
  -d '{"text":"部署完成"}'
```

返回：

```json
{"ok": true, "user_id": "oABC...@im.wechat", "client_id": "wxpush-...",
 "server_message_id": "7412...", "text_length": 14}
```

Python 里这样写：

```python
import json, urllib.request

data = json.dumps({"text": "部署完成 ✅"}, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request("http://127.0.0.1:8765/api/send", data=data,
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req)
```

不想走 HTTP 也可以直接用命令行发：

```bash
python wxpush.py send "部署完成"
```

例如在 shell 脚本里加个别名，随手就能推一条：

```bash
alias notify='curl -s -X POST http://127.0.0.1:8765/api/send -H "Content-Type: application/json" -d'
notify '{"text":"备份完成"}' > /dev/null
```

---

## HTTP 接口

都返回 JSON，不需要任何鉴权头，只适合内网。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 存活检查 |
| `GET` | `/api/status` | 登录状态、轮询状态、当前收件人、联系人数量 |
| `GET` | `/api/contacts` | 现在能推给谁 |
| `POST` | `/api/send` | `{"text":"..."}`，不用传 user_id |

`/api/send` 可能返回的状态码：

| 状态码 | `error` | 含义 |
| --- | --- | --- |
| `200` | – | 后端已经收下了 |
| `400` | `bad_request` | `text` 缺失、空白、太长，或者不是合法 UTF-8 |
| `409` | `no_recipient` | 联系人不止一个又没绑定，`detail` 里有该执行的 `bind` 命令 |
| `409` | `no_context_token` / `stale_token` | 没有能用的 token，得让对方再发条消息 |
| `404` | `unknown_recipient` | `--to` 指定的收件人不是已知联系人 |
| `502` | `delivery_failed` | 后端拒了，或者传输失败 |

**`409` 和 `502` 建议当成同一种情况处理**，两者都带可执行的 `detail`，意思基本都是「让对方再给机器人发条消息」：

```python
if status in (409, 502):
    notify_user_another_way("推送凭据需要刷新")
```

有个实测的坑值得单独说：会话 token 不能用的时候，后端返回的是 `ret=-2 errmsg=prepare failed`，**不是 `-14`**，所以它会落到 `502 delivery_failed` 上。别看到 502 就以为是网络问题，`detail` 里有后端的原始返回，也有相应的提示。

---

## 命令行

| 命令 | 用途 |
| --- | --- |
| `python wxpush.py` | 自动判断：没登录就扫码登录，登录了就启动服务 |
| `python wxpush.py --port 9000` | 换端口启动，也可以用 `set --port` 永久保存 |
| `python wxpush.py --host 127.0.0.1` | 只允许本机访问（默认监听所有网卡） |
| `python wxpush.py login` | 重新扫码登录 |
| `python wxpush.py status` | 看登录状态、当前收件人、联系人 |
| `python wxpush.py bind <user_id>` | 绑定收件人，不带参数就显示当前绑定，`--clear` 解除 |
| `python wxpush.py send "你好"` | 直接发送，不经 HTTP，`--to` 可临时指定收件人 |
| `python wxpush.py keepalive` | 看保活提醒状态，`--now` 立即发一条 |
| `python wxpush.py set --port 9000` | 改监听地址和端口，或者开关保活提醒 |
| `python wxpush.py --help` | 所有参数 |

`status` 大致长这样：

```
状态文件   : /path/to/wxpush.json
登录状态   : 已登录
账号       : xxxx@im.bot
base_url   : https://ilinkai.weixin.qq.com
监听配置   : 0.0.0.0:8765
保活提醒   : 开启（token 龄达 22h 时提醒）
接收者     : oABC...@im.wechat
联系人     : 1 个

  user_id                        状态          msgs  最近来信              最近发出
  oABC...@im.wechat              有效             3  09-20 18:06:14    09-20 19:05:14
```

想看细节就在命令前面加 `-v`，比如 `python wxpush.py -v`，会额外打印每个 HTTP 请求、客户端断开连接，以及向微信后端发送「开始 / 停止接收消息」通知失败的原因；`收到来信`、`本轮收到 N 条消息` 属于普通日志，不加 `-v` 也会打印。

---

## token 到底能活多久

下面几条都是实测出来的，不是协议保证：

- 每收到一条消息就换发一个新 token。连续三条来信带了三个不同的 token，每个 token 里嵌着自己那条消息的时间戳。
- 旧 token 不会立刻失效。有一条 1.4 小时前的 token 依然被后端接受，说明新消息是又开了一个有效期窗口，而不是把旧窗口作废。
- 后端返回里没有任何有效期信息。一次完整的 `getupdates` 响应只有 `msgs`、`sync_buf`、`get_updates_buf` 三个顶层字段，所有时间字段描述的都是消息，从不描述 token 能活多久。
- 唯一能说明 token 失效的信号，是后端返回 `-14`。

实际用起来就是：只要对方偶尔发条消息，推送就一直是通的。本服务在每一条来信时都更新存下来的 token，所以发送时用的一定是最新的那个。

常说的 24 小时是个经验值，说了算的永远是后端。这个数字在这里只用来做提示：`/api/contacts` 会返回 `stale_suspected`、`token_age_s` 和 `token_issued_at`，你想提醒用户就自己判断；发送路径绝不会因为本地算出来的年龄去拒绝一次发送，真正失效的 token 会以 `409 stale_token` 或者 `502` 的形式返回。`/api/status` 里的 `contacts_pushable` 含义是「我们手上有个 token」，不按年龄过滤。

如果你想要更严格的策略，比如只推给 24 小时内说过话的人，那就在你自己的调用方按 `last_message_at` 过滤。那是产品决策，不是协议事实。

---

## 保活提醒

**问题在于**：token 只在收到消息时下发，一旦失效所有推送都会失败，而且**服务自己恢复不了**，只能等对方再发一条消息。

**解决办法**：token 龄快到时，服务主动给对方发一条微信，请他回一句。默认阈值是 22 小时，比实测的 24 小时窗口早一点，这样提醒本身还发得出去。

```
【wxpush 保活提醒】推送凭据即将到期，请回复本条消息任意内容以刷新凭据、继续接收推送。
```

对方回一条，计时就重置；不回的话，每 2 小时才再提醒一次，不会刷屏。提醒发送失败的原因会被记下来，在 `python wxpush.py keepalive` 或者 `/api/status` 里能看到。

```bash
python wxpush.py keepalive            # 看每个联系人的 token 龄和倒计时
python wxpush.py keepalive --now      # 立即发一条，验证链路是通的
python wxpush.py set --keepalive-off  # 关掉
```

---

## token 失效之后

有两种 token，坏掉以后的表现完全不一样。

### 会话 token（context_token）失效

| | 表现 |
| --- | --- |
| 发送 | 失败并报错 |
| 收消息轮询 | **照常工作**，它用的是 bot token |
| 怎么恢复 | 对方发一条消息，马上就能继续推 |

发送失败对应的状态码：

- 后端 `-14` → HTTP `409` `stale_token`
- 后端 `-2`（`prepare failed`）→ HTTP `502` `delivery_failed`
- 传输层错误 → HTTP `502` `delivery_failed`

### bot token（登录凭据）失效

这个更麻烦，它是所有功能的基础：

| | 表现 |
| --- | --- |
| 收消息轮询 | 打印 `轮询被后端拒绝：bot token 陈旧（errcode -14）`，然后**暂停一小时** |
| 发送 | 一样失败 |
| 怎么恢复 | 重新扫码：`python wxpush.py login` |

暂停期间 `/api/status` 里的 `poller.paused` 是 `true`，原因在 `poller.last_error`。

这里还有个容易误判的地方：bot token 陈旧时，`sendmessage` 报的是 `-2 prepare failed`，只有 `getupdates` 会报 `-14`。所以**如果所有发送突然全都失败，先怀疑登录凭据过期**，别只盯着会话 token。保活提醒存在的意义，就是在会话 token 失效之前提醒对方。

---

## 安全

不做鉴权是「单人 + 内网」场景下的刻意取舍，但有几个事实你要清楚：

- 它监听 `0.0.0.0`，内网任何机器都能调它，谁调谁就能往你微信发消息。
- 只部署在可信网络里。要只允许本机访问就加 `--host 127.0.0.1`，或者用防火墙限制来源。
- `wxpush.json` 里存着 bot 凭据，权限写成 `0600`（Windows 上是尽力而为），已经在 `.gitignore` 里。别提交，也别外传。
- 消息正文不落盘。状态文件里只有时间戳和计数，对方说过什么不会被记下来。

---

## 排查

**发了消息但控制台没反应** —— 空闲时轮询本来就安静。先看 `python wxpush.py status` 和 `/api/status`：后端拒绝时轮询会打印错误并暂停。另外每次 `getupdates` 会阻塞约 35 秒，几秒钟没输出是正常的。

**二维码扫不出来** —— 二维码是用 Unicode 实心和半块字符（`█ ▀ ▄`）画的，这是唯一能稳定被手机相机识别的字形。如果你的终端显示不了这些字符，直接用在手机微信里打开二维码下方打印的那个链接就能完成登录。

**报 `no_context_token` 或 `stale_token`** —— 对方必须先给机器人发过消息。`python wxpush.py status` 能看到当前收件人和 token 状态。

**报 `no_recipient`** —— 联系人有多个且没绑定。按 `detail` 的提示执行 `python wxpush.py bind <user_id>`。

**联系人显示 `stale_suspected`** —— 距 TA 上次说话超过约 24 小时了。发送还是会尝试，只是成功率不高，最好先提醒对方一句。

**轮询显示 paused** —— 后端返回了 `-14`，也就是 bot token 陈旧，轮询暂停一小时。要是反复出现，重新执行 `python wxpush.py login`。

**端口被占用** —— `python wxpush.py --port 9000`，或者用 `set --port 9000` 永久改掉。

---

## 运行时会产生的文件

| 路径 | 说明 |
| --- | --- |
| `wxpush.json` | 凭据、联系人、轮询游标，首次登录时创建 |
| `wxpush.json.tmp` | 原子写入用的临时文件 |
| `WXPUSH_HOME` / `--state` | 把状态文件放到别处 |

---

## 许可

MIT。这个项目是根据可观察到的协议行为独立实现的，与腾讯无关。
