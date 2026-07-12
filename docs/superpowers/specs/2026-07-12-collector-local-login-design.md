# Collector 本地登录后台设计

日期：2026-07-12  
状态：已批准方案 A，待实施计划

## 背景

Collector 已能运行预检和采集，但新安装后没有创建公众号登录状态的入口。底层 `MooreExporterHelper` 已提供 `exporter-server-start` 本地网页后台，其中包含扫码登录、登录状态检查和凭据保存。若直接打包当前 Collector，首次安装者只能看到预检失败，无法在没有命令行或 Codex 的情况下完成登录。

本设计补齐本机登录入口，然后再制作 Collector 自用 ad-hoc 试用 DMG。它不改变 Reader，也不扩大客户分发范围。

## 目标

1. Collector 的“采集”页提供“打开本地登录后台”按钮。
2. 点击后只从 App 自身的 `Contents/PlugIns/MooreExporterHelper` 启动后台。
3. 后台只监听 `127.0.0.1:18765`，浏览器只打开该回环地址。
4. 登录凭据只由 Moore 保存到本机 Keychain 与 Collector 的 Application Support 运行目录。
5. 登录成功后，用户回到 Collector 运行预检；预检仍是正式采集的强制门槛。
6. 登录入口通过自动化测试和真实 App 构建验证后，再生成自用 Collector DMG、安装说明与 SHA-256。

## 非目标

- 不在 SwiftUI 内原生渲染二维码。
- 不修改 Moore 的登录协议、账号同步或文章下载实现。
- 不把 Cookie、Token、二维码、运行目录或真实文章打入 App/DMG。
- 不让 Reader 获得登录、联网采集或项目配置。
- 不创建正式 tag 或 GitHub Release。
- 不用本功能替代 Developer ID 签名、公证或干净账户验收。
- 本轮不引入 XPC Service、LaunchAgent、自动更新或后台常驻服务。

## 方案比较

### A. 打开 Moore 本地网页后台（采用）

复用已经存在的扫码登录页面、状态轮询和 Keychain 存储。实现范围小，且登录细节继续由上游维护。代价是扫码发生在浏览器，而不是 Collector 原生窗口。

### B. Collector 原生二维码流程

分别封装 `exporter-login-qr-start/status/complete`，在 SwiftUI 中展示二维码和状态。体验更集中，但需要新增协议、二维码文件边界、轮询和恢复逻辑，超过自用试用版的最小范围。

### C. 手工迁移已有登录状态

不改 App，要求使用者用命令行或复制运行目录。它违反“没有 Codex 和开发环境也能使用”的目标，因此不采用。

## 架构

### `LocalLoginServing` 接口

Collector Feature 内新增一个小型接口，向 ViewModel 暴露：

- `open() throws`：启动或复用本地后台并打开浏览器；
- `stop()`：终止由当前 App 启动的后台。

ViewModel 依赖接口而不是直接操作 `Process`，以便测试成功、重复点击和失败路径。

### `MooreLocalLoginServer`

具体实现拥有一个 `Process`，依赖以下不可变输入：

- `MooreExporterHelper` 的绝对 URL；
- `ExporterRuntime` 的绝对 URL；
- 固定后台 URL `http://127.0.0.1:18765/`；
- 可注入的浏览器打开函数。

启动前必须验证：

1. Helper 是常规可执行文件，不是符号链接；
2. Helper 的标准化父目录等于当前 App 的 `Contents/PlugIns`；
3. 运行目录位于 `~/Library/Application Support/com.inno.news.collector/` 内；
4. URL 的主机严格等于 `127.0.0.1`，端口严格等于 `18765`。

启动参数固定为：

```text
--runtime-dir <ExporterRuntime>
exporter-server-start
--host 127.0.0.1
--port 18765
--no-open
```

Collector 自己调用 `NSWorkspace` 打开 URL，避免 Helper 决定任意浏览器目标。子进程的 stdout/stderr 指向空设备，避免将运行目录或潜在诊断写入日志。

### 进程生命周期

- 当前实例仍在运行时，重复点击只重新打开同一 URL，不创建第二个进程。
- 启动后等待一个短且有上限的就绪窗口；只有确认子进程仍在运行且本地页面可访问时才打开浏览器。
- 端口已被其他进程占用、Helper 立即退出或页面未就绪时，停止本次进程并返回稳定错误；不能打开端口上未知服务。
- `stop()` 只终止当前对象亲自启动的进程，不扫描或杀死其他进程。
- App 正常退出或服务对象释放时调用 `stop()`。

固定端口是本轮的有意取舍：上游当前在 `--port 0` 时不能把操作系统实际分配的端口可靠返回给 App。自用试用版采用单实例固定端口并在冲突时安全失败。若未来需要多实例或处理强制退出后的孤儿进程，再升级为 XPC 或让上游返回动态端口。

## UI 与数据流

“采集”页顺序如下：

1. 显示“登录状态与采集能力只保存在这台 Mac”的现有提示；
2. 新增“打开本地登录后台”按钮和说明：“仅供采集者本人扫码登录，请勿将登录状态或采集端分享给客户”；
3. 用户在浏览器完成扫码登录；
4. 用户回到 Collector 点击“运行预检”；
5. 只有预检成功后，“开始采集”按钮可用。

登录服务错误通过 ViewModel 转换为稳定中文消息，不把 Helper 路径、端口占用进程、Cookie、Token 或底层异常原文显示给用户。

## 安全边界

- 监听地址必须为回环地址，不使用 `0.0.0.0`、局域网地址或公网地址。
- DMG 只包含 App、安装说明和许可证；运行时目录在首次启动后于 Application Support 创建。
- App 和 DMG 扫描继续拒绝 `/Users/<name>/`、`/Volumes/<name>/` 和高置信秘密。
- Collector 继续只有 `com.apple.security.network.client=true`；不新增 Reader 权限。
- Reader 构建与隔离测试必须保持不变。
- App 外层签名和构建器继续验证嵌套 Helper；运行时路径验证用于防止意外启动 App 包外的二进制。
- 自用 DMG 明确标注 ad-hoc、未公证、不得转发给客户。

## 错误处理

| 场景 | 行为 |
|---|---|
| Helper 缺失、非可执行或为符号链接 | 不启动，显示“本地登录后台不可用” |
| Helper 或运行目录越过允许边界 | 不启动，显示同一稳定错误 |
| 端口冲突 | 不打开浏览器，显示“本地登录端口被占用，请关闭旧后台或重启后重试” |
| Helper 启动后立即退出 | 清理进程引用，显示“本地登录后台启动失败” |
| 页面在就绪窗口内不可访问 | 终止本次进程，显示启动失败 |
| 浏览器无法打开 | 后台保持运行，显示“请在浏览器打开 http://127.0.0.1:18765/” |
| 重复点击 | 复用当前后台并再次打开 URL |

## 测试与验收

### Swift 单元测试

- 只生成固定的 Helper 路径、运行目录、host、port 和 `--no-open` 参数；
- 拒绝缺失文件、符号链接、包外 Helper 和越界运行目录；
- 首次调用启动一次并打开固定 URL；
- 重复调用不重复启动；
- 端口冲突、立即退出和就绪超时返回稳定错误；
- `stop()` 只终止自己启动的进程；
- ViewModel 成功时清除错误，失败时显示稳定中文消息；
- Collector UI 调用登录方法，Reader 不出现任何登录入口。

### 回归测试

- Python 全量测试；
- Swift 全量测试；
- 仓库策略检查；
- Reader Helper 仍拒绝 `collect`；
- Reader bundle 仍不含 Moore/Collector Helper 或 `projects.json`。

### 真实 App 与 DMG 验收

- release Collector/Reader App 构建和深度签名结构验证；
- Collector 登录后台只监听 `127.0.0.1:18765`；
- 浏览器页面可打开并显示本地扫码登录入口；
- 停止 Collector 后，由它启动的后台正常退出；
- Collector DMG 不含登录凭据、真实文章、本机路径或用户原始项目清单；
- Collector DMG 生成 SHA-256，并明确为自用、ad-hoc、未公证；
- 不创建 tag，不上传 GitHub Release。

## 完成标准

只有在登录入口、预检门槛、角色隔离、真实 App 构建和 Collector DMG 反向挂载审计全部通过后，才交付 Collector 自用试用 DMG。正式公开发布仍由 Issues #1、#2、#4 的签名、公证和干净账户门槛控制。
