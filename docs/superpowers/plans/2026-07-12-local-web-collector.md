# 本地 Web Collector 实施计划

> 本计划只定义实施步骤。执行时严格按 RED → GREEN → REFACTOR 推进；每个阶段独立提交、独立验收。阶段 1–8 不删除当前 r3，阶段 9 只有在切换门槛全部通过后才能执行。

**目标：** 将 Collector 迁移为一个本地浏览器产品界面，保留极薄 macOS 启动器、既有本机数据和 Reader 协议，并在完整验收后移除旧 SwiftUI Collector 与两个旧 Helper。

**推荐架构：** 冻结后的 `InnoCollectorWebServer` 在单一进程内提供 HTTP/API、静态前端、任务调度和 Moore 运行模块适配。服务绑定 `127.0.0.1:0`，通过 stdout ready 握手把实际端口交给启动器。浏览器只访问本机动态端口。Reader 不变。

**技术约束：** Python 3.11+ 标准库 HTTP Server、现有 `unittest`、小型 HTML/CSS/JavaScript、PyInstaller、Swift 6/AppKit、现有签名与 DMG 工具。不增加云端服务、Node 运行时或运行时 Python 依赖。

---

## 总体门槛

每个任务结束都运行：

```bash
/Users/yzy/Desktop/playground/inno-portfolio-collector/.venv/bin/python \
  -m unittest discover -s tests
./scripts/test_swift.sh
/Users/yzy/Desktop/playground/inno-portfolio-collector/.venv/bin/python \
  scripts/check_repository_policy.py
git diff --check
```

任何失败都先修复，不得带失败进入下一阶段。

## 文件总览

### 新增 Python Web 层

- `src/inno_collector/web/__init__.py`
- `src/inno_collector/web/server.py`
- `src/inno_collector/web/security.py`
- `src/inno_collector/web/controller.py`
- `src/inno_collector/web/jobs.py`
- `src/inno_collector/web/moore_runtime.py`
- `src/inno_collector/web/assets/index.html`
- `src/inno_collector/web/assets/app.css`
- `src/inno_collector/web/assets/app.js`
- `packaging/collector_web_server_entry.py`

### 新增 Python 测试

- `tests/test_web_security.py`
- `tests/test_web_server.py`
- `tests/test_web_controller.py`
- `tests/test_web_jobs.py`
- `tests/test_web_moore_runtime.py`
- `tests/test_web_assets.py`
- `tests/test_web_end_to_end.py`

### 修改现有 Python 与打包

- `src/inno_collector/exporter.py`
- `src/inno_collector/collector_helper.py`（阶段 9 才删除旧入口）
- `pyproject.toml`
- `scripts/build_helpers.py`
- `scripts/build_macos_apps.py`
- `scripts/release_macos.py`
- `tests/test_build_helpers.py`
- `tests/test_build_macos_apps.py`
- `tests/test_release_macos.py`
- `scripts/check_repository_policy.py`

### 新增/修改 macOS 启动器

- Create: `macos/Sources/InnoCollectorFeature/LocalWebLauncher.swift`
- Create: `macos/Tests/InnoCollectorAppTests/LocalWebLauncherTests.swift`
- Modify: `macos/Sources/InnoAppCore/FileLocations.swift`
- Modify: `macos/Tests/InnoAppCoreTests/FileLocationsTests.swift`
- Modify: `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`
- Delete only after cutover: `macos/Sources/InnoCollectorFeature/CollectorContentView.swift`
- Delete only after cutover: `macos/Sources/InnoCollectorFeature/CollectorViewModel.swift`
- Delete only after cutover: `macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift`
- Delete matching legacy tests only after cutover.

---

## Task 1：建立 Moore 直接运行适配层

**目的：** Web Server 直接调用 Moore 二维码和采集运行函数，避免每次状态轮询都冷启动一个 PyInstaller 进程。

**Files**

- Create: `src/inno_collector/web/__init__.py`
- Create: `src/inno_collector/web/moore_runtime.py`
- Create: `tests/test_web_moore_runtime.py`
- Modify: `src/inno_collector/exporter.py`

### 1.1 写失败测试

测试一个注入式 `MooreRuntime`，覆盖：

- `start_login()` 只返回 `login_id`、过期时间和二维码内容类型；
- `read_qrcode(login_id)` 只能读取当前 `ExporterRuntime` 下登记的 QR 文件；
- `login_status()` 删除 token、cookie、key、uuid 和本机路径；
- `complete_login()` 不返回 auth-key；
- `auth_check()`、accounts、sync、articles、download 继续满足现有适配契约；
- 缺少会话、符号链接 QR、越界路径和异常均返回稳定领域错误。

运行：

```bash
.venv/bin/python -m unittest tests.test_web_moore_runtime
```

预期：因 `inno_collector.web.moore_runtime` 不存在而失败。

### 1.2 最小实现

`MooreRuntime` 构造函数接收 Moore 函数集合；生产构造器才延迟导入 `wechat_exporter`。普通单元测试不依赖上游源码。

适配层不得把以下字段交给 Web 层：

```text
auth-key, token, cookie, uuid, qrcode_path, db, runtime_dir
```

`src/inno_collector/exporter.py` 提取可复用的结果验证函数，使 CLI 适配和直接运行适配使用同一套字段校验。

### 1.3 验证与提交

```bash
.venv/bin/python -m unittest tests.test_web_moore_runtime tests.test_exporter
git add src/inno_collector/web src/inno_collector/exporter.py \
  tests/test_web_moore_runtime.py
git commit -m "feat: add direct Moore runtime adapter"
```

---

## Task 2：建立本地 HTTP 安全边界和 ready 握手

**目的：** 先做一个无业务写操作的安全服务器骨架。

**Files**

- Create: `src/inno_collector/web/security.py`
- Create: `src/inno_collector/web/server.py`
- Create: `tests/test_web_security.py`
- Create: `tests/test_web_server.py`

### 2.1 写失败测试

覆盖：

- 只接受 host `127.0.0.1`；
- `port=0` 后 ready 握手包含 `protocol`, `host`, `port`, `pid`，不含令牌或路径；
- 非本机 Host 返回 421；
- 写请求要求 `Content-Type: application/json` 和当前会话令牌；
- `Origin` 只能是当前动态 origin；
- 不返回 `Access-Control-Allow-Origin`；
- 请求正文、上传和响应都有硬上限；
- 默认头包含 CSP、`nosniff`、`no-store` 和 frame 限制；
- 未知路由、畸形 JSON、内部异常使用稳定 JSON 错误，不泄漏路径；
- stop 只关闭当前 server。

### 2.2 最小实现

使用 `ThreadingHTTPServer`，但通过 controller 串行化所有写任务。服务启动时生成随机会话令牌，只把令牌注入同源 HTML，不输出到 ready、URL 或日志。

ready stdout 只允许一行：

```json
{"protocol":1,"host":"127.0.0.1","port":54321,"pid":12345}
```

ready 后 stdout/stderr 切换为有界、脱敏诊断，禁止访问日志。

### 2.3 验证与提交

```bash
.venv/bin/python -m unittest tests.test_web_security tests.test_web_server
git add src/inno_collector/web/security.py \
  src/inno_collector/web/server.py \
  tests/test_web_security.py tests/test_web_server.py
git commit -m "feat: add secure local Web server skeleton"
```

---

## Task 3：建立只读页面壳、首页和资料库摘要

**目的：** 在不触发登录或采集的情况下，先验证一个页面、一个服务和现有数据兼容。

**Files**

- Create: `src/inno_collector/web/controller.py`
- Create: `src/inno_collector/web/assets/index.html`
- Create: `src/inno_collector/web/assets/app.css`
- Create: `src/inno_collector/web/assets/app.js`
- Create: `tests/test_web_controller.py`
- Create: `tests/test_web_assets.py`
- Modify: `pyproject.toml`

### 3.1 写失败测试

API：

- `GET /api/bootstrap` 返回版本、登录布尔值、最近任务摘要和能力列表；
- `GET /api/library/summary` 复用 `lint_vault`，不存在时返回空库；
- 响应不包含绝对路径、Cookie、Token；
- 静态资源只从固定包资源目录读取，拒绝 `..`、符号链接和任意 MIME。

页面契约：

- 有“首页、登录与预检、采集、资料库、交付、稿件收件箱、关于与许可证”；
- 初次载入只请求同源 `/api/bootstrap`；
- 没有 CDN、外链脚本或远程字体；
- 所有写按钮初始禁用，直到 bootstrap 完成；
- 无 JavaScript 时显示明确说明。

### 3.2 最小实现

前端使用语义 HTML、CSS 和 ES Modules，不引入前端框架。所有状态由一个小型 store 管理，渲染函数保持纯函数以便静态契约测试。

在 `pyproject.toml` 声明 `web/assets/*` 包数据，确保开发安装和 PyInstaller 使用同一份资源。

### 3.3 人工预览

用临时 Application Support 根目录启动：

```bash
INNO_COLLECTOR_SUPPORT_ROOT=/tmp/inno-web-preview \
  .venv/bin/python -m inno_collector.web.server --host 127.0.0.1 --port 0
```

只验证页面布局和只读 API；此阶段不连接真实登录资料。

### 3.4 提交

```bash
git add src/inno_collector/web pyproject.toml \
  tests/test_web_controller.py tests/test_web_assets.py
git commit -m "feat: add local Collector Web shell"
```

---

## Task 4：页面内二维码登录和逐项目预检

**目的：** 消除第二个 Moore 网页后台和 App/浏览器往返。

**Files**

- Modify: `src/inno_collector/web/controller.py`
- Modify: `src/inno_collector/web/moore_runtime.py`
- Modify: `src/inno_collector/web/assets/app.js`
- Modify: `src/inno_collector/web/assets/index.html`
- Create: `tests/test_web_login_flow.py`
- Create: `tests/test_web_preflight.py`

### 4.1 登录 RED 测试

覆盖完整状态机：

```text
idle → waiting_for_scan → scanned_waiting_confirm → confirmed → complete
                                               ↘ expired / failed
```

验证：

- QR 端点只接受当前 login id；
- 二维码响应 `no-store` 且有正确 MIME；
- 页面最多每两秒轮询一次；
- complete 后自动清理 QR 临时文件；
- 浏览器响应中不存在 auth-key；
- 过期、未绑定邮箱、用户取消和网络失败都有可理解中文提示。

### 4.2 预检 RED 测试

将当前 dry-run 结果转换为 10 个项目的显式行：

```text
project, account, mapping, login, catalog, date_filter, status, reason
```

禁止只返回一个总失败数字。配置必须从 App 资源中读取并逐字节保持，API 不允许修改映射。

### 4.3 实现与验证

新增 API：

```text
POST /api/login/start
GET  /api/login/{id}/qrcode
GET  /api/login/{id}/status
POST /api/login/{id}/complete
POST /api/preflight
```

运行 fake Moore 全流程测试后，再由用户本人做一次真实扫码验收。自动化不得扫码或读取真实凭据。

### 4.4 提交

```bash
.venv/bin/python -m unittest tests.test_web_login_flow tests.test_web_preflight
git add src/inno_collector/web tests/test_web_login_flow.py \
  tests/test_web_preflight.py
git commit -m "feat: add Web login and project preflight"
```

---

## Task 5：单写任务、采集进度与取消

**目的：** 把长时间采集变成可观察、可取消、可恢复解释的本地任务。

**Files**

- Create: `src/inno_collector/web/jobs.py`
- Create: `tests/test_web_jobs.py`
- Modify: `src/inno_collector/pipeline.py`
- Modify: `src/inno_collector/web/controller.py`
- Modify: `src/inno_collector/web/assets/app.js`
- Modify: `src/inno_collector/web/assets/index.html`

### 5.1 RED 测试

覆盖：

- 同时只能有一个 preflight/collection/delivery 写任务；
- 任务 id 不可枚举且只在当前进程有效；
- 状态为 queued/running/succeeded/partial/failed/cancelled；
- 事件只含项目名、阶段和计数，不含路径或异常原文；
- 取消在项目边界和下载边界生效；
- 一个项目失败不阻断其他项目；
- 进程重启后旧任务 id 返回 gone；
- 完成任务有数量和时间双重清理上限。

### 5.2 领域进度回调

为 `CollectionPipeline.run()` 增加可选的进度回调与取消检查，默认值保持现有 CLI 和 Helper 行为不变。

事件类型固定：

```text
project_started, catalog_synced, articles_selected,
download_progress, project_finished, validation_finished
```

### 5.3 API 与页面

```text
POST /api/collection
GET  /api/jobs/{id}
GET  /api/jobs/{id}/events
POST /api/jobs/{id}/cancel
```

只有最近一次成功预检的配置哈希与当前配置一致时才能采集。

### 5.4 提交

```bash
.venv/bin/python -m unittest tests.test_web_jobs tests.test_end_to_end
git add src/inno_collector/pipeline.py src/inno_collector/web \
  tests/test_web_jobs.py
git commit -m "feat: add observable Web collection jobs"
```

---

## Task 6：交付、下载与稿件收件箱

**目的：** 让浏览器覆盖当前 Collector 剩余功能，同时保持 Reader 协议不变。

**Files**

- Modify: `src/inno_collector/web/controller.py`
- Modify: `src/inno_collector/web/jobs.py`
- Modify: `src/inno_collector/web/assets/app.js`
- Modify: `src/inno_collector/web/assets/index.html`
- Create: `tests/test_web_delivery.py`
- Create: `tests/test_web_drafts.py`
- Create: `tests/test_web_end_to_end.py`

### 6.1 交付 RED 测试

- 基线和增量参数与现有 `build_update_package` 一致；
- 下载 id 只能读取本任务登记文件；
- 下载文件名、MIME、长度和 SHA-256 正确；
- 超时或下载完成后清理临时文件；
- 下载目录不进入 Vault 或 ExporterRuntime；
- 生成包可被当前 Reader Helper 预览并应用。

### 6.2 稿件 RED 测试

- multipart 总大小、单文件大小和文件数受限；
- 只接受 `.inno-drafts`；
- 上传先写安全临时目录；
- preview 不修改 Vault；
- accept 必须带 preview receipt 与显式确认；
- 重复包和冲突版本保持现有语义。

### 6.3 端到端往返

用冻结前 Python 入口完成：

```text
Collector Web 生成基线 → Reader 预览/应用 → Reader 导出稿件
→ Collector Web 预览/确认 → Reader 人工区保持不被更新覆盖
```

### 6.4 提交

```bash
.venv/bin/python -m unittest \
  tests.test_web_delivery tests.test_web_drafts tests.test_web_end_to_end
git add src/inno_collector/web tests/test_web_delivery.py \
  tests/test_web_drafts.py tests/test_web_end_to_end.py
git commit -m "feat: add Web delivery and draft workflows"
```

---

## Task 7：冻结统一 Web Server 二进制

**目的：** 证明最终用户不需要 Python、上游仓库或开发环境。

**Files**

- Create: `packaging/collector_web_server_entry.py`
- Modify: `scripts/build_helpers.py`
- Modify: `tests/test_build_helpers.py`
- Modify: `scripts/check_repository_policy.py`
- Modify: `tests/test_repository_policy.py`

### 7.1 RED 构建测试

`pyinstaller_commands()` 新增：

```text
role: collector-web
name: InnoCollectorWebServer
entry: packaging/collector_web_server_entry.py
paths: Moore scripts directory
data: src/inno_collector/web/assets
```

测试要求：

- 仍校验上游 `wechat_exporter.py` 与 `wechat_downloader.py`；
- 冻结产物支持 `--smoke`，输出 role/protocol，不启动浏览器；
- `strings` 审计不允许 Reader 出现 web server、Moore、auth-key 和项目配置；
- Web Server 不包含构建机 `/Users`、`/Volumes` 或秘密；
- 静态资源和三方许可证存在。

### 7.2 实际冻结验证

```bash
rm -rf /tmp/inno-web-helper-build
.venv/bin/python scripts/build_helpers.py \
  --output /tmp/inno-web-helper-build --clean
/tmp/inno-web-helper-build/collector-web/InnoCollectorWebServer --smoke
```

再用临时 HOME 启动冻结服务并跑 `tests/test_web_end_to_end.py` 的 frozen 模式。

### 7.3 提交

```bash
git add packaging/collector_web_server_entry.py scripts/build_helpers.py \
  scripts/check_repository_policy.py tests/test_build_helpers.py \
  tests/test_repository_policy.py
git commit -m "build: freeze unified Collector Web server"
```

---

## Task 8：加入薄启动器预览模式，不切换默认产品

**目的：** 在保留 r3 默认界面的情况下，验证动态端口、ready 握手、浏览器和退出生命周期。

**Files**

- Create: `macos/Sources/InnoCollectorFeature/LocalWebLauncher.swift`
- Create: `macos/Tests/InnoCollectorAppTests/LocalWebLauncherTests.swift`
- Modify: `macos/Sources/InnoAppCore/FileLocations.swift`
- Modify: `macos/Tests/InnoAppCoreTests/FileLocationsTests.swift`
- Modify: `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`
- Modify: `scripts/build_macos_apps.py`
- Modify: `tests/test_build_macos_apps.py`

### 8.1 Swift RED 测试

覆盖：

- Web Server 必须直属 `Contents/PlugIns`、非 symlink、可执行；
- 参数固定为 support root、projects 配置、`127.0.0.1` 和 port 0；
- stdout 只读取一行且有字节/时间上限；
- ready 必须协议匹配、PID 等于子进程、host 为 loopback、port 合法；
- 不接受 URL、token 或任意浏览器目标；
- 浏览器 URL 由启动器自己从 host/port 构造；
- 重复 open 复用服务；
- stop 只停止自己启动的进程；
- 取消、退出、畸形 ready 和 server 提前退出稳定清理。

### 8.2 预览开关

暂时仅当：

```text
INNO_COLLECTOR_WEB_PREVIEW=1
```

时使用 `LocalWebLauncher`；默认继续显示 r3 SwiftUI。预览开关不写入 UserDefaults，也不出现在正式 UI。

### 8.3 打包预览

Collector 暂时包含三个旧部件和一个新部件：

```text
InnoCollectorHelper
MooreExporterHelper
InnoCollectorWebServer
Swift preview launcher
```

Reader 仍只含 `InnoReaderHelper`。

### 8.4 真实预览验收

从新 App 的环境变量模式启动，依次验证：

- 浏览器单一产品界面；
- 动态回环端口；
- 页面内真实扫码；
- 10 项目预检详情；
- 一次真实增量采集；
- Obsidian/Vault；
- 更新包被当前 Reader 导入；
- 稿件往返；
- 退出后端口消失。

### 8.5 提交

```bash
./scripts/test_swift.sh --filter LocalWebLauncherTests
git add macos scripts/build_macos_apps.py tests/test_build_macos_apps.py
git commit -m "feat: add Collector Web launcher preview"
```

---

## Task 9：切换门槛审核

**此任务只审核，不删代码。**

必须同时具备以下证据：

- Python、Swift、策略检查全部通过；
- 冻结 Web Server 在干净临时 HOME 运行；
- 用户本人完成真实扫码；
- 10 项目预检逐项可解释；
- 一次真实采集完成，失败隔离与 Vault 校验通过；
- Reader 基线/增量导入和稿件往返通过；
- 退出后无监听和孤儿进程；
- App/DMG 无凭据、文章、本机路径和用户原始项目清单；
- r3 DMG 和当前 main commit 仍可回退；
- 用户明确批准切换默认 Collector。

任何一项缺失，停止在预览模式，不执行 Task 10。

记录审核：

```text
docs/compliance/YYYY-MM-DD-local-web-collector-cutover-review.md
```

提交：

```bash
git add docs/compliance/*local-web-collector-cutover-review.md
git commit -m "docs: review local Web Collector cutover"
```

---

## Task 10：切换默认 Collector 并删除旧实现

**前置：Task 9 全部通过并获得用户切换批准。**

### 10.1 先写失败的最终布局测试

最终 Collector 只允许：

```text
Contents/MacOS/InnoCollectorApp
Contents/PlugIns/InnoCollectorWebServer
Contents/Resources/config/projects.json
Contents/Resources/ThirdPartyLicenses/*
```

明确拒绝：

```text
InnoCollectorHelper
MooreExporterHelper
CollectorContentView
CollectorViewModel
MooreLocalLoginServer
```

Reader 布局完全不变。

### 10.2 切换启动器

`InnoCollectorApp` 默认启动 `LocalWebLauncher`，移除预览环境变量分支。启动器只显示不可用错误窗口和菜单栏生命周期，不保留采集功能页。

### 10.3 删除旧代码

删除旧 SwiftUI Collector Feature 文件、测试、旧 Collector Helper 入口和 Moore 独立可执行产物。保留仍被 Web Server 使用的 `inno_collector` 领域模块。

更新：

- `macos/Package.swift`
- `scripts/build_helpers.py`
- `scripts/build_macos_apps.py`
- `scripts/release_macos.py`
- `README.md`
- `docs/macos-release-checklist.md`
- 法律声明与构建审计测试。

### 10.4 全量和真实 App 验证

```bash
rm -rf /tmp/inno-web-cutover-apps
.venv/bin/python scripts/build_macos_apps.py \
  --configuration release --output /tmp/inno-web-cutover-apps
codesign --verify --deep --strict \
  /tmp/inno-web-cutover-apps/InnoCollector.app
codesign --verify --deep --strict \
  /tmp/inno-web-cutover-apps/InnoReader.app
```

反向扫描 Reader 和 Collector，验证角色隔离、动态端口、无本机路径和无秘密。

### 10.5 提交

```bash
git add -A
git commit -m "refactor: switch Collector to local Web interface"
```

---

## Task 11：试用 DMG、PR 与回退说明

### 11.1 生成新的自用试用包

名称使用新的架构标识，避免与 r3 混淆：

```text
InnoCollector-Web-0.2.0-pilot-YYYYMMDD.dmg
```

DMG 包含 App、`/Applications` 快捷方式和中文说明。说明明确：

- 本地浏览器界面不等于云服务；
- 关闭浏览器不一定停止服务，应从 App 菜单退出；
- 凭据和数据仍只在本机；
- r3 是回退包；
- ad-hoc、未公证、不得转发。

### 11.2 反向挂载审计

验证：

- 顶层条目准确；
- App 深度签名结构；
- 仅一个 Collector Web Server Helper；
- 动态回环端口；
- 三方许可证；
- 无登录会话、文章、运行目录、本机路径和秘密；
- Reader 不在 Collector DMG 中；
- SHA-256 正确。

### 11.3 PR 与 CI

推送 feature branch，PR 必须包含：

- 架构与迁移摘要；
- Task 9 切换审核链接；
- 自动化数量；
- 真实扫码/采集/Reader 往返证据；
- r3 回退路径；
- 明确“本地 pilot DMG 不上传”。

等待 PR CI 与 main push CI 全部成功后 squash 合并。仍不创建 tag 或 GitHub Release，正式发布继续受 Developer ID、公证和干净账户门槛约束。

---

## 实施顺序摘要

```text
1 Moore 直接适配
→ 2 安全 HTTP 骨架
→ 3 只读 Web 页面
→ 4 页面内登录/预检
→ 5 采集任务
→ 6 交付/稿件
→ 7 冻结二进制
→ 8 启动器预览
→ 9 切换审核与用户批准
→ 10 删除旧 Collector
→ 11 新 pilot DMG 与合并
```

每一步都可停、可回退。Task 9 以前，当前 r3 始终保留并继续可用。
