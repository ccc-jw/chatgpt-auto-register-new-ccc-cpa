# Python 文件职责、改动影响与接口清单补充

> 生成时间：2026-06-06  
> 主分析文档：`docs/analysis/project-analysis.md`  
> 本补充文档覆盖：每个 `.py` 文件职责、改动影响、项目内 Flask 接口、主要外部 HTTP 接口的作用与出入参。

---

## 1. 阅读说明

本文中的“改动影响”按软件工程影响范围描述，重点回答：

> 如果改这个文件，哪些入口、流程、数据或外部依赖会受影响？

影响范围分为：

- **高影响**：会影响注册主流程、鉴权、安全、支付、数据库或外部账号状态。
- **中影响**：影响某个子流程、管理工具、邮箱/短信/支付某一路集成。
- **低影响**：测试、辅助脚本、静态 JS 片段或包装层。

安全说明：项目包含自动化注册、接码、支付等敏感能力。本文只做工程理解、维护影响与接口契约整理，不提供规避风控或滥用指导。

---

## 2. 每个 Python 文件职责与改动影响

### 2.1 根目录核心文件

| 文件 | 主要职责 | 关键函数 / 类 | 改动影响 |
|---|---|---|---|
| `_payment_jslib.py` | 浏览器端 JS 注入片段集合，供支付/浏览器自动化流程注入使用。 | 无 Python 函数，主要是 JS 字符串片段。 | **中影响**。改动会影响支付浏览器自动化行为，可能导致按钮点击、页面检测、风控挑战处理失败。 |
| `_upstream_payment.py` | 支付核心逻辑，生成 Plus/Team 支付链接、无痕浏览器打开、检测订阅状态。 | `fetch_billing_address`、`generate_plus_link` 类流程函数、浏览器点击/选择/检测函数。 | **高影响**。改动会影响 Plus/Team 支付链路、浏览器自动化、订阅状态判断和支付成功率。 |
| `auth.py` | 多用户平台 JWT 鉴权中间件。 | `make_token`、`decode_token`、`login_required`、`admin_required` | **高影响**。改动会影响所有 `/api/member/*` 和 `/api/admin/*` 权限边界，可能造成登录失效、鉴权绕过或管理员权限异常。 |
| `auto_register.py` | Phase 1 CLI 入口和注册主编排。 | `load_config`、`register_one`、`main`、`StopRequested` | **高影响**。改动会直接影响 CLI、单机 GUI、多用户 runner 的注册主流程、结果字段、重试逻辑、配置读取和输出文件。 |
| `card_generator.py` | 本地生成 Luhn 合规的虚拟 Visa 卡号、有效期、CVV。 | `_luhn_check_digit`、`is_luhn_valid`、`generate_visa_card` | **中影响**。改动会影响支付测试/支付流程中使用的卡片数据格式和有效性。 |
| `chatgpt_register.py` | ChatGPT/Auth 协议注册核心。 | `ChatGPTRegister`、`register_phone_account` | **高影响**。改动会影响 CSRF、OAuth 跳转、手机号注册、OTP 校验、创建账户、session/access token 获取。 |
| `config.py` | 多用户服务端固定配置与环境变量读取。 | 常量：`ADMIN_USERNAME`、`ADMIN_PASSWORD`、`DB_URL`、`JWT_SECRET` | **高影响**。改动会影响数据库连接、管理员账号、JWT 有效性、每日邀请码策略和默认资产配置。 |
| `db.py` | PostgreSQL 连接池、建表、用户/邀请码/卡密/日志/资产 CRUD。 | `init_db`、`create_user`、`verify_login`、`consume_quota`、`redeem_card` 等 | **高影响**。改动会影响多用户平台数据结构、登录、额度扣减、卡密兑换、注册日志、管理员资产。 |
| `gopay_pay.py` | GoPay 纯协议支付流程。 | `GoPayPayment`、`pay`、`_midtrans_get/post/delete`、`_gwa_get/post` | **高影响**。改动会影响 GoPay 支付流程、OTP、PIN 验证和 Midtrans 交互。 |
| `gopay_register.py` | GoPay 注册/登录协议、签名、设备画像、加密 PIN 等。 | `DeviceProfile`、`XESigner`、`GoPayProtocol` 等 | **高影响**。改动会影响 GoPay 账户/支付相关协议请求、签名正确性和设备参数。 |
| `icloud_hme.py` | iCloud Hide My Email、Chrome cookies 提取、别名创建、邮件验证码轮询。 | `ICloudHME`、`extract_chrome_cookies`、`create_alias`、`poll_mail_for_code` | **高影响**。改动会影响 iCloud 邮箱别名创建、邮箱验证码获取、Phase 2 邮箱绑定。 |
| `mailmanage_client.py` | MailManage 邮箱管理平台 API 客户端。 | `MailManageClient`、`list_mailboxes`、`get_available_email`、`get_code` | **中影响**。改动会影响免费邮箱池获取、邮箱标记已用、验证码读取。 |
| `openai_bind_email.py` | OpenAI 后半段协议流程：OAuth 登录、手机号验证、密码验证、绑定邮箱、邮箱 OTP、workspace、code 获取、SUB2API 上传。 | `OAuthSecondHalf`、`run_second_half`、内部 `_Sentinel` | **高影响**。改动会影响 Phase 2 全链路、邮箱绑定、OAuth code、SUB2API 账号导入。 |
| `openai_oauth.py` | OpenAI OAuth 通用模块，处理 code verifier/challenge、token exchange、refresh、userinfo、SUB2API 生成 OAuth URL。 | `OpenAI_OAuth`、`OAuthTokens`、`build_oauth_url`、`parse_oauth_url` | **高影响**。改动会影响 OAuth token 获取、刷新、SUB2API 授权链接生成和登录态检查。 |
| `openai_pipeline.py` | 全链路编排器：Phase 1 注册、邮箱获取、Phase 2 绑定/上传。注意：模块内有 MailManage helper，但 `run_full()` 当前实装路径总是调用 `create_icloud_alias()`，未按 `email_provider` 切换 MailManage/Outlook。 | `FullPipeline`、`resume_pipeline`、`main` | **高影响**。改动会影响端到端自动化流程、CLI 参数、邮箱提供商、SUB2API 上传逻辑；若要接入 MailManage/Outlook，需要补 orchestration 分支。 |
| `outlook_mail.py` | Outlook 邮箱池管理和验证码轮询，支持 IMAP / Graph fallback。 | `OutlookMailClient`、`poll_outlook_for_code`、`reserve_next_outlook` | **中到高影响**。改动会影响 Outlook 邮箱池可用性、验证码获取和 Phase 2 邮箱绑定成功率。 |
| `outlook_manager.py` | 本地 Outlook 邮箱池管理 CLI。 | `cmd_stats`、`cmd_list`、`cmd_next`、`cmd_mark`、`cmd_test` | **中影响**。改动会影响邮箱池维护、状态标记、导出和测试命令。 |
| `payment_protocol.py` | ChatGPT 测试支付协议 pipeline 骨架，整合 Stripe/PayPal 多阶段。 | `ProtoState`、`StageResult`、`run_protocol_checkout`、多个 `proto_stage_*` | **高影响**。改动会影响支付协议 pipeline、阶段状态、PayPal/Stripe 支付测试流程。 |
| `paypal_fraudnet.py` | PayPal FraudNet / magnes / DFP device session 协议化注册。 | `register_fraudnet_session` | **中到高影响**。改动会影响 PayPal 支付前设备风控数据注册。 |
| `paypal_http.py` | PayPal Checkout 协议层，处理 approve、GraphQL、signup、OTP、challenge。 | `paypal_get_approve`、`paypal_graphql_batch`、`paypal_post_signup`、`paypal_post_otp_*` | **高影响**。改动会影响 PayPal 支付/注册/OTP/授权链路。 |
| `phase2_codex.py` | Phase 2 包装层，但有两条不同路径：`codex_login()` 包装 `run_second_half` 做邮箱绑定 + OAuth；`upload_session()` 是直接导入 codex-session。 | `codex_login`、`get_oauth_url`、`upload_session` | **高影响**。`auto_register.py --phase2` 当前调用的是 `upload_session()`，并不执行邮箱绑定/OAuth 后半段；改动会影响 SUB2API 登录、OAuth URL 获取、session 导入。 |
| `phone_sms.py` | 多接码平台统一抽象，支持 hero-sms、SMSBower、5sim。 | `Activation`、`HeroSMS`、`FiveSim`、`SmsBower`、`PhoneSMS` | **高影响**。改动会影响 `openai_pipeline.py` 使用的接码平台兼容性、号码购买、验证码轮询和取消/完成状态。 |
| `plus_payment.py` | ChatGPT Plus 支付链路入口，支持生成支付链接、PayPal 协议 checkout、浏览器抓 Midtrans URL。 | `generate_plus_link`、`complete_paypal_checkout_protocol`、`grab_midtrans_url` | **高影响**。改动会影响 Plus 支付链接生成、PayPal 支付和浏览器抓取支付 URL。 |
| `quick_fix.py` | 快速修复/实验脚本：登录、提交 profile、绑邮箱、上传 SUB2API。 | `make_session`、`login_and_submit_profile`、`bind_email_and_upload`、`main` | **中影响**。改动主要影响临时/手工修复流程，不应作为主链路依赖。 |
| `runner.py` | 多用户注册任务引擎，负责线程、SSE 日志、额度、邮箱选择、调用 `auto_register.register_one`。 | `start`、`stop`、`_run`、`get_email_for_user` | **高影响**。改动会影响多用户注册启动/停止、日志、额度扣减、邮箱获取和任务并发。 |
| `scheduler.py` | 每日邀请码生成调度器。 | `start_scheduler`、`_gen_daily` | **中影响**。改动会影响管理员邀请码自动生成策略。 |
| `sentinel.py` | OpenAI Sentinel anti-bot token 生成器。 | `Sentinel.get`、`_requirements_token`、`_pow_token` | **高影响**。改动会影响注册流程中 Sentinel header，有可能导致注册/OTP/创建账户请求被拒。 |
| `server.py` | 多用户 Flask Server，提供登录、注册、用户配置、注册任务、管理员、卡密和资产 API。 | `api_login`、`api_register`、`api_reg_start`、`api_assets`、`start_server` | **高影响**。改动会影响整个多用户平台 API、鉴权边界、管理员能力和前端交互。 |
| `sms_channel.py` | GoPay 接码渠道抽象，支持 herosms、smspool、smsbower，主要用于 GoPay worker patch。 | `SmsPoolChannel`、`SmsActivateStyleChannel`、`patch_worker_with_*` | **中到高影响**。改动会影响 GoPay 相关 OTP 接码和 worker 集成。 |
| `smsbower.py` | SMSBower API 客户端，供 Phase 1 主流程直接使用。 | `SmsBower.balance`、`get_number`、`wait_code`、`complete`、`cancel` | **高影响**。改动会直接影响手机号获取、验证码等待、激活完成/取消。 |
| `stripe_http.py` | Stripe Checkout 协议层。 | `stripe_init`、`stripe_update_tax_region`、`stripe_create_paypal_payment_method`、`stripe_confirm_paypal`、`stripe_poll` | **高影响**。改动会影响 Stripe / PayPal payment method 相关支付阶段。 |
| `web_gui.py` | 单机 Flask GUI，内置页面、配置、注册任务、日志、结果、Phase 2、Plus、Outlook 管理。 | `start_gui`、`api_config`、`api_start`、`_run`、大量 GUI API | **高影响**。改动会影响本地 GUI 所有功能。该文件体积大且存在同名 `_run` 重复定义，维护风险高。 |

### 2.2 platforms 目录

| 文件 | 主要职责 | 关键函数 / 类 | 改动影响 |
|---|---|---|---|
| `platforms/__init__.py` | platforms 包初始化。 | 无 | **低影响**。通常只影响包导入路径。 |
| `platforms/_browser_backend.py` | 浏览器后端抽象，目前偏 Camoufox 支持。 | `BrowserBackendConfig`、`open_browser_backend` | **中影响**。改动会影响支付/浏览器自动化模块打开浏览器的方式。 |
| `platforms/chatgpt/__init__.py` | chatgpt 平台包初始化。 | 无 | **低影响**。通常只影响包导入路径。 |
| `platforms/chatgpt/_payment_jslib.py` | `_payment_jslib.py` 的平台目录版本，浏览器端 JS 注入片段。 | 无 Python 函数 | **中影响**。影响平台化支付自动化 JS 注入。 |
| `platforms/chatgpt/card_generator.py` | `card_generator.py` 的平台目录版本，生成 Luhn 合规虚拟卡。 | `_luhn_check_digit`、`is_luhn_valid`、`generate_visa_card` | **中影响**。影响平台化支付测试卡数据。 |
| `platforms/chatgpt/payment.py` | `_upstream_payment.py` 的平台目录版本，支付核心逻辑。 | 支付链接、浏览器、订阅检测相关函数 | **高影响**。影响平台化 ChatGPT 支付流程。 |
| `platforms/chatgpt/payment_protocol.py` | `payment_protocol.py` 的平台目录版本，支付协议 pipeline。 | `ProtoState`、`StageResult`、`run_protocol_checkout` | **高影响**。影响平台化支付协议 pipeline。 |
| `platforms/chatgpt/paypal_fraudnet.py` | PayPal FraudNet 平台目录版本。 | `register_fraudnet_session` | **中到高影响**。影响 PayPal device session。 |
| `platforms/chatgpt/paypal_http.py` | PayPal Checkout 平台目录版本。 | `paypal_get_approve`、`paypal_graphql_batch`、`paypal_post_*` | **高影响**。影响 PayPal 支付链路。 |
| `platforms/chatgpt/stripe_http.py` | Stripe Checkout 平台目录版本。 | `stripe_init`、`stripe_confirm_paypal`、`stripe_poll` | **高影响**。影响 Stripe 支付链路。 |

### 2.3 scripts 与测试文件

| 文件 | 主要职责 | 关键函数 / 类 | 改动影响 |
|---|---|---|---|
| `scripts/pre-commit-scan.py` | pre-commit secrets 扫描脚本。 | 顶层脚本逻辑 | **中影响**。改动会影响提交前密钥检测能力，可能误拦截或漏检。 |
| `test_auto_register_retry.py` | 测试手机号获取重试和 stop 中断。 | `AutoRegisterRetryTests` | **低到中影响**。改动影响测试覆盖，不直接影响运行时代码。 |
| `test_icloud_phase2.py` | 测试 iCloud、Phase 2、Outlook fallback、SUB2API 参数转发。 | `ICloudPhase2Tests`、`Phase2WrapperTests` | **低到中影响**。改动影响 Phase 2 相关回归保障。 |
| `test_pipeline.py` | 集成/手工测试脚本：OAuth、iCloud、SUB2API。 | `login_sub2api`、`get_oauth_url`、`exchange_code_and_create` | **中影响**。虽名为 test，但可直接访问外部服务，改动可能影响手工集成验证。 |
| `test_web_gui_html.py` | Web GUI HTML/JS 静态结构测试。 | `WebGuiHtmlTests` | **低影响**。影响 GUI 前端回归保障。 |
| `test_web_gui_stats.py` | Web GUI 状态、日志、配置、Outlook pool API 测试。 | `WebGuiStatsTests` | **低到中影响**。影响 GUI API 回归保障。 |

---

## 3. 项目内 Flask 接口清单：作用、入参、出参、改动影响

### 3.1 多用户平台接口：`server.py`

| 方法 | 路径 | 处理函数 | 作用 | 入参 | 出参 | 改动影响 |
|---|---|---|---|---|---|---|
| `GET` | `/` | `index` | 返回 `public/index.html` 前端页面。 | 无 | HTML 文件 | 改动影响前端入口加载。 |
| `POST` | `/api/auth/login` | `api_login` | 用户登录并签发 JWT。 | JSON：`username`、`password` | 成功：`ok`、`token`、`role`、`username`、`quota`；失败：`ok=false`、`error` | 改动影响登录、token 签发、角色识别。 |
| `POST` | `/api/auth/register` | `api_register` | 使用邀请码注册用户。 | JSON：`username`、`password`、`invite_key` | 成功：`ok`、`token`、`role`、`username`、`quota`；失败：`error` | 改动影响用户注册、邀请码消费、初始额度。 |
| `GET` | `/api/member/me` | `api_me` | 查询当前用户信息、配置、iCloud 权益。 | Header：`Authorization: Bearer <token>` | `ok`、`user`、`config`、`icloud.active`、`icloud.remaining` | 改动影响用户首页展示、额度和配置回显。 |
| `PUT` | `/api/member/config` | `api_config` | 更新用户接码配置。 | Header token；JSON：`smsbower_key`、`proxy`、`country`、`max_price`、`sms_timeout` | `ok=true` | 改动影响注册任务读取的短信/代理/国家配置。 |
| `POST` | `/api/member/redeem` | `api_redeem` | 兑换卡密或邀请码。 | Header token；JSON：`key` | 卡密成功：`type=card`、`product`、`remaining`；邀请码成功：`type=invite`、`quota_added`；失败：`error` | 改动影响额度、iCloud 权益、卡密和邀请码消费。 |
| `POST` | `/api/member/register/start` | `api_reg_start` | 启动当前用户注册任务。 | Header token；JSON：`count` | 成功：`ok=true`；失败：`ok=false`、`error` | 改动影响注册任务启动、并发控制和目标数量。 |
| `POST` | `/api/member/register/stop` | `api_reg_stop` | 停止当前用户注册任务。 | Header token | `ok=true` | 改动影响 stop 信号和后台线程中断。 |
| `GET` | `/api/member/register/log` | `api_reg_log` | 注册任务 SSE 日志流。 | Header token；当前前端 EventSource 实现无法带 header，存在设计问题。 | `text/event-stream`，每条为 `data: {msg, tag, time}` | 改动影响前端日志显示和任务状态可见性。 |
| `GET` | `/api/member/history` | `api_history` | 查询当前用户注册历史。 | Header token | `ok`、`history[]` | 改动影响历史表格展示。 |
| `GET` | `/api/admin/stats` | `api_stats` | 管理员统计。 | Admin token | `users`、`today_invites`、`today_ok`、`total_quota` | 改动影响后台统计卡片。 |
| `POST` | `/api/admin/invite-gen` | `api_invite_gen` | 生成邀请码。 | Admin token；JSON：`count`；前端还传 `quota` 但当前后端未使用该字段。 | `ok`、`keys[]` | 改动影响邀请码发放和额度策略。 |
| `GET` | `/api/admin/invite-list` | `api_invite_list` | 查询邀请码列表。 | Admin token | `ok`、`invites[]` | 改动影响后台邀请码表。 |
| `DELETE` | `/api/admin/invite/<key_id>` | `api_invite_revoke` | 吊销邀请码。 | Admin token；URL path：`key_id` | `ok=true` | 改动影响邀请码停用。 |
| `POST` | `/api/admin/card-gen` | `api_card_gen` | 生成 iCloud 卡密。 | Admin token；JSON：`count`、`product`、`grant_count`、`duration_days` | `ok`、`keys[]` | 改动影响卡密权益、次数和有效期。 |
| `GET` | `/api/admin/card-list` | `api_card_list` | 查询卡密列表。 | Admin token | `ok`、`cards[]` | 改动影响后台卡密表。 |
| `DELETE` | `/api/admin/card/<key_id>` | `api_card_revoke` | 吊销卡密。 | Admin token；URL path：`key_id` | `ok=true` | 改动影响卡密停用。 |
| `GET` | `/api/admin/users` | `api_users` | 查询用户列表。 | Admin token | `ok`、`users[]` | 改动影响用户管理页。 |
| `GET` | `/api/admin/logs` | `api_logs` | 查询注册日志。 | Admin token | `ok`、`logs[]` | 改动影响管理员审计和日志展示。 |
| `PUT` | `/api/admin/assets` | `api_assets` | 保存管理员资产。 | Admin token；JSON：`icloud_cookies`、`mailmanage_key` | `ok=true` | 改动影响 iCloud/MailManage 后续邮箱获取。 |
| `GET` | `/api/admin/assets` | `api_assets_get` | 查询管理员资产是否配置。 | Admin token | `ok`、`icloud_cookies: bool`、`mailmanage_key: bool` | 改动影响后台资产状态展示。 |

### 3.2 单机 GUI 接口：`web_gui.py`

`web_gui.py` 很大且存在编码/重复定义问题。以下为已识别的主要接口。

| 方法 | 路径 | 作用 | 入参 | 出参 | 改动影响 |
|---|---|---|---|---|---|
| `GET` | `/` | 返回内置 HTML GUI。 | 无 | HTML | 改动影响单机 GUI 页面。 |
| `GET/POST` | `/api/config` | 读取或保存单机 GUI 配置。 | POST JSON：`api_key`、`proxy`、`country`、`service`、`password`、`max_price`、`code_timeout`、`provider`、`sub2api_*`、`icloud_cookies`、`mailmanage_*`、`outlook_pool`、`plus_*` 等 | `ok`、`config` | 改动影响 GUI 配置回显、保存和后续任务配置。 |
| `GET` | `/api/balance` | 查询 SMSBower 余额。 | 从当前配置读取 `smsbower.api_key` | 成功：`ok`、`balance`；失败：`error` | 改动影响余额展示。 |
| `GET/POST` | `/api/icloud-cookies` | 保存或读取本地 iCloud cookies。 | POST JSON：`cookies`，字符串形式 JSON | `ok`、`loaded`、`size`、`preview` 或 `error` | 改动影响 iCloud Phase 2 能力。 |
| `POST` | `/api/plus-upgrade` | 触发 Plus 升级后台线程。 | JSON：`access_token` 或 `session_token`，以及 `plus_method`、`plus_email` 等 | `ok`、`message` | 改动影响 Plus 支付入口。 |
| `POST` | `/api/start` | 启动单机注册任务。 | JSON：`count`、`retries`、`concurrency` | `ok=true` 或 `error` | 改动影响本地注册任务启动和并发。 |
| `POST` | `/api/stop` | 请求停止单机注册任务。 | 无 | `ok=true` | 改动影响停止任务。 |
| `GET` | `/api/status` | 查询当前运行状态、结果和统计。 | 无 | `running`、`results`、`stats` | 改动影响状态轮询和页面展示。 |
| `GET` | `/api/download` | 导出成功结果 JSON。 | 无 | 文件下载 | 改动影响结果导出，尤其是 token 脱敏策略。 |
| `POST` | `/api/submit-code` | 手动提交验证码。 | JSON：`code`、`thread_id` | `ok=true` 或 `error` | 改动影响手动验证码流程。 |
| `GET` | `/api/waiting-code` | 查询是否等待手动验证码。 | 无 | `waiting`、可选 `thread_id`、`hint` | 改动影响验证码输入 UI。 |
| `GET` | `/api/proxies` | 从 SUB2API 查询代理列表。 | 从当前配置读取 `sub2api.url/email/pwd` | `ok`、`items[]` | 改动影响代理选择。 |
| `GET` | `/api/waiting-pause` | 查询 Phase2 是否暂停等待用户继续/跳过。 | 无 | `paused`、`phase2_retry` | 改动影响 Phase2 人工干预 UI。 |
| `POST` | `/api/continue` | 继续暂停的 Phase2 流程。 | 无 | `ok=true` | 改动影响暂停恢复。 |
| `POST` | `/api/skip-phase2` | 跳过 Phase2。 | 无 | `ok=true` | 改动影响全链路降级。 |
| `GET` | `/api/log-since/<cursor>` | 读取增量日志。 | URL path：`cursor` | `lines[]`、`cursor` | 改动影响日志刷新。 |
| `GET` | `/api/results-list` | 查询结果文件或 `_all.json` 中的结果列表。 | Query：`source=files|all` | `ok`、`items[]` | 改动影响结果列表。 |
| `GET` | `/api/outlook-pool/summary` | 查询 Outlook 邮箱池统计。 | Query 无 | `ok`、`total`、`counts`、`current_bind_email`、`email_provider` | 改动影响 Outlook 池概览。 |
| `GET` | `/api/outlook-pool/list` | 分页查询 Outlook 邮箱池。 | Query：`status`、`q`、`page`、`page_size` | `ok`、`items[]`、`total`、`page`、`page_size` | 改动影响 Outlook 池列表和筛选。 |
| `GET` | `/api/outlook-pool/detail` | 查询某个 Outlook 邮箱详情。 | Query：`email` | `ok`、`entry`、`current_bind_email` | 改动影响邮箱详情展示。 |
| `GET` | `/api/outlook-pool/messages` | 拉取 Outlook 最近邮件。 | Query：`email`、`limit`、`include_body` | `ok`、`email`、`items[]` | 会触发 Outlook/Graph/IMAP 外部读取，改动影响邮件查看和验证码定位。 |
| `POST` | `/api/outlook-pool/action` | 标记或分配 Outlook 邮箱。 | JSON：`action`、`email`、`status` | `ok`、`action`、`email`、`entry` | 改动影响邮箱池状态和本次运行绑定邮箱。 |
| `POST` | `/api/batch-phase2` | 对已有结果批量执行 Phase 2。 | JSON：`source`、`files`、`email`、`concurrency` | `ok=true` 或 `error` | 会触发 SUB2API、邮箱、OAuth 等外部副作用，改动影响批量补跑。 |

---

## 4. 主要外部 HTTP 接口清单：作用、入参、出参、改动影响

> 说明：以下接口按“代码中已实现调用”整理。由于多个外部平台响应字段可能变化，出参按当前代码读取字段描述。

### 4.1 SMSBower / SMS Activate 风格接口

调用文件：`smsbower.py`、`phone_sms.py`、`sms_channel.py`

Base URL：

```text
https://smsbower.page/stubs/handler_api.php
https://hero-sms.com/stubs/handler_api.php
```

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 查询余额 | `GET handler_api.php?action=getBalance&api_key=...` | `api_key` | 文本余额，如 `ACCESS_BALANCE:*` 或平台余额文本 | 启动前检查余额。 | 影响余额展示和任务前置检查。 |
| 服务列表 | `GET handler_api.php?action=getServicesList&api_key=...` | `api_key` | JSON：`services[]` | 搜索服务代码。 | 影响服务发现。 |
| 查询价格 | `GET handler_api.php?action=getPricesV3&service=dr&country=151&api_key=...` | `service`、`country`、`api_key` | JSON：`{country: {service: {provider_id: {price}}}}` | 找最便宜运营商。 | 影响 CLI 价格显示和供应商选择。 |
| 获取号码 | `GET handler_api.php?action=getNumber&service=dr&country=151&providerIds=&maxPrice=&api_key=...` | `service`、`country`、可选 `providerIds`、`maxPrice` | 文本：`ACCESS_NUMBER:<activation_id>:<phone>` | 购买/分配手机号。 | 影响注册起点，失败会阻断整个流程。 |
| 设置就绪 | `GET handler_api.php?action=setStatus&status=1&id=<activation_id>&api_key=...` | `id`、`status=1` | 文本状态 | 告知平台号码已准备接码。 | 影响验证码接收状态。 |
| 查询验证码 | `GET handler_api.php?action=getStatus&id=<activation_id>&api_key=...` | `id` | `STATUS_OK:<code>`、`STATUS_CANCEL`、等待状态 | 轮询短信验证码。 | 影响 OTP 阶段。 |
| 完成激活 | `GET handler_api.php?action=setStatus&status=6&id=<activation_id>&api_key=...` | `id`、`status=6` | 文本状态 | 标记接码任务完成。 | 影响平台余额/号码状态。 |
| 取消激活 | `GET handler_api.php?action=setStatus&status=8&id=<activation_id>&api_key=...` | `id`、`status=8` | 文本状态 | 失败或超时时释放号码。 | 影响成本控制和号码释放。 |

### 4.2 5sim 接口

调用文件：`phone_sms.py`

Base URL：`https://5sim.net/v1`

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 查询余额/资料 | `GET /user/profile` | Header：`Authorization: Bearer <api_key>` | JSON 用户资料 | 查询 5sim 账户状态。 | 影响 5sim provider 可用性。 |
| 购买号码 | `GET /user/buy/activation/{country}/{operator}/{product}` | `country`、`operator`、`product` | JSON：`id`、`phone` | 获取号码。 | 影响 5sim 号码获取。 |
| 查询短信 | `GET /user/check/{activation_id}` | `activation_id` | JSON 消息列表，字段可能含 `code`、`text`、`sms` | 轮询验证码。 | 影响 OTP 获取。 |
| 取消号码 | `GET /user/cancel/{activation_id}` | `activation_id` | HTTP 200 | 取消激活。 | 影响失败释放。 |
| 完成号码 | `GET /user/finish/{activation_id}` | `activation_id` | HTTP 200 | 标记完成。 | 影响任务闭环。 |

### 4.3 ChatGPT / OpenAI 注册协议接口

调用文件：`chatgpt_register.py`

| 步骤 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 访问登录页 | `GET https://chatgpt.com/auth/login` | 浏览器导航 headers、cookies session | HTML / cookies | 建立初始会话和 cookie。 | 影响后续 CSRF / OAuth。 |
| 获取 CSRF | `GET https://chatgpt.com/api/auth/csrf` | JSON headers、session cookies | JSON：`csrfToken` | 获取登录 CSRF token。 | 无 token 会终止注册。 |
| 发起登录/注册 | `POST https://chatgpt.com/api/auth/signin/openai?...` | form：`callbackUrl=/`、`csrfToken`、`json=true`；query 含 `login_hint`、`ext-oai-did` 等 | JSON：`url` | 发起 OpenAI OAuth 登录/注册跳转。 | 影响 OAuth 跳转 URL。 |
| OAuth 跳转 | `GET <redirect_url>` | redirect URL、导航 headers | Header：`Location` | 跟随到 `auth.openai.com`。 | 影响 auth 会话建立。 |
| 注册手机号 + 密码 | `POST https://auth.openai.com/api/accounts/user/register` | JSON：`username=<phone>`、`password`；Header 含 Sentinel token | JSON：`continue_url`、状态码 | 提交手机号和密码。 | 影响注册是否被接受。 |
| 发送 OTP | `GET <continue_url>` | continue_url、session cookies | 页面/重定向 | 触发手机验证码发送。 | 影响是否收到短信。 |
| 校验 OTP | `POST https://auth.openai.com/api/accounts/phone-otp/validate` | JSON：`code`；Header 含 Sentinel token | JSON：`continue_url`、状态码 | 校验短信验证码。 | 影响是否进入 about-you。 |
| 访问 about-you | `GET <continue_url>` | continue_url | HTML / cookies | 建立创建账户上下文。 | 影响 create_account 成功率。 |
| 创建账户 | `POST https://auth.openai.com/api/accounts/create_account` | JSON：`name`、`birthdate`；Header 含 Sentinel token | JSON：`continue_url`、状态码、body | 提交姓名生日。 | 影响账号最终创建。 |
| OAuth callback | `GET <callback_url>` | callback_url | Cookie：`__Secure-next-auth.session-token` | 完成回调并提取 session token。 | 影响最终登录态。 |
| 获取 access token | `GET https://chatgpt.com/api/auth/session` | session cookies | JSON：`accessToken` | 获取访问 token。 | 影响后续 SUB2API/Plus/接口调用。 |

### 4.4 Sentinel 接口

调用文件：`sentinel.py`、`openai_bind_email.py`

| 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|
| `POST https://sentinel.openai.com/backend-api/sentinel/req` | text/plain JSON：`p` requirements token、`id` device id、`flow` | JSON：`token`、可选 `proofofwork.required/seed/difficulty`、`so` 或 `t` | 获取 Sentinel token 和 PoW challenge。 | 影响多个 OpenAI/Auth 请求是否通过风控校验。 |

代码会根据返回的 `proofofwork` 计算 PoW token，然后组装：

```text
OpenAI-Sentinel-Token
OpenAI-Sentinel-SO-Token
```

### 4.5 SUB2API 接口

调用文件：`openai_pipeline.py`、`phase2_codex.py`、`openai_bind_email.py`、`quick_fix.py`、`test_pipeline.py`、`web_gui.py`

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 登录 | `POST {sub2api_url}/api/v1/auth/login` | JSON：`email`、`password` | JSON：`code=0`、`data.access_token` | 获取 SUB2API 管理员 token。 | 影响后续所有 SUB2API 操作。 |
| 生成 OAuth URL | `POST {sub2api_url}/api/v1/admin/openai/generate-auth-url` | Header Bearer；JSON：`redirect_uri`、可选 `proxy_id` | JSON：`code=0`、`data.auth_url`，部分包装层还期望 `session/state` | 为 OpenAI OAuth 流程生成授权链接。 | 影响 Phase 2 OAuth 起点。 |
| 导入 codex session | `POST {sub2api_url}/api/v1/admin/accounts/import/codex-session` | Header Bearer；JSON：`content`、`group_ids`、`priority`、`auto_pause_on_expired`、`update_existing`、可选 `proxy_id` | JSON：`code=0`、`data.created/updated` | 把 session token 作为账号导入 SUB2API。该路径对应 `upload_session()` / CLI `--phase2`，不等同于邮箱绑定 OAuth 全流程。 | 影响账号入库。 |
| 创建账号 | `POST {sub2api_url}/api/v1/admin/accounts` | Header Bearer；JSON 账号字段 | JSON：`code=0`、`data` | 创建/上传账号。 | 影响 Phase 2 后续账号管理。 |
| exchange code | `POST {sub2api_url}/api/v1/admin/openai/exchange-code` | Header Bearer；JSON：OAuth `code` 等 | JSON：token/account 相关字段 | 用 OAuth code 换 token 或创建账号。该路径对应 `run_second_half()` 的邮箱绑定 + OAuth code 后半段。 | 影响 OAuth 完整链路。 |
| 查询代理 | `GET {sub2api_url}/api/v1/admin/proxies` | Header Bearer | JSON：`data.items[]` | GUI 代理下拉列表。 | 影响代理选择。 |
| 查询 groups | `GET {sub2api_url}/api/v1/admin/groups` | Header Bearer | JSON：`data.items[]` | quick_fix 中选择分组。 | 影响账号分组。 |

### 4.6 iCloud Hide My Email / 邮件接口

调用文件：`icloud_hme.py`

`icloud_hme.py` 通过 cookies 访问 iCloud 私有接口。代码中统一经 `_request()`、`_build_url()` 封装，因此实际 URL 由服务发现和 endpoint 拼接。

| 动作 | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|
| validate session | iCloud cookies | 成功状态 / session 信息 | 验证 cookies 是否有效。 | 影响 iCloud 能否使用。 |
| resolve service | iCloud cookies / service metadata | service endpoint | 发现 Hide My Email 服务地址。 | 影响所有 HME 请求。 |
| list aliases | cookies | JSON：`result.hme` 别名列表 | 列出已有隐私邮箱。 | 影响 reuse 逻辑。 |
| generate alias | cookies | JSON：`result.hme` | 生成新别名候选。 | 影响创建邮箱。 |
| reserve/create alias | alias metadata | JSON：`success`、`result.hme` 或 `error` | 保留并创建别名。 | 影响邮箱绑定输入。 |
| deactivate/delete alias | alias id | JSON：`success` | 停用/删除别名。 | 影响邮箱资源管理。 |
| poll mail | target email、start time、timeout | 邮件 body / 验证码 | 轮询邮件验证码。 | 影响邮箱 OTP 阶段。 |

### 4.7 MailManage 接口

调用文件：`mailmanage_client.py`

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 列邮箱 | `GET {base_url}/...`，由 `list_mailboxes` 组装 | Header/API key、category/keyword 等 | 邮箱列表 JSON | 获取可用邮箱。 | 影响免费邮箱选择。 |
| 获取验证码 | `GET {base_url}/...`，由 `get_code` 组装 | API key、email、timeout/keyword | code / message JSON | 获取邮箱验证码。 | 影响 Phase 2 邮箱 OTP。 |

### 4.8 Outlook / Microsoft Graph / IMAP

调用文件：`outlook_mail.py`

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| OAuth token | `POST <Microsoft token endpoint>` | client/account credential 或 refresh token | JSON：`access_token` | 获取 Graph/IMAP token。 | 影响 Outlook 邮件读取。 |
| Graph messages | `GET <Graph messages endpoint>` | Bearer token、过滤参数 | JSON：`value[]` | 查询最近邮件。 | 影响验证码提取。 |
| IMAP | IMAP over SSL | 邮箱、token/密码、搜索条件 | 邮件列表和正文 | fallback 或主邮件读取。 | 影响 Outlook 验证码轮询。 |

### 4.9 OpenAI Phase 2 OAuth / 绑邮箱接口

调用文件：`openai_bind_email.py`

| 步骤 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| 打开 OAuth URL | `GET <oauth_url>` | oauth_url | HTML/form/redirect | 初始化 OAuth 登录。 | 影响后半段起点。 |
| 提交表单 | `POST <form action>` | form hidden fields | redirect / HTML | 跟随登录表单。 | 影响 auth session。 |
| authorize continue | `POST https://auth.openai.com/api/accounts/authorize/continue` | JSON/headers：手机号、Sentinel | JSON：continue/page 信息 | 提交手机号继续授权。 | 影响密码验证前置。 |
| password verify | `POST https://auth.openai.com/api/accounts/password/verify` | JSON：password；Sentinel | JSON：continue_url 等 | 验证账号密码。 | 影响是否能绑定邮箱。 |
| add-email send | `POST https://auth.openai.com/api/accounts/add-email/send` | JSON：email | JSON：状态/continue | 发送邮箱绑定验证码。 | 影响邮箱是否收到验证码。 |
| email OTP validate | `POST https://auth.openai.com/api/accounts/email-otp/validate` | JSON：code | JSON：continue/workspace 信息 | 校验邮箱验证码。 | 影响邮箱绑定成功。 |
| session dump | `GET https://auth.openai.com/api/accounts/client_auth_session_dump` | cookies | JSON：`client_auth_session.workspaces` | 获取 workspace 信息。 | 影响 workspace 选择。 |
| workspace select | `POST https://auth.openai.com/api/accounts/workspace/select` | JSON：workspace id | JSON：continue_url/page | 选择 workspace。 | 影响 OAuth code 获取。 |
| final OAuth | `GET <continue/final url>` | cookies | URL 中 `code` 或 redirect | 取得 OAuth code。 | 影响 token exchange / SUB2API 上传。 |
| create_account fallback | `POST https://auth.openai.com/api/accounts/create_account` | JSON：name/birthdate | JSON：continue_url | 缺资料时补建 profile。 | 影响异常恢复。 |

### 4.10 OpenAI OAuth 通用接口

调用文件：`openai_oauth.py`

| 动作 | 方法 / URL | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| exchange code | `POST <OAUTH_TOKEN_URL>` | `grant_type=authorization_code`、`code`、`redirect_uri`、`code_verifier` 等 | JSON：`access_token`、`refresh_token`、`id_token`、`expires_in` | OAuth code 换 token。 | 影响 OAuth 完整性。 |
| refresh token | `POST <OAUTH_TOKEN_URL>` | `grant_type=refresh_token`、`refresh_token` | JSON：新 token | 刷新 access token。 | 影响长期可用性。 |
| userinfo | `GET <OAUTH_USERINFO_URL>` | Bearer token | JSON 用户信息 | 验证 token 和用户身份。 | 影响登录态检查。 |
| chat requirements | `GET https://chatgpt.com/backend-api/sentinel/chat-requirements` | session/cookies | JSON | 检查会话要求。 | 影响 session 检查。 |

### 4.11 支付相关接口

调用文件：`plus_payment.py`、`payment_protocol.py`、`stripe_http.py`、`paypal_http.py`、`paypal_fraudnet.py`、`gopay_pay.py`、`_upstream_payment.py`

| 模块 | 主要接口 | 入参 | 代码期望出参 | 作用 | 改动影响 |
|---|---|---|---|---|---|
| ChatGPT payment checkout | `POST PAYMENT_CHECKOUT_URL` | access token/cookies、plan、country、currency、payment method 等 | JSON：checkout/cashier URL、invoice/config 信息 | 生成 Plus/Team 支付链接或 checkout session。 | 影响支付入口。 |
| ChatGPT me / usage | `GET https://chatgpt.com/backend-api/me`、usage URL | access token/cookies | JSON：账号/订阅/usage 信息 | 检测订阅或账号状态。 | 影响支付后状态判断。 |
| Stripe | Stripe hosted checkout 多个 endpoint | checkout session、payment method、tax region 等 | JSON：state、success_url、redirect_url | 支付阶段推进和轮询。 | 影响 Stripe/PayPal method 流程。 |
| PayPal approve / GraphQL / signup / OTP | `paypal.com` 多个 endpoint | cookies、token、signup form、OTP code 等 | HTML、JSON、access token、challenge 状态 | PayPal 登录/注册/授权/OTP。 | 影响 PayPal 支付路线。 |
| PayPal FraudNet | FraudNet p1/p2/p3/pa endpoint | device session payload | 成功状态 / tracking payload | 注册风控设备会话。 | 影响 PayPal 风控上下文。 |
| GoPay / Midtrans | Midtrans / GoPay wallet API | midtrans_url、phone、PIN、OTP | 交易状态、challenge、verification URL | GoPay 支付。 | 影响 GoPay 支付路线。 |
| 地址服务 | `MEIGUODIZHI_ADDRESS_URL` | 请求参数 | 地址 JSON/HTML | 获取账单地址。 | 影响支付资料填写。 |

### 4.12 支付模块调用/重复实现矩阵

| 模块组 | 根目录版本 | `platforms/chatgpt` 版本 | 当前判断 | 修改建议 |
|---|---|---|---|---|
| 支付主流程 | `_upstream_payment.py`、`plus_payment.py` | `platforms/chatgpt/payment.py` | 两套实现并存；根目录 `plus_payment.py` 是 `web_gui.py` 直接调用入口之一。 | 修改支付前先反向追踪 import，确认实际入口，不要只改平台副本。 |
| 协议 pipeline | `payment_protocol.py` | `platforms/chatgpt/payment_protocol.py` | 两套高度相似实现并存。 | 后续应标注主实现并删除/冻结旧副本。 |
| PayPal | `paypal_http.py`、`paypal_fraudnet.py` | `platforms/chatgpt/paypal_http.py`、`platforms/chatgpt/paypal_fraudnet.py` | 根目录支付模块会直接 import 根目录 PayPal 文件。 | PayPal 改动需同时确认根目录和平台目录差异。 |
| Stripe | `stripe_http.py` | `platforms/chatgpt/stripe_http.py` | 两套实现并存。 | 改动前确认调用方 import 路径。 |
| 卡号生成 | `card_generator.py` | `platforms/chatgpt/card_generator.py` | 两套实现并存。 | 若修改算法或格式，需确认支付流程实际使用哪个版本。 |

---

## 5. 文件改动影响的快速判断表

| 如果要改... | 优先关注影响 |
|---|---|
| `auto_register.py` | CLI、GUI、runner 都会受影响；重点回归 `register_one()` 成功/失败路径、结果字段、重试、输出。 |
| `chatgpt_register.py` | Phase 1 所有协议步骤受影响；重点回归 CSRF、signin、OTP、create_account、token 获取。 |
| `smsbower.py` / `phone_sms.py` | 接码受影响；重点回归号码获取、验证码轮询、取消/完成状态。 |
| `sentinel.py` | OpenAI/Auth 请求通过率受影响；重点回归所有带 Sentinel header 的请求。 |
| `server.py` / `auth.py` / `db.py` | 多用户平台、安全边界、数据库受影响；重点回归登录、注册、member/admin 权限、额度、卡密。 |
| `runner.py` | 多用户任务执行受影响；重点回归 start/stop、SSE、额度扣减、邮箱选择、失败重试。 |
| `web_gui.py` | 单机 GUI 大面积受影响；重点回归配置保存、启动任务、日志、下载、Phase2/Plus/Outlook 相关按钮。 |
| `openai_bind_email.py` / `openai_pipeline.py` / `phase2_codex.py` | Phase 2 受影响；重点回归 OAuth URL、密码验证、邮箱 OTP、workspace、SUB2API 上传。 |
| `icloud_hme.py` / `mailmanage_client.py` / `outlook_mail.py` | 邮箱资源和验证码受影响；重点回归别名创建、邮箱池选择、验证码提取。 |
| `plus_payment.py` / `payment_protocol.py` / `stripe_http.py` / `paypal_http.py` / `gopay_pay.py` | 支付相关受影响；重点回归支付链接、跳转、OTP、支付状态轮询。 |
| `config.py` | 服务启动、安全和默认行为受影响；重点回归环境变量、JWT、DB_URL、管理员账号。 |
| 测试文件 | 不影响运行时，但影响回归保障；改动后应运行对应测试。 |

---

## 6. 建议后续补充方式

如果后续继续维护该项目，建议把本文拆成长期维护文档：

```text
docs/architecture/files.md
docs/architecture/internal-apis.md
docs/architecture/external-apis.md
docs/security/audit.md
docs/refactor/plan.md
```

并在每次新增外部接口时同步补充：

- 调用文件。
- 方法和 URL。
- 认证方式。
- 请求参数。
- 响应字段。
- 失败时行为。
- 改动影响。
