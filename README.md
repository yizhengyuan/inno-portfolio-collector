# 英诺公众号资讯 macOS 工具

这是一个单客户场景下的本地 macOS 产品：采集者在自己的 Mac 安装“英诺资讯采集”，维护 10 个既定项目的公众号资料，再生成一个客户资料包 ZIP。客户无需安装英诺专用 App，只需解压后用推荐安装的 Obsidian，或直接打开离线 HTML 看板进行阅读、搜索和编辑。安装后的 Collector 不依赖 Python 或 Codex。

## 当前模式：一个 Collector，一份客户资料包

### 英诺资讯采集

只安装在负责采集的 Mac 上。App 是一个很薄的 macOS 启动器：双击后，它会在随机的 `127.0.0.1` 本机端口启动 Collector，并在默认浏览器打开本地 Web 界面。这不是云服务，浏览器不会连接远程 Collector；退出“英诺资讯采集”App 才会停止本地服务。

本地 Web 界面保管本机公众号登录状态，执行扫码登录、预检、增量采集、资料校验、离线看板生成和客户资料包 ZIP 导出，并接收客户回传的编辑稿包。请在使用期间保持 App 开启；只关闭浏览器标签页不会结束 App。

采集必须先通过最近一次预检。项目配置来自原始 `config/projects.json`，打包时逐字节复制，不会重新生成或修改。公众号登录凭据、Cookie 和 Token 不进入客户资料包，也不得发送给客户。

### 客户如何使用

Collector 生成的客户资料包 ZIP 直接包含完整 Obsidian Vault、离线 HTML 看板和一页使用说明。客户解压后可以：

- 按标题、项目和公众号搜索文章；
- 打开完全本地的离线看板；
- 新建笔记、摘要、选题或编辑稿；
- 用推荐安装的 Obsidian 打开整个本地 Vault；
- 把 `10-编辑稿` 中的内容回传给采集者。

原文和附件位于内容区；人工内容应写入 `10-编辑稿` 与 `11-个人笔记`。阅读、搜索、编辑和离线 HTML 看板均可断网使用，客户不需要任何公众号登录凭据。

## 单客户协作流程

1. 采集者打开“英诺资讯采集”，完成扫码登录、预检和采集。
2. 在“交付”板块点击“生成客户资料包 ZIP”并下载。
3. 把 ZIP 发送给唯一客户；不要发送登录状态或 Collector App。
4. 客户解压后，用 Obsidian 打开资料库，或双击 `80-离线看板/index.html`。
5. 后续更新时重新生成完整资料包。客户自己的稿件应先单独备份或回传，避免手工覆盖。

## 未来多人模式

仓库仍保留“英诺资讯阅读”、`.inno-update` 增量包和 `.inno-drafts` 稿件包的源码与自动化测试，供未来多客户、频繁增量同步时启用。它们不是当前单客户交付的安装前提，也不会从现有源码中删除。

## 安装与发布状态

当前只有采集者需要安装 Collector DMG；客户拿到的是普通 ZIP，不需要安装第二个英诺 App。正式分发 Collector 时仍应完成 Developer ID 签名、公证和 Gatekeeper 验证。当前仓库可以生成 ad-hoc 签名 App 用于本机 QA；在 `docs/macos-release-checklist.md` 全部通过前，不应把它标记为正式可分发版本。Collector App 只携带一个 `InnoCollectorWebServer` 本地组件。

开发构建：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[build]'
# 另将 moore-wechat-article-downloader clone 到本仓库的同级目录
./.venv/bin/python scripts/build_macos_apps.py \
  --configuration release --output .build-macos/apps
./scripts/test_swift.sh
```

正式签名工具要求 `MACOS_SIGNING_IDENTITY`；启用公证时还要求 `APPLE_ID`、`APPLE_TEAM_ID` 和 `APPLE_APP_PASSWORD`。凭据只从环境读取，不写入发布清单。

## 开源署名与内容版权

产品适配 [wechat-article-exporter](https://github.com/wechat-article/wechat-article-exporter) 和 [moore-wechat-article-downloader](https://github.com/Moore-developers/moore-wechat-article-downloader)，两者均为 MIT。完整许可证随 Collector 分发，详见 `THIRD_PARTY_NOTICES.md` 与 `third_party/licenses/`。

文章版权、图片及附件版权归原作者或其他权利人所有。使用者应仅在获得授权或法律允许的范围内采集、保存和分享内容。

本项目的自有代码以 [MIT License](LICENSE) 开源。进度、缺陷和功能建议在 [GitHub Issues](https://github.com/yizhengyuan/inno-portfolio-collector/issues) 中统一管理；贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

最新的 Web Collector 切换证据见 [Local Web Collector 切换门槛审核](docs/compliance/2026-07-12-local-web-collector-cutover-review.md)。此前的 [2026-07-12 开源与分发技术合规复核](docs/compliance/2026-07-12-open-source-and-distribution-review.md) 保留为旧双 Helper 架构的历史记录。技术复核不替代内容授权、Apple 正式签名、公证和干净账户安装验收。

## 兼容命令行测试

底层 Python 模块仍保留 `unittest`、内容包命令和 Reader Helper 协议，供开发、自动化验收与故障排查使用。旧 Collector Helper 与独立 Moore Helper 已从产品和构建入口删除；它们都不是日常使用的前置条件。
