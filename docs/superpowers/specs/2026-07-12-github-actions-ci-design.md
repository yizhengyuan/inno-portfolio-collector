# GitHub Actions 持续集成设计

## 背景

`inno-portfolio-collector` 已以 MIT 许可证公开，并用 GitHub Issues 与里程碑管理进度。当前 Python 与 Swift 测试只在本地手动执行，还没有服务端变更门禁。仓库同时包含故意构造的假密钥测试夹具，所以简单搜索 `token=` 会产生大量误报。

## 目标

1. 对每次推送到 `main` 和每个 Pull Request 自动运行完整 Python 与 Swift 测试。
2. 自动阻止高置信密钥、私钥、本地设计目录和用户原始项目清单进入 Git 历史。
3. 自动确认项目 MIT 许可证、两个上游 MIT 许可证及第三方署名文件仍存在。
4. 工作流不读取任何 Apple、GitHub 或公众号密钥，对来自 fork 的 Pull Request 也安全。
5. 失败时给出可定位的作业名和文件路径，便于通过 GitHub Issues 跟踪。

## 非目标

- 不在 CI 中登录公众号、采集真实文章或上传内容包。
- 不在本阶段执行 Developer ID 签名、Apple 公证、DMG 发布或 GitHub Release。
- 不用自动化检查替代干净 macOS 账户上的人工验收。
- 首次上线不强制分支保护或必须经过 Pull Request，避免给单人维护过早增加阻力。

## 方案选择

### 选中：三个并行作业和一个本地策略检查器

一个 `.github/workflows/ci.yml` 并行运行 `repository-policy`、`python-tests` 和 `swift-tests`。仓库策略放在标准库 Python 脚本中，既能在 GitHub Actions 运行，也能在提交前本地运行和单元测试。

这个方案比把所有命令放在一个 macOS 作业中更容易定位失败，也能并行缩短等待时间。它不依赖第三方密钥扫描 Action，避免对含假密钥的测试夹具产生不可控误报。

### 未选：单一 macOS 作业

优点是 YAML 最短，缺点是策略、Python 和 Swift 任一失败都难以从作业概览区分，且不能并行。

### 未选：直接引入通用密钥扫描服务

优点是规则库广，缺点是需要维护较复杂的允许列表，并增加第三方 Action 供应链和版本升级成本。当贡献者规模增长后可再评估。

## 工作流架构

### 触发和权限

- `push` 到 `main`；
- 以 `main` 为目标的 `pull_request`；
- 用于故障重试的 `workflow_dispatch`；
- 顶层设置 `permissions: contents: read`，作业不需要写权限；
- 同一分支的旧运行在新提交到达时取消，但 `main` 的运行不互相取消。

GitHub 当前对公开仓库提供 `macos-15` ARM64 标准 Runner，并建议把 `GITHUB_TOKEN` 限制为只读权限：

- https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- https://docs.github.com/en/actions/reference/security/secure-use
- https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md

### `repository-policy`

在 `ubuntu-24.04` 上运行，因为只需 Git 和 Python 标准库。它调用 `python3 scripts/check_repository_policy.py`，检查：

- 必需文件：`LICENSE`、`THIRD_PARTY_NOTICES.md`、两个上游许可证和 `SECURITY.md`；
- 禁止跟踪的路径：`.superpowers/`、`英诺项目清单-2026/`、运行时凭据目录、`.env*`、私钥和常见密钥容器；
- 高置信内容特征：PEM/OpenSSH 私钥头、GitHub PAT、AWS Access Key 等具有稳定前缀和长度的凭据。

检查器不把 `token=`、`cookie=` 或单词 `secret` 本身视为泄漏，因为这些字符串是脱敏逻辑的必要测试数据。它只输出相对路径和规则名，不回显匹配内容。

### `python-tests`

在 `macos-15` 上安装 Python 3.11 和当前项目，然后运行：

```bash
python -m unittest discover -s tests
```

常规 CI 保留需要冻结 Helper 环境变量的现有长链路测试为 skip。它属于发布验收，不在本阶段重复构建。

### `swift-tests`

在 `macos-15` 上明确选择 `/Applications/Xcode_16.4.app/Contents/Developer`，输出 `xcodebuild -version` 和 `swift --version` 后运行现有入口：

```bash
./scripts/test_swift.sh
```

GitHub 的 `macos-15` 当前把 Xcode 16.4 作为默认版本，且保留稳定路径。明确选择可防止 Runner 默认工具链未来切换到 Xcode 26 时意外改变 Swift 编译语义。

实现时同步修正 `scripts/test_swift.sh`：只有 `xcode-select -p` 确实指向 `/Library/Developer/CommandLineTools` 时，才注入该目录下的 `Testing.framework` 特殊路径；使用完整 Xcode 时直接运行 SwiftPM，避免混用两套工具链。依赖冻结 Helper 的 3 个现有角色隔离测试仍保留为 skip，完整隔离验收由发布流程承担。

## 本地策略检查器

`scripts/check_repository_policy.py` 拆分为纯函数与窄命令行入口：

- 纯函数接收已跟踪路径和读取文件的回调，返回排序后的策略违规；
- CLI 通过 `git ls-files -z` 获取真实跟踪集合，不扫描未跟踪的用户本地文件；
- 二进制或超过大小上限的文件不进行内容解码，但仍执行路径规则；
- 任何违规都以非零状态退出，成功时给出简短摘要。

## 错误处理

- 策略失败：按 `relative/path: rule-name` 输出，不回显可能的凭据。
- Git 命令或文件读取失败：明确标记为检查器自身失败，不当作“无违规”。
- Python 或 Swift 测试失败：保留原生测试输出和非零退出码，不做自动重试，避免隐藏非稳定测试。
- 网络或 Runner 调度失败：由 GitHub Actions 标记基础设施失败，不改变项目测试命令。

## 测试设计

1. 先为策略检查器写失败测试，覆盖必需文件、禁止路径、高置信凭据、假密钥不误报、稳定排序和无泄漏错误输出。
2. 为 `scripts/test_swift.sh` 增加可注入的 Developer Directory 分支测试，证明只有 Command Line Tools 模式注入特殊 framework 路径。
3. 实现到新单元测试通过，再对当前仓库运行策略脚本。
4. 本地完整运行 Python 和 Swift 测试。
5. 推送功能分支，确认 GitHub Actions 三个作业在远端实际通过；本地 YAML 解析成功不代替这一验证。
6. 经过代码评审后合并到 `main`，然后关闭对应 GitHub Issue。

## 交付标准

- 本地 Python 和 Swift 全套测试通过；
- 本地仓库策略检查通过；
- GitHub Pull Request 上 `repository-policy`、`python-tests` 和 `swift-tests` 都达到成功终态；
- Workflow 的有效权限只有 `contents: read`；
- 任何 Workflow 与日志中均不含真实凭据、原始项目清单或文章内容。
