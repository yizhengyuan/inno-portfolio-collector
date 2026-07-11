# 英诺公众号资讯 macOS 工具

这是一个面向两类使用者的本地 macOS 产品：用户本人用“英诺资讯采集”维护 10 个既定项目的公众号资料，朋友用“英诺资讯阅读”离线阅读、搜索和编辑。安装后的 App 不依赖 Python 或 Codex。

## 两个 App

### 英诺资讯采集

只安装在负责采集的 Mac 上。它保管本机公众号登录状态，执行预检、增量采集、资料校验、离线看板生成和更新包导出，并接收朋友回传的编辑稿包。

采集必须先通过最近一次预检。项目配置来自原始 `config/projects.json`，打包时逐字节复制，不会重新生成或修改。公众号登录凭据、Cookie 和 Token 不进入更新包，也不得发送给朋友。

### 英诺资讯阅读

安装在朋友的 Mac 上。它只能导入 `.inno-update` 更新包，不能调用采集命令，也不包含 Moore 导出侧车或项目配置。朋友可以：

- 按标题、项目和公众号搜索文章；
- 打开完全本地的离线看板；
- 新建笔记、摘要、选题或编辑稿；
- 导出 `.inno-drafts` 编辑稿包回传；
- 继续使用推荐安装的 Obsidian 打开同一个本地 Vault。

原文和附件位于只读内容区；更新不会覆盖 `10-编辑稿` 与个人笔记等人工工作区。

## 两人协作流程

1. 采集者在“英诺资讯采集”运行预检和采集，首次生成基线 `.inno-update`。
2. 朋友在“英诺资讯阅读”预览差异并明确确认导入。
3. 后续采集只生成增量更新包；朋友的稿件在导入前后保持不变。
4. 朋友导出 `.inno-drafts`，采集者在稿件收件箱接收；冲突版本并列保留。

阅读、搜索、编辑和离线 HTML 看板均可断网使用。朋友无需获得任何公众号登录凭据。

## 安装与发布状态

正式给朋友分发时，应分别提供经过 Developer ID 签名、公证和 Gatekeeper 验证的 Collector/Reader DMG。当前仓库可以生成 ad-hoc 签名 App 用于本机 QA；在 `docs/macos-release-checklist.md` 全部通过前，不应把它标记为正式可分发版本。

开发构建：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[build]'
./.venv/bin/python scripts/build_macos_apps.py \
  --configuration release --output .build-macos/apps
./scripts/test_swift.sh
```

正式签名工具要求 `MACOS_SIGNING_IDENTITY`；启用公证时还要求 `APPLE_ID`、`APPLE_TEAM_ID` 和 `APPLE_APP_PASSWORD`。凭据只从环境读取，不写入发布清单。

## 开源署名与内容版权

产品适配 [wechat-article-exporter](https://github.com/wechat-article/wechat-article-exporter) 和 [moore-wechat-article-downloader](https://github.com/Moore-developers/moore-wechat-article-downloader)，两者均为 MIT。完整许可证随两个 App 分发，详见 `THIRD_PARTY_NOTICES.md` 与 `third_party/licenses/`。

文章版权、图片及附件版权归原作者或其他权利人所有。使用者应仅在获得授权或法律允许的范围内采集、保存和分享内容。

本项目的自有代码以 [MIT License](LICENSE) 开源。进度、缺陷和功能建议在 [GitHub Issues](https://github.com/yizhengyuan/inno-portfolio-collector/issues) 中统一管理；贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

最新的仓库、双 App、隐私与许可证技术复核见 [2026-07-12 开源与分发技术合规复核](docs/compliance/2026-07-12-open-source-and-distribution-review.md)。该复核不替代内容授权、Apple 正式签名、公证和干净账户安装验收。

## 兼容命令行测试

底层 Python 模块仍保留 `unittest`、内容包和 Helper 协议入口，供开发、自动化验收与故障排查使用；它们不是朋友日常使用的前置条件。
