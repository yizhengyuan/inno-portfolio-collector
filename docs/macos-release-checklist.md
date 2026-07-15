# macOS 正式发布验收清单

只有以下项目全部完成并留存记录后，版本才可标记为“可供单客户使用”。不得在记录中附带本地用户名、公众号登录信息或 Apple 凭据。

## 发布信息

- App 版本：__________
- Build：__________
- 测试 macOS 版本：__________
- 测试人缩写：__________
- Collector DMG SHA-256：__________
- 客户资料包 ZIP SHA-256：__________
- 验收日期：__________

## 干净账户与 Gatekeeper

- [ ] 使用全新的非开发者 macOS 13 或更高版本账户，未安装 Python 和 Codex。
- [ ] 从公证后的 Collector DMG 安装“英诺资讯采集”，首次打开通过 Gatekeeper。
- [ ] Collector DMG 的本地 SHA-256 与发布记录一致。
- [ ] 客户测试账户不安装英诺专用 App，也不获得 Collector DMG 或公众号凭据。

## 采集者流程

- [ ] 双击 Collector 后，默认浏览器打开随机 `127.0.0.1` 端口；页面明确这是本地 Web 界面而非云服务。
- [ ] Collector 明确显示本机负责登录与采集，客户资料包不接收任何公众号登录凭据。
- [ ] 正常退出和强制结束 Collector 后，`InnoCollectorWebServer` 及其监听端口都被清理；仅关闭浏览器标签页不会被误写为“已退出 App”。
- [ ] 完成一次登录状态与 10 个精确公众号映射预检。
- [ ] 只对受控样本执行一次采集，预检未成功时“开始采集”保持禁用。
- [ ] 点击“生成客户资料包 ZIP”，确认任务显示文章数、下载文件名和 SHA-256。
- [ ] 确认 ZIP 内无 Cookie、Token、auth-key、绝对用户路径或 `.moore` 数据。

## 客户离线阅读

- [ ] 在第二个干净账户解压客户资料包；系统中没有 Python 和 Codex。
- [ ] ZIP 只有一个“英诺被投项目资讯库”顶层目录，并包含 `客户使用说明.md`。
- [ ] 断开网络后，按标题、项目和公众号搜索文章。
- [ ] 断开网络后打开本地文章与 `80-离线看板/index.html`，确认没有远程依赖。
- [ ] 安装 Obsidian 后能把解压目录作为 Vault 打开；未安装时仍能阅读离线 HTML。

## 编辑与回传

- [ ] 客户在 `10-编辑稿` 创建一份编辑稿并记录稿件字节哈希。
- [ ] 客户单独备份或回传编辑稿，再接收下一份完整客户资料包。
- [ ] Collector 稿件收件箱能预览并确认接收既有 `.inno-drafts` 回传包。
- [ ] 冲突稿件并列保留，未静默覆盖；原文和附件未被编辑功能修改。

## 签名、隔离与许可证

- [ ] `codesign --verify --deep --strict --verbose=2` 对 Collector App 通过。
- [ ] `spctl --assess --type execute --verbose=2` 对 Collector App 通过。
- [ ] `xcrun stapler validate` 对公证后的 Collector DMG 通过。
- [ ] Collector 的 `Contents/PlugIns` 精确只包含 `InnoCollectorWebServer`，没有旧 Collector Helper 或独立 Moore Helper。
- [ ] Collector Web Server 的 `--smoke` 与动态 ready 握手通过。
- [ ] Collector 包含 wechat-article-exporter 与 Moore 下载器的完整第三方许可证和署名。
- [ ] 客户 ZIP 只包含资料库内容和使用说明，不包含 App、导出脚本、项目配置或凭据文件。
- [ ] 文章版权提示在 README 与第三方说明中可见。

## 结论

- [ ] 所有项目通过，批准本版本用于一个采集者和一个客户。
- 未通过项与处理记录：__________________________________________________
