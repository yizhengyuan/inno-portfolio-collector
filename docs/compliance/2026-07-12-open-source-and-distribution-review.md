# 2026-07-12 开源与分发技术合规复核

> 历史记录：本报告验证的是 0.1.0 双 Helper 架构。2026-07-13 获批的默认 Web Collector 已删除旧 Collector Helper 和独立 Moore Helper；当前架构、真实采集和切换证据以 [Local Web Collector 切换门槛审核](2026-07-12-local-web-collector-cutover-review.md) 为准。下文保留当时事实，不代表当前包布局。

## 结论

本次复核从提交 `31bf0baeaa9683cefdfc91a81c82c6d01c9c4939` 开始，并在本报告所在提交完成。仓库源码与重新构建的 Collector/Reader App 通过本报告列出的隐私、开源许可证、角色隔离和内容边界检查；相关 CI 通过并合并后，可以关闭 GitHub Issue #3。

这是技术合规复核，不是法律意见，也不代表任何公众号运营主体或文章权利人已经授权采集、保存或分享内容。Developer ID 签名与 Apple 公证、干净 macOS 账户验收及正式 DMG 发布仍分别由 Issues #1、#2 和 #4 跟踪；完成这些事项前，版本仍不能标记为“可给朋友正式分发”。

## 复核范围

- 当前 Git 跟踪树及全部本地 Git 历史；
- 项目自身 MIT License、两份上游 MIT License 与第三方署名；
- 使用 Python 3.11、PyInstaller 6.21 和 Swift release 配置重新构建的两个 App；
- Collector/Reader Helper 协议、包内资源、权限和角色隔离；
- README、Notice、贡献指南、安全政策与 App 内用户提示；
- 高置信凭据、本机绝对路径、真实文章库和用户原始项目清单。

临时构建目录和 ad-hoc App 未提交到 Git。正式发布必须重新构建、正式签名、公证，并生成新的发布清单和哈希。

## 证据

| 检查项 | 结果 | 证据摘要 |
|---|---|---|
| 项目许可证 | 通过 | 根 `LICENSE` 为 MIT License，Copyright (c) 2026 yizhengyuan。 |
| wechat-article-exporter 许可证 | 通过 | GitHub 当前默认分支许可证与本地文件 SHA-256 均为 `485a586ef411226e84cb978f8921ff1f743b10b4f1807f78b194cb991246e072`。 |
| Moore 下载器许可证 | 通过 | GitHub 当前默认分支许可证与本地文件 SHA-256 均为 `ccb1f5de267e74faef97c828acee0cb48980d9971db4a73ef54894d3d379f5b9`。 |
| 双 App 许可证与署名 | 通过 | 两个 App 都逐字节包含项目自身 MIT License、两份上游 MIT License 和 `THIRD_PARTY_NOTICES.md`，并可从侧栏“关于与许可证”直接查看全文。 |
| 当前仓库敏感信息 | 通过 | `scripts/check_repository_policy.py` 检查全部当前 Git 跟踪文件通过。 |
| Git 历史敏感信息 | 通过 | 全历史未命中 GitHub PAT、AWS Access Key 或私钥头等高置信凭据。 |
| 用户原始项目清单 | 通过 | 当前树和全历史均没有 `.superpowers/` 或 `英诺项目清单-2026/`；两个 App 也不包含该目录。 |
| 产品项目配置 | 已确认 | Collector 按设计包含已审阅的 `config/projects.json`；这是公开产品配置，不是用户原始清单。Reader 不包含该文件。 |
| 真实文章与附件 | 通过 | 仓库和 App 均没有 Vault、更新包、稿件包、DMG、真实文章目录或附件目录；`tests/fixtures/` 仅包含带 `example.com`/`fixture-secret` 的合成测试数据。 |
| Reader 采集隔离 | 通过 | Reader 只含 `InnoReaderHelper`，不含 Collector/Moore Helper 或项目配置；Reader Helper 对 `collect` 返回 `ok=false` 且不返回结果。 |
| Collector 采集能力 | 已确认 | Collector 包含 Collector Helper、Moore Helper 和项目配置；公众号凭据保留在采集者本机运行目录，不进入 App 或更新包。 |
| 网络权限 | 通过 | Collector 仅声明 `com.apple.security.network.client=true`；Reader entitlements 为空。 |
| 外部 AI、遥测与分析 | 通过 | 源码和构建配置未包含 DeepSeek/OpenAI、Sentry、Mixpanel、Segment 或其他遥测集成；Python 无运行时第三方依赖，Swift 无外部 package 依赖。 |
| 文档与界面提示 | 通过 | README、Notice、贡献指南和安全政策说明凭据与角色边界；编辑业务页说明不会修改采集原文；两个 App 的“关于与许可证”页面说明文章版权归属和授权责任。 |
| 包内凭据文件 | 通过 | 两个 App 未发现 `.env`、私钥、证书容器、Cookie 数据库、更新包或稿件包。 |
| 包内高置信密钥 | 通过 | 两个 App 的二进制与资源均未命中 GitHub PAT、AWS Access Key 或私钥头。 |
| 本机构建路径 | 修复后通过 | 初次 release 构建在 Swift 二进制中发现本机源路径；`31bf0ba` 增加 `strip -S -x` 以及签名前 fail-closed 路径扫描。重新构建后 `/Users/` 与 `/Volumes/` 命中均为零。 |
| ad-hoc 签名完整性 | 通过 | 重新构建的两个 App 均通过 `codesign --verify --deep --strict`。这不替代 Developer ID、公证和 Gatekeeper 验收。 |

上游来源：

- https://github.com/wechat-article/wechat-article-exporter
- https://github.com/Moore-developers/moore-wechat-article-downloader

## 自动化与复现

仓库层检查：

```bash
python scripts/check_repository_policy.py
python -m unittest discover -s tests
./scripts/test_swift.sh
```

双 App 技术审计构建：

以下命令要求同级目录已经准备 `moore-wechat-article-downloader/scripts`，与当前构建脚本的输入约定一致。

```bash
python -m pip install -e '.[build]'
python scripts/build_macos_apps.py \
  --configuration release \
  --output "$TMPDIR/inno-compliance/apps"
codesign --verify --deep --strict "$TMPDIR/inno-compliance/apps/InnoCollector.app"
codesign --verify --deep --strict "$TMPDIR/inno-compliance/apps/InnoReader.app"
```

构建器现在会在 Swift 可执行文件签名前剥离本地符号，并扫描 App 内全部常规文件；任何 `/Users/<name>/` 或 `/Volumes/<name>/` 路径都会使构建失败，错误不会回显具体路径。

## 分发与使用边界

1. 只有用户本人或明确承担采集责任的人使用 Collector，并自行扫码登录；不得把 Cookie、Token、Apple 凭据或 Collector 运行目录发送给朋友。
2. 朋友只安装 Reader，通过 `.inno-update` 阅读和通过 `.inno-drafts` 回传编辑稿；Reader 不具备采集能力。
3. MIT License 只覆盖软件代码，不授予公众号文章、图片、附件、商标或个人信息的权利。
4. 使用者必须自行确认平台规则、账号权限和内容权利基础。公司公众号运营人员能够操作账号，并不当然等于能够代表所有内容权利人授权采集或再分发。
5. 任何未来 AI 问答能力在上传文章前，必须单独设计密钥保管、内容出境提示、用户确认和离线退化方案；本版本没有该能力。

## 未完成门槛

- Issue #1：Developer ID 正式签名、Apple 公证、staple 与 Gatekeeper 验证；
- Issue #2：无 Python、无 Codex 的干净 macOS 账户双 App 全流程验收；
- Issue #4：生成正式 Collector/Reader DMG、发布清单、校验和及 GitHub Release。

只有以上门槛全部通过，才能发布 `v0.1.0` 朋友试用安装包。
