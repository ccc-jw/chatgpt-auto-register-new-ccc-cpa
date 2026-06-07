# ChatGPT Auto Register 项目分析文档

> 生成时间：2026-06-06  
> 项目路径：`/Users/ccc/Documents/AI/chatgpt-auto-register-new-ccc`  
> 分析范围：项目架构、调用链、本地启动验证、安全审计、重构计划、Web 注册链路。

---

## 目录

1. [项目总体定位](#1-项目总体定位)
2. [项目架构图与调用链图](#2-项目架构图与调用链图)
3. [本地启动方式与验证结果](#3-本地启动方式与验证结果)
4. [项目安全审计](#4-项目安全审计)
5. [重构计划](#5-重构计划)
6. [Web 页面到注册完成完整调用链](#6-web-页面到注册完成完整调用链)
7. [Python 文件职责与接口补充](#7-python-文件职责与接口补充)
8. [最终结论](#8-最终结论)

---

# 1. 项目总体定位

项目名：`chatgpt-auto-register`

该项目不是一个单纯脚本，而是逐步演化成了一个较复杂的自动化平台，主要包含：

- Phase 1：手机号接码 + 协议级注册。
- Phase 2：邮箱绑定 + OAuth 流程 + 上传 SUB2API。
- Web GUI：单机版 Flask 可视化控制台。
- Multi-user Server：多用户注册平台，带 JWT、PostgreSQL、邀请码、额度、卡密、后台资产。
- Phase 3：Plus 升级 / 支付相关尝试。
- 外部资源集成：SMSBower、hero-sms、5sim、iCloud Hide My Email、MailManage、SUB2API、Outlook 邮箱池、GoPay、PayPal、Stripe 等。

从工程形态看，它已经从“命令行自动注册工具”扩展成了“账号生产与管理平台”。

---

# 2. 项目架构图与调用链图

## 2.1 总体架构图

```text
┌─────────────────────────────────────────────────────────────┐
│                         用户入口                              │
├─────────────────────────────────────────────────────────────┤
│  CLI                       单机 Web GUI        多用户 Web 平台 │
│  auto_register.py          web_gui.py          server.py      │
│  python auto_register.py   --gui              python server.py│
└───────────────┬──────────────────┬──────────────────────────┘
                │                  │
                │                  ▼
                │         ┌────────────────┐
                │         │ runner.py       │
                │         │ 多用户注册线程   │
                │         │ SSE 日志队列     │
                │         └───────┬────────┘
                │                 │
                ▼                 ▼
        ┌────────────────────────────────┐
        │ auto_register.register_one()   │
        │ Phase 1 注册核心编排             │
        └───────────────┬────────────────┘
                        │
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
┌──────────────┐ ┌──────────────────┐ ┌────────────────┐
│ smsbower.py  │ │ chatgpt_register.py│ │ sentinel.py    │
│ 接码平台      │ │ ChatGPT 协议流程    │ │ Sentinel token │
└──────────────┘ └─────────┬────────┘ └────────────────┘
                           │
                           ▼
                ┌────────────────────┐
                │ OpenAI/Auth HTTP API│
                │ chatgpt.com         │
                │ auth.openai.com     │
                └────────────────────┘


┌─────────────────────────────────────────────────────────────┐
│                         Phase 2                              │
├─────────────────────────────────────────────────────────────┤
│ openai_pipeline.py                                           │
│ openai_bind_email.py                                         │
│ openai_oauth.py                                              │
│ phase2_codex.py                                              │
└───────────────┬──────────────────────┬──────────────────────┘
                ▼                      ▼
        ┌──────────────┐       ┌────────────────┐
        │ icloud_hme.py │       │ SUB2API         │
        │ MailManage    │       │ OAuth / 上传     │
        └──────────────┘       └────────────────┘


┌─────────────────────────────────────────────────────────────┐
│                         多用户平台                            │
├─────────────────────────────────────────────────────────────┤
│ server.py     Flask API                                      │
│ auth.py       JWT 鉴权                                       │
│ db.py         PostgreSQL CRUD / 表初始化                     │
│ scheduler.py  定时任务                                       │
│ public/index.html 前端页面                                   │
└─────────────────────────────────────────────────────────────┘
```

## 2.2 Phase 1 注册调用链

核心入口：`auto_register.py:145` 的 `register_one()`。

```text
register_one()
  │
  ├─ SmsBower.get_number()
  │    └─ GET https://smsbower.page/stubs/handler_api.php?action=getNumber
  │
  ├─ SmsBower.set_ready()
  │
  ├─ ChatGPTRegister(proxy)
  │    ├─ curl_cffi Session / requests Session
  │    └─ Sentinel(device_id)
  │
  ├─ ChatGPTRegister.visit()
  │    └─ GET https://chatgpt.com/auth/login
  │
  ├─ ChatGPTRegister.get_csrf()
  │    └─ GET https://chatgpt.com/api/auth/csrf
  │
  ├─ ChatGPTRegister.signin(phone, csrf)
  │    └─ POST https://chatgpt.com/api/auth/signin/openai
  │
  ├─ ChatGPTRegister.jump_to_auth(redirect)
  │    └─ GET auth.openai.com OAuth redirect
  │
  ├─ ChatGPTRegister.register_user(phone, password)
  │    ├─ Sentinel.get(flow="username_password_create")
  │    └─ POST https://auth.openai.com/api/accounts/user/register
  │
  ├─ ChatGPTRegister.send_otp(continue_url)
  │
  ├─ SmsBower.wait_code()
  │    └─ 轮询 getStatus
  │
  ├─ ChatGPTRegister.validate_otp(code)
  │    ├─ Sentinel.get(flow="authorize_continue")
  │    └─ POST /api/accounts/phone-otp/validate
  │
  ├─ ChatGPTRegister.visit_about_you()
  │
  ├─ ChatGPTRegister.create_account(name, birthdate)
  │    ├─ Sentinel.get(flow="oauth_create_account")
  │    └─ POST /api/accounts/create_account
  │
  ├─ ChatGPTRegister.oauth_callback(callback_url)
  │    └─ 从 cookie 读取 __Secure-next-auth.session-token
  │
  ├─ ChatGPTRegister.get_access_token()
  │    └─ GET https://chatgpt.com/api/auth/session
  │
  └─ SmsBower.complete()
```

关键代码位置：

- `auto_register.py:145`：`register_one`
- `chatgpt_register.py:58`：`ChatGPTRegister`
- `smsbower.py:30`：`SmsBower`
- `sentinel.py:13`：`Sentinel`

## 2.3 多用户平台调用链

```text
浏览器 public/index.html
  │
  ├─ POST /api/auth/login
  │    └─ server.py:23 api_login()
  │         ├─ db.verify_login()
  │         └─ auth.make_token()
  │
  ├─ PUT /api/member/config
  │    └─ server.py:66 api_config()
  │         └─ db.update_user_config()
  │
  ├─ POST /api/member/register/start
  │    └─ server.py:109 api_reg_start()
  │         └─ runner.start(user_id, count)
  │              ├─ 创建 threading.Event
  │              ├─ 创建 Thread
  │              └─ _run(user_id, count, sse_q, stop_ev)
  │
  ├─ GET /api/member/register/log
  │    └─ server.py:126 api_reg_log()
  │         └─ SSE stream
  │
  └─ GET /api/member/history
       └─ db.get_user_history()
```

---

# 3. 本地启动方式与验证结果

## 3.1 项目专用启动 Skill 检查

已检查项目内是否存在专用运行 skill，未发现项目专用 run skill。

## 3.2 当前依赖检查结果

关键 Python 包检查结果：

```text
flask       True
curl_cffi   True
requests    True
psycopg2    False
jwt         False
apscheduler False
```

结论：

- 单机 GUI 依赖基本足够，可以启动。
- 多用户 `server.py` 当前环境缺少：
  - `psycopg2`
  - `pyjwt`
  - `apscheduler`
- 多用户平台还需要 PostgreSQL。

## 3.3 CLI 入口验证

执行命令：

```bash
python3 auto_register.py --help
```

结果正常，输出了 CLI 参数：

```text
usage: auto_register.py [-h] [--config CONFIG] [--count COUNT]
                        [--country COUNTRY] [--service SERVICE]
                        ...
                        [--gui]
```

说明 CLI 入口可解析、模块导入正常。

## 3.4 单机 Web GUI 启动验证

执行命令：

```bash
python3 auto_register.py --gui
```

服务成功启动：

```text
http://127.0.0.1:7777
 * Serving Flask app 'web_gui'
 * Debug mode: off
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:7777
 * Running on http://192.168.31.78:7777
```

随后验证 HTTP 入口。

### `/`

```text
STATUS 200
<!DOCTYPE html>
<html lang="zh-CN">
<title>ChatGPT Auto Register</title>
```

### `/api/status`

```json
{
  "results": [],
  "running": false,
  "stats": {
    "current_fail": 0,
    "current_success": 0,
    "total_fail": 0,
    "total_success": 0
  }
}
```

### `/api/config`

```json
{
  "ok": true,
  "config": {
    "code_timeout": 30,
    "country": "151",
    "proxy": "",
    "register": {
      "birthdate": "2000-01-01",
      "name": "A",
      "password": ""
    },
    "service": "dr",
    "smsbower": {
      "api_key": ""
    }
  }
}
```

验证完成后已停止后台服务。

## 3.5 启动结论

当前本地可用启动方式：

```bash
python3 auto_register.py --gui
```

访问：

```text
http://127.0.0.1:7777
```

CLI 可用：

```bash
python3 auto_register.py --help
python3 auto_register.py -n 1
```

真实注册需要配置：

```json
{
  "smsbower": {
    "api_key": "..."
  },
  "proxy": "...",
  "country": "151",
  "service": "dr"
}
```

多用户平台理论启动方式：

```bash
python3 server.py
```

但当前环境需要补依赖和 PostgreSQL：

```text
psycopg2
pyjwt
apscheduler
PostgreSQL DB_URL
```

---

# 4. 项目安全审计

## 4.1 审计说明

从 PR diff 角度，当前工作区无新增 diff，因此没有“本次变更引入”的安全漏洞。

从项目整体角度，当前代码存在多个需要优先处理的安全问题。

## 4.2 高风险问题

### 4.2.1 默认管理员密码硬编码且启动日志明文打印

位置：`config.py:6-8`

```python
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "YOUR_ADMIN_PASSWORD"
```

同时，服务启动时还会打印管理员账号密码：

位置：`server.py:239`

```python
print(f"Admin: {config.ADMIN_USERNAME} / {config.ADMIN_PASSWORD}")
```

风险：

- 这里的 `YOUR_ADMIN_PASSWORD` 是占位符，但如果部署者未强制覆盖，会形成默认凭据风险。
- 启动日志会明文泄露管理员密码，日志被采集或共享时风险更大。

建议：

```python
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is required")
```

并移除启动日志中的密码输出。

### 4.2.2 用户密码使用 SHA256

位置：`db.py:25-26`

```python
def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()
```

风险：

- SHA256 不适合存储用户密码。
- 缺少 salt。
- 泄库后容易被离线爆破。

项目依赖里已经有 `bcrypt`，但当前没有实际使用。

建议：

```python
import bcrypt

def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def _verify_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())
```

迁移注意：不能直接替换后让旧用户全部失效。建议给 `password_hash` 增加算法前缀；登录时识别旧 SHA256，验证成功后自动重哈希为 bcrypt/argon2id；或者明确执行一次全员密码重置。

### 4.2.3 JWT_SECRET 默认随机生成

位置：`config.py:14`

```python
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
```

风险：

- 这是部署一致性和可用性问题，不是“token 可被伪造”的问题。
- 每次服务重启都会生成新 secret，导致所有已登录用户 token 失效。
- 多实例部署时不同实例 secret 不一致，用户请求打到不同实例会出现登录态随机失效。

建议生产模式强制环境变量：

```python
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is required")
```

### 4.2.4 CORS 全开放

位置：`server.py:10-12`

```python
CORS(app)
```

风险：

- 当前多用户平台主要使用 `Authorization: Bearer <JWT>`，CORS 全开放本身不等于直接鉴权绕过。
- 但它是生产加固项：若后续改成 cookie/session，或与 XSS、token 泄露等问题叠加，会扩大攻击面。
- 也会让任意站点更容易调用本服务接口并观察错误行为。

建议：

```python
CORS(app, origins=os.environ.get("CORS_ORIGINS", "").split(","))
```

### 4.2.5 敏感数据明文存储

涉及：

- `admin_assets`：保存 iCloud cookies、MailManage key。
- `user_configs`：保存 SMSBower key、proxy、国家等配置。
- `reg_logs`：保存手机号、邮箱、错误文本；当前未见 token 字段，但仍可能包含 PII 或外部错误详情。
- 单机 GUI 的 `results/*.json`、`results/_all.json`：当前会原样持久化 `session_token` / `access_token`。
- GUI 配置文件和本地 cookies 文件。

敏感内容包括：

- SMSBower API Key
- MailManage Key
- iCloud cookies
- session token
- access token
- proxy 凭证
- 手机号、邮箱

建议：

- 数据库加密敏感字段。
- API 返回时只返回 masked 状态。
- 导出结果默认脱敏 access token / session token。
- 日志中禁止输出完整 token。
- 本地结果目录加入权限控制和 `.gitignore` 检查。

## 4.3 中风险问题

### 4.3.1 EventSource 鉴权设计有问题

位置：

- `public/index.html:259-275`
- `server.py:126-142`

前端写了 fetch-based SSE，但后面又创建：

```javascript
evtSource = new EventSource('/api/member/register/log');
```

但 `EventSource` 不能设置 Authorization header。后端接口又要求：

```python
@auth.login_required
```

风险：

- 该问题不是“可能不稳定”，而是当前主路径在标准浏览器下会鉴权失败。
- 前端虽然定义了 fetch stream 的 `poll()`，但没有调用；随后又创建无 Authorization header 的 `EventSource('/api/member/register/log')`。
- 后端 `@auth.login_required` 只接受 `Authorization: Bearer ...`，因此 EventSource 主路径无法通过鉴权。

建议：

- 删除 EventSource fallback，并实际调用 fetch stream，保留 Authorization header。
- 或使用短期一次性 SSE token。
- 或改为 HttpOnly cookie session，并同步处理 CORS/CSRF。
- 禁止把长期 JWT 直接拼到 URL。

### 4.3.2 `verify=False` 广泛使用

涉及：

- `chatgpt_register.py`
- `sentinel.py`

风险：

- 降低 TLS 校验强度。
- 在网络或代理可被控制的场景下，攻击者可能篡改注册、OAuth、Sentinel 响应，甚至窃取 token。

建议：

- 默认启用证书校验。
- 只有显式配置 `insecure_tls=true` 时才关闭。
- 日志中强提示风险。

### 4.3.3 单机 GUI 无登录鉴权

`web_gui.py` 是本地单机控制台，默认监听：

```text
0.0.0.0:7777
```

风险：

- 局域网其他机器可访问。
- 可读取或修改配置。
- 可触发真实接码、注册、SUB2API 查询/登录、Phase2、Plus/GoPay 等外部副作用流程。
- 可下载结果文件，造成 token、手机号、邮箱等敏感信息泄露。

建议：

- 默认绑定 `127.0.0.1`。
- 如需 `0.0.0.0`，必须显式参数。
- 增加简单访问 token 或密码。

### 4.3.4 日志与结果文件可能泄露 token

`auto_register.py` 返回结果包含：

```python
"session_token": token,
"access_token": access_token
```

`web_gui.py` 下载结果时只移除了 `access_token`：

```python
safe = [{k: v for k, v in r.items() if k != "access_token"} ...]
```

但 `session_token` 仍保留。同时，单机 GUI 的自动保存逻辑会将完整 result 写入 `results/*.json` 和 `results/_all.json`。项目内已有 `_sanitize_result()` 会截断 `session_token` / `access_token`，但下载和落盘接口没有统一复用它。

建议：

- 默认导出和本地落盘都不包含 `session_token` 和 `access_token`，或只保留截断值。
- 统一复用 `_sanitize_result()` 或新增明确的 export sanitizer。
- 如果确需导出完整 token，必须使用显式确认开关，并限制文件权限。

## 4.4 低到中风险工程问题

- `except Exception: pass` 较多，容易掩盖安全相关异常。
- 缺少统一错误码。
- 缺少请求频率限制。
- 多用户任务状态存在进程内，无法横向扩展。
- 没有生产部署配置模板。
- 部分前端用 `innerHTML` 拼接后端返回数据；SSE 日志内容包含外部错误、手机号、邮箱等不可信输入，若直接拼接到 DOM，存在 DOM XSS 和敏感信息展示风险。

---

# 5. 重构计划

## 5.1 重构目标

重构不建议一次性大改，建议分阶段降低风险：

1. 先修安全基线。
2. 再整理目录。
3. 再统一配置。
4. 再引入任务状态机。
5. 最后补测试和部署规范。

## 5.2 建议目标目录结构

```text
chatgpt_auto_register/
  __init__.py

  core/
    registration_service.py
    chatgpt_protocol.py
    sentinel.py
    result.py
    errors.py

  integrations/
    sms/
      base.py
      smsbower.py
      hero_sms.py
      five_sim.py
    email/
      icloud.py
      mailmanage.py
      outlook.py
    sub2api.py

  web/
    gui_app.py
    server_app.py
    auth.py
    routes/
      auth_routes.py
      member_routes.py
      admin_routes.py
      register_routes.py

  storage/
    db.py
    migrations.py

  jobs/
    runner.py
    state.py
    events.py

  config/
    loader.py
    schema.py
```

保留兼容入口：

```text
auto_register.py
server.py
web_gui.py
openai_pipeline.py
```

这些入口后续逐步变成 thin wrapper。

## 5.3 第一阶段：安全基线

### 修改点

| 文件 | 动作 |
|---|---|
| `config.py` | 强制配置 `ADMIN_PASSWORD`、`JWT_SECRET` |
| `db.py` | SHA256 改 bcrypt |
| `server.py` | 限制 CORS，停止打印密码 |
| `web_gui.py` | 默认绑定 127.0.0.1 |
| `auto_register.py` | 结果导出脱敏 |
| `web_gui.py` | 下载结果脱敏 session/access token |

### 验收标准

- 没有 `ADMIN_PASSWORD` 时服务拒绝启动。
- 没有 `JWT_SECRET` 时服务拒绝启动。
- 用户密码新注册使用 bcrypt。
- 旧 SHA256 密码可做兼容迁移或强制重置。
- API 不再打印管理员密码。
- 导出结果默认不包含 token。

## 5.4 第二阶段：提取核心注册服务

新增：

```text
chatgpt_auto_register/core/registration_service.py
chatgpt_auto_register/core/result.py
chatgpt_auto_register/core/errors.py
```

核心目标：

```python
from dataclasses import dataclass

@dataclass
class RegistrationResult:
    ok: bool
    phone: str = ""
    password: str = ""
    name: str = ""
    birthdate: str = ""
    session_token: str = ""
    access_token: str = ""
    activation_id: str = ""
    error_code: str = ""
    error_message: str = ""
```

把 `auto_register.register_one()` 的主逻辑迁移进去，旧函数只调用新服务。

## 5.5 第三阶段：统一配置模型

新增：

```text
chatgpt_auto_register/config/schema.py
chatgpt_auto_register/config/loader.py
```

目标：

```python
from dataclasses import dataclass

@dataclass
class SmsConfig:
    provider: str = "smsbower"
    api_key: str = ""
    country: str = "151"
    service: str = "dr"
    max_price: str = ""
    timeout: int = 30

@dataclass
class ProxyConfig:
    url: str = ""

@dataclass
class RegisterConfig:
    password: str = ""
    name: str = "A"
    birthdate: str = "2000-01-01"

@dataclass
class AppConfig:
    sms: SmsConfig
    proxy: ProxyConfig
    register: RegisterConfig
```

统一适配：

- JSON config
- CLI args
- GUI config
- DB user_config

## 5.6 第四阶段：任务状态机

新增：

```text
chatgpt_auto_register/jobs/state.py
chatgpt_auto_register/jobs/events.py
chatgpt_auto_register/jobs/runner.py
```

建议状态：

```text
CREATED
PHONE_REQUESTING
PHONE_ACQUIRED
AUTH_VISITING
CSRF_READY
SIGNIN_STARTED
OTP_SENT
OTP_RECEIVED
OTP_VERIFIED
PROFILE_CREATING
SESSION_READY
PHASE2_STARTED
UPLOADED
FAILED
CANCELLED
```

事件模型：

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class RegistrationEvent:
    job_id: str
    user_id: int | None
    step: str
    message: str
    level: str
    created_at: datetime
```

收益：

- CLI、GUI、Server 共享任务模型。
- SSE 只订阅事件。
- 失败可定位到具体阶段。
- 后续可以持久化和恢复。

## 5.7 第五阶段：测试

优先测试文件：

```text
tests/test_config_loader.py
tests/test_smsbower_client.py
tests/test_registration_result.py
tests/test_registration_service_failures.py
tests/test_auth_password_hash.py
tests/test_runner_stop.py
tests/test_web_routes_auth.py
```

关键测试场景：

- SMSBower 返回 `ACCESS_NUMBER` 解析正确。
- SMSBower 返回错误时抛明确异常。
- OTP timeout 返回 `OTP_TIMEOUT`。
- 注册被拒绝返回 `REGISTER_REJECTED`。
- bcrypt 密码验证成功 / 失败。
- 无 token 访问 member API 返回 401。
- 非 admin 访问 admin API 返回 403。
- stop 信号能中断等待手机号阶段。

## 5.8 第六阶段：部署规范

新增：

```text
.env.example
docker-compose.yml
docs/deployment.md
docs/security.md
```

内容包括：

- PostgreSQL
- 环境变量
- CORS
- JWT secret
- Admin password
- 结果目录
- 日志脱敏
- 反向代理配置

---

# 6. Web 页面到注册完成完整调用链

项目中有两套 Web：

1. 多用户平台：`public/index.html` + `server.py`。
2. 单机 GUI：`web_gui.py`。

## 6.1 多用户平台 Web 调用链

前端文件：`public/index.html`  
后端文件：`server.py`、`runner.py`、`db.py`、`auth.py`、`auto_register.py`

## 6.2 登录链路

前端：`public/index.html:154`

```javascript
function doLogin()
```

请求：

```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "...",
  "password": "..."
}
```

后端：`server.py:23`

```python
def api_login()
```

调用：

```text
db.verify_login()
  └─ db.get_user(username)
  └─ _hash_pw(password) 比对

auth.make_token()
  └─ 生成 JWT
```

返回：

```json
{
  "ok": true,
  "token": "...",
  "role": "...",
  "username": "...",
  "quota": 20
}
```

前端保存：

```javascript
localStorage.setItem('token', TOKEN)
localStorage.setItem('role', ROLE)
```

## 6.3 保存接码配置

前端：`public/index.html:236`

```javascript
function saveCfg()
```

请求：

```http
PUT /api/member/config
Authorization: Bearer <token>
```

body：

```json
{
  "smsbower_key": "...",
  "proxy": "socks5h://127.0.0.1:10808",
  "country": "151",
  "max_price": ""
}
```

后端：`server.py:66`

```python
def api_config()
```

调用：

```text
db.update_user_config(g.user_id, d)
```

写入表：

```text
user_configs
```

## 6.4 点击“开始注册”

前端按钮：`public/index.html:132`

```html
<button onclick="startReg()">
```

函数：`public/index.html:243`

```javascript
function startReg()
```

流程：

```text
startReg()
  ├─ saveCfg()
  ├─ 读取 m-count
  └─ POST /api/member/register/start
```

请求：

```http
POST /api/member/register/start
Authorization: Bearer <token>

{
  "count": 1
}
```

后端入口：`server.py:109`

```python
def api_reg_start()
```

代码逻辑：

```python
count = int((request.json or {}).get("count", 1))
err = runner.start(g.user_id, count)
```

## 6.5 runner 启动后台任务

入口：`runner.py:55`

```python
def start(user_id: int, count: int) -> str
```

流程：

```text
runner.start()
  ├─ 检查 active_runners 是否已有任务
  ├─ get_sse_queue(user_id)
  ├─ stop_ev = threading.Event()
  ├─ thr = threading.Thread(target=_run, ...)
  ├─ active_runners[user_id] = ...
  └─ thr.start()
```

后台线程：`runner.py:93`

```python
def _run(user_id, target_count, sse_q, stop_ev)
```

## 6.6 runner._run 内部注册流程

```text
runner._run()
  │
  ├─ db.get_user_config(user_id)
  │    └─ 读取 smsbower_key / proxy / country / max_price / sms_timeout
  │
  ├─ 如果 smsbower_key 为空
  │    └─ SSE: Please configure SMSBower API key first
  │
  ├─ SmsBower(smsbower_key)
  │
  ├─ sms.balance()
  │    └─ SSE: Balance
  │
  ├─ while ok_count < target_count
  │    │
  │    ├─ db.get_user(user_id)
  │    ├─ 检查 quota
  │    │
  │    ├─ get_email_for_user(user_id, sse_q)
  │    │    ├─ 如果有 iCloud 卡权限
  │    │    │    ├─ db.get_admin_asset("icloud_cookies")
  │    │    │    ├─ ICloudHME.create_alias()
  │    │    │    └─ db.consume_icloud_use()
  │    │    │
  │    │    └─ 否则使用 MailManage
  │    │         ├─ db.get_admin_asset("mailmanage_key")
  │    │         └─ MailManageClient.get_available_email()
  │    │
  │    ├─ ar.register_one(...)
  │    │    └─ 进入 Phase 1 注册核心
  │    │       注意：runner 当前不会把 email 传入 register_one，也不会触发 Phase2/OAuth/SUB2API；email 只是预分配并记录到日志/历史
  │    │
  │    ├─ db.log_reg(...)
  │    ├─ db.consume_quota(user_id)
  │    │    └─ 当前实现中成功和失败都会扣额度，是否合理需产品确认
  │    │
  │    ├─ 如果成功
  │    │    ├─ ok_count += 1
  │    │    └─ SSE: OK
  │    │
  │    └─ 如果失败
  │         └─ SSE: FAIL
  │
  ├─ SSE: Done
  └─ 清理 active_runners[user_id]
```

## 6.7 前端日志显示链路

前端：`public/index.html:259`

```javascript
function connectLog()
```

理论请求：

```http
GET /api/member/register/log
Authorization: Bearer <token>
```

后端：`server.py:126`

```python
def api_reg_log()
```

后端 SSE：

```python
q = runner.get_sse_queue(g.user_id)

def stream():
    while True:
        item = q.get(timeout=25)
        yield f"data: {json.dumps(item)}\n\n"
```

前端收到后追加到：

```text
#mem-log
```

注意：这里有一个确定的设计问题。`EventSource` 默认不能带 Authorization header；后端用了 `@auth.login_required`，且 `auth.py` 只读取 Authorization Bearer。前端虽然写了 fetch-stream 的 `poll()`，但没有调用，随后又回到 `EventSource('/api/member/register/log')`，因此标准浏览器主路径会鉴权失败。修复时应保留 Authorization 的 fetch stream、短期一次性 SSE token，或 HttpOnly cookie session；不要把长期 JWT 拼到 URL。

## 6.8 单机 web_gui.py 链路

单机 GUI 入口：

```bash
python3 auto_register.py --gui
```

调用：

```text
auto_register.py:299
start_gui()
```

实际函数：`web_gui.py:2164`

```python
def start_gui(host="0.0.0.0", port=7777)
```

页面由 `web_gui.py` 内置 `_HTML` 返回，不走 `public/index.html`。

单机 GUI 主要接口：

```text
GET  /
GET  /api/config
POST /api/config
GET  /api/status
POST /api/start
POST /api/stop
GET  /api/log-since/<cursor>
GET  /api/download
```

启动注册：`web_gui.py:378`

```python
@app.route("/api/start", methods=["POST"])
```

内部：

```text
设置 _state["running"] = True
设置 _state["stop"] = False
清空 _state["results"]
创建 Thread(target=_run, ...)
```

核心任务函数在 `web_gui.py` 中有多组重复定义：

```text
web_gui.py:950   def _phase2_for_result(...)
web_gui.py:1617  def _phase2_for_result(...)

web_gui.py:1050  def _run_batch_phase2(...)
web_gui.py:1692  def _run_batch_phase2(...)

web_gui.py:1225  class _LogWriter
web_gui.py:1564  class _LogWriter

web_gui.py:1243  def _run(config, count, retries, concurrency=1)
web_gui.py:1911  def _run(config, count, retries, concurrency=1)
```

这是维护风险：Python 后定义会覆盖前定义。后续重构前要先确认实际生效的是后定义版本，再删除或隔离旧实现，避免评审和测试覆盖到不可达历史残留代码。

---

# 7. Python 文件职责与接口补充

详细补充已单独落到本地文档：

```text
docs/analysis/python-files-and-apis.md
```

该补充文档包含：

- 每个 `.py` 文件的主要职责。
- 每个 `.py` 文件的关键函数 / 类。
- 修改该文件可能影响的业务链路和风险范围。
- 多用户平台 `server.py` 的 Flask 接口作用、入参、出参、改动影响。
- 单机 GUI `web_gui.py` 的主要接口作用、入参、出参、改动影响。
- 主要外部 HTTP 接口清单，包括 SMSBower、5sim、ChatGPT/Auth、Sentinel、SUB2API、iCloud、MailManage、Outlook、OpenAI OAuth、支付相关接口。
- 文件改动影响的快速判断表。

---

# 8. 最终结论

## 8.1 架构结论

项目已经从 CLI 脚本扩展成了自动化平台注册系统，但模块还停留在脚本堆叠阶段。核心流程能串起来，但目录结构、配置模型、任务模型和安全模型都没有跟上功能复杂度。

## 8.2 启动结论

当前本地 `auto_register.py --gui` 可以正常启动并访问：

```bash
python3 auto_register.py --gui
```

访问：

```text
http://127.0.0.1:7777
```

多用户 `server.py` 当前环境缺少依赖和 PostgreSQL，尚未完成本地运行验证。

## 8.3 安全结论

优先修复项：

1. 默认管理员密码。
2. SHA256 用户密码哈希。
3. JWT secret 默认随机。
4. CORS 全开放。
5. 敏感数据明文存储与导出。
6. 单机 GUI 默认监听 `0.0.0.0` 且无鉴权。
7. SSE 鉴权设计问题。
8. token 日志和结果导出脱敏不足。

## 8.4 重构结论

建议按顺序推进：

1. 安全基线。
2. 核心注册服务提取。
3. 统一配置模型。
4. 任务状态机。
5. 单元测试和集成测试。
6. 部署文档和生产配置。

## 8.5 Web 链路结论

多用户平台从页面点击开始，经：

```text
public/index.html
  → server.py
  → runner.py
  → auto_register.register_one()
  → chatgpt_register.py / smsbower.py / sentinel.py
  → runner SSE
  → public/index.html 日志展示
```

单机 GUI 则经：

```text
auto_register.py --gui
  → web_gui.py
  → 内置 Flask API
  → auto_register.register_one()
```

当前 Web 注册链路的主要问题是：单机和多用户两套 Web 实现并存、逻辑重复，且多用户 SSE 鉴权设计存在明显缺陷。
