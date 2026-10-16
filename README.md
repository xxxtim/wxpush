# wxpush

一个给自己用的微信推送服务：把微信当成你其他程序的告警和通知出口。任何语言的程序发一个 HTTP 请求过来，你手机上就收到一条微信。

代码按职责拆成多个模块，全部平铺在项目根目录：`wxpush.py` 是入口脚本，`cli.py`、`service.py`、`pusher.py` 等是服务本体，`qr.py` 负责在终端画登录二维码。仓库里没有第三方依赖。

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

- **每 24 小时要续一次凭证。** 推送用的 `context_token` 由微信后端在收到消息时下发，实测约 24 小时失效，而且**只能靠对方再发一条消息来续**，没法用代码刷新。本服务会在凭据快过期时主动发一条保活提醒（默认 22 小时），对方发一条即可；但对方长期不理的话，推送就会失效。
- **对方每发一条消息，最多只能回推 10 条。** 这是微信没有写在文档里的限流（见下文「推送额度」）：第 11 条会被后端拒绝，哪怕 token 还在 24 小时窗口内。所以高频推送要自己控制节奏，或者靠第 10 条上自动附带的提醒（请对方发条消息）来续额度。
- **服务得一直开着。** 只有一直在线长轮询，才能及时收到消息、拿到新凭证。进程停了，续期也就停了。
- **只能推给一个人。** 收件人绑定在服务端，没有多用户的概念。

---


## 微信的token机制

发消息必须带一个叫 `context_token` 的东西，而这个 token 只会在**收到消息**的时候由后端下发。也就是说你造不出来它，也推不了一个从没跟你的机器人说过话的人。

所以这个服务一边在后台长轮询 `getupdates`，把每个给机器人发过消息的人、以及随那条消息一起下发的 `context_token` 记下来；一边开一个很小的 HTTP 接口，你发消息的时候它把存下来的 token 原样带上。

结论就一句：**你想推给谁，得先让谁给机器人发条消息。** `GET /api/contacts` 能看出当前谁可以推。

---

## 推送额度：每条来信最多回推 10 条

微信**没有明说**的一条限流，实测出来的：

- 对方每给机器人发一条消息，机器人最多只能回推 **10 条**，第 11 条被拒。
- 拒绝时返回的仍是那个含糊的 `ret=-2 errmsg=prepare failed` —— 和 token 失效**一模一样**。所以「token 还在 24 小时内却发不出去」，八成是额度用满，不是 token。
- 对方再发一条消息（**随便发什么都行，不必回复哪条**），额度和 `context_token` 一起重置。

额度内最后一条（第 10 条）的正文底部会自动附一句：

```
（你原本的消息正文）

【wxpush】本次额度已满（10 条），给机器人随便发条消息即可继续接收推送。
```

额度按联系人算，`GET /api/contacts` 里能看到；`POST /api/send` 的返回也带同一组字段：

```json
{"user_id": "oABC...@im.wechat", "sent_since_inbound": 7,
 "quota_limit": 10, "quota_remaining": 3, "quota_synced": true}
```

- `quota_remaining` 为 0 就代表下一条多半会被拒，调用方可以据此换渠道。
- `quota_synced` 为 `false` 表示 `sent_since_inbound` 只是**下界**：服务重启前推过的、或上次传输层出错（响应丢了但后端其实收到了）的都没被数到。这种窗口不等数到 10，**下一条**就提醒对方（文案换成「额度可能已用满」），发出去一次就不再重复。`status` 的额度列用 `10/10!` / `2/10?` 区分这两种情况。

其它几点：**保活提醒也占额度**（它就是一条推送），所以额度用满时保活会被跳过（`keepalive` 的状态列会写「本轮额度已用满，提醒也发不出去（等对方来信）」）；**提醒不保证消息不丢** —— 用满到对方回消息之间推送都会被拒（`502 delivery_failed`），要真不丢，调用方得把 409/502 当成「换渠道」的信号。

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
  推送额度  : 对方每发一条消息可推 10 条（实测限流；第 10 条会自动附上提醒，让对方发条消息续额度）
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

凭据、联系人、轮询游标都存在 `wxpush.json` 里 —— 它和入口 `wxpush.py` 同级，也就是项目根目录。这里面有你的 bot 凭据，别提交到仓库（`.gitignore` 已经忽略了）。想放别处就用 `WXPUSH_HOME` 环境变量或者 `--state`。

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
 "server_message_id": "7412...", "text_length": 14,
 "quota_used": 3, "quota_limit": 10, "quota_remaining": 7,
 "quota_synced": true, "quota_note_appended": false}
```

`quota_*` 描述的是「对方最后一条来信之后已经推了几条」（见上文「推送额度」）：`text_length` 是实际发出去的正文长度，如果这一条附带了那句提醒，`quota_note_appended` 会是 `true`，此时 `text_length` 也包含那句提醒。`quota_synced` 为 `false` 表示 `quota_used` 只是下界（服务重启前推过的没被数到），别拿它反推还剩几条。

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
| `502` | `delivery_failed` | 后端拒了，或者传输失败（含推送额度用满） |

**`409` 和 `502` 建议当成同一种情况处理**，两者都带可执行的 `detail`，意思基本都是「让对方再给机器人发条消息」：

```python
if status in (409, 502):
    notify_user_another_way("推送凭据需要刷新")
```

有个实测的坑值得单独说：**`ret=-2 errmsg=prepare failed` 是个筐，额度用满和 token 不可用都往里装**，而前者更常见。「token 明明没过期却发不出去」几乎都是这个 `-2`，所以：

- 别看到 502 就以为是网络问题，也别第一时间去重新扫码 —— **重新登录解决不了额度问题**，先让对方发条消息。
- `detail` 里带了后端的原始返回，服务还会按本地计数（以及计数是否可信）给出当前最可能的原因，比如「首要嫌疑是本轮推送额度已用满……让对方发条消息即可继续」。
- 只有 `-14`（`409 stale_token`）才是后端明确说 token 失效。

真正的传输失败（超时、连不上）也是 `502`，但那种 `detail` 写的是「发送传输错误」，可以直接重试。

**每一次调用都会在控制台留痕**，不用加 `-v`：成功是 `POST /api/send 已推送 → ...`，失败是带着状态码和 `detail` 的一行。所以调用方报错时，服务端控制台一定对得上：

```
2026-10-16 08:50:51 INFO    POST /api/send 已推送 → oABC...@im.wechat（12 字，本轮额度 3/10）
2026-10-16 08:50:51 INFO    已在正文底部附上额度提醒（本轮第 10 条，上限 10）
2026-10-16 08:50:51 WARNING POST /api/send 来自 127.0.0.1 → 409 no_context_token：联系人 ... 还没有 context_token
2026-10-16 08:50:51 ERROR   POST /api/send 来自 127.0.0.1 → 502 delivery_failed：发送失败：ret=-2 errmsg=prepare failed（后端 prepare 失败：...）
```

`4xx` 记 `WARNING`，`502`/`500` 记 `ERROR`，行里带的 `detail` 就是响应体里那个 `detail`。未知路径的 `404 not_found`（浏览器、扫描器打的）只进 debug，不刷屏。

---

## 命令行

| 命令 | 用途 |
| --- | --- |
| `python wxpush.py` | 自动判断：没登录就扫码登录，登录了就启动服务 |
| `python wxpush.py --port 9000` | 换端口启动，也可以用 `set --port` 永久保存 |
| `python wxpush.py --host 127.0.0.1` | 只允许本机访问（默认监听所有网卡） |
| `python wxpush.py login` | 重新扫码登录 |
| `python wxpush.py status` | 看登录状态、当前收件人、联系人、本轮推送额度 |
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

  user_id                        状态          msgs      本轮额度  最近来信              最近发出
  oABC...@im.wechat              有效             3        10/10!  09-20 18:06:14    09-20 19:05:14
  oDEF...@im.wechat              有效             1         2/10?  09-20 19:31:02    09-20 19:40:11
```

「本轮额度」是对方最后一条来信之后已推的条数：`10/10!` 表示已用满（下一条会失败，等对方发条消息即可），`2/10?` 表示这个数只是下界 —— 服务重启前推过的没被数到，真实剩几条只有后端知道（见「推送额度」一节）。

想看细节就在命令前面加 `-v`，比如 `python wxpush.py -v`，会额外打印每个 HTTP 请求、客户端断开连接，以及向微信后端发送「开始 / 停止接收消息」通知失败的原因；`收到来信`、`本轮收到 N 条消息` 属于普通日志，不加 `-v` 也会打印。`/api/send` 的每次调用和每次失败同样不加 `-v` 就会打印，见上面「HTTP 接口」一节。

---

## token 到底能活多久

下面几条都是实测出来的，不是协议保证：

- 每收到一条消息就换发一个新 token。连续三条来信带了三个不同的 token，每个 token 里嵌着自己那条消息的时间戳。
- 旧 token 不会立刻失效。有一条 1.4 小时前的 token 依然被后端接受，说明新消息是又开了一个有效期窗口，而不是把旧窗口作废。
- 后端返回里没有任何有效期信息。一次完整的 `getupdates` 响应只有 `msgs`、`sync_buf`、`get_updates_buf` 三个顶层字段，所有时间字段描述的都是消息，从不描述 token 能活多久。
- 唯一能说明 token 失效的信号，是后端返回 `-14`。

实际用起来就是：只要对方偶尔发条消息，推送就一直是通的。本服务在每一条来信时都更新存下来的 token，所以发送时用的一定是最新的那个。

常说的 24 小时是个经验值，说了算的永远是后端。这个数字在这里只用来做提示：`/api/contacts` 会返回 `stale_suspected`、`token_age_s` 和 `token_issued_at`，你想提醒用户就自己判断；发送路径绝不会因为本地算出来的年龄去拒绝一次发送，真正失效的 token 会以 `409 stale_token` 或者 `502` 的形式返回。`/api/status` 里的 `contacts_pushable` 含义是「我们手上有个 token」，不按年龄过滤。

token 没过期却发不出去时，**先看额度**再看年龄：一条来信只换 10 条推送，超了就会被后端拒（见「推送额度」）。`/api/contacts` 的 `quota_remaining` 和 `/api/status` 的 `contacts_quota_exhausted` 就是给这种情况准备的。

如果你想要更严格的策略，比如只推给 24 小时内说过话的人，那就在你自己的调用方按 `last_message_at` 过滤。那是产品决策，不是协议事实。

---

## 保活提醒

**问题在于**：token 只在收到消息时下发，一旦失效所有推送都会失败，而且**服务自己恢复不了**，只能等对方再发一条消息。

**解决办法**：token 龄快到时，服务主动给对方发一条微信，请他发条消息。默认阈值是 22 小时，比实测的 24 小时窗口早一点，这样提醒本身还发得出去。

```
【wxpush 保活提醒】凭据即将到期，给机器人随便发条消息即可继续接收推送。
```

对方发一条，计时就重置；不发的话，每 2 小时才再提醒一次，不会刷屏。提醒发送失败的原因会被记下来，在 `python wxpush.py keepalive` 或者 `/api/status` 里能看到。

**提醒本身也占推送额度**（它就是一条推送）。额度用满时服务会跳过保活提醒并且不再重试 —— 那一刻提醒也发不出去，只能等对方发消息，而第 10 条推送里已经带了同样的提示。这种情况在 `python wxpush.py keepalive` 的「状态」列里写作「本轮额度已用满，提醒也发不出去（等对方来信）」。

**发送失败不会一直重试。** 如果后端明确拒绝（`-14`，或 `-2 prepare failed`），说明凭据已经不可用，本地再怎么试都不会成功 —— 这时服务会**停止**对这个联系人的重试，不会再每分钟刷一条一模一样的错误日志，等对方发来消息换发新凭据、或者你重新 `login` 之后自动恢复。`python wxpush.py keepalive` 的「状态」列和 `/api/status` 里的 `keepalive.suspended` 能看到谁被暂停了。传输层错误（超时、连不上）属于暂时性故障，不会判定成凭据失效，只是按上面的重复间隔限速重试。

```bash
python wxpush.py keepalive            # 看每个联系人的 token 龄和倒计时
python wxpush.py keepalive --now      # 立即发一条，验证链路是通的
python wxpush.py set --keepalive-off  # 关掉
```

---

## token 失效之后

有两种 token，坏掉以后的表现完全不一样。另外还有一类失败跟 token 毫无关系 —— 推送额度用满（`ret=-2`），它的表现和会话 token 失效几乎一样，而且**更常见**，所以先看下面第三节。

### 会话 token（context_token）失效

| | 表现 |
| --- | --- |
| 发送 | 失败并报错 |
| 收消息轮询 | **照常工作**，它用的是 bot token |
| 怎么恢复 | 对方发一条消息，马上就能继续推 |

发送失败对应的状态码：

- 后端 `-14` → HTTP `409` `stale_token`（后端**明确**说 token 失效，只有这一种）
- 后端 `-2`（`prepare failed`）→ HTTP `502` `delivery_failed`（**额度用满也是这个**）

### 推送额度用满

表现和会话 token 失效**几乎一模一样**，都是 `-2 prepare failed` → `502 delivery_failed`，区别只在原因：

| | 表现 |
| --- | --- |
| 发送 | 第 11 条起失败并报错 |
| 收消息轮询 | **照常工作**，和额度无关 |
| token | 还在 24 小时窗口内，`stale_suspected` 是 `false` |
| 怎么判断 | `/api/contacts` 的 `quota_remaining` 为 0，或 `/api/status` 的 `contacts_quota_exhausted` 里有这个人；计数不可信时看 `quota_synced` / `contacts_quota_unsynced` |
| 怎么恢复 | 对方发一条消息，额度立刻回到 10 条 |

第 10 条推送会自动带上提醒，所以正常情况下对方看到那条就会发一条，你不会撞上这个错。

### 传输层错误

| | 表现 |
| --- | --- |
| 发送 | HTTP `502`，但 `detail` 里写的是「发送传输错误」，不是后端返回的 `ret=` |
| 怎么恢复 | 直接重试，凭据和额度都没问题 |

### bot token（登录凭据）失效

这个更麻烦，它是所有功能的基础：

| | 表现 |
| --- | --- |
| 收消息轮询 | 打印 `轮询被后端拒绝：bot token 陈旧（errcode -14）`，然后**暂停一小时** |
| 发送 | 一样失败 |
| 怎么恢复 | 重新扫码：`python wxpush.py login` |

暂停期间 `/api/status` 里的 `poller.paused` 是 `true`，原因在 `poller.last_error`。

这里还有个容易误判的地方：bot token 陈旧时，`sendmessage` 报的是 `-2 prepare failed`，只有 `getupdates` 会报 `-14`。所以判断顺序应该是：**先看是不是额度用满（一条来信换 10 条，撞了就是它），再看是不是所有发送全都失败（那才是登录凭据过期）**。只盯着 `-2` 就去重新扫码，很可能白扫一次 —— 额度的问题扫码解决不了。

---

## 安全

不做鉴权是「单人 + 内网」场景下的刻意取舍，但有几个事实你要清楚：

- 它监听 `0.0.0.0`，内网任何机器都能调它，谁调谁就能往你微信发消息。
- 只部署在可信网络里。要只允许本机访问就加 `--host 127.0.0.1`，或者用防火墙限制来源。
- `wxpush.json` 里存着 bot 凭据，权限写成 `0600`（Windows 上是尽力而为），已经在 `.gitignore` 里。别提交，也别外传。
- 消息正文不落盘。状态文件里只有时间戳和计数，对方说过什么不会被记下来。

---

## 排查

**调用方报 `HTTP Error 502`（或 409 / 400），想知道到底发生了什么** —— 线索就在控制台：每次 `/api/send` 都会留下日志，`502` 是 `ERROR`，`4xx` 是 `WARNING`，行尾就是把返回给调用方的 `detail` 原样抄了一份。`502 delivery_failed` 里如果是 `ret=-2 errmsg=prepare failed`，**先按额度用满排查**（`detail` 会直接写出来），再考虑 token；确实不是额度、而且所有发送都失败，才去重新登录。注意日志级别是 `INFO` 起步，不用加 `-v` 也能看到。

**token 没过期却发不出去了（第 11 条起）** —— 这是推送额度用满，不是 token 的问题。对方每发一条消息只换 10 条推送，超了后端就拒。看 `python wxpush.py status` 的「本轮额度」列（`10/10!` 是确定用满，`2/10?` 是本地只数到 2、后端可能更多）或 `/api/contacts` 的 `quota_remaining` / `quota_synced`；让对方发条消息即可恢复。服务本来就会在第 10 条（或计数不可信时的下一条）的正文底部附上同样的提醒。

**刚升级过版本，怕计数对不上** —— 升级后第一个窗口本地计数是从 0 起的，后端却可能已经用掉几条。服务会把这个窗口标成「计数不可信」，并在**第一条**推送里就附上提醒请对方发条消息；对方一发，窗口归零、计数就准了。`status` 的额度列这时显示 `?`。

**发了消息但控制台没反应** —— 空闲时轮询本来就安静。先看 `python wxpush.py status` 和 `/api/status`：后端拒绝时轮询会打印错误并暂停。另外每次 `getupdates` 会阻塞约 35 秒，几秒钟没输出是正常的。

**二维码扫不出来** —— 二维码是用 Unicode 实心和半块字符（`█ ▀ ▄`）画的，这是唯一能稳定被手机相机识别的字形。如果你的终端显示不了这些字符，直接用在手机微信里打开二维码下方打印的那个链接就能完成登录。

**报 `no_context_token` 或 `stale_token`** —— 对方必须先给机器人发过消息。`python wxpush.py status` 能看到当前收件人和 token 状态。

**报 `no_recipient`** —— 联系人有多个且没绑定。按 `detail` 的提示执行 `python wxpush.py bind <user_id>`。

**联系人显示 `stale_suspected`** —— 距 TA 上次说话超过约 24 小时了。发送还是会尝试，只是成功率不高，最好先提醒对方一句。

**轮询显示 paused** —— 后端返回了 `-14`，也就是 bot token 陈旧，轮询暂停一小时。要是反复出现，重新执行 `python wxpush.py login`。

**保活提醒不再重试了** —— 后端已经明确拒绝发送（`-14` / `-2 prepare failed`），说明凭据不可用（也可能是额度用满，处理方式一样），服务停止刷日志等你处理。让对方给机器人发一条消息，或者 `python wxpush.py login` 重新登录，就会自动恢复；`python wxpush.py keepalive` 的「状态」列能看到哪些联系人被暂停。想立刻强制再试一次用 `keepalive --now`。

**保活提醒一直不发** —— 先看 `python wxpush.py keepalive` 的状态列：如果写的是「本轮额度已用满」，那是推送额度用尽（提醒也发不出去），让对方发条消息即可；如果 token 龄还没到阈值，那就是还没到时候。

**端口被占用** —— `python wxpush.py --port 9000`，或者用 `set --port 9000` 永久改掉。

---

## 代码结构

入口是项目根目录的 `wxpush.py`（等价于 `python -m wxpush`），它只负责转交给 `cli.py`。其余模块都在同一层，按职责分工：

| 文件 | 说明 |
| --- | --- |
| `cli.py` | 命令行：`run` / `login` / `status` / `bind` / `send` / `set` / `keepalive` |
| `config.py` | 常量、超时、路径 |
| `protocol.py` | 协议枚举、请求头、消息编解码 |
| `client.py` | 微信后端 HTTP 客户端 |
| `state.py` | 状态文件（凭据 + 联系人 + 轮询游标） |
| `login.py` | 扫码登录 |
| `poller.py` | 收消息轮询（拿 `context_token`） |
| `pusher.py` | 发送与推送额度记账 |
| `keepalive.py` | 保活提醒 |
| `service.py` | 把上面几件东西组合成一个服务 |
| `server.py` | HTTP 接口 |
| `qr.py` | 终端二维码编码器（只被 `login.py` 用到，可单独拿走） |

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
