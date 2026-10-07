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
2. `scripts/publish_production_dashboard.py` 将 `index.html` 与 `release.json` 发布到 CloudBase 的 `production-dashboard/` 目录，公开地址回读内容摘要一致后才记为发布成功。
3. 手机和电脑访问同一正式入口，每 30 秒核查同目录 `release.json`；发现新快照后更新，网络异常时保留当前快照并提示。
4. 页面中的“网页规则版本”“网站快照已核对”和台账快照时间须与本次发布清单一致；仅有 Git 提交或本地构建成功不能证明线上已更新。

## 显示口径

两年度统一显示“下机翻袜产量”和“烤边产量”。李鸿玉与张小翠同产品的烤边产量合计为“总产量（烤边）”。内部工序仍用“下机／烤边”，年度台账的原有文件名、人员标题与工序列继续保留，解析及数量统计使用原值。

## 历史入口

- 仓库中的 `docs/production-dashboard.html` 现为兼容跳转页，转至同目录 `index.html` 并保留查询参数和页面锚点，避免继续显示内嵌旧台账。是否能通过某个线上旧地址访问该跳转页，取决于该地址所属部署是否已发布此文件。
- 当前 CloudBase 发布器只发布 `index.html` 与 `release.json`；2026-10-07 回读 `/production-dashboard/production-dashboard.html` 为 404，不把仓库兼容页视为已在线发布。
- `https://xianer-ai-brain.pages.dev/production-dashboard` 为历史 Cloudflare 入口；尚未完成本轮线上核验，不标记为最新正式入口。
- CloudBase 域名根目录为旧入口，不作为本轮生产工作台的核验地址。应使用上表包含 `/production-dashboard/index.html` 的完整地址。
- CloudBase 测试域名首次访问可能显示平台访问提示，通过提示后再核验实际工作台内容。
