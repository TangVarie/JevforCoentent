# D-112 评论门槛轮结论（2026-10-16）

**通过（按预注册判据）。** 这只说明模型在这 40 条上达到了预先定好的门槛；`release_accepted` 仍为 false，上线要等 owner 验收（truth-vault DECISIONS D-112 第 7 条）。

- 运行：[comment-gate-eval run 38862116728](https://github.com/TangVarie/JevforCoentent/actions/runs/38862116728)，main `fa7ec36`，`jev-1.13.0` × `cb-v0.6-candidate4`，真实调用（`live: true`）40 次、0 次重问，40/40 有效。
- 单次授权已用掉：tag `comment-gate-d112-run-claimed` 指向 `fa7ec36`。
- 样本 sha256 `f7b7ffc5…`；参考 sha256 `01023661…`（单人人工，280/280 格，0 未决，未给 AI 建议答案）。

| 题 | 角色 | 一致 / 40 | 模型标歧义 |
|---|---|---|---|
| speech_act 主要行为 | 门槛 | 38 | 5 |
| names_brand 点名品牌 | 门槛 | 40 | 1 |
| echoes_post 接住帖子 | 门槛 | 38 | 8 |
| detail_level 细节程度 | 门槛 | 34 | 6 |
| register 说话方式 | 参考 | 28 | 9 |
| reader_value 读者价值 | 参考 | 32 | 3 |
| arranged 像不像安排 | 参考 | 33 | 14 |

门槛题合计 150/160 = 93.8%（线 ≥ 85%）；每题最低 detail_level 34/40 = 85%（线 ≥ 75%）；门槛题歧义 20/160 = 12.5%（线 ≤ 25%）；无歧义背书漏报 0（线 = 0）。

文件只含 comment_id、闭集答案、概率和统计，不含评论或帖子原文（本批原文未获公开许可）。
