# 分类与标签

## 推荐分类

- `concept`：概念、定义、原理
- `guide`：方法、流程、操作指南
- `domain`：某个主题或领域的长期知识
- `decision`：个人决策、经验、复盘
- `source`：来源本身或来源摘要
- `record`：从来源中提取的事实记录

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

标签使用小写英文或稳定的中文词语，不混用同义词。事实页至少记录一个来源或明确说明“个人观察”；知识页应尽量列出支撑它的事实页。
