# J-03 · 已收两份标注，等待第三人仲裁（2026-10-13）

Jev #9 / autowriter #96 / truth-vault #178 已合并。Jev生产部署的GitHub状态success，正确域名为 `https://judge-production-601c.up.railway.app`；`/health`确认鉴权开启、非mock、评论开关false。写作台生产commit `487403b75fb0`、协议 `3fdff2c0b985`，24工具已部署。

用户提供A/B已完成表，各100条、700格。按comment_id对齐，两份ID集合与固定样本一致；帖子、评论、角色、要点均未改动。A表用是/否，B表300个布尔答案使用true/false，仅作等价格式规范化，不改实质标注。

| 题 | 一致 / 100 | 一致率 | Cohen κ |
|---|---|---|---|
| speech_act | 68 | 68% | 0.593 |
| names_brand | 99 | 99% | 0.979 |
| echoes_post | 92 | 92% | 0.668 |
| detail_level | 71 | 71% | 0.548 |
| register | 82 | 82% | 0.707 |
| reader_value | 64 | 64% | 0.519 |
| arranged | 50 | 50% | 0.183 |

总计526格一致、174格分歧，涉及90条评论；10条七题全部一致。以上为两人一致性，**不是Jev命中率**。另有两条仅评论位标签/空占位、无实质文本，单列来源复核；建议换成同品类实质评论，并由原A/B独立补标。不能把98条当成100条，也不替人类选择分歧答案。

用户已选择安排第三位人员仲裁。仲裁包一行一格，包含原始上下文、按篇填充的题面/定义、A/B答案与备注，仲裁答案/仲裁人/理由均留空；两条来源问题另表处理。原表、文本、规范化明细、共识和仲裁表只在本地保留，不推GitHub。

## 重建对账包

```bash
python scripts/ingest_comment_annotations.py --a /local/annotation-A.csv --b /local/annotation-B.csv \
  --sample /local/fixed-sample.json --manifest /local/original-packet/manifest.json \
  --output /local/comment-arbitration
```

manifest绑定原始样本版本/摘要与题库摘要。重复/缺失/未知ID、空答/闭集外答案、上下文变动或摘要不匹配均拒绝；任意Git仓库内的输出目录禁止，非空输出目录拒绝覆盖，以保护已经填写的人工仲裁。CSV公式起始符转义，JSON原文不改。

输出 `分歧仲裁.csv`、`样本复核.csv`、`共识待仲裁.json`、`格式规范化记录.json`、`summary.json` 和说明。共识记录只包含已一致的格；未决格仍缺失，gold_ready / accepted固定false，不生成验收记录、不调用Jev、不写数据库。即使答案全一致，也还需来源复核、模型评估和owner验收，不能直接启用服务。

收到第三人裁决及补标后，再验证裁决的ID/闭集/版本/理由，冻结人类金标准并跑模型逐题对照、混淆、歧义和危险漏报；验收后才开评论开关、回填正式账本、统一导入skill。定义若调整，升版重标，不能按模型答案改人类标签。
