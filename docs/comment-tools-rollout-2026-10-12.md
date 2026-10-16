# J-01 / J-02 / J-03 · 评论工具与回填

> **2026-10-16 owner 验收 D-112（truth-vault DECISIONS），写手评论工具按下面的口径开放：**
> - 读者侧题库换成 `comment_reader_v0.6`：C4 原样发布，规范化 digest `a0c04acf…` 与验收时一致，只加了 `status` / `frozen_sha256` 两行元数据。
> - 通过与否只看四道门槛题（`speech_act` / `names_brand` / `echoes_post` / `detail_level`）和背书复核。
> - `register` / `reader_value` / `arranged` 只作提示：不合评论位要求时进返回值的 `hints`，不进 `plan`，不影响 `passed`；漏答或标歧义也不送复核。
> - 运营侧 `comment_intent` 没有验收过，同样只提示。
> - 整组检查（`comment_thread_v0.4`）不在 D-112 范围内，照旧判定。
> - 开关仍是 `JUDGE_COMMENT_TOOLS_ENABLED=1`（Railway 环境变量）。
> - 内部 stdio 工具与历史回填仍用 v0.4，J-02 小批影子回填时再换。

> **当前阶段更新（2026-10-15）见 [收尾结论](comment-line-closeout-2026-10-15.md)及[HANDOFF](../HANDOFF.md)**：人员40条已返回；C3和获此次许可的C4均完成验证，C4新40与AI参考225/280，未达预注册标准，暂不推广。工程及本地合成接口验证完成；生产评论关闭、无shadow/primary，下文早期阶段快照保留追溯。

两个候选评论接口已实现：`POST /comment_repair_plan_for`（帖子 + 单条评论 + slot 目标画像）和 `POST /judge_thread`（帖子 + 1..20 条评论，可带每条 slot）。单条/整组使用同一slot角色（贴主回复不会在整组误标读者位）。两者接受管理 key 或受限 writer key；均只读、不入稿件库、不回账本行。额外的 write / published 参数直接 422，候选评论固定按未发布内容检查出境；未清出的处方药项目 403、一次 Jev 调用也不发。

`passed=true` 必须同时满足有效答案、单条/整组硬伤均通过、无人工复核项。缺答与亲历背书/旁观推荐不能靠通过其他题掩盖。先判单条再判整组；整组缺 slot 时可给画像但不能假装已定位应换的评论位。正文最多展示前 600 字，自动要点只从同一段提取；显式 points 必须由真实正文/brief 提供。

**默认关闭**：`JUDGE_COMMENT_TOOLS_ENABLED` 未设为 `1` 时返回 503；`/health.comment_tools_enabled` 回显布尔。J-03 完成之前不打开。单条歧义进复核，运营题「其他」也按原题库的 unclear_labels 进复核；不能因概率高就宣称通过。候选评论仍不替代医疗/平台合规审核，模型不能验证亲历是否属实。

## 人工金标准

固定样本留在本地 `fixtures/local/comment_health_annotation_20261012.json`（gitignored），不上传 GitHub。仓内提供 `scripts/sql/sample-comment-gold-20261012.sql` 与生成脚本，可在受控环境重建；重新抽样应固定文本快照和摘要，不混不同快照的人工答案。本次样本：100 条，OTC药 35 / 保健品 35 / 处方药 30，素人90 / 贴主10。SQL 与样本摘要记录在产物 manifest；同项目同文去重，每帖最多3条，每品类贴主最多5条。仅使用入库文本与角色，不展示互动量、历史运营标签或模型答案。`is_displayed` 是历史同步字段，未证明当前仍在平台可见。

```bash
python scripts/prepare_comment_gold.py --output artifacts/comment-gold
```

CSV的公式起始符会加前置单引号（原始JSON文本不改），避免平台原文在表格软件中执行公式。产生标注A.csv、标注B.csv（相同100条、不同顺序、七题答案均留空）、标注说明.md、逐条题面.json 和 manifest.json。两位独立标注人先按原定义填表，owner/第三人仲裁分歧；之后才跑 Jev 对照并逐题报告一致率、混淆、歧义及高风险漏报。不能拿 Jev 答案补人类答案。当前只完成样本与标注包，尚没有人工金标准或验收结论。

## 历史评论回填

手动 workflow `backfill-comments` 默认100条、只导出 artifacts，不写库；可选 `write_shadow=true` 只写独立 `shadow-comments-<run_id>` 账本。历史文本可先用于影子核查，不能据此宣布候选工具过验收。CLI reader 升至 cb-v0.4，按帖子真实主体和要点填充；运营题不再假定所有素人评论都是运营写的。两侧共9行/条，答案只入 `note_feature_answers`，不改 TV comments 的业务三列。

```bash
python scripts/backfill_comments.py --from-db --limit 100 --run-tag shadow-comments-first \
  --input-snapshot inputs.json --rows-json rows.json --raw raw.json --sql comments.sql --summary summary.csv
# 同 run_tag 续跑；只跳过同题库摘要、同模型、七/两题完整有效的整套答案
python scripts/backfill_comments.py --input inputs.json --limit 100 --run-tag shadow-comments-first \
  --resume rows.json --rows-json rows.json --raw raw.json --summary summary.csv
```

评论与note上下文都分页；上下文缺失立即拒绝整批，不用空正文判评论。短页也继续请求，只有空页或达到 limit 才停，避免 PostgREST 自定义上限截断。每25条原子保存 rows checkpoint；单条/单题库失败（包括嵌套模型JSON形状异常）保留其他结果、退出非零，不给整批成功假象、不写库。workflow 最后始终上传产物。`resume_run` 下载旧 run 的固定文本快照、checkpoint 与 run_tag.txt，沿用它实际的 shadow tag（含再次续跑的链）；续跑条数从旧快照总数推导，忽略dispatch默认limit=100，不截断大批次；CLI手动续跑需保持完整快照。mock 可离线验流程但不允许 `--write`，不能把 mock SQL 当真实答案。

正式 primary 回填需人工验收记录 `--acceptance acceptance.json`，内容至少包含 accepted=true、sample_count>=100、两位不同的 independent_annotators、arbitration_artifact、evaluation_artifact、accepted_by 和 bank_sha256（两个题库名到当前摘要的映射）。CLI 校验记录的完整性和题库摘要；记录由 owner 完成真实验收后保存，它不能代替标注/仲裁/模型误差评估。当前 workflow 只提供 shadow 路径。若修改定义，需升版、重新标金标准与验收。

## 发布顺序

1. 合入 Jev 和 autowriter 工程，评论接口保持关闭；MCP 阻塞和跨项目防护可独立发布。
2. 人工100条独立标注、仲裁，跑模型对照并审查逐题误差和危险漏报，保存验收记录。
3. 开启 Jev 评论开关、真服务小批核查；再让写手重新导入更新后的协议/skill（W-02）。
4. 历史评论先小批影子回填、回读核对；验收后跑正式全量与续跑。尚未执行正式全量，也没有把评论特征送入 L2。
