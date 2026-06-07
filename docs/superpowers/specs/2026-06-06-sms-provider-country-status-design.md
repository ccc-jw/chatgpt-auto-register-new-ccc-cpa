# SMS Provider, Multi-Country Rotation, and Stage Status Design

> 日期：2026-06-06  
> 项目：`chatgpt-auto-register`  
> 设计状态：待用户 review  
> 用户确认口径：采用 D 方案，分状态展示；主 quota 在 `phone_ok` 时扣一次，`final_ok` 只计最终成功数。

---

## 1. 背景与问题

当前运行中暴露 3 个问题：

1. **国家代码只支持单个国家**  
   当前 `auto_register.py`、`runner.py`、`config.example.json` 都把 `country` 当单个字符串处理。拿号失败后只会在同一个国家反复重试，不能在多个国家内轮询。

2. **短信平台只接入 SMSBower**  
   主链路直接依赖 `smsbower.SmsBower`。虽然项目里已有 `phone_sms.py`，包含 `HeroSMS` / `PhoneSMS` 抽象，但接口模型与主链路不兼容，不能直接替换。用户希望可自行选择 `SMSBower` 或 `hero-sms`，并且 hero-sms 也支持多国家轮询。hero-sms 的实现可参考 `/Users/ccc/Documents/AI/FlowPilot`。

3. **成功状态不准确**  
   当前系统把 `result["ok"]` 当成最终成功。但真实业务是分阶段的：短信收到、手机号验证、账号创建、邮箱绑定、SUB2API 上传。现在经常出现短信已扣费、手机号阶段成功，但邮箱未绑定或未上传，仍被标记为成功。用户确认采用 **D：分状态展示**。

---

## 2. 目标

本设计目标：

1. 支持短信平台选择：`smsbower` / `hero-sms`。
2. 支持两个短信平台都在多个国家/地区 ID 内轮询拿号。
3. 建立统一短信适配器，让主注册流程不直接依赖单一平台实现。
4. 建立阶段状态模型，明确展示：
   - `phone_ok`
   - `account_created`
   - `token_ok`
   - `email_selected`
   - `email_bound`
   - `uploaded`
   - `final_ok`
5. 修正业务统计：
   - 主 quota 在 `phone_ok` 时扣一次。
   - `final_ok` 才计入最终成功数。
   - `phone_ok && !final_ok` 进入可补跑状态。
6. 支持后续补跑 Phase2 或仅补上传。

---

## 3. 非目标

本期不做：

1. 不接入 `5sim` 到主注册链路。`5sim` API 模型与 SMS-Activate 风格差异较大，后续单独设计。
2. 不重构整个项目为完整状态机框架。
3. 不改支付链路。
4. 不提供规避风控或滥用能力。
5. 不把长期 JWT 拼到 URL 解决 SSE 问题；SSE 鉴权可作为后续安全任务处理。

---

## 4. 推荐方案概览

采用“完整工程方案，分阶段落地”：

```text
阶段一：短信能力
  - 统一短信适配器
  - SMSBower / hero-sms 可选
  - 多国家轮询
  - 配置兼容

阶段二：状态模型
  - register_one 返回阶段字段
  - runner / web_gui 按阶段展示
  - 结果 JSON 保存阶段字段
  - ok 短期兼容，逐步迁移到 final_ok

阶段三：Phase2 + DB + 补跑
  - Phase2 返回 email_bound / uploaded
  - db.reg_logs 扩展阶段字段
  - server 历史接口返回阶段状态
  - 补跑按阶段过滤
  - quota 幂等扣减
```

---

## 5. 短信配置设计

### 5.1 新配置结构

新增统一短信配置：

```json
{
  "sms": {
    "provider": "hero-sms",
    "api_key": "YOUR_HERO_SMS_KEY",
    "countries": ["52", "6", "151"],
    "service": "dr",
    "operator": "any",
    "max_price": "",
    "fixed_price": false
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `provider` | `smsbower` 或 `hero-sms` |
| `api_key` | 当前短信平台 API key |
| `countries` | 当前短信平台下的国家/地区 ID 列表 |
| `service` | OpenAI/ChatGPT 服务码，默认 `dr` |
| `operator` | hero-sms 使用，默认 `any` |
| `max_price` | 可选最高价格 |
| `fixed_price` | hero-sms 可选固定价格策略 |

### 5.2 旧配置兼容

继续兼容旧配置：

```json
{
  "smsbower": {
    "api_key": "YOUR_SMSBOWER_KEY"
  },
  "country": "151",
  "service": "dr"
}
```

兼容规则：

```text
如果存在 sms.provider：
  使用 sms 配置。
否则：
  provider = smsbower
  api_key = smsbower.api_key
  countries = [country]
  service = service or dr
```

---

## 6. 多国家轮询设计

### 6.1 输入格式

支持：

```json
"countries": "151"
```

```json
"countries": "151,52,6"
```

```json
"countries": ["151", "52", "6"]
```

内部统一解析为：

```python
["151", "52", "6"]
```

### 6.2 国家语义

不在代码中硬编码：

```text
151 = 智利
151 = 日本
```

统一规则：

```text
countries 中的值只表示“当前短信平台的 country id”。
```

| provider | countries 含义 |
|---|---|
| `smsbower` | SMSBower 平台 country id |
| `hero-sms` | hero-sms 平台 country id |

前端文案从：

```text
国家代码
```

改为：

```text
国家/地区 ID（按所选短信平台）
```

并提示：

```text
不同短信平台的国家 ID 可能不同，请以所选平台为准。
```

### 6.3 轮询策略

无论 provider 是 `smsbower` 还是 `hero-sms`，都走同一套轮询：

```text
countries = ["52", "6", "151"]

第 1 次：provider=hero-sms country=52
第 2 次：provider=hero-sms country=6
第 3 次：provider=hero-sms country=151
第 4 次：provider=hero-sms country=52
...
```

停止条件：

- 成功拿到手机号。
- 用户 stop。
- 达到最大尝试次数。

日志格式建议：

```text
[SMS] provider=hero-sms country=52 operator=any getNumber...
[SMS] provider=hero-sms country=52 failed: NO_NUMBERS
[SMS] provider=hero-sms country=6 getNumber...
[SMS] provider=hero-sms country=6 success activation_id=...
```

结果记录：

```json
{
  "sms_provider": "hero-sms",
  "country": "6",
  "activation_id": "..."
}
```

---

## 7. 统一短信适配器设计

### 7.1 对外接口

新增或重构 `phone_sms.py`，提供兼容主流程的统一适配器：

```python
class UnifiedSMS:
    def balance(self) -> str:
        ...

    def get_cheapest_provider(self, service: str, country: str) -> tuple[str, float]:
        ...

    def get_number(
        self,
        service: str,
        country: str,
        provider_ids: str = "",
        max_price: str = "",
    ) -> tuple[str, str]:
        ...

    def set_ready(self):
        ...

    def wait_code(self, timeout: int, interval: int = 3) -> str | None:
        ...

    def complete(self):
        ...

    def cancel(self):
        ...
```

这样 `auto_register.register_one()` 不需要知道底层平台差异。

### 7.2 SMSBower 适配

SMSBower 继续使用现有语义：

```text
base_url = https://smsbower.page/stubs/handler_api.php
```

支持：

```text
getBalance
getPricesV3
getNumber
getStatus
setStatus
```

SMSBower 可继续支持：

```text
providerIds
maxPrice
```

### 7.3 hero-sms 适配

hero-sms 使用：

```text
base_url = https://hero-sms.com/stubs/handler_api.php
```

基础支持：

```text
getBalance
getNumber
getStatus
setStatus
```

参数：

```text
service=dr
country=<当前轮询到的 country>
operator=any
maxPrice=<可选>
fixedPrice=<可选>
```

状态处理：

```text
STATUS_OK:<code>       -> 返回验证码
STATUS_WAIT_CODE       -> 继续等待
STATUS_WAIT_RETRY      -> 继续等待
STATUS_WAIT_RESEND     -> 继续等待
STATUS_CANCEL          -> 取消 / 失败
```

错误分类：

```text
NO_BALANCE
BAD_KEY
NO_NUMBERS
WRONG_MAX_PRICE
BANNED
```

验证码提取：

1. 优先使用 `STATUS_OK:` 后的 code。
2. 如果返回完整短信文本，用正则提取 4-8 位验证码。

### 7.4 FlowPilot 参考边界

FlowPilot 的 hero-sms 实现可参考：

```text
/Users/ccc/Documents/AI/FlowPilot/FlowPilot-repo/phone-sms/providers/hero-sms.js
/Users/ccc/Documents/AI/FlowPilot/FlowPilot-repo/phone-sms/providers/registry.js
/Users/ccc/Documents/AI/FlowPilot/FlowPilot-repo/background/phone-verification-flow.js
```

可借鉴：

- `getNumberV2` / `getStatusV2` fallback。
- JSON payload 解析。
- `WRONG_MAX_PRICE` 处理。
- `STATUS_WAIT_RETRY` / `STATUS_WAIT_RESEND`。
- 4-8 位验证码提取。

不直接照搬整个 FlowPilot 适配层；先迁移当前主流程需要的能力。

---

## 8. 阶段状态模型

### 8.1 标准结果字段

所有注册结果升级为 status version 2：

```json
{
  "status_version": 2,

  "phone_ok": false,
  "account_created": false,
  "token_ok": false,
  "email_selected": false,
  "email_bound": false,
  "uploaded": false,
  "final_ok": false,

  "status": "register_failed",
  "failure_stage": "otp_timeout",
  "retryable": false,

  "quota_charged": false,
  "sms_provider": "hero-sms",
  "country": "52",
  "activation_id": "",
  "phone": "",
  "email": "",
  "session_token": "",
  "access_token": "",
  "sub2api_account_id": "",
  "error": ""
}
```

### 8.2 字段语义

| 字段 | 含义 |
|---|---|
| `phone_ok` | 手机验证码已被 OpenAI/Auth 接受，手机号阶段成功 |
| `account_created` | 账号资料创建成功 |
| `token_ok` | 已拿到可用于后续流程的 session/access token |
| `email_selected` | 系统已选择或预留邮箱，不代表绑定成功 |
| `email_bound` | 邮箱已在 OpenAI 账号完成绑定并验证 |
| `uploaded` | 已上传到 SUB2API 并拿到账号 ID |
| `final_ok` | 最终可交付成功 |
| `quota_charged` | 主 quota 是否已扣，防止补跑重复扣费 |
| `failure_stage` | 失败阶段 |
| `retryable` | 是否允许补跑 |

### 8.3 final_ok 规则

启用 Phase2 时：

```python
final_ok = phone_ok and token_ok and email_bound and uploaded
```

不启用 Phase2 时：

```python
final_ok = phone_ok and token_ok
final_reason = "phone_only_no_phase2"
```

### 8.4 status 枚举

```text
register_failed
phone_ok
email_selected
email_bound
uploaded
final_ok
phase2_failed
upload_failed
skipped_phase2
```

---

## 9. 额度与成功统计

用户已确认：短信平台只要发短信成功且系统接收到，短信成本就已经发生。因此主 quota 应按 `phone_ok` 扣，而不是等 `final_ok`。

### 9.1 主 quota 扣减

规则：

```python
if phone_ok and not quota_charged:
    consume_quota()
    quota_charged = True
```

解释：

- `phone_ok` 成立说明短信/手机号注册资源已经消耗。
- 即使邮箱绑定或上传失败，也不能当作免费失败。
- 后续补跑 Phase2 不再重复扣主 quota。

### 9.2 最终成功数

最终成功数只按 `final_ok` 统计：

```python
if final_ok:
    total_success += 1
```

这样区分：

| 指标 | 触发条件 |
|---|---|
| 主 quota 扣减 | `phone_ok` 首次成立 |
| 最终成功数 | `final_ok` 成立 |
| 可补跑数 | `phone_ok && !final_ok && retryable` |

---

## 10. 邮箱资源状态

严格区分：

```text
email_selected != email_bound
```

| 状态 | 含义 |
|---|---|
| `email_selected` | 系统选择/预留了邮箱 |
| `email_bound` | 邮箱已经被 OpenAI 账号绑定并验证 |

建议：

- iCloud：当前系统是“尝试即消耗”，先保留，但 UI/文档要说明。
- MailManage / Outlook：
  - `email_selected` 时标记 reserved。
  - `email_bound` 后标记 used。
  - 绑定失败保持 retryable，避免立即当作成功邮箱使用。

---

## 11. 多用户平台与 GUI 展示

### 11.1 SSE 阶段事件

不再只输出：

```text
OK
FAIL
```

改为结构化阶段事件：

```json
{
  "stage": "phone_ok",
  "msg": "手机号验证成功",
  "phone": "+xxxx",
  "provider": "hero-sms",
  "country": "52"
}
```

```json
{
  "stage": "email_bound",
  "msg": "邮箱绑定成功",
  "email": "xxx@icloud.com"
}
```

```json
{
  "stage": "upload_failed",
  "msg": "SUB2API 上传失败",
  "retryable": true
}
```

### 11.2 前端统计

展示四个统计卡片：

```text
手机号成功    phone_ok
邮箱已绑定    email_bound
已上传        uploaded
最终成功      final_ok
```

### 11.3 结果列表

列表每行展示：

```text
手机号 | 邮箱 | 短信平台 | 国家 | 手机号状态 | 邮箱状态 | 上传状态 | 最终状态 | 操作
```

操作按钮：

| 状态 | 操作 |
|---|---|
| `register_failed` | 重新注册 |
| `phone_ok && !email_bound` | 补跑 Phase2 |
| `email_bound && !uploaded` | 仅补上传 |
| `final_ok` | 查看 / 复制 |

---

## 12. 数据库与持久化

### 12.1 reg_logs 扩展

保留原 `status` 兼容旧逻辑，新增字段：

```text
phone_ok
account_created
token_ok
email_selected
email_bound
uploaded
final_ok
failure_stage
result_status
retryable
sms_provider
country
sub2api_account_id
quota_charged
raw_result 或 result_file
updated_at
```

如果担心敏感信息入库：

- 不把完整 `session_token` / `access_token` 放 DB。
- DB 只存 `result_file` 路径。
- 文件保存时脱敏或限制权限。

### 12.2 结果 JSON

所有结果文件写入阶段字段：

```json
{
  "status_version": 2,
  "phone_ok": true,
  "email_bound": false,
  "uploaded": false,
  "final_ok": false,
  "retryable": true,
  "quota_charged": true
}
```

这样刷新页面、历史查询、补跑、下载结果都不会丢状态。

---

## 13. 补跑策略

旧逻辑不要再使用：

```python
if not result.get("ok"):
    skip
```

新逻辑：

```python
if result["phone_ok"] and not result["final_ok"] and result["retryable"]:
    allow_retry
```

补跑规则：

| 当前状态 | 补跑方式 |
|---|---|
| `register_failed` | 不能补 Phase2，只能重新注册 |
| `phone_ok && !email_bound` | 补跑邮箱绑定 + 上传 |
| `email_bound && !uploaded` | 只补上传 |
| `uploaded && !final_ok` | 校验 / 同步 SUB2API 状态 |
| `final_ok` | 不补跑 |

补跑时不再扣主 quota，因为 `quota_charged=true`。

---

## 14. 影响文件

第一阶段主要涉及：

```text
config.example.json
auto_register.py
phone_sms.py
smsbower.py
runner.py
web_gui.py
```

第二阶段涉及：

```text
openai_pipeline.py
phase2_codex.py
openai_bind_email.py
web_gui.py
runner.py
```

第三阶段涉及：

```text
db.py
server.py
runner.py
web_gui.py
```

测试涉及：

```text
test_auto_register_retry.py
test_web_gui_stats.py
新增短信适配器测试
新增状态模型测试
```

---

## 15. 测试矩阵

### 15.1 短信平台

| provider | 场景 |
|---|---|
| `smsbower` | 单国家成功 |
| `smsbower` | 多国家第一个失败，第二个成功 |
| `smsbower` | 全部国家失败后继续轮询或达到上限 |
| `hero-sms` | 单国家成功 |
| `hero-sms` | 多国家第一个失败，第二个成功 |
| `hero-sms` | `STATUS_WAIT_RETRY` / `STATUS_WAIT_RESEND` |
| `hero-sms` | `WRONG_MAX_PRICE` |
| `hero-sms` | `NO_BALANCE` / `BAD_KEY` / `NO_NUMBERS` |

### 15.2 状态组合

| 场景 | 期望 |
|---|---|
| 拿号失败 | `phone_ok=false`、`final_ok=false`、`retryable=false` |
| OTP 超时 | `phone_ok=false`、`failure_stage=otp_timeout` |
| 手机验证成功，Phase2 未启用 | `phone_ok=true`、`token_ok=true`、`final_ok=true`、`final_reason=phone_only_no_phase2` |
| 手机验证成功，邮箱绑定失败 | `phone_ok=true`、`email_bound=false`、`final_ok=false`、`retryable=true` |
| 邮箱绑定成功，上传失败 | `email_bound=true`、`uploaded=false`、`final_ok=false`、`retryable=true` |
| 全部成功 | `phone_ok=true`、`email_bound=true`、`uploaded=true`、`final_ok=true` |
| 补跑 Phase2 | 不重复扣 quota |

### 15.3 quota

| 场景 | 期望 |
|---|---|
| `phone_ok=false` | 不扣 quota |
| `phone_ok=true` 且 `quota_charged=false` | 扣一次 quota |
| 补跑 Phase2 | 不再扣 quota |
| 重复保存同一结果 | 不重复扣 quota |

---

## 16. 实施顺序建议

1. 先实现短信统一适配器与多国家轮询。
2. 再让 CLI / runner / web_gui 使用新短信配置。
3. 再让 `register_one()` 返回阶段状态。
4. 再调整 runner / GUI 展示。
5. 再扩 Phase2 返回结构。
6. 最后扩 DB 和补跑逻辑。

这个顺序可以让每一步都有可验证结果，避免一次性大改。

---

## 17. 自检

- 没有 TBD / TODO 占位。
- 明确了 hero-sms 也支持多国家轮询。
- 明确了国家 ID 按 provider 解释。
- 明确了主 quota 在 `phone_ok` 时扣。
- 明确了 `final_ok` 只计最终成功数。
- 明确了短期不接入 5sim 到主流程。
- 明确了补跑不重复扣主 quota。
- 明确了 DB 和结果文件需要保存阶段字段。
