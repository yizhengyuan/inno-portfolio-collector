# 英诺被投项目公众号采集工具

这是一个在 macOS 本地运行的个人采集工具。它读取 `config/projects.json` 中已经核验的 10 个项目与公众号映射，采集自 2026-01-01 起发布的公开文章，整理成 Obsidian 仓库，并生成可以通过微信发送的 ZIP。

当前版本只负责“采集和整理”。选题、编辑和发布仍由人工完成。

## 安装

需要 Python 3.11 或更高版本。进入本项目目录后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

采集依赖相邻目录中的 `moore-wechat-article-downloader` 导出脚本，以及 `~/.moore/wechat-article-downloader` 中的本地运行环境。路径不同时，可以分别设置 `INNO_EXPORTER_SCRIPT` 和 `INNO_EXPORTER_RUNTIME`，也可以在命令中使用对应参数覆盖默认值。

## 开始采集

运行前请先在导出工具中登录，并确保 10 个目标公众号已经加入账号列表。工具只接受公众号名称或微信号的精确匹配，不会用模糊结果代替；它只处理当前登录账号有权访问的公开文章。

依次复制运行下面三条命令：

```bash
python3 -m inno_collector collect --dry-run
python3 -m inno_collector collect
python3 -m inno_collector package
```

第一条只做登录、账号映射和目录预检，不下载或写入文件。第二条增量采集文章并写入 `runtime/vault/英诺被投项目资讯库`。第三条先校验交付内容，再把 ZIP 和摘要写入 `dist`。

如果某个账号登录失效、名称不匹配或文章下载失败，请先查看仓库中的 `01-采集状态.md` 和 `90-系统/collection-report.md`。失败项目会被明确列出，不应把不完整结果当作完整采集。

## 交付给阅读者

接收方只需安装 Obsidian：

1. 解压收到的 ZIP；
2. 在 Obsidian 中选择“打开本地仓库”；
3. 选择解压后的 `英诺被投项目资讯库` 文件夹。

接收方不需要安装 Codex、Python 或采集工具，也不需要任何公众号登录凭据。

只发送 `dist` 中生成的 ZIP。不要发送 `runtime`、导出工具的运行目录、Cookie、Token 或其他登录文件；交付 ZIP 的校验会拒绝这些敏感内容。
