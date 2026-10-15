# 新40条返回参考与独立评估准备 · 2026-10-15

用户返回已填写Excel并委托完成后续处理。已核对44个工作表、40条帖子/评论/角色/编号、隐藏ID/qid映射、填写人、全部280个下拉答案及备注：原文与映射未变，缺答/越界/答案公式均0。原Excel、旧700格参考、AI草稿和C3题面均未改，原始文本与返回备注只本地保留。

## 参考版本与来源

按返回原判冻结 `J03-holdout-returned-review-20261015-v1`，40条/280格，来源为**一位复核人员的返回判断**，AI覆盖0格。此处冻结是固定后续比较的版本；不是双人共识、无争议金标准或上线验收。此前“先复核暂不冻结”是返回前状态；本次完整返回后依用户委托处理，已另建参考文件，原AI草稿不变。

参考SHA-256：`953dc0975d58574045324d55b7f01b97a352b724c12e2c08ee8ff616045341d3`；返回工作簿SHA-256：`dd58dcd1ceaff5b754b77e3e23f20523699d4681c4ca1ad70e982673446a8b11`。样本、C3、题面摘要仍为HANDOFF所列版本。

| 与AI草稿不同的题 | 格数 |
| --- | --- |
| speech_act | 10 |
| names_brand | 7 |
| echoes_post | 7 |
| detail_level | 16 |
| register | 15 |
| reader_value | 27 |
| arranged | 10 |
| 合计 | 92 |

其余188格相同。这个对账不是Jev命中率，也不是两位真人的一致率。返回原判完整保留，不因AI认为不同就回改。

返回备注中有与C3显式规则不同的解释，例如把品牌名本身当具体细节、明确剂量仍判无；品牌/通用药名/机构及可行动信息/判断依据也有边界差异。它们作为参考局限记录，`definition_conformance_accepted=false/human_gold_ready=false/release_accepted=false`。后续报告称“与单人返回参考的一致性”，不把不一致全部算模型错误；如需裁决另建版本，不改题追分。

## 独立40条入口

`scripts/evaluate_comment_holdout.py`为专门入口，严格40条/280格；旧100条loader仍严格100/700。共用汇总函数按已验证的case数量计分，不放宽旧参考的摘要/来源/题面约束。

- 调用前核对参考、样本、旧开发样本、C3题库/模型、逐条题面、原返回工作簿的摘要及来源。
- 从固定样本重新生成state/fill/题面核对；40帖唯一、同项目同文唯一，且与旧100条帖子/同项目原文零重叠。
- 固定分母，缺答/无效仍计错，歧义单列；背书误报、漏报都报。旧100与新40参考/样本不同，不作总分差的统计提升声明。
- 每条原子checkpoint；只复用完全同绑定、live且7题完整有效的结果，完整恢复无需凭据/新许可；跨参考/题库/来源拒绝。
- `--validate-only`零调用；有待调用的case须有该批owner许可记录，绑定全部摘要和唯一输出目录。无许可退出5，无调用凭据退出4；输入错误2、部分失败1、全有效但不全一致3、全一致0。
- 输出必须在Git库外，非空目录拒绝覆盖；无数据库/开关/验收写入。模型错误不输出自由文本异常，报告不含原文或返回备注。

```bash
python scripts/evaluate_comment_holdout.py \
  --reference /private/reference.json --manifest /private/manifest.json \
  --questions /private/filled-questions.json --sample /private/sample.json \
  --development-sample /private/original100.json \
  --review-workbook /private/returned.xlsx --output /private/validation-new \
  --validate-only
```

新40条真实输入离线预检通过，Jev调用0。合成回归覆盖40/280、缺答固定分母、错误文本保护、来源/上下文/摘要/重叠拒绝、跨版恢复、无许可/仅预检零客户端、许可不能启动另一输出批次及双向背书风险。原100条评估回归同时保留，全量217项通过。

## 下一步

本次标签处理不扩展旧C1的一次试跑许可。待owner明确许可此次40条向既有Jev-1.13.0的C3独立复验后才执行；调用端凭据只用既有环境/进程内机制，原文不传GitHub。批次结果不得自动修改参考、C3、生产开关或主账本。

本地目录 `/workspace/annotation-packets/J03-health-20261015-holdout-reference/` 保存reference/manifest、92格差异表、返回备注、核验说明与validate-only状态；原Excel仍在上传附件目录。复验后更新本报告与TV待办；primary旧验收契约和评论上线门槛继续保留。
