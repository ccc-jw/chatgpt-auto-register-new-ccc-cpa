# 新入职实习生项目指导说明书

> 项目：`chatgpt-auto-register`  
> 角色视角：项目组老人 / 维护者  
> 生成时间：2026-06-06  
> 适用对象：刚接手本项目、需要快速理解结构并安全参与维护的实习生  
> 相关文档：
>
> - `docs/analysis/project-analysis.md`
> - `docs/analysis/python-files-and-apis.md`

---

## 0. 先说结论：你要怎么理解这个项目

这个项目表面上叫 `chatgpt-auto-register`，但你不要把它当成一个简单脚本看。

从项目演化过程看，它现在已经是一个“多入口、多外部依赖、多阶段自动化平台”，里面至少叠了几条线：

1. **Phase 1：手机号接码 + 协议级注册**
2. **Phase 2：邮箱绑定 + OAuth + SUB2API 上传**
3. **单机 Web GUI：本地 Flask 控制台**
4. **多用户 Web 平台：JWT + PostgreSQL + 邀请码 + 额度 + 管理后台**
5. **支付 / Plus 相关实验链路**
6. **邮箱池、接码平台、代理、iCloud、Outlook、MailManage 等外部资源集成**

所以你接手时第一件事不是急着改代码，而是先搞清楚：

> 你改的是哪条链路？入口是谁？调用到哪里？失败会影响谁？会不会动到外部账号、余额、token 或用户数据？

---

## 1. 安全与合规边界：这部分必须先看

本项目涉及自动化注册、接码、OAuth、邮箱绑定、支付等敏感能力。你在参与维护时必须遵守以下规则：

1. **不要在未授权环境运行真实注册、支付或账号操作。**
2. **不要提交任何真实密钥、cookies、session token、access token、手机号、邮箱池文件。**
3. **不要为了“测试通过”绕过鉴权、风控、安全校验或删除失败处理。**
4. **不要把生产配置写死在代码里。**
5. **不要把 token 打到日志里。**
6. **不要直接改核心协议请求参数，除非你知道它在哪个流程里被用到，并且有回归验证方案。**
7. **如果需求涉及支付、账号、验证码、风控、绕过限制等内容，先找导师确认授权范围。**

你可以做的安全范围内工作包括：

- 代码结构整理。
- 配置模型统一。
- 测试补充。
- 日志脱敏。
- 文档补充。
- 错误码规范化。
- 本地 GUI / 多用户平台的工程质量改进。
- 外部接口 mock 测试。

---

## 2. 你应该先建立的项目心智模型

### 2.1 三个主要入口

这个项目有三个主要入口，你要分清楚：

| 入口 | 文件 | 说明 | 适合谁用 |
|---|---|---|---|
| CLI | `auto_register.py` | 命令行注册入口，也是 Phase 1 主流程所在地。 | 开发调试、脚本调用 |
| 单机 GUI | `web_gui.py`，由 `python auto_register.py --gui` 启动 | 本地 Flask 页面，适合单机操作。 | 本地使用者 |
| 多用户平台 | `server.py` + `public/index.html` | 带登录、额度、邀请码、卡密、管理后台。 | 平台化部署 |

你要记住：

```text
CLI 和单机 GUI 最终都会走 auto_register.register_one()
多用户平台则是 server.py -> runner.py -> auto_register.register_one()
```

也就是说，**`auto_register.register_one()` 是 Phase 1 的核心发动机**。

---

### 2.2 主流程一图看懂

```text
用户入口
  │
  ├─ CLI: python auto_register.py
  │
  ├─ 单机 GUI: python auto_register.py --gui
  │
  └─ 多用户平台: python server.py
          │
          ▼
      runner.py
          │
          ▼
auto_register.register_one()
          │
          ├─ smsbower.py：获取手机号 / 等验证码
          ├─ chatgpt_register.py：协议注册主步骤
          └─ sentinel.py：生成 Sentinel token
```

Phase 2 另有一条后续链路：

```text
openai_pipeline.py
  ├─ Phase 1 注册
  ├─ 获取邮箱：iCloud / MailManage / Outlook
  ├─ openai_bind_email.py：绑定邮箱 + OAuth
  └─ phase2_codex.py / openai_oauth.py：上传 SUB2API / token exchange
```

支付相关是另一条独立实验链路：

```text
plus_payment.py
payment_protocol.py
stripe_http.py
paypal_http.py
gopay_pay.py
gopay_register.py
```

这条线风险更高，新人不要贸然动。

---

## 3. 推荐阅读顺序

不要一上来就读所有文件。这个项目文件多、历史包袱多，乱读会迷路。

### 第 1 轮：只理解主线

按这个顺序读：

1. `README.md`
2. `config.example.json`
3. `auto_register.py`
4. `smsbower.py`
5. `chatgpt_register.py`
6. `sentinel.py`

目标：你能讲清楚一次 Phase 1 注册从获取手机号到拿到 token 的调用链。

---

### 第 2 轮：理解 Web 入口

读：

1. `web_gui.py`
2. `server.py`
3. `runner.py`
4. `auth.py`
5. `db.py`
6. `public/index.html`

目标：你能讲清楚：

```text
页面点击开始注册
  -> API 请求
  -> 后台线程启动
  -> 调用 register_one
  -> 日志如何回到页面
```

---

### 第 3 轮：理解 Phase 2

读：

1. `openai_pipeline.py`
2. `openai_bind_email.py`
3. `openai_oauth.py`
4. `phase2_codex.py`
5. `icloud_hme.py`
6. `mailmanage_client.py`
7. `outlook_mail.py`

目标：你能讲清楚邮箱绑定、OAuth code、SUB2API 上传分别在哪些文件里。

---

### 第 4 轮：只在需要时看支付

支付相关文件不要作为新人第一阶段重点。

需要时再读：

- `plus_payment.py`
- `payment_protocol.py`
- `stripe_http.py`
- `paypal_http.py`
- `paypal_fraudnet.py`
- `gopay_pay.py`
- `gopay_register.py`
- `_upstream_payment.py`
- `_payment_jslib.py`

目标：能识别支付链路文件，不随便改。

---

## 4. 本地怎么启动

### 4.1 当前验证过的启动方式

当前本地已经验证过：

```bash
python3 auto_register.py --gui
```

访问：

```text
http://127.0.0.1:7777
```

注意：源码 `web_gui.py` 当前默认绑定 `0.0.0.0:7777`，局域网也可能访问到。学习环境启动前请确认处于可信网络或有防火墙限制，且不要在 GUI 中填入真实密钥、cookies 或 token。README 中曾写 `127.0.0.1:8080`，那是历史说明；以源码默认端口 `7777` 为准。

验证结果：

- `/` 返回 HTML 页面。
- `/api/status` 正常返回运行状态。
- `/api/config` 正常返回配置。

---

### 4.2 CLI 是否可用

已验证：

```bash
python3 auto_register.py --help
```

可以正常输出 CLI 参数。

---

### 4.3 多用户平台为什么暂时不要直接跑

多用户平台入口是：

```bash
python3 server.py
```

但当前环境缺少：

```text
psycopg2
pyjwt
apscheduler
PostgreSQL
```

所以你如果只是学习项目，不要第一时间跑 `server.py`。另外，`server.py` 在 import 后会立即执行 `db.init_db()` 并连接 `DB_URL` 指向的 PostgreSQL；在未配置数据库前，不要为了交互实验直接运行或导入它。若要写接口测试，应先 monkeypatch/mock `db.init_db`，或后续重构为 app factory，避免 import-time DB 副作用。

---

## 5. 你必须知道的核心文件

### 5.1 `auto_register.py`

这是 Phase 1 主入口。

你重点看：

- `load_config()`
- `register_one()`
- `main()`

最重要的是：

```python
register_one(...)
```

它负责串起：

1. 拿手机号。
2. 设置接码状态 ready。
3. 创建 `ChatGPTRegister`。
4. 访问登录页。
5. 获取 CSRF。
6. 发起 signin。
7. 跳转 auth。
8. 注册手机号和密码。
9. 发送 OTP。
10. 等接码平台验证码。
11. 校验 OTP。
12. 创建账户资料。
13. OAuth callback。
14. 获取 session token 和 access token。
15. 标记接码完成。

#### 改这个文件会影响什么？

会影响：

- CLI 注册。
- 单机 GUI 注册。
- 多用户平台注册。
- Phase 1 返回结果字段。
- 失败重试逻辑。
- 输出文件格式。

所以这个文件属于 **高影响文件**。

---

### 5.2 `chatgpt_register.py`

这是协议层核心。

重点类：

```python
class ChatGPTRegister
```

重点方法：

- `visit()`
- `get_csrf()`
- `signin()`
- `jump_to_auth()`
- `register_user()`
- `send_otp()`
- `validate_otp()`
- `visit_about_you()`
- `create_account()`
- `oauth_callback()`
- `get_access_token()`

#### 改这个文件会影响什么？

会影响所有 ChatGPT/Auth 协议步骤。这里任何一个 header、URL、JSON 字段、cookie 读取逻辑改错，都可能让整个注册链路失败。

新人不要随便改。

---

### 5.3 `smsbower.py`

这是 SMSBower 的接码客户端。

重点方法：

- `balance()`
- `get_number()`
- `set_ready()`
- `wait_code()`
- `complete()`
- `cancel()`

#### 改这个文件会影响什么？

会影响：

- 手机号获取。
- 验证码轮询。
- 号码完成/取消状态。
- 注册成本控制。

如果你改了 `wait_code()`，一定要确保 timeout、interval、取消状态都正确。

---

### 5.4 `sentinel.py`

这是 Sentinel token 生成器。

它负责请求：

```text
https://sentinel.openai.com/backend-api/sentinel/req
```

并根据返回结果生成：

```text
OpenAI-Sentinel-Token
OpenAI-Sentinel-SO-Token
```

#### 改这个文件会影响什么？

会影响注册、OTP 校验、创建账户等 Auth 请求是否能通过。

这是高风险文件。新人第一阶段不要改。

---

### 5.5 `web_gui.py`

这是单机 GUI。

它的问题是：**太大，职责太多**。

它现在承担：

- 页面返回。
- 配置保存。
- 任务启动。
- 日志队列。
- 结果下载。
- iCloud cookies 保存。
- Outlook 管理。
- Plus 升级入口。
- Phase 2 控制。

而且里面存在同名 `_run()` 重复定义的问题。

#### 改这个文件会影响什么？

可能影响整个本地 GUI。你改之前先确认：

- 改的是页面？
- 改的是 API？
- 改的是任务执行？
- 改的是日志？
- 改的是 Phase2？
- 改的是 Plus？

不要在一个 PR 里混着改。

---

### 5.6 `server.py`

这是多用户平台 API。

它提供：

- 登录。
- 注册。
- 用户配置。
- 注册任务 start/stop/log。
- 用户历史。
- 管理员统计。
- 邀请码。
- 卡密。
- 用户列表。
- 管理员资产。

#### 改这个文件会影响什么？

会影响多用户平台前后端交互和权限边界。

重点注意：

- `/api/member/*` 必须登录。
- `/api/admin/*` 必须管理员。
- 不要为了前端方便绕过装饰器。

---

### 5.7 `runner.py`

这是多用户平台的后台任务执行器。

它负责：

1. 每个用户只允许一个 active runner。
2. 创建后台线程。
3. 管理 stop event。
4. 建立 SSE queue。
5. 读取用户配置。
6. 获取邮箱。
7. 调用 `auto_register.register_one()`。
8. 记录日志。
9. 扣额度。
10. 清理任务状态。

#### 改这个文件会影响什么？

会影响多用户注册稳定性。

特别注意：

```python
db.consume_quota(user_id)
```

当前逻辑里失败也可能扣额度。你如果要改，必须先确认产品规则。

---

### 5.8 `db.py`

这是 PostgreSQL 数据层。

主要表：

- `users`
- `user_configs`
- `invite_keys`
- `card_keys`
- `user_card_access`
- `reg_logs`
- `admin_assets`

#### 改这个文件会影响什么？

会影响数据结构、登录、额度、卡密、日志、管理员资产。

特别注意：当前密码哈希是 SHA256，不适合生产。后续安全基线应改成 bcrypt。

---

### 5.9 `auth.py`

这是 JWT 鉴权。

重点函数：

- `make_token()`
- `decode_token()`
- `login_required()`
- `admin_required()`

#### 改这个文件会影响什么？

会影响登录态、管理员权限、所有受保护 API。

如果改 token payload，前端和后端所有读取角色/用户 ID 的地方都要回归。

---

## 6. Web 注册链路你要能背下来

### 6.1 多用户平台链路

```text
public/index.html
  │
  ├─ doLogin()
  │    └─ POST /api/auth/login
  │         └─ server.py api_login()
  │              ├─ db.verify_login()
  │              └─ auth.make_token()
  │
  ├─ saveCfg()
  │    └─ PUT /api/member/config
  │         └─ db.update_user_config()
  │
  ├─ startReg()
  │    └─ POST /api/member/register/start
  │         └─ runner.start(user_id, count)
  │              └─ Thread(target=runner._run)
  │                   ├─ db.get_user_config()
  │                   ├─ get_email_for_user()
  │                   ├─ auto_register.register_one()
  │                   ├─ db.log_reg()
  │                   └─ db.consume_quota()
  │
  └─ connectLog()
       └─ GET /api/member/register/log
            └─ SSE stream
```

### 6.2 单机 GUI 链路

```text
python auto_register.py --gui
  │
  └─ web_gui.start_gui()
       │
       ├─ GET /
       ├─ GET/POST /api/config
       ├─ POST /api/start
       │    └─ Thread(target=_run)
       │         └─ auto_register.register_one()
       ├─ GET /api/status
       ├─ GET /api/log-since/<cursor>
       └─ GET /api/download
```

---

## 7. 接口怎么理解

本项目接口分三类：

1. **项目内部 Flask API**
2. **接码平台 API**
3. **外部协议 API**

详细表在：

```text
docs/analysis/python-files-and-apis.md
```

你最先要掌握的是内部 API。

---

### 7.1 多用户平台常用 API

| API | 作用 |
|---|---|
| `POST /api/auth/login` | 登录，返回 JWT。 |
| `POST /api/auth/register` | 用邀请码注册。 |
| `GET /api/member/me` | 获取当前用户信息、配置和 iCloud 权益。 |
| `PUT /api/member/config` | 保存用户接码配置。 |
| `POST /api/member/register/start` | 启动注册任务。 |
| `POST /api/member/register/stop` | 停止注册任务。 |
| `GET /api/member/register/log` | SSE 注册日志。 |
| `GET /api/member/history` | 注册历史。 |
| `GET /api/admin/stats` | 管理员统计。 |
| `POST /api/admin/invite-gen` | 生成邀请码。 |
| `POST /api/admin/card-gen` | 生成卡密。 |
| `PUT /api/admin/assets` | 保存 iCloud cookies / MailManage key。 |

---

### 7.2 外部接口要谨慎

外部接口包括：

- SMSBower。
- 5sim。
- ChatGPT/Auth。
- Sentinel。
- SUB2API。
- iCloud。
- MailManage。
- Outlook / Microsoft Graph / IMAP。
- Stripe / PayPal / GoPay / Midtrans。

这些接口多数会产生真实外部副作用，比如：

- 消耗接码余额。
- 创建邮箱别名。
- 改变账号状态。
- 写入 SUB2API。
- 触发支付流程。

所以测试时优先使用 mock，不要直接连真实服务。

---

## 8. 这个项目当前最大的问题

你作为新人，先记住这几个“坑”。

### 8.1 结构混乱

根目录文件太多，很多模块职责混杂。

典型问题：

- `web_gui.py` 太大。
- 单机 GUI 和多用户平台逻辑重复。
- 支付相关文件散落在根目录和 `platforms/chatgpt/`。
- Phase 2 配置字段命名不统一。
- 多用户 runner 当前只执行 Phase 1，email 只是预分配/记录，不会传入 `register_one()`，也不会自动触发 Phase2/OAuth/SUB2API。

---

### 8.2 配置模型割裂

配置来源包括：

- `config.example.json`
- `config.json`
- 环境变量
- `config.py`
- Web GUI 内存状态
- PostgreSQL `user_configs`
- `admin_assets`

同一个含义可能有多个字段名，比如：

- `sub2api_password`
- `sub2api_pwd`
- `sub2api.password`
- `phase2.sub2api_password`

改配置相关代码时一定要检查入口和转换逻辑。

Phase 2 常见字段对照：

| 语义 | `config.example.json` | CLI 参数 | GUI / 内部配置 | 说明 |
|---|---|---|---|---|
| iCloud cookies | `phase2.icloud_cookies` | `--icloud-cookies` | `icloud_cookies` | 单机 GUI 还会读取 `icloud_cookies.json` / `cookies.json`。 |
| SUB2API URL | `phase2.sub2api_url` | `--sub2api-url` | `sub2api.url` / `sub2api_url` | 不同入口命名不统一。 |
| SUB2API 邮箱 | `phase2.sub2api_email` | `--sub2api-email` | `sub2api.email` / `sub2api_email` | 用于登录 SUB2API。 |
| SUB2API 密码 | `phase2.sub2api_password` | `--sub2api-pwd` | `sub2api.pwd` / `sub2api_password` | 同一语义至少三种名称。 |
| 绑定邮箱 | `phase2.bind_email` | `--bind-email` | `bind_email` | CLI `--phase2` 直接导入 session，不等于完整邮箱绑定 OAuth。 |
| 分组 | `phase2.sub2api_group` | `--sub2api-group-id` | `sub2api.group` / `group_ids` | 名称和 ID 两套表达。 |



---

### 8.3 安全基线不足

当前已知问题：

1. 默认管理员密码硬编码。
2. 用户密码使用 SHA256。
3. JWT secret 默认随机。
4. CORS 全开放。
5. 敏感数据明文存储。
6. 单机 GUI 默认监听 `0.0.0.0` 且无鉴权，可触发外部副作用。
7. 结果下载和本地自动落盘可能包含 session/access token。
8. SSE 鉴权设计存在问题，且不能把长期 JWT 放进 URL。
9. iCloud 权益当前是“尝试即消耗”：alias 创建成功或异常都会扣 `remaining_uses`，修改需产品确认。

这些是后续优先修复方向。

---

### 8.4 任务模型不可靠

当前任务靠进程内线程和队列：

```text
threading.Thread
queue.Queue
active_runners dict
```

这适合本地或低并发，但不适合生产：

- 重启任务丢失。
- 多进程部署状态不共享。
- SSE queue 在进程内。
- 任务没有持久化状态机。

---

## 9. 新人可以从哪些任务开始

### 9.1 推荐第一批任务

这些任务比较适合实习生，不容易造成外部副作用：

1. **补充测试**
   - `test_auto_register_retry.py`
   - `test_web_gui_stats.py`
   - `test_web_gui_html.py`

2. **文档整理**
   - 文件职责文档。
   - API 入参出参文档。
   - 本地启动文档。

3. **日志脱敏**
   - 避免完整 token 输出。
   - 避免导出 access token / session token。

4. **配置字段梳理**
   - 列出重复字段。
   - 先写文档和测试，不急着改实现。

5. **低风险 bugfix**
   - 修复前端显示问题。
   - 修复测试中的 mock。
   - 修复明显的编码/文案问题。
   - 例外：多用户 SSE 日志鉴权不是普通前端显示 bug，牵涉 token 传递和鉴权方案，必须先找导师确认。

---

### 9.2 不建议新人第一阶段做的任务

以下任务不要单独做，必须找导师一起看：

1. 改 `chatgpt_register.py` 请求参数。
2. 改 `sentinel.py` PoW/token 逻辑。
3. 改真实外部支付流程。
4. 改数据库表结构但没有 migration 方案。
5. 改鉴权逻辑但没有安全 review。
6. 改额度扣减规则但没有产品确认。
7. 改接码平台状态码处理但没有 mock 测试。
8. 修改 SSE 日志鉴权方案但没有安全 review；禁止把长期 JWT 直接拼到 URL。

---

## 10. 开发前检查清单

每次动代码前，先回答这 10 个问题：

1. 我改的是哪个入口？CLI、单机 GUI、多用户平台，还是 Phase 2 / 支付？
2. 这个文件是不是高影响文件？
3. 这个改动会不会调用真实外部接口？
4. 会不会消耗余额、创建资源、写入外部平台？
5. 会不会影响 token、cookies、密码、手机号、邮箱等敏感数据？
6. 会不会影响登录或管理员权限？
7. 有没有测试可以覆盖？没有的话能不能先写 mock 测试？
8. 输出日志里有没有敏感信息？
9. 失败时会不会正确 cancel / cleanup？
10. 改完怎么验证？

如果你不能回答，就先不要改。

---

## 11. 常见修改场景怎么做

### 11.1 修改注册主流程

涉及文件通常是：

- `auto_register.py`
- `chatgpt_register.py`
- `smsbower.py`
- `sentinel.py`

建议流程：

1. 先画出当前步骤。
2. 明确改哪一步。
3. 写 mock 测试。
4. 不直接跑真实注册。
5. 只在授权环境做 smoke。

必须回归：

- 手机号获取失败。
- OTP timeout。
- 注册被拒。
- 创建账户失败。
- 成功返回字段。
- stop_requested 中断。

---

### 11.2 修改多用户 API

涉及文件：

- `server.py`
- `auth.py`
- `db.py`
- `public/index.html`

建议流程：

1. 先确认接口路径。
2. 确认是否需要登录。
3. 确认是否 admin only。
4. 确认前端传参。
5. 确认数据库字段。
6. 写接口测试或最少手工 curl smoke。

注意：不要为了让前端“能请求到”就移除 `@auth.login_required` 或 `@auth.admin_required`。

特别注意：`/api/member/register/log` 的 SSE 当前前端主路径使用 `EventSource`，无法携带 Authorization header；这不是普通前端 bug。可选修复方向包括：fetch stream 保留 Authorization、短期一次性 SSE token、或 HttpOnly cookie session。方案需导师和安全评审确认，禁止把长期 JWT 放进 URL。

---

### 11.3 修改单机 GUI

涉及文件：

- `web_gui.py`

建议流程：

1. 先定位是 HTML、API、任务还是配置。
2. 避免顺手改多个功能。
3. 如果改前端，跑 `test_web_gui_html.py`。
4. 如果改状态/配置，跑 `test_web_gui_stats.py`。
5. 启动 `python3 auto_register.py --gui` 做 smoke。注意当前默认绑定 `0.0.0.0`，只在可信网络中启动，且不要填真实密钥。

---

### 11.4 修改 Phase 2

涉及文件：

- `openai_pipeline.py`
- `openai_bind_email.py`
- `openai_oauth.py`
- `phase2_codex.py`
- `icloud_hme.py`
- `mailmanage_client.py`
- `outlook_mail.py`

建议流程：

1. 先确认邮箱来源：iCloud、MailManage 还是 Outlook。
2. 确认当前入口是哪条路径：`upload_session()` 直接导入 codex-session，还是 `run_second_half()` 邮箱绑定 + OAuth code + exchange/upload。
3. 确认是否需要 OAuth URL。
4. 确认是否要上传 SUB2API。
5. 尽量 mock 外部接口。
6. 不要在本地误用真实 cookies/token。
7. 注意 `openai_pipeline.run_full()` 当前实际使用 iCloud alias；虽然有 MailManage helper，但没有完整接入 run_full orchestration。

---

### 11.5 修改支付相关

涉及文件：

- `plus_payment.py`
- `payment_protocol.py`
- `stripe_http.py`
- `paypal_http.py`
- `gopay_pay.py`
- `gopay_register.py`
- `_upstream_payment.py`

新人不要单独改。需要导师确认：

- 授权范围。
- 测试账号。
- 是否允许真实请求。
- 是否有沙箱环境。
- 是否会触发支付或外部交易。

---

## 12. 测试建议

### 12.1 当前已有测试

项目已有：

- `test_auto_register_retry.py`
- `test_icloud_phase2.py`
- `test_pipeline.py`
- `test_web_gui_html.py`
- `test_web_gui_stats.py`

注意：`test_pipeline.py` 更像集成/手工测试，可能访问外部服务。不要随便跑。

---

### 12.2 安全的优先测试

推荐先准备隔离环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

安全优先只跑根目录中不需要真实外部凭据的测试：

```bash
python3 -m unittest test_auto_register_retry.py
python3 -m unittest test_web_gui_html.py
python3 -m unittest test_web_gui_stats.py
```

`test_pipeline.py` 以及任何需要真实 SMSBower、iCloud、SUB2API、Outlook、支付凭证的脚本，都按手工集成测试处理，默认禁止新人自行运行。

如果缺依赖，根据错误再补。

---

### 12.3 推荐新增测试

当前项目测试文件都在根目录，短期建议继续沿用根目录 `test_*.py`，例如：

```text
test_config_loader.py
test_smsbower_client.py
test_registration_result.py
test_registration_service_failures.py
test_auth_password_hash.py
test_runner_stop.py
test_web_routes_auth.py
```

长期可以迁移到 `tests/` 目录，但要同步配置 unittest/pytest 发现规则，避免新旧测试散落。

优先 mock 外部 HTTP，不要真实请求。

---

## 13. 调试方法

### 13.1 看 CLI 是否正常

```bash
python3 auto_register.py --help
```

如果这个都失败，说明基础 import 或依赖有问题。

---

### 13.2 看单机 GUI 是否正常

```bash
python3 auto_register.py --gui
```

然后访问：

```text
http://127.0.0.1:7777/
http://127.0.0.1:7777/api/status
http://127.0.0.1:7777/api/config
```

---

### 13.3 多用户平台问题怎么查

如果 `server.py` 起不来，优先检查：

1. `psycopg2` 是否安装。
2. `pyjwt` 是否安装。
3. `apscheduler` 是否安装。
4. `DB_URL` 是否正确。
5. PostgreSQL 是否可连接。
6. `config.py` 中管理员配置是否安全。

---

### 13.4 注册失败怎么定位

按步骤定位，不要直接猜。

```text
1. SMSBower 是否拿到手机号？
2. set_ready 是否成功？
3. visit 登录页是否成功？
4. CSRF 是否拿到？
5. signin 是否返回 url？
6. auth 跳转是否有 Location？
7. register_user 是否有 continue_url？
8. OTP 是否发送？
9. wait_code 是否拿到验证码？
10. validate_otp 是否有 continue_url？
11. create_account 是否有 callback_url？
12. callback 后 cookie 里是否有 session_token？
13. /api/auth/session 是否有 accessToken？
```

每一步都对应具体代码位置，不要跨步骤猜测。

---

## 14. 代码风格建议

本项目历史代码偏脚本风格，所以你写新代码时要兼顾现状和可维护性。

建议：

1. 新增逻辑尽量写小函数。
2. 不要继续扩大 `web_gui.py`。
3. 复杂返回值用 dataclass。
4. 外部 API 错误要保留上下文，但不要打印敏感数据。
5. 新增配置要有默认值、文档和测试。
6. 能 mock 的外部请求必须 mock。
7. 不要引入全局可变状态，除非是现有模式下不得不做。
8. 不要在多个文件复制同一段协议逻辑。

---

## 15. 你要特别小心的敏感字段

以下字段不要直接打印、提交或导出：

- `smsbower.api_key`
- `MAILMANAGE_KEY`
- `icloud_cookies`
- `session_token`
- `access_token`
- `refresh_token`
- `JWT_SECRET`
- `ADMIN_PASSWORD`
- proxy 中的用户名密码
- Outlook 邮箱密码 / refresh token
- PayPal / GoPay / Stripe 相关 token

如果必须展示，使用脱敏：

```text
abcdef...1234
```

敏感文件命名和 `.gitignore` 也要核对。生成任何 cookies、token、result、pool 文件后先看 `git status`；常见敏感文件包括 `cookies.json`、`register_results*.json`、`*_cookies.json`、`outlook_pool*.json`、`email_pool*.json`。如果发现敏感文件已经进入版本控制，立即停止提交并找导师处理，不要自行继续提交。

---

## 16. 第一周学习安排

### Day 1：跑起来 + 读主线

目标：

- 能启动单机 GUI。
- 能解释 `auto_register.register_one()` 做了什么。

任务：

1. 读 `README.md`。
2. 跑 `python3 auto_register.py --help`。
3. 在可信网络/防火墙限制下跑 `python3 auto_register.py --gui`；不要填真实密钥。
4. 读 `auto_register.py`。
5. 画出 Phase 1 调用链。

---

### Day 2：读协议层和接码层

目标：

- 能解释 `ChatGPTRegister` 每个方法对应哪一步。
- 能解释 `SmsBower` 状态流转。

任务：

1. 读 `chatgpt_register.py`。
2. 读 `smsbower.py`。
3. 读 `sentinel.py`。
4. 不改代码，只写笔记。

---

### Day 3：读 Web 和多用户平台

目标：

- 能解释页面点击后到 runner 的调用链。

任务：

1. 读 `public/index.html`。
2. 读 `server.py`。
3. 读 `runner.py`。
4. 读 `auth.py`。
5. 读 `db.py`。

---

### Day 4：读 Phase 2

目标：

- 能解释 OAuth、邮箱绑定、SUB2API 上传分别在哪个文件。

任务：

1. 读 `openai_pipeline.py`。
2. 读 `openai_bind_email.py`。
3. 读 `phase2_codex.py`。
4. 读 `icloud_hme.py`。
5. 读 `mailmanage_client.py`。
6. 读 `openai_oauth.py`。
7. 读 `outlook_mail.py`。

---

### Day 5：做第一个低风险任务

推荐任务：

1. 补充文档。
2. 给 `SmsBower` 写 mock 单元测试。
3. 给结果导出增加脱敏测试。
4. 修复前端显示/文案。

不要第一周就改协议核心或支付。

---

## 17. 提交流程与提交前自查

如果当前目录不是 git 仓库，先确认真实仓库位置，不要在解压包/副本里假装提交。标准流程：

1. 从主分支新建功能分支。
2. 改前记录需求、入口和影响链路。
3. 小步修改，小步验证。
4. 提交前运行 `git status` 和 `git diff --stat`。
5. 检查不得包含 `config.json`、`.env`、cookies、results、token、邮箱池等敏感文件。
6. 按改动类型运行对应测试。
7. PR/提交说明写清楚：改动入口、影响链路、验证命令、是否触达外部接口、是否需要导师/安全 review。

提交前至少确认：

```text
[ ] 没有提交真实配置文件
[ ] 没有提交 cookies / token / 密钥
[ ] 没有把 token 打到日志
[ ] 没有绕过鉴权
[ ] 没有真实调用外部付费接口
[ ] 对应测试已跑
[ ] 文档已更新
[ ] 失败路径考虑过 cleanup/cancel
[ ] 改动影响范围已写清楚
[ ] 已运行 git status，确认无敏感文件进入提交
[ ] 如改动 SSE、鉴权、密码、token、支付、外部接口，已找导师或安全评审
```

---

## 18. 项目后续推荐演进方向

如果你后续参与重构，推荐顺序是：

1. **安全基线**
   - bcrypt。
   - 强制 JWT_SECRET。
   - 强制 ADMIN_PASSWORD。
   - CORS 限制。
   - token 脱敏。

2. **配置统一**
   - 把 JSON、环境变量、GUI、DB 配置统一成 dataclass schema。

3. **核心服务提取**
   - 从 `auto_register.py` 提取 `RegistrationService`。

4. **拆分 `web_gui.py`**
   - routes、state、services、templates 分离。

5. **任务状态机**
   - 明确注册步骤和事件。
   - 日志通过事件流输出。

6. **测试体系**
   - mock 外部 API。
   - 覆盖失败路径。

7. **部署规范**
   - `.env.example`。
   - `docker-compose.yml`。
   - `docs/deployment.md`。
   - `docs/security.md`。

---

## 19. 最后给新人的一句话

这个项目不是难在 Python 语法，而是难在：

- 外部依赖多。
- 入口多。
- 历史包袱多。
- 协议链路长。
- 安全敏感点多。

你每次改动都要带着链路意识：

```text
我改的是哪个入口？
谁会调用它？
它会不会碰外部资源？
失败会不会释放资源？
返回字段会不会被前端或后续流程依赖？
敏感信息有没有泄露？
```

能回答这些问题，你就已经超过大多数“只会改一行”的维护者了。
