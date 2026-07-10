# inno-portfolio-collector

第一阶段目标：采集指定的已认证微信公众号自 2026-01-01 起发布的公开文章，并整理为可直接导入 Obsidian 的 ZIP 文件。

这是一个在本地 macOS 环境中运行的采集工具。当前版本仅提供命令行骨架，后续阶段将逐步实现采集、校验和打包能力。

命令行骨架包含以下子命令：

- `inno-collect collect`
- `inno-collect lint`
- `inno-collect package`

目前这些子命令仅可被命令行解析器识别，尚未实现实际业务行为。
