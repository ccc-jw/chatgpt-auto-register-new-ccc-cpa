# CPA 上传支持设计

日期：2026-06-12

## 背景

当前项目在 Phase2 完成 OpenAI/Codex 授权后，只支持把生成好的账号上传到 SUB2API。用户希望新增 CLIProxyAPI（以下简称 CPA）作为上传目标，并在 WebUI 中支持在 SUB2API 与 CPA 之间二选一。

CPA 源码仓库为 `https://github.com/router-for-me/CLIProxyAPI`。已确认线上 CPA 管理 API 可用，管理接口前缀为 `/v0/management`，管理鉴权支持 `X-Management-Key: <key>` 或 `Authorization: Bearer <key>`。

## 目标

1. WebUI 支持选择上传平台：`SUB2API` 或 `CPA`。
2. 选择 SUB2API 时保持现有行为不变。
3. 选择 CPA 时使用 CPA 的 Codex 原生 OAuth 导入流程，失败后自动 fallback 到 auth-files 上传。
4. 多线程生成账号时，每个账号独立上传，互不影响。
5. 账号生成成功但上传失败时，把账号凭据保存到本地文件，后续可重新上传。

## 非目标

1. 不移除 SUB2API 支持。
2. 不在失败文件中保存 CPA/SUB2API 管理密钥或密码。
3. 首版不做复杂的 CPA 高级 metadata 配置，例如 priority、prefix、proxy_url、disabled 等。
4. 首版不要求完整实现可视化失败文件管理面板；但需要保留后续重传所需的数据结构和函数边界。

## 配置设计

`config.example.json` 增加顶层上传目标与 CPA 配置：

```json
{
  "upload_target": "sub2api",
  "cpa": {
    "management_url": "http://47.89.129.103:18317",
    "api_url": "http://47.89.129.103:8317",
    "management_key": "",
    "upload_mode": "auto"
  }
}
```

字段含义：

- `upload_target`: `sub2api` 或 `cpa`。老配置缺失该字段时默认 `sub2api`。
- `cpa.management_url`: CPA 管理面板地址，主要用于展示和人工打开。
- `cpa.api_url`: CPA API 地址，用于调用 `/v0/management/...`。
- `cpa.management_key`: CPA 管理密钥。
- `cpa.upload_mode`: 首版固定使用 `auto`，含义是 CPA 原生 OAuth 优先，失败后 auth-files fallback。

WebUI 表单保留现有 SUB2API 字段，并新增：

- 上传平台：SUB2API / CPA
- CPA 管理地址
- CPA API 地址
- CPA 管理密钥
- CPA 上传模式：auto

## 架构设计

新增上传目标适配层，避免 WebUI 直接写具体平台协议。

### `phase2_codex.py`

保留现有 SUB2API 能力，并新增 CPA 相关函数：

- `get_sub2api_oauth_url(...)`: 现有 SUB2API OAuth URL 生成逻辑，可从当前 `get_oauth_url(...)` 拆名或兼容保留。
- `get_cpa_oauth_url(cpa_api_url, management_key)`: 调用 CPA `GET /v0/management/codex-auth-url`，返回 `auth_url/state`。
- `upload_sub2api_session(...)`: 现有 SUB2API session/token 上传逻辑，可兼容当前 `upload_session(...)`。
- `complete_cpa_oauth_callback(cpa_api_url, management_key, code, state)`: 调用 CPA `POST /v0/management/oauth-callback`。
- `upload_cpa_auth_file(cpa_api_url, management_key, auth_payload, filename)`: 兜底上传 Codex auth JSON 到 CPA auth-files。
- `list_cpa_auth_files(cpa_api_url, management_key)`: 回查 CPA auth-files，用于幂等判断和验证上传结果。

### `openai_bind_email.py`

`run_second_half(...)` 继续负责 OpenAI OAuth、手机号/密码验证、邮箱绑定和获取授权 `code`。第 `[11]` 步从 SUB2API 专用逻辑改为基于 `upload_target` 分支：

- `sub2api`: 维持现有 exchange-code + 创建账号逻辑。
- `cpa`: 调用 CPA callback 完成原生导入；若失败或无法确认，再用当前可获得的 token 信息构造 Codex auth JSON 上传。

### `web_gui.py`

WebUI 只负责配置保存、运行调度和结果展示：

- `/api/config` 保存和返回 `upload_target`、`cpa` 配置。
- 启动 Phase2 时根据 `upload_target` 生成对应 OAuth URL。
- 结果展示区区分账号生成状态和上传状态：
  - `ok`: 注册/绑定是否成功。
  - `uploaded`: 是否上传成功。
  - `upload_error`: 上传失败原因摘要。
  - `failed_upload_file`: 上传失败后保存的本地文件路径。

## CPA 数据流

### 1. 生成 CPA OAuth URL

当 `upload_target=cpa` 时，每个账号独立调用：

```http
GET {cpa.api_url}/v0/management/codex-auth-url
X-Management-Key: <management_key>
```

CPA 返回：

```json
{
  "status": "ok",
  "state": "...",
  "url": "https://auth.openai.com/oauth/authorize?..."
}
```

本项目把 `url` 作为 OAuth URL 传给现有 Phase2 流程，并保留 `state` 供后续 callback 使用。

### 2. 完成 OpenAI 授权

`run_second_half(...)` 使用 CPA 返回的 OAuth URL 继续执行：

1. 发起 OAuth。
2. 提交手机号。
3. 验证密码。
4. 绑定邮箱。
5. 获取授权 `code`。

### 3. CPA 原生导入

拿到 `code/state` 后调用：

```http
POST {cpa.api_url}/v0/management/oauth-callback
X-Management-Key: <management_key>
Content-Type: application/json

{
  "provider": "codex",
  "code": "...",
  "state": "..."
}
```

随后通过 `GET /v0/management/auth-files` 回查是否出现对应 Codex auth file。若能确认存在，上传成功。

### 4. auth-files 兜底上传

如果 CPA 原生 callback 失败、超时或无法确认成功，构造 Codex auth JSON 并上传到：

```http
POST {cpa.api_url}/v0/management/auth-files
X-Management-Key: <management_key>
```

Codex auth JSON 使用 CPA 源码中的 `CodexTokenStorage` 字段：

```json
{
  "type": "codex",
  "email": "...",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "account_id": "...",
  "expired": "...",
  "last_refresh": "..."
}
```

实现时以 CPA 源码和接口实际要求为准。若某些字段在当前流程中不可得，则只保存/上传可得字段，并通过回查确认是否可用。

## 多线程并发设计

1. 每个注册线程为自己的账号单独生成 CPA OAuth URL，拥有独立 `state`。
2. 上传函数不使用全局可变状态；全部依赖当前账号参数和当前配置。
3. WebUI 共享 `_state["results"]` 的写入继续使用现有锁保护。
4. 每条线程日志带现有 thread id，并在上传阶段记录：上传目标、OAuth state 前 8 位、原生导入结果、fallback 结果、失败文件路径。
5. 任一账号上传失败不会中断其他线程继续生成或上传。

## 上传失败本地保存设计

当账号生成成功但上传 CPA/SUB2API 失败时，必须把该账号保存为本地文件，避免账号丢失。

### 保存目录

```text
failed_uploads/
```

每个失败账号保存为一个 JSON 文件，避免多线程写同一个文件产生冲突。

文件名格式：

```text
failed_uploads/20260612_153012_codex_<email-safe-or-phone>_<short-id>.json
```

### 文件内容

```json
{
  "schema_version": 1,
  "created_at": "2026-06-12T15:30:12+08:00",
  "upload_target": "cpa",
  "upload_mode": "auto",
  "phone": "+56...",
  "email": "xxx@outlook.com",
  "session_token": "...",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "account_id": "...",
  "expires_at": 0,
  "oauth_state": "...",
  "last_error": "CPA auth-files upload failed: ...",
  "attempts": 1
}
```

保存规则：

1. 不保存 CPA/SUB2API 管理密钥、后台密码或 API 管理账号密码。
2. 字段可用多少保存多少；至少保存 `phone`、`email`、`session_token`、`access_token` 中已有的字段。
3. 写入时先写 `.tmp` 文件，再原子 rename 为 `.json`，避免进程中断留下半截 JSON。
4. 文件名带时间戳和短随机 ID，避免多线程冲突。
5. 如果 CPA 原生 OAuth 失败但 auth-files fallback 成功，不保存失败文件；只有最终上传失败才保存。

## 后续重新上传设计

首版需要保留可复用函数边界，支持后续从失败文件重传：

- `save_failed_upload(record) -> path`
- `load_failed_upload(path) -> record`
- `retry_failed_upload(path, config) -> result`

重传时从当前 `config.json` 读取 CPA/SUB2API 地址和密钥，不从失败文件读取密钥。

重传成功后，将文件移动到：

```text
failed_uploads/done/
```

或改名为 `.uploaded`。首版 WebUI 至少要显示失败文件路径；若实现时间允许，再增加“查看失败上传”和“重试上传”按钮/API。

## 幂等与重复导入

- SUB2API 继续使用 `update_existing=true`。
- CPA auth-files fallback 使用稳定文件名，例如 `codex-<email-safe>.json`，同一邮箱重复上传时尽量覆盖/更新同一个 auth file。
- CPA 上传前后都可通过 `GET /v0/management/auth-files` 按 email 或文件名回查：
  - 已存在则视为成功或更新成功。
  - 不存在则报告失败并保存失败文件。

## 错误处理

- CPA 管理密钥缺失：启动 Phase2 前直接失败并提示“CPA 管理密钥未配置”。
- CPA OAuth URL 获取失败：记录状态码和响应摘要，该账号不进入 OpenAI 授权流程。
- CPA 原生 callback 失败：记录失败原因，自动进入 auth-files fallback。
- CPA fallback 失败：账号结果标记 `ok=true, uploaded=false`，保存失败文件并显示路径。
- SUB2API 上传失败：同样保存失败文件并显示路径。
- 保存失败文件本身失败：日志中明确提示，并在结果中保留 `upload_error`；不伪装成上传成功。

## 测试计划

### 单元测试

`phase2_codex.py`：

1. CPA OAuth URL 获取成功。
2. CPA OAuth URL 获取失败。
3. CPA OAuth callback 成功。
4. CPA callback 失败后进入 auth-files fallback。
5. CPA auth-files 上传成功后回查成功。
6. SUB2API 现有上传函数行为不变。
7. 上传最终失败时调用失败文件保存函数。

失败文件模块：

1. 保存失败记录时生成 `.json` 文件。
2. 不把管理密钥写入失败文件。
3. 多次保存生成不同文件名。
4. `.tmp` 写入后 rename 为 `.json`。
5. 读取失败文件后能构造重传参数。

`web_gui.py`：

1. `/api/config` 能保存和返回 `upload_target`、`cpa` 配置。
2. 老配置缺字段时默认 `sub2api`。
3. 选择 CPA 时 Phase2 使用 CPA OAuth URL 生成逻辑。
4. 上传失败结果包含 `uploaded=false`、`upload_error` 和 `failed_upload_file`。

### 手动验收

1. WebUI 选择 SUB2API，确认原有流程不回归。
2. WebUI 选择 CPA，确认账号完成后出现在 CPA `auth-files` 或账号池。
3. 人为配置错误 CPA key，确认生成成功但上传失败时本地出现失败文件。
4. 多线程生成多个账号，确认每个账号 state 独立，某个上传失败不影响其他账号。

## 验收标准

1. WebUI 可以选择 SUB2API 或 CPA。
2. 选择 SUB2API 时现有流程保持兼容。
3. 选择 CPA 时优先使用 CPA 原生 OAuth 导入。
4. CPA 原生导入失败时自动尝试 auth-files fallback。
5. 多线程同时生成账号时，每个账号使用独立 CPA OAuth state。
6. 任一账号注册成功但上传失败时，本地保存失败上传 JSON 文件。
7. WebUI 明确显示“生成成功、导入失败”和失败文件路径。
8. 上传失败不会影响其他线程继续生成/上传。
