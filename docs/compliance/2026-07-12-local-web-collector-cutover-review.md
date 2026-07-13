# Local Web Collector 切换门槛审核

日期：2026-07-12  
功能分支：`feat/local-web-collector`  
审核状态：**技术门槛通过；2026-07-13 已获用户批准，默认 Web Collector 与自用 pilot DMG 已完成本地切换验收**

## 审核范围

本审核覆盖本地 Web Collector 的冻结 App、真实本机登录、10 个项目预检、真实采集、Vault 校验、基线交付、Reader 导入、稿件往返和退出清理。Task 9 审核期间未修改原始项目清单、未输出登录凭据，也未提前切换默认 Collector；用户于 2026-07-13 明确批准后，Task 10/11 才执行默认入口切换、旧实现删除和新 pilot DMG 制作。

## 回退与配置保护

- 当前 `main` 回退点保持为 `58f9c23`。
- 旧 r3 自用 DMG 仍保留在 `dist/自用试用-r3/`，SHA-256 为 `bc5e74ba938bfafa74ae45dca600caca9f24b166ac2947f119d18d98abbb361f`。
- 原始配置、功能分支配置和冻结 App 内配置三者 SHA-256 均为 `8be2a6a98481dd1155071387cd39b40672799c1fbdff0534f1febe3eb84ae691`。
- Task 9 审核阶段未合并功能分支、未删除旧 Collector；获批后的 Task 10 已删除旧实现。全过程未改写用户的配置文件。

## Task 9 自动化与构建证据

- Python：440 项通过，默认运行有 2 项环境门控跳过。
- Swift：72 项通过，默认运行有 3 项真实 Helper 环境门控跳过。
- 冻结 Web Server 冷启动、HTTP 资源和退出测试：1 项通过，实测冷启动 24.4 秒。
- 冻结 Collector/Reader 完整往返：3 项通过，包含 MIT 许可证、人工稿件保留和角色隔离。
- 3 项 Swift 真实 Helper 测试随后按实际使用方式串行运行，分别 12、7、1 项通过。
- 仓库策略检查通过；Collector 与 Reader 深度签名检查通过。
- Collector Web Server SHA-256：`26de3504ebf56b01b07088d979105d2a53240a4ff9f43260030d4f85674f4fbc6`。
- 构建审计确认冻结包包含 Web 资源、两个上游 MIT 许可证和署名，并拒绝本机绝对路径与高置信凭据。

## 真实 App 验收

- 从 macOS App 启动后，本地服务使用动态 `127.0.0.1` 端口；浏览器页面正常打开。
- App 首次冷启动约 26 秒，超过旧 20 秒上限但在新的 90 秒有界上限内完成。
- 第一次预检即为 10/10：登录有效、10 个精确映射全部匹配、0 个预检失败。
- 真实采集完成 10 个项目、227 篇文章、0 个重复；2 个项目完全成功，8 个项目部分成功。
- 43 次单篇下载失败被隔离，没有阻塞其余项目；这是上游正文或附件下载失败，不是映射或登录失败。
- 实际 Vault 含 227 篇文章、641 个附件，共 885 个文件，大小约 339 MB。
- Vault 校验结果：0 断链、0 敏感信息、0 非法文件、0 manifest 错误、0 状态错误。

## 交付与阅读验收

- Web Collector 生成基线包成功：884 个纳入文件、0 个删除、348,252,944 字节。
- 基线包下载文件、HTTP 响应头和任务结果的 SHA-256 三方一致：`ff883aca8370687ea88a894ebe60a2b4ef5e354f10f6f9fae62bbac2b74e2d7f`。
- 冻结版 Reader Helper 成功预览并导入全部 884 个文件；导入后仍为 227 篇、10 个项目、0 校验错误。
- Reader 创建 1 篇人工稿件并生成 `.inno-drafts`；隔离 Web Collector 完成“预览后明确确认收录”。
- 稿件收录后隔离 Vault 仍为 0 断链、0 敏感信息、0 校验错误。

## 生命周期与安全边界

- 强制结束 App 后，父进程、PyInstaller 子进程和动态监听端口均在 1 秒内清理。
- 登录资料和采集能力只保存在 Collector 本机运行目录；交付包不包含凭据。
- Reader 冻结包不含 Collector、Moore、项目配置或采集能力。
- 文章版权仍归原作者；MIT 仅覆盖本项目和两个上游软件代码。

## 已知限制

- 当前候选 App 仍是 ad-hoc 签名、未公证的自用预览，只供本人试用，不得作为正式客户安装包转发。
- Task 9 切换前，将 3 个独立 PyInstaller Helper 同时冷启动的非产品测试曾触发 60 秒资源争抢超时；相同测试串行运行全部通过。该记录只描述历史环境，当前构建已经删除旧 Collector Helper 和独立 Moore Helper。

## 2026-07-13 默认切换与 pilot 复核

- Collector 默认使用唯一 macOS 窗口启动 `InnoCollectorWebServer`，无需预览环境开关，并在默认浏览器打开随机 `127.0.0.1` 端口。
- 旧原生 Collector UI、旧本地扫码服务、旧 Collector Helper、独立 Moore Helper 及其构建入口和测试已删除；Collector `Contents/PlugIns` 精确只含 `InnoCollectorWebServer`。
- 产品版本统一为 0.2.0；Collector/Reader Info.plist、Python 包版本与 Web bootstrap 一致。
- 最终 Python 总套件运行 447 项：446 项通过，默认环境门控跳过冻结二进制测试 1 项；该门控测试随后传入真实 Web Server，另行 1/1 通过。Swift 41 项通过；传入真实 App/Helper 后，Swift 真实角色隔离与 Reader Helper 测试也全部通过。
- 最终真实 App bootstrap 返回 0.2.0、登录有效与六项已批准能力；正常退出和强制结束 App 后，Web Server 进程与动态端口均被清理。
- 最终 App 内 `projects.json` SHA-256 仍为 `8be2a6a98481dd1155071387cd39b40672799c1fbdff0534f1febe3eb84ae691`，与仓库配置逐字节一致。
- 新自用包为 `dist/自用试用-web/InnoCollector-Web-0.2.0-pilot-20260713.dmg`，SHA-256 为 `0465f697f5ec33e00fd417349a034db8949a2f34aae7bf8dfe0786a6da39db75`。
- DMG 通过 `hdiutil verify` 与只读反向挂载审计：顶层精确为中文 App、`Applications` 快捷方式和安装说明；App 深度签名有效；版本 0.2.0；配置与四份许可证/声明一致；无 Reader、运行数据、文章、附件、本机路径或高置信秘密。
- 旧 r3 DMG 保持原位，SHA-256 仍为 `bc5e74ba938bfafa74ae45dca600caca9f24b166ac2947f119d18d98abbb361f`，没有被新包覆盖。
- pilot DMG 是本地忽略文件，没有上传到 GitHub；Developer ID、公证、干净账户与正式客户分发门槛仍未完成。

## 审核结论

Task 9 技术门槛已满足，且用户已明确批准切换。Task 10 的默认入口切换与旧实现删除、Task 11 的本地自用 pilot DMG 及反向审计均已完成；正式客户分发仍受 Developer ID、公证和干净账户验收门槛约束。
