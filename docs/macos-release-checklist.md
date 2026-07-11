# macOS 正式发布验收清单

只有以下项目全部完成并留存记录后，版本才可标记为“可给朋友分发”。不得在记录中附带本地用户名、公众号登录信息或 Apple 凭据。

## 发布信息

- App 版本：__________
- Build：__________
- 测试 macOS 版本：__________
- 测试人缩写：__________
- Collector DMG SHA-256：__________
- Reader DMG SHA-256：__________
- 验收日期：__________

## 干净账户与 Gatekeeper

- [ ] 使用全新的非开发者 macOS 13 或更高版本账户，未安装 Python 和 Codex。
- [ ] 从公证后的 Collector DMG 安装“英诺资讯采集”，首次打开通过 Gatekeeper。
- [ ] 从公证后的 Reader DMG 安装“英诺资讯阅读”，首次打开通过 Gatekeeper。
- [ ] 两个 DMG 的本地 SHA-256 与 `release-manifest.json` 一致。

## 采集者流程

- [ ] Collector 明确显示本机负责登录与采集，Reader 不接收任何公众号登录凭据。
- [ ] 完成一次登录状态与 10 个精确公众号映射预检。
- [ ] 只对受控样本执行一次采集，预检未成功时“开始采集”保持禁用。
- [ ] 生成基线 `.inno-update`，确认包内无 Cookie、Token、auth-key、绝对用户路径或 `.moore` 数据。

## 第二账户离线阅读

- [ ] 在第二个干净账户导入基线更新包；系统中没有 Python 和 Codex。
- [ ] 断开网络后，按标题、项目和公众号搜索文章。
- [ ] 断开网络后打开本地文章与 `80-离线看板/index.html`，确认没有远程依赖。
- [ ] 安装 Obsidian 后能打开同一 Vault；未安装时 App 显示指引且其他功能可用。

## 编辑、增量与回传

- [ ] 创建一份编辑稿并记录稿件字节哈希。
- [ ] Collector 生成第二个增量包，Reader 预览并明确确认导入。
- [ ] 导入后稿件字节与导入前完全一致，原文和附件未被编辑功能修改。
- [ ] Reader 导出 `.inno-drafts`，Collector 收件箱成功接收。
- [ ] 重复包幂等，冲突稿件并列保留，未静默覆盖。

## 签名、隔离与许可证

- [ ] `codesign --verify --deep --strict --verbose=2` 对两个 App 均通过。
- [ ] `spctl --assess --type execute --verbose=2` 对两个 App 均通过。
- [ ] `xcrun stapler validate` 对两个公证 DMG 均通过。
- [ ] Reader 包内没有 Collector Helper、MooreExporterHelper、`projects.json`、导出脚本或凭据文件。
- [ ] Reader Helper 实际拒绝 `collect`；Collector Helper 和 Reader Helper 的 `status` 角色正确。
- [ ] 两个 App 均包含 wechat-article-exporter 与 Moore 下载器的完整第三方许可证和署名。
- [ ] 文章版权提示在 README 与第三方说明中可见。

## 结论

- [ ] 所有项目通过，批准本版本给朋友分发。
- 未通过项与处理记录：__________________________________________________
