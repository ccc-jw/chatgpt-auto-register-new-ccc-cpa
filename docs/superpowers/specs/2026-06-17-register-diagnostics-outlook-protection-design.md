# 注册失败诊断与 Outlook 邮箱保护设计

## 背景

2026-06-17 凌晨日志显示，注册任务最终 0 成功。主要失败集中在两类：ChatGPT/OpenAI 手机号注册接口返回 `status=400`，以及短信供应侧大量 `NO_NUMBERS`、`BANNED`、`WRONG_MAX_PRICE`。其中 `register_rejected(status=400)` 只有状态码，没有响应摘要，无法判断是手机号段、代理风控、参数格式还是频率限制导致。同时日志显示 Outlook 邮箱池后续变成全部已使用，需要确认失败路径不会过早消耗邮箱。

## 目标

本次只做最小安全改动：

1. 让手机号注册 400 失败可诊断。
2. 确保手机号注册失败不会消耗 Outlook 邮箱池。

本次不以提升注册成功率为目标。

## 非目标

- 不修改国家/地区排序策略。
- 不修改 `NO_NUMBERS` 重试次数或冷却策略。
- 不调整短信价格上限。
- 不新增自动暂停或熔断。
- 不新增 UI 页面。
- 不改变 Phase2 邮箱绑定成功/失败后的状态语义。

## 设计

### 1. 注册拒绝响应摘要

在 `auto_register.py` 的手机号注册阶段，`ChatGPTRegister.register()` 返回非成功结果时，如果状态码是 400，将失败原因从当前的：

```text
注册被拒(status=400)
```

扩展为包含安全摘要的形式，例如：

```text
注册被拒(status=400, error=invalid_request, code=phone_rejected, message=Phone number rejected)
```

摘要来源优先级：

1. 结构化字段：`error`、`code`、`message`、`detail` 等非敏感字段。
2. 如果只有原始响应体，则截取短文本摘要。
3. 如果无法提取，则保持现有状态码信息。

摘要必须过滤敏感信息，不记录 cookie、csrf、token、authorization、password、session、完整手机号上下文以外的凭据。摘要长度限制在较短范围内，避免日志污染。

### 2. Outlook 邮箱保护

检查普通注册主流程中 Outlook 邮箱的 reserve/mark 位置。正确行为是：

- 手机号注册阶段失败（例如 `register_rejected`）时，不 reserve Outlook 邮箱，也不写入 `outlook_used.txt`。
- 只有流程进入 Phase2 且确实需要邮箱绑定/验证时，才 reserve Outlook 邮箱。
- Phase2 后续仍按现有规则写入 `verified`、`verify_failed` 等状态。

如果当前代码已经满足上述行为，则不改变运行逻辑，只补充测试保护该行为。

### 3. 测试

新增或扩展现有测试覆盖：

1. 当注册返回 `status=400` 且携带错误字段时，失败结果包含可诊断摘要。
2. 当注册在手机号阶段返回 400 时，不调用 Outlook reserve，也不标记邮箱 used/reserved。
3. 保持现有注册、短信、Web 统计测试通过。

## 验证

实施后运行：

```bash
.venv/bin/python -m pytest
```

通过标准：全部现有测试通过，新增测试证明 400 日志可诊断且手机号注册失败不消耗 Outlook 邮箱。
