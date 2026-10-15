# Jev交接 · 2026-10-15

本轮接管记录覆盖10-12～15；此前生产快照核对 **10-15 12:04，北京时间**；本次评论返回、C3真实复验及结果核对 **10-15 19:34，北京时间**。本库默认分支`main`，功能基线为已合并 [PR12](https://github.com/TangVarie/JevforCoentent/pull/12)，merge `59e4f6f7fb4fa0f0a51ee30b29b6306561f3b3a7`。

跨库总览和统一待办：[TV交接](https://github.com/TangVarie/truth-vault/blob/main/HANDOFF.md)、[OPEN-ITEMS](https://github.com/TangVarie/truth-vault/blob/main/OPEN-ITEMS.md)；写手端：[autowriter交接](https://github.com/TangVarie/autowriter/blob/claude/xhs-content-workstation-BSEuH/HANDOFF.md)。

## 当前可以直接接着做什么

**后续收尾正在本轮推进**：C4已固定七题的单选边界，231项测试通过，20条虚构合成模型验证131/140；真实旧40开发回归/新40 AI参考验证已零调用预检，因自动审批要求这80条C4的明确出境许可而暂未发送。见[收尾记录](docs/comment-line-closeout-2026-10-15.md)。下面C3结果保留，不覆盖原参考或生产默认。

**40条人员复核已返回并核验，280格原判已另存为固定参考。** 与AI草稿92格不同，AI覆盖0格；来源为单人判断，保留题意边界差异，不称双人金标准。专用评估入口已完成、全量217项测试通过，真实输入离线预检0调用。已获此次明确许可并完成C3独立复验：172/280一致、137无歧义一致、69歧义、0缺答/无效；相对参考背书漏报0/28、误报4/12。不推广C3；下一步先统一分类边界，生产评论仍关闭。详见[本次返回报告](docs/comment-holdout-return-2026-10-15.md)。

## 实际改动与证据

| PR | 已完成 |
| --- | --- |
| [9](https://github.com/TangVarie/JevforCoentent/pull/9) | 单条评论修改单/整组HTTP、受限writer权限、未发布出境检查；缺答/歧义/背书/运营不明进入复核；独立shadow回填/续跑/完整快照；100条标注准备工具 |
| [10](https://github.com/TangVarie/JevforCoentent/pull/10) | 原A/B标准化、ID/上下文/题面/闭集对账、占位替换及分歧包准备；原始表不修改 |
| [11](https://github.com/TangVarie/JevforCoentent/pull/11) | 固定参考本地真实模型评估、完整概率验证、来源先验、原子checkpoint、严格resume、错误/歧义/漏报报告 |
| [12](https://github.com/TangVarie/JevforCoentent/pull/12) | 候选仅改两题文字，锁模型/标签/顺序/阈值/fill/路由/其余5题，隔离runtime discovery；历史C1/C2保留、当前C3采用owner确认的文本细节范围 |

审阅已处理：全选项概率的有限/范围检查、完整checkpoint无需密钥恢复、来源元数据调用前拒绝、候选选项顺序、缺失回复对象不作否定证据。本次新增独立40条评估后，本地全量217项测试通过；此前基线200项通过，PR/main CI通过；都是合成工程验证，不能代替模型效果。

### 生产接口核对

- 正确服务：`https://judge-production-601c.up.railway.app`，旧`judge-production.up.railway.app`是另一服务。
- 10-15 `/health`：`ok=true`、`mock=false`、`auth.required=true`、writer keys=2、`comment_tools_enabled=false`；候选目录未被发现。
- `POST /comment_repair_plan_for`、`POST /judge_thread`已经实现并部署，默认关闭返回503。管理key或受限writer key可调用只读接口；额外write/published参数拒绝。deskcore已转发，HTTP和MCP工具存在不等于可对用户宣布评论通过。
- 单条与整组沿用同一slot角色；正文与自动要点限同一600字片段。缺答、歧义、亲历背书/旁观推荐、运营“其他”等仍进复核。模型不验证亲历真假。
- **纠正旧待办**：`config/data_policy.yaml`在10-08既有 [PR5](https://github.com/TangVarie/JevforCoentent/pull/5)已将OKMAN与OKMAN_phase1加入`rx_cleared`；本轮没新增该许可。未清出的Rx项目仍403；`project_layer_cleared`为空，项目层权限另算。本次只读健康检查没有重新实测鉴权后的policy回显。

## 参考数据与来源：不要合并成一个“已验收”状态

1. 原100条有2条占位，已用同品类/角色实质评论替换，A/B各补14格。新版100条共700格，522格A/B一致、178格分歧（原174+新增4）。
2. 两份仲裁178格的仲裁人均填AI。此前漏查来源已向用户纠正；用户知情后指示“直接按照最终裁决来对待”。因此700格已冻结为最终参考，保留AI来源，不再追索这批真人重裁；**不称独立真人金标准**。
3. 原标签/题面/state/fill/模型摘要严格绑定。`reference_ready=true/label_decisions_final=true`只表示标签已定，不表示`release_accepted`、`gold_ready`或上线已通过。
4. 独立40条来自新帖子及同项目不同文本，OTC14/保健14/Rx12、36素人/4贴主；无新OTC贴主，配额变更已记录。样本冻结不等于参考标签冻结。
5. owner先选AI拟稿，已本地写满280格、逐格理由与边界说明。之后owner明确暂不冻结，并安排人员复核。已重做中文答题Excel/说明，不预填AI答案；10-15已收到填写表并获委托完成处理；核验40条/280格完整、上下文及隐藏映射未变。返回原判另建固定参考，92格不同于AI稿、覆盖0格；来源为一位复核人员，题意差异另记，未接受为上线金标准。

### 真实评估结果

| 题 | cb-v0.4一致/100 | C1一致/100 | C1修复/新回退 |
| --- | --- | --- | --- |
| speech_act | 73 | 75 | 9 / 7 |
| names_brand | 96 | 96 | 0 / 0 |
| echoes_post | 78 | 77 | 2 / 3 |
| detail_level | 39 | 86 | 58 / 11 |
| register | 76 | 77 | 2 / 1 |
| reader_value | 69 | 69 | 2 / 2 |
| arranged | 72 | 73 | 1 / 0 |

正式eval-v2基线：503/700=71.9%，无歧义一致414，歧义172，0缺答/无效。初次503/411/173试跑因概率校验修复重跑，保留但不混算。基线2条无歧义背书漏报均另有其他歧义题，没有证据称为实际HTTP绕过。

C1获owner“允许这一次候选试跑”后完整跑一次：553/700=79.0%，无歧义一致470，歧义142，0无效；74修复、24新回退。原背书漏报改善，但无歧义背书误报4→12，总答案误报4→14；不推广。其他5题的浮动不自动归因于改题。旧100条参与过诊断，只能作为开发回归。

### 当前题库及定义

| 文件 | 版本/状态 |
| --- | --- |
| `banks/comment_reader_v0.4.yaml` | 生产默认；未改 |
| `banks/candidates/comment_reader_v0.5_candidate1.yaml` | C1历史原样保留；仅此版本试跑一次 |
| `banks/candidates/comment_reader_v0.5_candidate2.yaml` | 未试跑；修缺回复对象，仍采用个人经历细节提案 |
| `banks/candidates/comment_reader_v0.5_candidate.yaml` | 当前C3；本次一次40条独立复验172/280，不能套C1分数 |

owner确认合成A（12W/三档亮度/129元）和D（官网搜索型号→售后页→上传订单号）都算“具体”。C3因此衡量**可核对文本细节，不要求个人经历**；规格/价格/年龄/时长/数量/明确步骤可计，品牌、术语或笼统动作本身不够。C1把个人年龄/实际购买价格降档也属于过严，不能全归因于定义范围。speech_act按主要目的；看不到回复对象不等于没有问题，中性款型/渠道回复不自动等于推荐。

arranged定义及阈值未改；原A/B仅50/100一致，这题仍是普通读者文本读感，不是运营安排的事实证明。

## 新40条返回后的状态和下一步

1. **已完成**：只读解析原Excel，核对44个工作表、40条原文/角色/ID/qid、280格闭集及填写人；缺答/答案公式/映射和原文变化均0。逐格与AI草稿对账，188格相同、92格不同；只保留实际返回原判，AI覆盖0格。用户此次完整返回并委托处理后，固定为单人参考v1；原稿/旧700格未改。
2. **已完成**：`scripts/evaluate_comment_holdout.py`严格验证40条/280格、C3/模型/参考/题面/原工作簿摘要，重新生成上下文、检查开发帖及同项目同文零重叠。共用汇总按实际条数计分，旧100条loader保持100/700。完整checkpoint可无凭据生成报告；待调用部分须有绑定摘要及输出目录的该批许可。合成回归和调用前真实离线预检已过，预检0调用。
3. **已完成一次独立复验**：用户明确“允许本次40条C3独立复验”，40/40有效、172/280一致、137无歧义一致、69歧义、0缺答；相对参考背书漏报0/28、误报4/12（无歧义）。checkpoint摘要见返回报告；本次许可已用完，无追加调用。
4. **待边界复核/下一版设计**：不推广C3。优先可行动信息/判断依据互斥顺序、具体承接范围、数值细节与品牌/药名边界；7条输入含编辑标记需在下一批明确处理。只称与单人参考一致性，不能拿旧100与新40总分差证明提升；若据本40条改题，记录其参与开发，下一版另用新样本验证。
5. **待owner验收**：之后才小批开放接口、shadow/primary和W-02。primary现有CLI还要求`sample_count>=100`、两位独立标注人及绑定两个bank摘要的验收记录；独立40条不自动满足该结构。如需调整验收契约应显式设计，不能造人名/验收字段。

## 本地材料与摘要

真实文本/标注/题面/模型产物不上传GitHub；以下是当前工作区路径，不是新机器必有的文件，须安全转交完整材料。

| 目录 | 内容 |
| --- | --- |
| `/workspace/annotation-packets/J03-health-20261013-final-reference/` | 冻结旧参考/manifest；`evaluation-live-4wj31n53`为正式eval-v2 |
| `/workspace/annotation-packets/J03-health-20261013-round2/` | 新版原样本、合并补标、原逐条题面 |
| `/workspace/annotation-packets/J03-health-20261013-candidate-experiment/` | C1许可、`candidate1-live-gnjpf90t`完整批次、24回退/74修复及本地报告 |
| `/workspace/annotation-packets/J03-health-20261013-independent-holdout/` | 新样本/抽样SQL、C3题面、AI稿、`40条评论_复核人员直接填写包.zip` |
| `/workspace/annotation-packets/J03-health-20261015-holdout-reference/` | 返回单人参考/manifest、92格差异、备注、核验说明、零调用预检、一次C3真实结果、中文结果Excel；原Excel另在attachments目录 |

| 绑定 | SHA-256 |
| --- | --- |
| 返回单人280格参考 | `953dc0975d58574045324d55b7f01b97a352b724c12e2c08ee8ff616045341d3` |
| 旧700格参考 | `18ac3677862d62f72a4d48287ce26e39a1cc623fdb10dfd067e47435d04cdcf6` |
| cb-v0.4 | `e7c4c14da386fc1d10f3bd5989a9a29571157039b95b0cd7816a2c1db249fbda` |
| C1 | `82d159dc2481afecf813d0e61c280aece2623e5dcff9e8444656ffc49b488f64` |
| C2 | `c81abda5d74c2f42a13786acd395e381cf076280a75a51579fa74b9e537edc45` |
| 当前C3 | `ac8260ddbe5a5614fa9da31154f9eb2f5438e444b2141bf55ef1c74d89fea48e` |
| 独立40样本 | `acd6b5de112eb15000f2b9aa91226a8374fd363bc879af5bdd2c15a774544d38` |
| 新40逐条题面 | `c48ffab04fb79dea43f630cc361d512b88ce1528fb89aa412894d2b2a339e941` |
| AI草稿 | `42de2e005027378739d2a53977a57dea1984cffcf6a07e2beda8eca9c360ede6` |
| 正式基线checkpoint | `6682c83625be22cb4c4b107d129ac43e7b723179de73541f28aa5208be9583cc` |
| C1 checkpoint | `4f44eef7aaa969908a7072893f9d073c0f4fe54dc762caa888bcbd68c2275cf7` |

## 继续跟踪与常用检查

外部语料周跑 [38237686717](https://github.com/TangVarie/JevforCoentent/actions/runs/38237686717)在10-12 schedule成功，job日志的apply_rows报告external_notes178行、note_feature_answers4272行；“从没跑成功”已过时。后续仍核对正文、账本写入与新鲜度，不能只用抓取job的绿色替代实际入库。评论回填尚未执行真实shadow或primary，三省六部/借卡/发牌的其他接线继续按TV清单推进。

```bash
python -m pytest -q
sha256sum banks/comment_reader_v0.4.yaml banks/candidates/comment_reader_v0.5_candidate*.yaml
curl -fsS https://judge-production-601c.up.railway.app/health
gh run list --repo TangVarie/JevforCoentent --limit 10
```

`TYPESAFE_API_KEY`或既有key-file机制只在运行环境使用，不写文档/源码/产物。完整有效checkpoint可无凭据重建报告；错误/无效要重试才会新调用，先核对授权与摘要。更细资料：[原参考评估](docs/comment-reference-evaluation-2026-10-13.md)、[候选与回退](docs/comment-reader-candidate-2026-10-13.md)、[HTTP和回填合同](docs/comment-tools-rollout-2026-10-12.md)。这些早期文档的阶段性“尚未标注”描述以本交接明确日期的状态为准。
