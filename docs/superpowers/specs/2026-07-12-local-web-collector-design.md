# 本地 Web Collector 简化设计

日期：2026-07-12  
状态：方向已批准，详细设计待用户审批

## 结论

Collector 改为“本地 Web 应用”：功能界面全部运行在浏览器中，但服务、登录凭据、文章、附件和 Obsidian Vault 仍只保存在用户自己的 Mac。

最终产品只保留四个核心部分：

1. 一个很薄的 macOS 启动器；
2. 一个只监听 `127.0.0.1` 的本地 Web 服务；
3. 一套浏览器页面；
4. 一个既有的 Application Support 数据根目录。

不再保留“Swift 功能界面 + 第二个 Moore 网页后台”两套交互，也不部署云端采集服务。

## 为什么调整

当前方案把已存在的 Moore 网页能力重新包进 SwiftUI，产生了额外复杂度：

- 用户需要在原生 App 与浏览器之间来回切换；
- Swift 需要管理另一个后台进程、端口、冷启动和生命周期；
- 登录失败、预检失败的详细原因难以在原生界面展示；
- 每增加一个采集功能，都要同时扩展 Swift 协议、ViewModel、Helper 和网页；
- DMG、原生导航和后台就绪问题与采集业务本身无关，却消耗大量开发时间。

本地 Web 方案把复杂度集中在一个地方，同时保持原来的安全边界。

## 目标

1. 一个浏览器页面完成扫码登录、预检、采集、查看结果、生成更新包和接收编辑稿。
2. 用户不需要 Codex、Terminal、Python 或手工启动后台。
3. 服务只监听本机回环地址，不允许局域网或公网访问。
4. 直接复用 Moore 的二维码登录和公众号数据能力，不重写微信协议。
5. 继续复用 `inno_collector` 的项目映射、日期过滤、去重、校验、Vault、更新包和稿件逻辑。
6. 原有登录状态、Vault、草稿收件箱和项目配置无需迁移或重建。
7. Reader 保持当前离线 App、HTML 看板和 Obsidian 工作流，本轮不改。

## 非目标

- 不制作云端 SaaS 采集平台。
- 不把 Cookie、Token、二维码、文章或附件上传到第三方服务器。
- 不允许朋友或客户远程访问 Collector。
- 不在本轮重写 Reader。
- 不在本轮加入 DeepSeek 对话。
- 不加入自动更新、多人账号、团队权限或后台常驻同步。
- 不修改用户原始 `config/projects.json` 内容。

## 三个方案

### A. 直接扩展 Moore 现有网页

在 Moore Dashboard 中加入英诺预检、采集、资料库和交付页面。

优点：页面和登录已经存在，最初代码量较少。  
缺点：英诺产品逻辑会与上游 UI 紧密耦合；上游更新、项目交付和 Reader 协作功能不属于 Moore 的职责。

结论：适合短期原型，不适合作为长期产品边界。

### B. 英诺统一本地 Web 服务，直接复用 Moore 运行模块（推荐）

新增一个冻结后的 `InnoCollectorWebServer`。它提供英诺页面和 API，并在同一个进程中调用 Moore 的二维码登录、账号同步和文章下载函数。

优点：

- 只有一个本地服务和一个浏览器界面；
- 二维码状态轮询不再反复冷启动 PyInstaller 子进程；
- 英诺业务与上游能力仍通过适配层分开；
- 可继续保留两份 MIT 许可证和清晰署名；
- 预检、采集进度和失败详情容易展示。

缺点：构建时需要把 Moore 的两个运行模块放进统一冻结二进制，并维护适配契约测试。

结论：采用。

### C. 云端托管网页

把 Web 服务部署到公网，用户用网址访问。

优点：无需本地 DMG，更新方便。  
缺点：公众号凭据、文章版权、服务器运维、远程访问控制和数据泄漏风险显著增加，也偏离“用户本人负责采集”的合规边界。

结论：不采用。

## 推荐架构

```text
英诺资讯采集.app
  └─ 极薄 macOS 启动器
       ├─ 启动 InnoCollectorWebServer
       ├─ 读取一次性 ready 握手
       ├─ 打开默认浏览器
       └─ 提供“重新打开 / 停止 / 退出”菜单

浏览器 http://127.0.0.1:<动态端口>/
  └─ 英诺本地 Web UI
       ├─ 登录与授权
       ├─ 预检与 10 项目详情
       ├─ 采集进度与失败隔离
       ├─ 资料库与 Obsidian
       ├─ 更新包交付
       └─ 编辑稿收件箱

InnoCollectorWebServer（单一冻结进程）
  ├─ Web/API 层
  ├─ inno_collector 领域逻辑
  ├─ MooreExporterAdapter
  └─ Moore 运行模块（MIT，保留署名）

~/Library/Application Support/com.inno.news.collector/
  ├─ ExporterRuntime/
  ├─ Runtime/vault/英诺被投项目资讯库/
  └─ DraftInbox/
```

## macOS 启动器

启动器不再承载采集业务 UI，只负责本机进程生命周期：

1. 验证 Web Server 二进制位于当前 App 的 `Contents/PlugIns`，是常规可执行文件且不是符号链接；
2. 用 `--host 127.0.0.1 --port 0` 启动服务；
3. 服务绑定系统分配的动态端口后，向启动器输出一行有上限的 JSON ready 握手；
4. 启动器验证 PID、host、port 和协议版本，然后打开浏览器；
5. 重复打开 App 时只重新打开现有页面；
6. 用户选择“停止”或“退出”时，只终止当前启动器拥有的服务。

动态端口和 ready 握手替代固定 `18765` 与 HTTP 轮询，因此不再有端口冲突和 PyInstaller 冷启动误判。

## 统一 Web 服务

优先使用 Python 标准库 HTTP Server 与小型静态前端，避免为本地单用户工具引入云端框架、数据库服务或 Node 运行时。

服务内部可以使用线程或受控任务执行长时间采集，但同时只允许一个写任务：

- 登录状态轮询可以并发读取；
- 预检和正式采集互斥；
- 更新包生成与采集互斥；
- 资料库读取不阻塞；
- 取消只作用于当前任务。

服务退出后不留下后台常驻进程。

## 登录流程

网页直接调用 Moore 已有的三个二维码能力：

1. `exporter-login-qr-start` 对应的运行函数创建本地登录会话和二维码；
2. 页面通过本地 API 每两秒查询 `exporter-login-qr-status`；
3. 手机确认后，服务调用 `exporter-login-qr-complete`；
4. Moore 将 auth-key 优先写入 macOS Keychain，并把非秘密状态写入 `ExporterRuntime`；
5. 页面自动进入“运行预检”，不要求用户回到另一个 App。

Web API 只返回不透明的 `login_id`、状态和二维码字节，不返回二维码文件路径、Cookie、Token、auth-key 或底层响应原文。

## 页面结构

### 1. 首页

- 当前登录状态；
- 最近一次预检与采集结果；
- 文章数、项目数、部分失败数；
- 推荐下一步按钮。

### 2. 登录与预检

- 在页面内显示二维码；
- 明确提示仅由采集者本人扫码；
- 显示等待扫码、手机确认、完成、过期等状态；
- 登录完成后自动运行或引导运行预检；
- 逐一显示 10 个项目的公众号映射、登录、目录和日期检查结果；
- 失败必须展示可理解的原因，而不是只显示“存在失败项目”。

### 3. 采集

- 只有最近一次预检成功才能启动；
- 显示当前项目、同步数、下载数、跳过数和失败数；
- 失败项目独立列出，不中断其他项目；
- 支持安全取消；
- 完成后显示 Vault 校验、断链、哈希和敏感信息扫描摘要。

### 4. 资料库

- 项目、文章和附件统计；
- 最近文章与项目筛选；
- 打开离线 HTML 看板；
- 用 Obsidian 打开现有 Vault；
- 不在本轮重新制作完整阅读器。

### 5. 交付

- 生成基线或增量 `.inno-update`；
- 浏览器下载生成的更新包；
- 显示版本、文件数、哈希与失败原因；
- 生成物只从允许的交付临时目录读取，并在下载完成或超时后清理。

### 6. 稿件收件箱

- 通过文件选择器上传 `.inno-drafts` 到本地服务；
- 上传内容先进入大小受限的临时文件；
- 复用现有预览、重复检测和确认收录逻辑；
- 未确认前不写入 Vault；
- 冲突稿件继续并列保留。

### 7. 关于与许可证

- 项目 MIT License；
- wechat-article-exporter MIT；
- moore-wechat-article-downloader MIT；
- 文章版权与授权边界；
- 本地数据位置与清理方式。

## 本地 API 草案

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/bootstrap` | 页面初始状态、能力和非秘密版本信息 |
| POST | `/api/login/start` | 创建二维码登录会话 |
| GET | `/api/login/{id}/status` | 查询扫码状态 |
| POST | `/api/login/{id}/complete` | 完成本机凭据保存 |
| POST | `/api/preflight` | 启动预检任务 |
| POST | `/api/collection` | 启动正式采集 |
| GET | `/api/jobs/{id}` | 获取任务快照 |
| GET | `/api/jobs/{id}/events` | 获取受限进度事件流 |
| POST | `/api/jobs/{id}/cancel` | 取消当前任务 |
| GET | `/api/library/summary` | 获取 Vault 摘要 |
| POST | `/api/delivery` | 生成更新包 |
| GET | `/api/delivery/{id}/download` | 下载一次性本地生成物 |
| POST | `/api/drafts/preview` | 上传并预览编辑稿包 |
| POST | `/api/drafts/{id}/accept` | 明确确认收录 |
| POST | `/api/service/stop` | 用户明确停止本地服务 |

所有改变状态的接口只接受 JSON 或受限 multipart，请求必须带当前会话令牌。

## 安全边界

### 网络

- 只绑定 `127.0.0.1`，不绑定 `0.0.0.0`；
- 使用动态端口；
- 不启用 CORS；
- 校验 `Host` 与 `Origin`；
- 所有写请求要求 128 位以上的随机会话令牌；
- 令牌只注入本地页面内存，不进入 URL、日志或磁盘；
- 设置严格 CSP、`X-Content-Type-Options` 和禁止缓存的响应头。

### 文件

- 所有写路径必须位于既有 Application Support 根目录；
- 继续拒绝符号链接逃逸、绝对路径注入和 ZIP 路径穿越；
- QR 图片只能通过登录会话 ID 读取；
- 下载只能读取当前任务登记的生成物；
- 上传文件有大小、扩展名、数量和解压上限。

### 凭据与日志

- auth-key 优先保存在 Keychain；
- 浏览器永远拿不到 auth-key、Cookie 或 Token；
- 错误响应使用稳定错误码和中文说明；
- 底层异常先脱敏，不返回本机绝对路径；
- 不写访问日志中的查询参数和请求正文。

## 数据兼容与迁移

新服务沿用以下目录，不重新采集、不修改项目清单：

```text
~/Library/Application Support/com.inno.news.collector/ExporterRuntime
~/Library/Application Support/com.inno.news.collector/Runtime
~/Library/Application Support/com.inno.news.collector/DraftInbox
```

现有 Keychain 登录资料和 Vault 原地复用。首次启动只做只读兼容检查；发现不兼容时停止并给出备份提示，不自动迁移或删除。

## 打包与许可证

最终 Collector App 包含：

- 极薄启动器；
- `InnoCollectorWebServer` 冻结二进制；
- 静态 HTML/CSS/JavaScript；
- `config/projects.json` 的原样副本；
- 三份 MIT 许可证与第三方声明。

完成切换后，Collector 不再需要独立的 `InnoCollectorHelper`、`MooreExporterHelper` 和 SwiftUI 功能模块。构建时仍对 Moore 运行模块做来源固定、哈希、许可证与适配契约检查。

Reader 包继续拒绝 Collector、Moore、项目配置和采集接口。

## 迁移阶段

### 阶段 1：只读 Web 骨架

- 启动器动态端口与 ready 握手；
- 首页、资料库摘要、许可证；
- 不删除现有 Swift Collector。

### 阶段 2：登录与预检

- 页面内二维码登录；
- 10 项目预检详情；
- 与当前 r3 并行验收。

### 阶段 3：采集任务

- 后台任务、事件流、取消和失败隔离；
- Vault 校验与结果页；
- 使用测试数据与用户本机授权账号验收。

### 阶段 4：交付与稿件

- 基线/增量包下载；
- 编辑稿预览和确认收录；
- 保持 Reader 协议兼容。

### 阶段 5：切换与删旧

- Web 端全功能验收通过后，启动器默认打开 Web；
- 再删除 SwiftUI Collector 功能页和两个旧 Helper；
- Reader 不变；
- r3 在一个过渡版本内保留为回退包，不自动覆盖用户数据。

## 测试与验收

### 自动化

- API 输入、输出和稳定错误码；
- 登录状态机与二维码文件边界；
- 动态端口 ready 握手；
- Host、Origin、会话令牌、CORS 与 CSP；
- 单写任务、取消、崩溃恢复；
- 10 项目预检详情；
- 采集失败隔离、增量去重和 Vault 校验；
- 更新包与稿件协议兼容；
- 浏览器端关键流程测试；
- Reader 包隔离与仓库策略回归。

### 真实验收

1. 干净 macOS 账户安装；
2. App 启动后只出现一个浏览器产品界面；
3. 服务只监听随机回环端口；
4. 用户扫码后登录状态保存到本机；
5. 10 项目预检逐项可解释；
6. 正式采集和取消均可用；
7. Obsidian 打开原 Vault；
8. 生成的更新包能被现有 Reader 导入；
9. Collector 退出后服务和端口消失；
10. App/DMG 不含凭据、真实文章、本机路径或用户原始项目清单。

## 完成标准

只有阶段 1–4 的 Web 功能、数据兼容、安全测试、真实扫码、真实采集和 Reader 更新包往返全部通过后，才进入阶段 5 删除旧 SwiftUI Collector。

这意味着迁移是“先并行验证，再删旧”，不会为了简化架构而牺牲当前可用数据或回退能力。

## 待审批的关键决策

1. Collector 改为本地浏览器界面，Reader 暂时不改；
2. 保留一个极薄 macOS 启动器负责启动、重新打开和退出；
3. 采用单一 `InnoCollectorWebServer`，不再启动第二个 Moore 网页后台；
4. 页面内直接完成二维码登录、预检和采集；
5. Web 全功能验收前不删除当前 r3；验收后再移除旧 SwiftUI Collector 与两个 Helper。
