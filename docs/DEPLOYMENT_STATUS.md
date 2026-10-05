# 生产统计工作台部署登记

最后核对时间：2026-10-05（北京时间）

| 入口 | 地址 | 当前版本 | 状态 |
|---|---|---|---|
| 电脑/统一正式入口 | https://xianer-ai-brain.pages.dev/production-dashboard | GitHub main（2026-10-05） | 最新，已显示核对卡业务状态 |
| 手机当前入口 | https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/ | CloudBase `index.html`（107.31 KB，2026-10-05 12:00） | 最新，已验证显示徐超超待回复状态 |

## 判断规则

以后先对比入口页面的“台账快照时间”和 GitHub main 提交；手机入口只有在部署版本追上 GitHub main 后，才标记为“最新”。本次已将最新工作台覆盖到腾讯云 CloudBase，并在手机入口实测页面内容。以后更新 GitHub 后，需重新上传根目录 `index.html`，再核对页面刷新时间和台账快照时间。

## 当前结论

电脑 Cloudflare 与手机腾讯云是两个独立部署；本次两端已同步到最新工作台。手机访问腾讯云测试域名首次可能出现提示，点击“确定访问”即可。
