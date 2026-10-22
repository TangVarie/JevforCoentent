# Jev 接手卡 · 2026-10-22

先读 [三仓总交接PR TV#215](https://github.com/TangVarie/truth-vault/pull/215) 的 docs/handoff-2026-10-22-next-owner.md；全局待办继续在TV OPEN-ITEMS。本轮Jev只修正交接和契约说明，没有新增服务代码或待发布实现。

## 当前状态与边界

- main为e00c183（#26交接PR已合并）。本轮生产/health复核：comment_tools_enabled=true、write_enabled=true、compile.key=true、compile.model=claude-sonnet-5-5、writer_keys=2。
- **评论线10-16已发布C4=comment_reader_v0.6，开关已开。** 旧10-15“评论关闭/C4不推广”等只作历史；不要重新做已完成验收，也不要把AI参考答案标人工。
- /compile_rules已经在线。project_layer_cleared通配已清，**Rx闸不变**；不能把项目层可用当成Rx放开。
- aw#121（替代#115）已合并7042cebc，生产仍需核实际部署；aw#117最终head fdeaa9f已过审/CI但未合并。当前等待是aw部署、015、运行配置与接手人的真实验收，不是Jev缺接口。

## 编译契约：这次不需要改实现

Jev规则按(id,sha)返回，SHA与题号由规则文本/适用范围/题目指纹决定。代码检查仍由aw执行，Jev只校验生成的模式；**现在双方都限制pattern最长300字**，旧说明“只有Jev限300、aw不限”已过时。已用148个合成模式（含300/301边界）对拍放行/拒绝，差异0。

aw执行期限时、缺regex报告未查、URL话题和词边界修复只发生在执行端，不要求Jev照搬超时引擎。后续真的修改静态安全闸才重新做双端对拍，不因本次交接重复改逻辑。

#117已覆盖：手设检查与编译code独立计数、questions坏题/重复qid/空题库明确未查、部分覆盖不称守住、全部未判计入汇总、排队起跑时重算共同截止、global/promote/unmute/方向变化后补编。不要退回只按编译code推断手设检查存在的旧做法。

## 联调时配合什么

1. 接手人先用自己已有运行身份；我的环境缺Railway/写作台身份不代表你的也缺，既有上线授权无需再问。使用服务上下文里的JUDGE_*变量，不打印或复制密钥。
2. aw#117部署前先015、关闭服务自动补编；CLI dry-run再真编，一次最坏6分钟/批，后台开着时不要另外跑compile-rules --force或CLI重复编，避免重复计费。
3. 调用方核真实owner/project/TV映射、出境口径、模型非mock及缓存已落库。403/503/timeout/partial/not_checkable不是全部覆盖通过；用aw真实鉴权check_rules与实际保存版本验收，不能用健康检查替代。
4. Gate2 combo仍未过，不升级L2；既有金标和人工作业保持原有身份。10-23探索臂及周一灯仍由总清单追踪。

本PR只交接文档，不重跑C3/C4、不重新发生产验收、不触发额外批量模型调用；总交接列出了尚未执行的数据动作和回滚。
