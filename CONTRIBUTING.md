# 贡献指南

感谢你帮助改进英诺公众号资讯 macOS 工具。

## 提交问题

- 功能建议和缺陷请优先通过 GitHub Issues 提交。
- 请勿上传公众号登录凭据、Cookie、Token、未授权文章或含有个人信息的资料包。
- 安全问题请按 `SECURITY.md` 私下报告，不要公开披露可利用细节。

## 开发与测试

需要 macOS、Python 3.11 或更高版本，以及可用的 Swift 工具链。

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[build]'
./.venv/bin/python -m unittest discover -s tests
./scripts/test_swift.sh
```

请在 Pull Request 中说明动机、用户可见变化、测试结果和潜在风险。

## 合规边界

贡献者必须尊重公众号平台规则和内容权利人的权利。本项目的 MIT 许可证只适用于软件代码，不授予任何文章、图片或附件的权利。
