# Inno Portfolio Collector

**A local-first macOS tool that turns a client's WeChat public-account coverage into an offline, editable knowledge base — delivered as a single ZIP.**

[![CI](https://github.com/yizhengyuan/inno-portfolio-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/yizhengyuan/inno-portfolio-collector/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-451%20passing-brightgreen)](#verify-it-yourself)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![runtime deps](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A consultant tracks ten portfolio companies across WeChat public accounts. The client wants to read, search, annotate and write against that material — without a cloud account, without installing another app, and without ever touching the consultant's WeChat credentials.

That constraint set is the whole product.

---

## What it actually does

| Step | Who | What happens |
|---|---|---|
| **1 · Collect** | Consultant, on one Mac | A thin macOS launcher starts a local web UI on a random `127.0.0.1` port. QR login, preflight, incremental collection — login state never leaves this machine. |
| **2 · Package** | Same machine | One click validates the archive (broken links, forbidden files, manifest drift, **secret scan**) and emits a ZIP: Obsidian vault + offline HTML dashboard + `manifest.json` with per-file SHA-256. |
| **3 · Consume** | Client, offline | Unzip and open `80-离线看板/index.html`, or open the whole vault in Obsidian. Read, search, take notes, draft. Send back a `.inno-drafts` package. |

**What ships is a plain ZIP** — no Python, no app install, no network, no account.

---

## Why it is built this way

| Constraint | What it forces in the code |
|---|---|
| The client must install nothing | Output is a folder of Markdown plus one self-contained HTML dashboard. |
| WeChat credentials must never leave the collector's Mac | Cookies and tokens stay inside the local web session. `package.py` scans every outbound file and **refuses the build if it finds a secret** (`_scan_secrets`). |
| The client may be fully offline | No remote calls at read time; search and rendering are local. |
| One person has to keep this alive | **Python standard library only — `dependencies = []`.** 451 tests run in ~4.5 s. CI gates every push. |
| Delivery must be reproducible | Every package carries a content manifest with SHA-256 per file; updates are incremental diffs against a known base version. |

---

## Verify it yourself

```bash
git clone https://github.com/yizhengyuan/inno-portfolio-collector
cd inno-portfolio-collector

python3.11 -m venv .venv
./.venv/bin/python -m pip install -e .        # no runtime dependencies to fetch
./.venv/bin/python -m unittest discover -s tests
```

```
Ran 451 tests in 4.502s
OK (skipped=1)
```

CI runs three jobs on every push and PR: repository policy, Python tests (macOS 15 / 3.11), Swift tests.

---

## Architecture

| Layer | Location | Role |
|---|---|---|
| macOS launcher | `macos/` (Swift) | Double-click → starts the local server on a random loopback port → opens the browser. Quitting the app is what stops the server. |
| Local web UI | `src/inno_collector/web/` | Session-scoped control surface: login, preflight, collect, validate, package, receive drafts. Loopback-only bind, host-header validation, write-identity checks. |
| Collection core | `src/inno_collector/` | `exporter` → `ingest` → `vault` → `dashboard` → `package`; append-only state, content manifests, update/draft packages. |
| Release tooling | `packaging/`, `scripts/` | PyInstaller build, Swift app assembly, DMG layout, signing and notarization checks. |
| Tests | `tests/` | 38 modules: unit, contract, end-to-end delivery, web security, package validation. |

**Shape of the repo:** ~24k lines of Python (78 files), ~3.4k lines of Swift (21 files), 139 tracked files.

---

## Status — read this before you run it in production

- **Not yet a formally distributable build.** The app can be built with an ad-hoc signature for local QA. Developer ID signing, notarization and a clean-account install are still open — see [`docs/macos-release-checklist.md`](docs/macos-release-checklist.md).
- **Single-client mode is the current delivery path.** Multi-client / high-frequency incremental sync exists in source and tests, but is not the supported flow.
- **Content licensing is the operator's responsibility.** Article text, images and attachments belong to their original rights holders.

---

## License & attribution

Own code is [MIT](LICENSE). The collector adapts [wechat-article-exporter](https://github.com/wechat-article/wechat-article-exporter) and [moore-wechat-article-downloader](https://github.com/Moore-developers/moore-wechat-article-downloader), both MIT — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Issues and feature requests: [GitHub Issues](https://github.com/yizhengyuan/inno-portfolio-collector/issues). Before contributing, read [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md).

---

<details>
<summary><b>中文操作手册（采集者与客户）</b></summary>

### 采集者

1. 打开「英诺资讯采集」，完成扫码登录。
2. 通过预检后执行增量采集；采集必须先通过最近一次预检。
3. 在「交付」板块点击「生成客户资料包 ZIP」并下载。
4. 只把 ZIP 发给客户；**不要**发送登录状态、Cookie 或 Collector App 本身。

本地 Web 界面在随机的 `127.0.0.1` 端口启动，不是云服务。只关闭浏览器标签页不会结束 App，退出 App 才会停止本地服务。

项目配置来自 `config/projects.json`，打包时逐字节复制，不会重新生成或修改。公众号登录凭据、Cookie 和 Token 不进入客户资料包。

### 客户

解压 ZIP 后可以直接使用：

- 按标题、项目和公众号搜索文章；
- 双击 `80-离线看板/index.html` 打开完全离线的看板；
- 用推荐安装的 Obsidian 打开整个 Vault；
- 在 `10-编辑稿` 与 `11-个人笔记` 中新建笔记、摘要、选题或编辑稿；
- 把 `10-编辑稿` 中的内容回传（`.inno-drafts` 包）。

原文和附件位于内容区。阅读、搜索、编辑和离线看板均可断网使用，客户不需要任何公众号登录凭据。

### 更新

后续更新时重新生成完整资料包。客户自己的稿件应先单独备份或回传，避免手工覆盖。

### 开发构建

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[build]'
# 另将 moore-wechat-article-downloader clone 到本仓库的同级目录
./.venv/bin/python scripts/build_macos_apps.py \
  --configuration release --output .build-macos/apps
./scripts/test_swift.sh
```

正式签名工具要求 `MACOS_SIGNING_IDENTITY`；启用公证时还要求 `APPLE_ID`、`APPLE_TEAM_ID` 和 `APPLE_APP_PASSWORD`。凭据只从环境读取，不写入发布清单。

### 合规记录

- [Local Web Collector 切换门槛审核](docs/compliance/2026-07-12-local-web-collector-cutover-review.md)（当前架构）
- [2026-07-12 开源与分发技术合规复核](docs/compliance/2026-07-12-open-source-and-distribution-review.md)（旧双 Helper 架构，历史记录）

技术复核不替代内容授权、Apple 正式签名、公证和干净账户安装验收。

</details>
