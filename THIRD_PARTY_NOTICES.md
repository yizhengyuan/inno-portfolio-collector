# Third-Party Notices

本产品保留并随 macOS 分发包提供以下第三方许可证。许可证文本位于
`third_party/licenses/`，并在应用包中复制到 `Contents/Resources/ThirdPartyLicenses/`。

## wechat-article-exporter

- 项目：https://github.com/wechat-article/wechat-article-exporter
- 许可证：MIT，`third_party/licenses/wechat-article-exporter-LICENSE.txt`
- 使用方式：本产品适配其公众号搜索、历史目录、正文处理、导出与本地管理界面的产品思路；不打包该项目的完整前端源码。
- 本地组件：采集端工作流、离线结果看板与内容包设计。

## moore-wechat-article-downloader

- 项目：https://github.com/Moore-developers/moore-wechat-article-downloader
- 许可证：MIT，`third_party/licenses/moore-wechat-article-downloader-LICENSE.txt`
- 使用方式：采集端的 `MooreExporterHelper` 打包该项目的本地导出脚本能力；`MooreExporterAdapter` 负责进程隔离与结果接入。阅读端不包含这些采集组件。
- 本地组件：采集端账号同步、目录缓存、文章下载与 Markdown/图片输出侧车。

未来如复制或修改第三方源文件，必须在本文件中补充文件级来源、修改说明和适用许可证。

公众号文章、图片及附件的著作权归原作者或其他权利人所有；开源软件许可证不授予这些内容的版权。使用者应仅在获得授权或法律允许的范围内采集、保存和分享内容。
