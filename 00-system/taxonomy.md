# 分类与标签

## 推荐分类

- `concept`：概念、定义、原理
- `guide`：方法、流程、操作指南
- `domain`：某个主题或领域的长期知识
- `decision`：个人决策、经验、复盘
- `source`：来源本身或来源摘要
- `record`：从来源中提取的事实记录
- `project`：一个需要集中管理的长期项目及其专题资料

项目资料可以在项目目录内部继续按 `record`、`knowledge`、`guide` 或 `decision` 组织，不必强行拆回全局目录。

## 文档元数据

Markdown 文档可以在开头使用 YAML front matter：

```yaml
---
title: 文档标题
type: knowledge
status: draft
created: 2026-09-12
updated: 2026-09-12
tags:
  - example
sources:
  - ../01-facts/records/example.md
---
```

## 标签约定

- 标签使用小写英文或稳定的中文词语，不混用同义词。
- 标签表达主题、用途或状态，不把长句放进标签。
- 优先复用已有标签；新标签需要在 `03-index/index.md` 中有意义的入口。
- 事实页至少记录一个 `source` 或明确说明“个人观察”。
- 知识页应尽量列出支撑它的事实页。
