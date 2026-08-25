# 抖音扫码绑定与邀请码注册设计

## 目标

将控制台现有的“手工粘贴 Cookie JSON”改为服务器端抖音扫码绑定：普通用户登录平台后点击按钮、扫描抖音二维码并在手机确认，平台自动提取登录凭证并加密保存。同时在登录页提供邀请码注册入口，让管理员可安全邀请普通用户自助注册。

成功标准：

- 普通用户无需查看、复制或提交 Cookie、Token 等登录凭证。
- 全平台同一时间最多运行一个抖音扫码会话，每次最多 5 分钟。
- 扫码浏览器与 Web 进程隔离，失败或崩溃不影响平台登录和任务管理。
- 新凭证兼容当前消息发送流程；现有 Cookie-only 账号继续可用。
- 邀请码一次一用、默认 7 天有效，数据库不保存邀请码明文。
- Cookie、Token、二维码和底层浏览器异常不进入页面、应用日志或审计详情。

## 非目标

- 第一版不支持并发扫码、短信/邮箱注册、找回密码或第三方平台登录。
- 不允许用户继续手工录入 Cookie JSON。
- 不自动启用新的消息发送 Worker，也不切换或停用现有 systemd 定时器。
- 不尝试绕过抖音验证码、风控、设备确认或账号安全机制。

## 总体架构

### Web 服务

现有 `spark-web` 继续处理平台会话、权限、注册、邀请码管理、账号页面和扫码状态接口。Web 不启动 Chromium，也不接收或返回扫码成功后的明文凭证。

### 扫码认证服务

新增内部 `spark-auth` 服务，运行单实例认证循环和一个 Playwright Chromium。该容器不发布宿主机端口，通过共享 SQLite 数据库领取扫码会话。每次会话使用新的 browser context；会话结束后无条件关闭 context 并清除二维码。

`spark-auth` 使用与 Web 相同的凭证加密密钥，扫码成功后直接把凭证加密写入 `douyin_accounts`。明文凭证只存在于认证进程内存中，不经过 Web 服务。

### 资源隔离

- `spark-web` 保持 256 MiB 内存限制。
- `spark-auth` 限制为 768 MiB、1 CPU，使用只读根文件系统和 256 MiB `tmpfs`。
- `spark-auth` 不开放端口、不挂载 Docker socket、不连接现有 BPS 网络。
- 部署前确认根分区至少 5 GiB 可用、可用内存至少 1 GiB。

## 数据模型

### 邀请码 `invite_codes`

- `id`: UUID 主键。
- `code_hash`: 邀请码 SHA-256 摘要，唯一且不可为空。
- `created_by_user_id`: 创建邀请码的管理员。
- `expires_at`: 默认创建后 7 天。
- `used_by_user_id`, `used_at`: 注册成功后填写。
- `revoked_at`: 管理员撤销后填写。
- `created_at`: 创建时间。

状态不冗余存储，由时间和使用/撤销字段计算为 `unused`、`used`、`expired` 或 `revoked`。邀请码用 `secrets.token_urlsafe(24)` 生成，只在创建响应中显示一次。

### 扫码会话 `douyin_login_sessions`

- `id`: UUID 主键。
- `owner_user_id`: 发起扫码的平台用户。
- `slot`: 活动时固定为 `global`，终态时设为 `NULL`；唯一约束保证只有一个活动会话。
- `status`: `queued`、`loading_qr`、`awaiting_scan`、`confirming`、`succeeded`、`failed`、`expired` 或 `cancelled`。
- `qr_png`: 临时二维码 PNG；成功、失败、超时或取消时清空。
- `account_id`: 成功后指向新建的抖音账号。
- `error_code`: 只保存允许公开的稳定错误码。
- `expires_at`: 创建后 5 分钟。
- `created_at`, `updated_at`, `finished_at`: 生命周期时间。

用户只能读取和取消自己的扫码会话。管理员也不能读取二维码或凭证明文。服务启动时把遗留的过期活动会话标记为 `expired` 并释放全局槽。

现有 SQLite 初始化采用 `Base.metadata.create_all`；本次只新增表和现有账号表可兼容的新数据版本，不执行破坏性迁移。

## 凭证格式与兼容性

现有 `douyin_accounts.encrypted_cookies`、`cookie_nonce` 和 `cookie_version` 继续作为加密载体：

- 版本 1：现有 Cookie JSON 数组。
- 版本 2：`{"version":2,"storage_state":{"cookies":[],"origins":[]}}`。

扫码成功使用 Playwright `context.storage_state()` 生成版本 2 载荷，要求至少包含非空 Cookie；加密后才写数据库。执行器根据 `cookie_version` 选择 `context.add_cookies()` 或创建 context 时传入 `storage_state`。版本 1 数据不修改、不删除。

账号显示名默认使用扫码后读取到的抖音昵称；同时保存可用时读取到的抖音号作为审计外的账号元数据。用户可在后续页面修改自己的账号备注。

## 扫码状态机

1. 用户提交带 CSRF 的扫码开始请求。
2. Web 在单个事务中尝试创建 `slot=global` 的会话；唯一约束冲突时返回“当前有人正在绑定，请稍后重试”。
3. `spark-auth` 原子领取 `queued` 会话并改为 `loading_qr`。
4. 认证服务访问抖音创作者中心登录页，定位真实二维码元素并截取 PNG，状态改为 `awaiting_scan`。
5. Web 每 2 秒轮询状态；二维码接口返回 `image/png` 和 `Cache-Control: no-store`。
6. 检测到手机扫码/确认阶段时状态改为 `confirming`。
7. 检测到创作者中心已登录后，读取账号信息和 `storage_state`，校验、加密并在一个数据库事务中创建账号、写审计事件、标记会话成功、清空二维码并释放槽。
8. 任何异常只映射为允许公开的错误码；`finally` 关闭 context。到期或用户取消同样清空二维码并释放槽。

认证服务不自动重试失败的扫码会话，避免产生多个二维码或不明确的登录状态。用户可在终态后手动重新发起。

## Web 接口与页面

### 公共注册

- `GET /register`: 注册页面。
- `POST /register`: 接收用户名、密码、确认密码和邀请码。

用户名沿用平台现有规范。密码至少 10 位并同时包含字母和数字。邀请码摘要查找、有效性检查、邀请码消费和用户创建必须在一个事务内完成，防止重复使用。注册成功后跳转登录页，不自动建立平台会话。

公网注册请求按客户端 IP 做进程内滑动窗口限制；单实例 Web 下每 10 分钟最多 10 次失败提交。错误提示统一为“注册信息或邀请码无效”，不泄露用户名或邀请码是否存在。

### 管理员邀请码

- `POST /admin/invites`: 生成一次性邀请码，明文只在本次响应中展示。
- `POST /admin/invites/{id}/revoke`: 撤销未使用且未过期的邀请码。

管理员页面列出状态、创建时间、到期时间和使用者用户名，不显示邀请码明文或摘要。

### 扫码绑定

- `POST /accounts/scan`: 创建扫码会话。
- `GET /accounts/scan/{id}`: 返回所属用户可见的状态、剩余秒数、成功账号 ID 或公开错误信息。
- `GET /accounts/scan/{id}/qr`: 返回所属用户活动会话的二维码 PNG。
- `POST /accounts/scan/{id}/cancel`: 取消所属用户的活动会话。

创建和取消请求必须通过平台登录及 CSRF 校验。状态和二维码响应使用 `Cache-Control: no-store`。所有 ID 都使用不可预测 UUID，且每次查询仍执行 owner 检查。

## 用户界面

登录页保留用户名/密码登录，并增加“使用邀请码注册”次要按钮。

账号页面移除 Cookie JSON 表单，改为“扫码绑定抖音账号”按钮。点击后打开同页模态框，依次展示：排队/浏览器启动、二维码、手机确认、成功、失败或超时。二维码旁明确提示“请使用抖音 App 扫码并在手机确认”。关闭模态框时提示并取消仍活动的会话。

扫码成功后刷新已绑定账号列表。旧账号照常显示和删除。账号页不展示 Cookie、Token、storage state 或二维码历史。

## 安全与隐私

- 平台 Web 会话继续使用 HTTPS、Secure、HttpOnly、SameSite=Strict Cookie。
- 邀请码只保存摘要；生成值只显示一次，不写日志。
- 扫码二维码仅向会话所属用户提供且禁止缓存，终态立即清空。
- 凭证在 `spark-auth` 内存中生成并立即 AES-256-GCM 加密；任何异常信息必须脱敏。
- 页面、审计、容器日志和测试输出不得包含 Cookie、Token、邀请码明文或 storage state。
- 不信任客户端提交的 owner、状态、账号名或凭证；这些值由平台会话或认证服务生成。
- 服务不会绕过抖音验证码或风控；出现额外验证时返回可重试错误并关闭会话。

## 错误处理

公开错误码限定为：

- `slot_busy`: 当前已有扫码会话。
- `qr_load_failed`: 无法加载二维码。
- `login_timeout`: 五分钟内未完成。
- `cancelled`: 用户主动取消。
- `verification_required`: 抖音要求额外验证。
- `credential_invalid`: 登录完成但凭证校验失败。
- `automation_failed`: 其他已脱敏的自动化失败。

底层选择器、URL、堆栈和凭证不返回前端。认证容器重启时，遗留活动会话标记为失败并允许用户重新发起。

## 测试策略

- 邀请码：随机生成、只存摘要、一次消费、过期、撤销、并发消费和权限。
- 注册：密码规则、统一错误提示、普通用户角色、邀请码事务和频率限制。
- 扫码会话：全局唯一槽、owner 隔离、状态转换、取消、超时和启动清理。
- 凭证：版本 2 加密往返、日志/页面不泄密、版本 1 执行兼容。
- Web：注册入口、管理员邀请码操作、CSRF、二维码 no-store、非法跨用户访问。
- 认证适配器：用受控的 Playwright 边界替身验证二维码发布、成功落库和各错误映射；不伪造数据库、加密或状态机。
- 部署契约：`spark-auth` 无端口、无 Docker socket、资源限制、只读根文件系统且不连接 BPS 网络。
- 完整回归：运行现有全部单元测试和 secret regression。
- 线上验收：管理员生成邀请码；新普通用户注册登录；发起扫码并由用户真实扫码；确认账号入库且页面无凭证；重新加载页面和容器后账号仍可用。真实扫码需要用户本人配合，不能由自动化测试代替。

## 部署与回滚

部署前备份 SQLite 数据库并记录现有 Web、BPS 容器及两个旧定时器状态。先部署新增表和 Web，再启动 `spark-auth`；不启动 `spark-worker`，不修改现有定时器。

验收失败时停止 `spark-auth`，回退 Web 镜像到上一提交。新增表和版本 2 账号数据保留，不删除；旧版本 Web 不读取版本 2 账号执行任务，因此回滚期间这些新账号保持不可执行但不丢失。原 Cookie-only 账号和现有旧定时器不受影响。
