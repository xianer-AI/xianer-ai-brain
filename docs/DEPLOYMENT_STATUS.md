# 生产统计工作台部署登记

入口登记更新：2026-10-07（北京时间）。此文件登记入口与核验方式，不替代线上发布回读证据。

| 入口 | 地址 | 用途 |
|---|---|---|
| 手机／电脑统一正式入口 | https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/index.html | 自动选择年度，也可在页面切换 |
| 2026 年下半年 | https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/index.html?year=2026 | 2026 年下半年台账 |
| 2027 年全年 | https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/index.html?year=2027 | 2027 年全年台账 |
| 已发布版本清单 | https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/release.json | 核对规则版本、源摘要及各年度台账摘要 |

## 发布与核验

1. `scripts/build_production_dashboard.py` 从同一正式规则和两年度台账生成 `docs/index.html` 与 `docs/release.json`。
2. `scripts/publish_production_dashboard.py` 将 `index.html`、`release.json` 与历史入口兼容页 `production-dashboard.html` 发布到 CloudBase 的 `production-dashboard/` 目录；另以明确的单文件上传，将同一兼容页放到域名根 `index.html`。四个公开文件的回读内容摘要均一致后才记为发布成功。两个兼容入口都纳入发布变更判断，兼容页单独发生变化也会触发发布；只改变快照生成时间不会反复发布。
3. 手机和电脑访问同一正式入口，每 30 秒核查同目录 `release.json`；发现新快照后更新，网络异常时保留当前快照并提示。
4. 页面中的“网页规则版本”“网站快照已核对”和台账快照时间须与本次发布清单一致；仅有 Git 提交或本地构建成功不能证明线上已更新。

## 显示口径

两年度统一显示“下机翻袜产量”和“烤边产量”。李鸿玉与张小翠同产品的烤边产量合计为“总产量（烤边）”。内部工序仍用“下机／烤边”，年度台账的原有文件名、人员标题与工序列继续保留，解析及数量统计使用原值。

## 历史入口

- 仓库中的 `docs/production-dashboard.html` 为兼容跳转页，转至上表 CloudBase 绝对正式地址，保留年度等查询参数和页面锚点。不会继续显示内嵌旧台账，也不会因部署目录不同而跳进其他网站首页。
- CloudBase 历史兼容地址为 `https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/production-dashboard.html`，由同一发布器上传和公开回读。是否已在线生效，以发布状态文件中的该文件摘要及公开回读为准，不能仅依据本登记或 Git 提交判断。
- `https://xianer-ai-brain.pages.dev/production-dashboard` 与 `/production-dashboard.html` 为历史 Cloudflare 生产入口；2026-10-07 用浏览器 User-Agent 回读为 HTTP 200，仍显示 V1.17；该域名根目录显示 V1.19。默认程序 User-Agent 曾返回 403，不能据此推断站点不可用或缺少部署权限。仓库无 Cloudflare 发布工作流或 Wrangler 配置，GitHub 部署列表为空，尚未确定该站的部署渠道。此次不覆盖整个 Cloudflare 站点，也不标记这些地址已完成升级；取得该站部署配置后，只设置生产路径到上述正式地址的兼容跳转并复核参数、锚点。
- CloudBase 域名根目录 `/` 和 `/index.html` 改前回读为 V1.17 生产页，内容摘要与历史 Cloudflare 生产页一致，历史部署登记也确认它曾是手机生产入口。发布器现将根 `index.html` 单文件替换为兼容页，不上传目录到根路径、不使用 `--prune`、不删除或修改藤条等其他工具。`/production-dashboard` 及 `/production-dashboard/` 能访问当前正式页面；今后分享使用上表包含 `/production-dashboard/index.html` 的完整地址。根入口在线升级是否完成仍以公开回读为准。
- CloudBase 测试域名首次访问可能显示平台访问提示，通过提示后再核验实际工作台内容。
