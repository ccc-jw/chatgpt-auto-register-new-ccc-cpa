# SMS 多平台配置设计

## 背景

当前短信配置以单个平台为中心：`sms.provider`、`sms.api_key`、`sms.countries`、`sms.max_price`。这导致 Web GUI 只能保存当前平台的一套配置，切换短信平台时容易覆盖或丢失另一个平台的 API key、国家池和价格设置；短信价格检查也只能查询当前平台。

## 目标

- 支持同时保存 `smsbower` 和 `hero-sms` 的独立配置。
- 首页选择短信平台时，自动加载该平台对应的 API key、国家池、服务、最高价格等字段。
- 保存配置时，只更新当前选择平台的配置。
- 注册流程只使用当前激活平台。
- 短信价格检查页查询所有已配置 API key 的短信平台，并按平台独立判断是否在购买池。
- 保持旧配置兼容，现有 `config.json` 不需要手动迁移即可启动。

## 配置结构

新增标准结构：

```json
{
  "sms": {
    "active_provider": "hero-sms",
    "providers": {
      "smsbower": {
        "api_key": "",
        "countries": ["151", "33"],
        "service": "dr",
        "operator": "any",
        "max_price": "0.03"
      },
      "hero-sms": {
        "api_key": "",
        "countries": ["4", "16"],
        "service": "dr",
        "operator": "any",
        "max_price": "0.03"
      }
    }
  }
}
```

兼容字段：

- `sms.provider` 继续表示当前激活平台。
- `sms.api_key`、`sms.countries`、`sms.service`、`sms.operator`、`sms.max_price` 继续同步为当前激活平台的配置，供旧调用方使用。
- 旧 `smsbower.api_key`、顶层 `country` 等旧字段继续迁移到 `sms.providers.smsbower`。

## Web GUI 行为

首页短信平台下拉框切换时：

- 从当前配置的 `sms.providers[provider]` 读取字段。
- 更新页面上的 API Key、国家/地区 ID、服务、最高价格。
- 不清空其他平台的配置。

点击保存时：

- 将页面字段写入 `sms.providers[active_provider]`。
- 更新 `sms.active_provider` 和兼容字段 `sms.provider`。
- 同步当前平台配置到旧字段：`sms_api_key`、`sms_countries`、`sms_service`、`sms_max_price`。

## 注册流程

注册只使用当前激活平台：

- `provider = sms.active_provider || sms.provider`
- `sms` 兼容字段始终代表当前激活平台，所以现有注册调用可以继续读取 `config["sms"]`。

## 短信价格检查

价格检查页查询所有已配置 API key 的平台：

- 遍历 `sms.providers`。
- 跳过未配置 API key 的平台。
- 每个平台使用自己的 `service`、`countries`、`max_price`。
- 价格检查表中的“是否已在购买池”按当前行的短信平台对应 `countries` 判断。
- 成功价格统计仍按 `sms_provider + country + success_price` 聚合。

## 错误处理

- 某个平台 API key 未配置：跳过该平台，不影响其他平台查询。
- 某个平台价格接口失败：返回错误信息，但其他平台继续展示。
- 旧配置缺少 `sms.providers`：启动时自动生成。
- API 响应不得返回任何 API key、密码或 token。

## 测试计划

- 配置加载测试：旧 `sms.provider/api_key/countries` 自动迁移到 `sms.providers`。
- 配置保存测试：切换平台后只更新对应平台配置，不覆盖另一个平台。
- Web GUI 测试：配置接口返回多平台配置但不泄露敏感字段以外的新字段结构变化。
- 价格检查测试：同时查询多个已配置平台，购买池判断按平台隔离。
- 注册兼容测试：当前激活平台仍同步到 `config["sms"]` 兼容字段。
