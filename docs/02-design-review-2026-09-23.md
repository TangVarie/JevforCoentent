# 02 · judge 设计审查（2026-09-23）

> **对象**：上传包 `812beb15-judge.zip`，原样导入为本仓第一个 commit（未改一个字）。对照三仓当天 main：truth-vault `d7615d5`、autowriter `b09df8e`、sanshengliubu `cab7750`，与 docs/31 页首写的三个 commit 逐一相同，没有版本漂移。
> **方法**：读完全部源码、题库、fixtures、文档；本地跑 22 个测试；用机器上现成的 PostgreSQL 16 真跑 TV 的 v1_13 账本表 + 本仓 v1_17 + v1_18，再把三个脚本 mock 生成的账本 SQL 灌进去；联网核 TypeSafe 官方文档、TikHub OpenAPI、GitHub Actions 文档与 runner 源码；对三仓逐条打开文档引用的每个 file:line；七个维度并行审（core / loop+comments / 外部语料与基础设施 / TV / 写作台 / 三省六部 / 文档与题库），每条中高严重度发现另起怀疑者复核。
> **复核状态**：七个维度共报 94 条，其中 23 条（high 全部 + medium 的 bug / mismatch）送两路怀疑者复核；跑完 7 条时撞上会话额度，7 条全部成立、零驳倒（其中 4 条被「实际后果」视角降级，本文按降级后的口径写）。其余 16 条没跑复核的，我逐条自己核了一遍：v1_18 视图用真实 PostgreSQL 16 跑、`exec_sql` 与密钥变量名用全仓 grep、金标准处数用 diff、写作台 D-071 与 worker 的 subprocess 包装直接打开原文，其余按源码逐行读。
> **一句话**：设计立得住，三个仓库的现状与文档写的基本一致（§3）；挡在「跑起来」前面的是本仓自己的十来处硬伤（§1），加上几处设计里说了、代码里没有或做反了的事（§2）；文档对写作台那条「写手抽完就散场」读反了 D-071，影响的是入库判定的覆盖面（§4 #2）。三仓这边不需要先改什么，接入时各自要动的点见 §5。

---

## 0. 一页读完

- **能跑起来之前要修的**：`mcp` 装到 2.x（2 个测试挂、MCP 起不来）；v1_18 的视图在 PostgreSQL 上建不起来；写库脚本调一个 TV 里不存在的 RPC，每周一那次 workflow 会红；`bank_sha256` 与 TV 的规范化摘要不是一个值，冻结题库那天账本会被劈成两批；`/judge` 串行处理，写作台 8 秒超时一定打满；`/judge` 的接口装不下设计写的「四层题库 + 修改单」；`JUDGE_API_KEY` 没配就是公网免鉴权；TikHub 翻页不带会话参数、一半搜索费买重复。
- **设计说了、代码没做或做反的**：「未过闸二只记录不下发」的题实际下发到了生成端 prompt；暗题没有任何机制；brief 里的 P0 硬约束编译成题后没接到判定；评论区「有没有摩擦」那题不参与换位；数据出境没有开关。
- **与 TV 答题契约的差异**：`scope=full` 的题跳题口径、全部跳题时仍打一次空调用、`with_evidence=false` 会落「答是无证据」的行、`JUDGE_MOCK=1` 会把假答案按 `jev:1.13.0 / primary` 写进真账本。
- **文档读反的一处**：D-071 的实查结论是写手**没散场**，继续用写作台发牌，但稿子在外面写、发布后由 tv-sync 补录，不过 `commit_drafts`。把入库判定只挂在 `commit_drafts` 上，覆盖不到那些项目。
- **属实的**：docs/00、docs/01、docs/31 对三仓的几十处引用（行号、常量、决策编号）逐条核过，除引用编号偶有偏差外全部对得上；Jev 与 TikHub 的端点、鉴权、字段与官方文档一致。
- **SQL 链路**：v1_17 幂等；改好视图后，`fq_shadow`（1,000 行）、`backfill_comments`（297 行）、`external_corpus`（3,744 行账本 + 156 行外部笔记）三份 mock SQL 都能幂等灌入。

---

## 1. 先修：本仓自己的硬伤（不修跑不起来，或跑了也白跑）

> **修复记录（同日第二版）**：下面十三项已全部修掉，表格保留作当时的诊断。改了什么：
> #1 `requirements.txt` 钉 `mcp>=1.2,<2`；#2 v1_18 视图改成分位数 CTE 再 JOIN，并按 `question_version / extractor` 分组（PG 16 连跑两遍、能查）；#3 `banks.bank_sha256` 改用 vendor 进来的 `tv_feature_bank.bank_digest`，内联题库对规范化 JSON 算，测试钉住「冻结不改 digest」；#4 删掉 `apply_sql.py`，新加 `scripts/apply_rows.py` 经 PostgREST upsert（外部笔记表在前、账本行在后），`external_corpus.py --rows` 出 rows.json，workflow 改成 job 级 env、先上传产物再写库、写库失败不让 job 红；#5 `/judge` 按 subject 用线程池并行（`JUDGE_WORKERS`，默认 4），输入形状整批先校验，单个 subject 的 Jev 失败只带 `error`、全部失败才 502；#6 新加 `POST /judge_draft`（多层题库 + brief 现编项目题库 / 内联题库 + hard_rules + 修改单 + 账本行），`loop.judge_draft` 加 `subject_id / subject_type` 并保留每层的 `JudgeResult`，`compile_project_bank` 配套 `project_hard_rules` 把 brief 的 `want` 接到判定，`/judge` 也把账本行原样带回；#7 鉴权 fail-closed（没配 `JUDGE_API_KEY` 一律 503，`JUDGE_ALLOW_ANONYMOUS=1` 才放行，key 比较用 `hmac.compare_digest`），`/health` 回显 auth 模式；#8 `TikHubClient` 记住首页返回的 `search_id / search_session_id`，第 2 页起回传；#9 单页搜索、单条笔记的异常记进 `rep.errors` 继续跑，意外异常也写 `stopped_reason` 并保留产物；#10 `seen` 只在有结局（分诊拒绝 / 太短 / 入账本）后写，因上限、预算、报错没看的不写，每次 / 每月上限到了整个品类停搜；#11 `--mock` 的 state 默认落临时目录，`dry_run` 不写 seen，`state/` `out/` 进 `.gitignore`；#12 `SHA256SUMS` 改相对路径，CI 那步去掉 `--ignore-missing` 与 `continue-on-error`；#13 全仓改用 `SUPABASE_SERVICE_ROLE_KEY`。顺手：`.env.example` 按代码实际读的变量重写（去掉没有读取点的 `JEV_MODEL`、错名的 `SOCIALDATAX_API_KEY`），报告里「分诊通过 / 太短丢弃 / 打标 / 出错」分开计数。测试 22 → 28。
> **第二轮（对这次修复的对抗评审后）**：`/judge_draft` 的 `hard_rules` 布尔值按 brief 同款口径归一成「是」/「否」（此前 `false` 会把答「否」判成硬伤）；brief / 内联题库形状错、编出的题库 id 撞车、`project_bank_name` 与某层同名、brief 与 `project_bank` 同时给、一份题库都没有，都是 422 而不是 500 或静默通过；认不出层的题库名回显在 `ignored_banks`；`/judge` 里非 `JevError` 的异常也只让那一个 subject 带 error；`write=true` 在调 Jev 前先查写库配置。外部语料：脚本对提前停止只打 `::warning::` 不改退出码（否则 job 红、cache 不存 state）；首页没拿到翻页会话或搜索失败就不再花钱翻这个关键词的后续页；`--mock` 的 state 每次新建临时文件；`fetched_at` 显式写进行里，SQL 与 PostgREST 两条路冲突更新同口径；`apply_rows.py` 写库失败打出前置（v1_17 / v1_18）。视图先 `DROP VIEW IF EXISTS` 再建并多按 `bank_sha256` 分组。三个新测试的断言收紧到「改坏代码测试必红」：真实 `JevError` 路径 + Barrier 证明并行、brief 的 `want` 无条件断言、取全文报错的笔记不进 seen。
> **第三轮**：GitHub 上 `external-corpus.yml` 每次 push 都起一个立刻失败的 run——文件本身解析不了：zip 原版就把 `key: external-corpus-state-${{ github.run_id }}` 写在 `{…}` 流式映射里，`${{` 被当成嵌套映射，GitHub 拒绝整个文件（我第一轮沿用了这个写法）。改成块式 + 引号，并加一条「所有 workflow 文件必须能被 YAML 解析」的测试。

| # | 事 | 证据 | 后果 | 修法 |
|---|---|---|---|---|
| 1 | **依赖装到 mcp 2.x** | `requirements.txt:4` 写 `mcp>=1.2`；干净环境装到 2.x，`judge/mcp_server.py:16` 的 `from mcp.server.fastmcp import FastMCP` 抛 `ModuleNotFoundError`（2.x 改名 `MCPServer`） | 本地 22 个测试挂 2 个；`.github/workflows/ci.yml` 第一次 push 就红；`python -m judge.mcp_server` 起不来 | `mcp>=1.2,<2` |
| 2 | **v1_18 的视图建不起来** | `migrations/notes_v1_18_external_notes.sql:39-40` 在有序集聚合上加窗口：`percentile_cont(0.75) WITHIN GROUP (…) OVER (PARTITION BY …)`。PG 16.13 实跑：`ERROR: OVER is not supported for ordered-set aggregate percentile_cont` | 不加 `-1` 时表建了、视图没建；用 Supabase SQL 编辑器 / `apply_migration` 的单事务跑则整份回滚，表也没有。docs/00 #11 与 README 指望的 `v_external_reference` 不存在 | 分位数按品类 `GROUP BY` 算成 CTE 再 JOIN（文末附已验证的写法） |
| 3 | **`bank_sha256` 与 TV 不是同一个值** | `judge/banks.py:73-74, 108` 对整文件算 sha256（`9ef56d5…`）；TV `scripts/feature_bank.py:87-101` 的 `bank_digest` 先剔掉 `status:` / `frozen_sha256:` 两行再算（同一文件得 `3d299a1…`），`annotate_feature_pass.py:125`、`ingest_gate1_answers.py:177` 落库用的是后者。yaml 现在是 `status: draft` | 同一份题库 judge 与 TV 写进账本的 `bank_sha256` 不同；冻结那天改 `status`、写回 `frozen_sha256`，judge 的 `jev:1.13.0` 行会被劈成冻结前后两个校验和，`v_feature_contrast`（`notes_v1_13:105,114` 按 `bank_sha256` 分组）两批互不汇总，docs/28 附录 B「按 frozen_sha256 挑冻结那份」也挑不到 judge 的行 | 落库列改用 vendor 进来的 `tv_feature_bank.bank_digest`（`spans.py` 已加载该模块）；`SHA256SUMS` 继续钉整文件 |
| 4 | **写库脚本调的 RPC 不存在** | `scripts/apply_sql.py:16` 调 `rest/v1/rpc/exec_sql`；TV 全仓（schemas / scripts / docs / workflows）没有这个函数。TV 自己写库走 supabase-py 的 `.schema("truth_vault").table(...).upsert(...)`（`ingest_gate1_answers.py:298-300`），迁移经 MCP `apply_migration` | `external-corpus.yml:33` 那步的 `if: env.SUPABASE_SERVICE_KEY != ''` 本身能生效（变量在同一 step 的 `env:` 里，runner `StepsRunner.cs:122-129` 先合并 step env 再评估条件），所以只要配了 secret 就会跑到 `apply_sql.py` → PostgREST 404 → `sys.exit` → job 红 → `upload-artifact`（默认 `if: success()`）跳过、`actions/cache` 不保存 state，下周重新花钱搜同一批 | 删掉 `exec_sql` 路线：账本行复用 `core.postgrest_upsert`；外部笔记表同样 `POST /rest/v1/external_notes`（`Content-Profile: truth_vault`，`on_conflict=note_id`）；先上传产物再写库 |
| 5 | **`/judge` 串行，对不上 8 秒超时** | `judge/api.py:105` 对 subjects `for` 循环逐个调 Jev；fq 题库每篇 2 次调用（判题 + 证据）；实跑单次 521 ms、最慢 853 ms（docs/fq-shadow-run）；docs/00 #4 定「超时 8 秒即跳过」，docs/31 §5.2 说「一批 10 篇并行也是一两秒」 | 10 篇 × 2 × 0.5–0.85 s ≈ 10–17 秒，必超 8 秒；影子期会一直记 `judge_status = timeout`，账本里什么都落不下。三省六部采样 60 篇一次请求约 100 秒，不是「几十秒」 | `/judge` 内按 subject 开线程池（`scripts/fq_shadow.py:119` 已有 4 路写法）；影子期 `with_evidence=false` 减半 |
| 6 | **`/judge` 装不下设计写的挂法** | `api.py:69-71` 只收一个 `bank` 名，只从 `banks/` 目录按文件名加载（`:38-49`）；返回只有 `results / rows（行数）/ written`（`:129-130`），没有硬伤、没有修改单、`write=false` 时也不回 SQL 或行。`loop.judge_draft` / `repair_plan` 只在 MCP 里有 | README「commit_drafts 调 /judge（fq + platform + 项目题库）…返回里带修改单」、docs/31 §2.1「否则调用方拿 SQL」在现有接口上做不出来：项目题库是按 brief 现编的，HTTP 传不进去；deskcore 也没有题库文件去算「退回理由」；judge 不配 Supabase 时答案原地丢 | 加 `POST /judge_draft`：收 `banks` 列表 + 内联项目题库 + `hard_rules`，包 `loop.judge_draft` + `repair_plan`，返回 `hard_fails / plan / rows`（或 SQL） |
| 7 | **鉴权 fail-open** | `api.py:52-55`：`JUDGE_API_KEY` 为空时 `require_key` 不抛；`/health` 不回显鉴权状态。TV 三个服务已按 SUP-001 改成 fail-closed（`service_auth.py:93-105`「匿名放行等于把它交给公网」） | Railway 上漏配一个变量，服务照常起、`/health` 照常绿，任何人都能用服务端的 `TYPESAFE_API_KEY` 调 Jev（每请求最多 200 个 subject），配了 Supabase key 还能 `write=true` 往账本塞任意行。README 写「GET /health（X-Judge-Key）」与代码相反 | 照 `service_auth.resolve`：未配且未显式 `JUDGE_ALLOW_ANONYMOUS=1` 一律 401；`/health` 加 `auth` 字段 |
| 8 | **TikHub 翻页没带会话参数** | TikHub OpenAPI 对 `search_notes`：「首次请求只传 keyword 和 page；翻页请求传入首次返回的 search_id 和 search_session_id」。`judge/external.py:149` 每页只传 keyword / page / sort_type / note_type | `pages_per_sort: 2` 的第 2 页要么重复第 1 页、要么当新搜索回；默认 80 页里 40 页是花钱买重复（约 0.4 美元/次），覆盖只有计划一半 | `search()` 从首页返回取 `search_id` / `search_session_id`，page ≥ 2 回传 |
| 9 | **外部语料一次异常全丢** | `external.py:226-275` 的 `try` 只 `except BudgetExceeded`；`judge_state`（`:250`）、`judge_note`（`:265`）重试用尽会抛 `JevError`，`urlopen`（`:145`）的 429/5xx 没人接；`scripts/external_corpus.py:73-83` 要 `run_once` 正常返回后才 `save_state`、写报告和 SQL | 钱在请求前已扣（`:136`），第 N 条一失败，state 不保存、三份产物不生成、workflow 的 artifact 与 cache 也跳过。mock 实跑 1,600 次分诊 + 312 次打标全串行，按 0.4 秒/次约 11 分钟，离 40 分钟 `timeout-minutes` 不远 | 每条笔记的分诊 / 取全文 / 打标各包一层 `except` 记错继续；`save_state` 与写 SQL 放 `finally` 或按品类增量落盘 |
| 10 | **去重状态写早了、月上限 break 层级不对** | `external.py:247` 先 `seen[...] = …`，`:254` 才检查每次 / 月上限，`:258` 取全文抛预算异常时已在 seen；`:236-237` 页循环只查 `kept_this_run` 不查 `monthly`，`:254` 的 `break` 只跳出笔记循环 | 被上限或预算截掉的笔记永久标「见过」，第二周直接算重复：月度 160 篇的分批设计退化成只看每次搜索页前几十条。月上限到了以后同品类剩下的 关键词 × 排序 × 页 照样逐页付费 | seen 只在真正分诊后写；月上限命中 `continue` 到下一品类 |
| 11 | **mock 污染真实 state** | `scripts/external_corpus.py:55-56` 只有「dry_run 且非 mock」才提前返回，`:73-74` 之后无条件 `save_state` 到 `state/external_corpus_state.json`；`external.py:247` 在 `if dry_run: continue` 之前写 seen，`:272` 累加 `monthly`；`.gitignore` 没有 `state/` | 按 docstring 示例本地跑一次 `--mock`，state 里多 1,600 个假 id 和每品类 24–40 的月度计数，当月真实运行的 160 篇上限被吃掉四分之一；随手 `git add` 就进仓库 | mock 默认 `--state` 指临时文件；`state/` 进 `.gitignore`；`monthly` 按 provider 分 |
| 12 | **CI 的 vendor 棘轮空跑** | `banks/vendor/SHA256SUMS` 两条路径是另一台机器的临时绝对路径；`ci.yml:11` `sha256sum -c --ignore-missing` 一个文件都没核到，以「no file was verified」退出 1，被 `:12` 的 `continue-on-error: true` 吞掉 | 谁改了 vendor 的题库或切片器，CI 不会红；这一步现在每次都「失败」且打出「vendor 被改了」的错误结论。真正起钉住作用的只有 `tests/test_judge.py:27-32`（按 basename） | 改相对路径 `banks/vendor/…`，去掉 `continue-on-error` |
| 13 | **Supabase 密钥变量名与 TV 不一致** | 本仓全部用 `SUPABASE_SERVICE_KEY`（`api.py:125`、`fq_shadow.py:74`、`backfill_comments.py:46`、`external-corpus.yml:26/36`、`.env.example:5`）；TV 全仓 71 处用 `SUPABASE_SERVICE_ROLE_KEY`，且 workflow 里显式 `[ -n "$SUPABASE_SERVICE_ROLE_KEY" ] || exit 1` | README 说 worker 的 `/annotate-features` 改调 `/judge`，两个服务会共用 Railway 变量；名字不同就得双份维护，配错时 `/judge` 的 `write=true` 直接 503、workflow 的写库步安静跳过 | 统一成 `SUPABASE_SERVICE_ROLE_KEY` |

**#2 的改法**（当时的草案，已被 `migrations/notes_v1_18_external_notes.sql` 里的正式版取代：正式版多按 `question_version / bank_sha256 / extractor` 分组，并先 `DROP VIEW IF EXISTS` 再建，因为 `CREATE OR REPLACE VIEW` 不允许改列序。**不要直接执行下面这段**，留作说明）：

```sql
CREATE OR REPLACE VIEW truth_vault.v_external_reference AS
WITH eng AS (
    SELECT e.note_id, e.category,
           (COALESCE(e.liked,0) + COALESCE(e.collected,0) + COALESCE(e.comments,0)) AS engagement
    FROM truth_vault.external_notes e
), cut AS (
    SELECT category, percentile_cont(0.75) WITHIN GROUP (ORDER BY engagement) AS cut
    FROM eng GROUP BY category
), grp AS (
    SELECT eng.note_id, eng.category,
           CASE WHEN eng.engagement >= cut.cut THEN 'top' ELSE 'rest' END AS grp
    FROM eng JOIN cut USING (category)
)
SELECT g.category, a.question_id, a.answer, g.grp, COUNT(*) AS n,
       ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER (PARTITION BY g.category, a.question_id, g.grp), 3) AS share
FROM truth_vault.note_feature_answers a
JOIN grp g ON g.note_id = a.subject_id
WHERE a.subject_type = 'external_note' AND a.run_tag = 'external' AND a.answer IS NOT NULL
GROUP BY 1, 2, 3, 4;
```

顺手：这个视图按账本行数而不是笔记数计（`notes_v1_18:45-50` 不按 `question_version` / `extractor` 分组），`JEV_MODEL` 升级或题目改版后新旧两套行同时进分母；TV 自己的 `v_feature_contrast` 是分组的。加 `GROUP BY question_version, extractor` 或只取每篇每题最新一行。

SQL 链路其余部分是通的：v1_17 连跑两遍结果一样，自动约束名 `note_feature_answers_subject_type_check` 与 `DROP CONSTRAINT IF EXISTS` 对得上（`content_scores` 的同名 CHECK 没跟着扩，若 comment / ssll_sample 将来要打分会撞）；`fq_shadow`（50 × 20 = 1,000 行）、`backfill_comments`（33 × 9 = 297 行）、`external_corpus`（mock 156 篇 × 24 题 = 3,744 行 + 156 行外部笔记）三份 mock SQL 全部幂等灌入，改好的视图能查出分布。

---

## 2. 设计说了、代码没有的，或做反了的

| # | 设计原文 | 代码现状 | 影响 |
|---|---|---|---|
| 1 | **「fq 题的正向写法只对过了闸二的题下发」**（`loop.py:13` 模块头；docs/31 §5.4；§2.2「通用层的题不原样塞进生成端的 prompt」） | `loop.py:214-218` 对未 validated 的 fq 目标题仍 `plan.append(...)`，只是把 instruction 文案改成「此题未过闸二，只记录不下发」；`loop.py:236-237` 的 `repair_prompt` 把 plan 里每一条都拼进发给生成端的 prompt，不看 kind；`produce()`（`:286-287`）直接用它修补。实跑：修改单末尾出现「『正文里有没有某个人说的原话？』现在是『否』（目标『是』，此题未过闸二，只记录不下发）」 | 题干原文、当前答案、目标答案都到了生成端，只有定义没去；这正是 docs/31 §2.2 / §8 要防的「写手对着题库写」。今天只有 `produce(target=…)` 这条路会走到它（MCP 的 `repair_plan_for` 与 HTTP 都不传 target），所以是设计缺口而不是线上事故；但 target 正是闸二之后「目标画像进入库判定」（docs/31 §9.1）要用的参数，到那一步就会现原形。未 validated 的条目不该进 plan，只进 trail |
| 2 | **暗题**：「人感题库的 1/3 不写进任何 prompt，每季度换」（docs/00 #4 记为已拍板；docs/31 §2.2、§8、§9.2 第 4 条） | 题库格式没有 hidden 字段（`banks.py:29-41` 的 `Question` 字段、`_load_jev:111-126` 读的键都没有）；`loop.repair_plan:221-229` 把三道人感题的语义直接写进指令；`mcp_server.list_banks:48` 列出全部题 id，`judge_draft:63-64` 返回 `detail` 里所有题 | Goodhart 三道保险里唯一能在代码里做的一道没有落点，docs/00 把它写成事实而非待办。要做：`Bank` 加隐藏集合 + 轮换记录；修改单、`list_banks`、`detail` 输出按它过滤 |
| 3 | **brief 的 P0 硬约束编译成题**（docs/31 §5.4「生成端拿到的约束和判定端用的题是同一套定义」；docs/00 #9「项目层由写作台按 brief 编」） | `compile_project_bank:46-48` 把 `want` 只写进 `feeds` 文案；`judge_draft:155` 判硬伤只看另一个独立参数 `hard_rules`；`produce:276-282` 同样外部传入；`mcp_server.judge_draft:55-62` 没有 brief / hard_rules 参数，`repair_plan_for:73-74` 连 `project=` 都没传 | brief 里 `want=False` 的硬约束编译后只是一道普通题，调用方漏建 `hard_rules` 就等于没有硬约束；写手侧 MCP 根本送不进项目题库。`compile_project_bank` 应同时返回 `hard_rules`，MCP 加参数 |
| 4 | **数据出境**：「未发布稿默认只跑通用层 + 平台层；处方药项目的未发布稿不跑」（docs/00 #7） | `/judge` 与 MCP 都没有项目 / 品类 / 发布状态的开关 | 任何调用方都能把处方药草稿连项目题库一起发出去。要么 deskcore 调用侧按 `tv_project_map` 查品类，要么 `/judge` 加 policy；现在两边都没有 |
| 5 | **评论区 4 题「不过就换位」**（docs/01 §4；`comments.py:10`） | `judge_thread:296-301` 只把 praise_share / same_template / thread_arranged 当硬伤，`has_friction = 否` 不算；`thread_repair_plan:317-327` 也没有它的换位规则。实跑四题为「一半左右 / 否 / 否 / 否」→ `passed=True` | 默认组有贴主回复位所以通常有摩擦；brief 自定义没有回复位时，一个全是单向评价的评论区会整体通过。`praise_share = 一半左右`（25%–75% 夸）也放行，文档没说这是不是预期 |
| 6 | **修改单 = 题号 + 概率 + 要改成什么 + 那个选项的定义**（docs/01 §2） | `loop.repair_plan:203` 对硬伤一律 `want = "否" if ans == "是" else "是"`；`:205` 的 definition 取的是**当前答案**的定义。`hard_fails` 元组（`:157`）没存 want。实跑 choice 型硬约束 `product_role` 期望「未出现」、答「主角」→ 指令写「要改成『是』」，definition 是「主角」的定义 | `judge_draft` 支持任意期望答案，`repair_plan` 不知道期望值；noul 题给的也是「现在犯了什么」而不是 `comments.comment_repair_plan` 那样「要改成的选项的定义」，两个回路口径不一 |
| 7 | **「蓝词植入」勘误时从题里拿掉**（docs/00 #6） | `comment_ops_v0.1.yaml:34` 还在；`tests/test_judge.py:53-55` 钉着六值必须与 TV `comments.comment_intent` 的 CHECK 逐字相同（`notes_v1_2.sql:483`）。「拿掉」和「与表闭集逐字相同」互斥，文档没定选哪个 | 另外「33 条实跑一次没判出蓝词植入」的证据不干净：`backfill_comments.py:93-94` 只在有 `target_blue_keywords` 时才给 state 加蓝词清单，而 `fixtures/comments_ops_sample_33.json` 没有这个字段；同批数据反驳质疑 / 引导私信 / 其他也全为 0（`docs/comments-sample-33-summary.csv` 只有两列），运营侧题库整体只在两个值之间摆 |
| 8 | **Mode A 卫生**（docs/31 §2.3 第 10 条；README「state 里不放任何表现数据」） | 六个题库的中文题干与定义干净（用 TV 的 15 个 `PERFORMANCE_KEYWORDS` 逐词扫，命中全在注释和英文定义）；生产路径的三种 state 也干净。但 **`fixtures/thread_cases.json:7` 的 shared state 带「点赞 28，收藏 21，评论 33」**，`run_gold.py:78-82` 原样发给 Jev——评论区金标准 4/4 是带着互动数判的，与 `comments.thread_state:272-273` 的生产条件不同；`backfill_comments.py:93-94` 把 `target_blue_keywords` 放进 state（项目 / 品牌层信息，与 D-079「不给项目名、产品名」口径不一致）；本仓没有任何 hygiene 守卫（TV 的 CI 守卫只管 TV 自己的 `render_call`） | 删掉 fixture 里的「帖子数据」重跑 t1；`tests/` 加一条对所有题库文本与 state 键名的 `PERFORMANCE_KEYWORDS` 断言 |
| 9 | **「每题留出口」**（docs/31 §2.3 第 2 条） | 7 道选择题里 2 道没有：`human_feel_para_v0.1.yaml:43-66` 的 `para_function`、`comment_thread_v0.3.yaml:28-41` 的 `praise_share`，都没有 `unclear_labels` | 一段既讲事又讲产品、评论区只有一两条时只能硬选，`interpret` 永远不会因「选了出口」把它送进人工 |
| 10 | **529 按退避重试**（TypeSafe 官方 API 文档） | `jev_client.py:24` 的 `RETRY_STATUSES` 有 429 / 5xx / 52x，没有 529 | 高峰期 Jev 过载直接抛 `JevError` |

**与 TV 答题契约不等价的地方**（TV 是 `scripts/feature_bank.py`；本仓声称「按 TV 的答题契约」）

| # | 事 | 本仓 | TV | 影响 |
|---|---|---|---|---|
| a | `scope=full` 题的跳题口径 | `spans.py:55-56` 用正文长度判 `text_too_short`；`:53-54` 标题只查有没有 | `feature_bank.py:563-566` 对 full 用「标题+正文」长度；标题短于 2 个可见字记 `text_too_short` | 正文 <20 字但标题+正文 ≥20 的篇：TV 问 8 道 full 题、judge 记 NULL；1 字标题反过来。50 篇 fixture 上零差异（最短正文 25 字），影子跑扩大后会出现「一边 NULL 一边有答案」的假分歧 |
| b | 全部题都跳过时 | `core.py:97-100` 仍带着空 `questions` 打一次 Jev | `feature_bank.py:569-570` + `annotate_feature_pass.py:224-225` 零调用、只写 NULL 行 | 纯图片帖 / 只有话题标签的帖：最好白花一次调用，最坏 400/422 让整批（最多 200 个 subject）一起 502 |
| c | 答「是」必须有证据 | `core.py:105` 的 `with_evidence` 可关；`apply_evidence:70-73` 只处理返回里出现的题；`ledger_rows:163-165` 照写 `answer=是、evidence NULL、invalid_reason NULL` | `feature_bank.py:658-661` 空证据即 `evidence_not_found` | HTTP 传 `with_evidence=false` + `write=true`，或 Jev 漏答一题，账本里出现没过硬闸的「是」行，`v_feature_contrast` 只过滤 `answer IS NOT NULL`，它们直接进闸二分母 |
| d | mock 落真账本 | `api.py:101` 按 `JUDGE_MOCK` 建 mock client；`core.py:151` 的 extractor 取自题库 model，不看返回里的 `"model": "mock"`；`run_tag` 默认 `primary` | — | `.env.example:7` 就有 `JUDGE_MOCK=0` 这一行；部署误留 `=1` 时 `write=true` 会把 sha256 伪随机出来的答案按 `jev:1.13.0 / primary` 写进 `note_feature_answers`，与真实行同主键、直接覆盖、进分析 |
| e | 证据长度 | `core.py:82` 存整句（可上百字） | `EVIDENCE_MAX_CHARS = 30`，超长记 `evidence_too_long` | 同一列混着 ≤30 字片段与整句；TV 若用 `validate_answers` 复核 judge 行会全判超长。是有意的设计变更，但口径没写明 |
| f | 漏答与异常结构 | `banks.py:198-199` 漏答直接没有 item（少一行）；`:207-220` 概率为空时落 `answer NULL、prob 0.0、invalid_reason NULL` | `feature_bank.py:650-651` 记 `missing` | 按 subject 判「已答」的续跑会以为答过；账本出现无原因的 NULL 行 |
| g | 证据规则解析 | `banks.py:92-103`：bool 一律 `on_true`，choice 只对 `product_role` 硬编码 | `feature_bank.py:609-622` 通用解析：choice 默认要证据，只有「不需要」或「选『X』以外」豁免 | 现有 20 题两边逐题等价；新增一道 choice 题就可能错，没有测试钉住 |
| h | 无效行的 `prob` | `core.py:163-165` 无效行仍带 prob | 无效行 prob 为 NULL | 不影响闸二（过滤 answer），读账本的人要知道 prob 非空不代表答案有效 |

---

## 3. 与三仓对照：属实的

这一节是「文档说的，代码里在不在」。全部亲自打开文件核过，引用号偶有偏差的写在 §4。

**truth-vault**

| 声明 | 证据 |
|---|---|
| ci.yml 离 505,000 字节棘轮只剩约 1.3 KB | `ci.yml` 503,708 字节；上限在 `scripts/check_system_map.py:322` `CI_BYTES_CAP = 505_000` |
| 闸一名单分隔符不一致，NUC_phase1_recv46LaDAdFFc 的 11 个灰格答案入了库 | `build_gate1_human_sheets.py:329` `"|".join(r["skipped"])`；`ingest_gate1_answers.py:147` `.split(",")`；`data-analysis/gate1-human-sample-2026-09-28.csv:90` 那篇 11 个 id 用 `|` 连、标注员 C；`DECISIONS.md:5146` C 表 1000 行（A/B 各 999）——在现有代码下只有「11 格带答案入库」这一条路能得到 1000 行 |
| `prob` 口径不统一 | `ingest_gate1_answers.py:55` 只从「拿不准」列解析，bool 存 P(是)、choice 存第一名；`annotate_feature_pass.py:127` 生产抽取 `prob: None` |
| 续跑判据按 `llm:%` | `annotate_feature_pass.py:301` `extractor = f"llm:{model}"`；`:97-98` 默认 `like("extractor", "llm:%")`；`count_unannotated_features.py:37` 同 |
| Pattern A 同行「 7. … 8. …」并成一条，33 条里 4 条 | `sync_comments_from_raw_extra.py:134` 按 `splitlines()`、`:103` 只去行首编号；构造例实跑复现；`fixtures/comments_ops_sample_33.json` 4 条 `comment_text` 含「 7. … 8. …」 |
| 账本表结构、主键、`extractor` 允许 `jev:<模型>`、`prob` 留位；CHECK 只有 note / aw_version | `notes_v1_13_content_features.sql:19-34` |
| `v_l2_labels` / `v_feature_contrast` 天然排除非 note | `notes_v1_15:56-79` 只基于 notes；`notes_v1_13:97-98` `subject_type = 'note' AND run_tag = 'primary'` |
| comments 表六值、`comment_role` 五值、`contains_blue_keyword`；没有 `source` / `is_truncated` | `notes_v1_2.sql:479-491` |
| 「9,035 行三列全 NULL」「约 5,000 条读者评论未入库」 | `data-analysis/unified-feishu-table-assessment-2026-09-22.md:314-315` |
| `backfill_comments.py` / `fq_shadow.py --from-db` 用到的列全部存在 | `comments.content / comment_role / comment_order / is_pinned`、`notes.note_id / project_id / title / raw_content / target_blue_keywords` |
| vendor 的 fq 题库与切片器「原样」 | 与 TV `prompts/feature_questions_v0_1.yaml`、`scripts/feature_bank.py` sha256 相同 |
| `build_spans` 返回 title / first_sentence / last_para / body / full / `_truncated` / `_title_how`；`visible_len`、`MIN_BODY_CHARS = 20`、截断规则同口径；20 题的证据翻译逐题一致；50 篇 fixture 跳题零差异 | `feature_bank.py:32, 370-387, 563-566, 609-622, 666` |
| 馆员：`_select_via_llm`、`cache_key` 含 `draft_topic`、编造 id → degraded | `librarian/core.py:78-85, 173-180, 291, 311` |
| D-079 / D-081：`jev:1.13.0-A/-B/-C`、`run_tag gate1-20260928`、κ 0.91–1.00、999/999/1000 行 | `DECISIONS.md:5046-5047, 5122-5128, 5146` |
| 特征层 48–81 秒/篇、积压约 5,900 篇约 49 天、实际在用 opus-4-6 | `DECISIONS.md` D-076 / D-077 段、`:4172` |
| docs/28 §5.1 / §5.3 / §5.7 / §6 / §7.3 / §11 第 8 条 | 章节号与标题对得上 |

**autowriter**

| 声明 | 证据 |
|---|---|
| deskcore 零 LLM；守卫只禁 Anthropic 调用，HTTP 调 `/judge` 不会打红 | `deskcore/core.py:2642-2654`；`ci.yml:226-240` 用 AST 只禁 `get_anthropic_client / with_anthropic_retry / resolve_model / messages.create`；对 requests / urllib 无断言；`core.py:44` 已 `import librarian_client` |
| `SAFE_OK` 四项、`commit_drafts` 出错必须抛 | `ci.yml:92-103`；`tools.py:437-440` |
| 借卡客户端：同步 requests、`LIBRARIAN_TIMEOUT_SEC = 60`、fail-open 区分 Timeout | `librarian_client.py:29-150`；`config.py:128` |
| `check_drafts` 只查重（四路）；`validator.py` 未被 deskcore import | `core.py:741-760, 841-842`；grep 只在 `generation_service.py` 等 |
| `angle_ledger` 只记 `drawn_at / consumed_version_id`；`tv_note_links(version_id → note_id)`；`versions.id` UUID | `docs/deskcore.md:338`；`migrations/001_deskcore.sql:87-96`、`009_tv_links.sql:66-70`、`000_baseline.sql:168-169` |
| `draw_angles` 均匀随机；14,592 = 12 × 19 × 8 × 8；没有 15% 随机保留 | `core.py:435-451`；`vocab.py:170-173` |
| 版本 id 在入库后可拿到、判失败不回滚与现有纪律一致 | `core.py:1108-1111, 1243-1246, 1273-1274, 1290-1291, 1206-1211` |
| drafts 的字段名（title / body / version_id / angle_key）与 judge 的 draft 形状对得上 | `core.py:1067, 1082, 1110, 1253` |
| 09-10 途鸽 66 条里 49 条没带 angle_key；09-18 起交付即入库 | `core.py:1046-1052, 1267-1269`；`tests/test_deskcore_commit_gate.py:3-7` |
| MCP 客户端约 22 秒容忍 | `config.py:123-126`；D-074 |

**sanshengliubu**

| 声明 | 证据 |
|---|---|
| 辅助层用 anthropic SDK + Moonshot anthropic-compat；本仓 `AnthropicCompatGenerator` 兼容 | `kimi_client.py:4-5, 316-326`；`agents/__init__.py:149-153, 451-454`；`loop.py:66-81` |
| 二审只判主 critic 放过的 cell，模型 deepseek-v4-flash；顺序 主 critic → fail-closed → 二审 → prose_gate → 结构审 → 分流 | `orchestrator.py:4121-4322`；`config.py:851` |
| `multiplier_gate` 四项 pass/weak/fail、`template_test.still_holds` yes/partially/no | `vibe_critic.md:200-207, 278-283`；`quality_metrics.py:251-265` |
| `PIPELINE_STAGE_ORDER` / `REFINEMENT_MARKER_ANCHORS` 决定重跑删哪些 stage_log，新 stage 不登记会被旧快照盖掉 | `orchestrator.py:6021-6089, 6309-6324` |
| 采样只存 `opening[:60]` 与字数，正文不落库，只跑 `check_redlines / check_craft` | `batch_sampler.py:361-399` |
| `comment_seeds` 2–3 条、只查有没有 | `works_builder.md:75, 242`；`orchestrator.py:5720-5721, 5893` |
| ai_cliches 11 条；`_prose_soft_flags` 只写不读 | `orchestrator.py:5879-5883`、`:4260` |
| 「11 道闸，1 个样本」「5 池 + 人设轮换从来没被验证过」「放水票」 | `config.py:1700, 159-164`；`README.md:189` |
| 结构审 3 项 pass/incomplete；`reference_pack_analyzer` tone 11 / hook 9 / structure 6；SocialDataX 只给策略辩论看 10 条 | `kimi_structure_reviewer.md:31-76`；`reference_pack_analyzer.md:28-30`；`config.py:1471` |

**外部约定**（联网核）

| 声明 | 证据 |
|---|---|
| Jev：`POST https://api.typesafe.ai/v1/systemone`、Bearer、`state / model / questions`、choice 返回 `choice / confidence / probabilities`、noul 返回 `noul`、`usage.input_tokens / output_tokens`；choice ≤ 255 项；0.042 美元/百万输入 token、输出免费；250k token/s、1,200 次/分；`jev-1.13.0` 可钉版本 | TypeSafe 官方 API / models 页；与 `jev_client.py`、`banks.build_questions`、`banks.interpret` 一致 |
| TikHub 三个端点路径、Bearer、`sort_type` 有 `general` / `popularity_descending`、`note_type` 有 `普通笔记` | TikHub OpenAPI（1,050 个 path 逐一核） |
| GitHub Actions：同一 step 的 `env:` 在 `if` 里可见；`actions/cache` 只在 job 成功时保存、`restore-keys` 取最新一份 | runner `StepsRunner.cs:122-129, 201-221`；GitHub 官方 caching 文档 |

值得记一笔的三条官方口径：noul 清楚的「是」官方标 0.84–0.99、歧义 0.40–0.50，docs/31 §1.3 说自家跑出来「0.75 上下」——「阈值从数据定」这条纪律是对的；jev-1.13 的已知短板里有「state 里无关内容越多准确率越低」，本仓把四段一起放进 state、20 题一次问，实跑 90.7% 一致率说明可接受，但闸二前收紧定义时要记得这个变量；CJK「能处理但不如英文，需自测」，评论级与特征层两次实跑正好是这个自测。

---

## 4. 与三仓对照：不符、引错或找不到的

| # | 事 | 说明 |
|---|---|---|
| 1 | **D-071 读反了** | docs/31 §5.1 与 docs/00 #4 说「写手抽完就散场，commit_drafts 是每篇稿必经的唯一一步」。`DECISIONS.md:4205-4213`（D-071）原文：「第一版写的是『会话停在发牌之后就散了』——那是猜的，而且错了」；写手继续用写作台发牌，但写稿和交付走了别的路，成品发布后由 tv-sync 倒灌回来（sportsix 438 条 ingested 里 114 条是 9 月发的）；`deskcore/core.py:2428` 的 `_ingest_published_unlocked` 明写「没走 commit_drafts 的稿子补进库…不过闸」。09-18 的「交付即入库」是协议改动，之后有没有回到 commit_drafts 路径没有证据。把入库判定只挂在 commit_drafts 上，覆盖的是走写作台交付的那部分稿；sportsix / Hatherine 那种在外面写的项目，写前 / 写后判定都碰不到。另外「review_drafts 一次没被调用过」在 D-072（`:4250-4260`），不是 D-071 |
| 2 | TV 里没有 docs/30、docs/31 | README、docs/01、docs/31 引用 docs/30 共 19 处；docs/31 本身只在本仓。`jev-comment-unit/`、`iteration-log.md`、`raw_comment_v0.3.json`、`comment_bank.yaml`、`thread_bank.yaml`、`jev_unit_judge.py`（gold 与 v0.3 题库注释里的旧文件名）在本仓和三仓都不存在。docs/01 自称「能对着代码逐行核」，但实跑证据全在仓库外 |
| 3 | 「505,000 字节棘轮（D-075）」 | 数值对；出处是 `scripts/check_system_map.py:322`，D-075 正文只记 heredoc 块数棘轮（`DECISIONS.md:4813-4818`），没有字节上限 |
| 4 | 「D-081 的 12% 歧义」「on_demand 7 个项目约 2,419 篇」 | 12% 是按 `DECISIONS.md:5146` 的 prob 行数 123/124/113 推算，D-081 没写这个数；2,419 篇 / 7 个 on_demand 在 TV 仓核不到（D-081 只写 5 个项目 2,592 篇） |
| 5 | docs/01 §3「每版跑 fq + 平台 + 项目题库（一次调用，二十多题）」 | 代码是每个题库各一次调用、答是的题再各一次证据调用，最多 6 次（`loop.py:131-147`）；docs/00 #10「每篇 10–12 次」与代码相符 |
| 6 | docs/31 §3 ②「运营侧填 comment_intent / comment_type / is_scripted」 | `comment_ops_v0.1.yaml` 只有前后两题，没问 `comment_type`；`backfill_comments.py` 只写账本，不回写 comments 三列——回填后 comments 表层面「三列全 NULL」的现状不变 |
| 7 | docs/31 §1.1「画像模拟 click/skip/save、消费者模拟 stop/scroll：k2.6 + v4-flash 两路取交集」 | 交集的单位是「一个 backend 对某 cell 的 ≥3 个画像一致否决」（`orchestrator.py:3178-3198`），不是逐票；消费者模拟只调一路、只复判 `interest_align = weak` 的 cell（`:4948-5005`） |
| 8 | 「成本走 accumulate_auxiliary_cost」 | orchestrator 三处调用点确实这么做（`:4198-4206, 3803-3809, 1226-1232`），但 `agents/__init__.py:967-969` 的 docstring 写着「辅助层【不】走这里」，过时 |
| 9 | 「AI 黑名单是两份」 | 是五份：`orchestrator.py:5879`（11 条）、`prose_gate.py:80-85`（15 条）、`vibe_critic.md:252-255`、`vibe_rewriter.md:82`、`works_builder.md:130`；`orchestrator.py:5876` 注释指的 `works_builder.md:60` 现在是人设轮换规则 |
| 10 | 金标准「改了 7 处」 | diff cg-v0.1 → v0.2 是 8 处（多的一处是 c3 的 `arranged` 从「歧义」改成 true，且 `why` 仍写「期望 Jev 也犹豫」，与 expect 矛盾）；`docs/gold-comment-reader-v0.3.md` 标题写 cb-v0.3、期望值却来自 cg-v0.2-proposed，报告头不记 gold 版本，49/49 是对提议版而非勘误版；README 默认命令也用 proposed |
| 11 | README「50 篇笔记 + Opus 答案」 | Opus fixture 只覆盖 47/50（这正好解释 921 与 999 两个分母） |
| 12 | v1_17 注释「external_note 是 SocialDataX 抓的」「v1_16 之后执行」 | 拍板是 TikHub（`.env.example:6` 也写错成 `SOCIALDATAX_API_KEY`）；TV 的部署链里 v1_13 排在 v1_16 之后（`scripts/README.md:107-110`、`ci.yml:7216-7310`），v1_17 改的是 v1_13 建的表，照字面插在 v1_16 后会 `relation does not exist` |
| 13 | README / `.env.example` / workflow 的变量集合 | 代码读的 `TYPESAFE_KEY_FILE`、`TYPESAFE_BASE_URL`、`JUDGE_BANKS_DIR`、`TIKHUB_BASE_URL`、`ANTHROPIC_*` 三处都没写；`.env.example` 缺 `TIKHUB_API_KEY`；README 说 `JEV_MODEL` 可覆盖模型版本，代码里没有任何读取点（只出现在 docstring 和错误提示里） |
| 14 | `fixtures/comments_ops_sample_33.json` | 3 条正文自带「【素人评论】【贴主回复】【素人回复】」运营前缀，`comment_role` 全是「素人」（含贴主回复那条）；对抗性题看到这种标签会被带偏 |

---

## 5. 三仓各自要动的点（接入清单）

**truth-vault**

- **迁移落地**：v1_17 / v1_18 放进 `schemas/` 就触发 `ci.yml:308-334` 对 `scripts/README.md` Step 0 的两向对账，README 不加行即红；sql job 的「连跑两遍」是逐文件手写的（`ci.yml:7216-7217` 那种），不加就没有幂等测试。顺序写成「在 `notes_v1_13` 之后（当前链的最后）」。
- **续跑判据**：`annotate_feature_pass.py:301, 313-314, 97-98` 三处，但 `ci.yml:1296-1298` 直接断言默认路径「必须是 `llm:%` 前缀且不带 extractor」，改判据必改守卫；守卫在内联 heredoc 里，只剩 1.3 KB 余量，按 D-075 的规矩把逻辑挪进 `scripts/`。另外 `worker/app.py:286` 的 `_MODEL_RE` 放行冒号，若图省事从 `model` 参数传 `jev:1.13.0`，`:301` 会拼成 `llm:jev:1.13.0`，与本仓 `core.py:151` 生成的 `jev:1.13.0` 永远对不上。
- **worker `/annotate-features`**：是 subprocess 包装（`worker/app.py:334`），judge 不产 `code:v1` 的 8 个代码特征 + 3 道占位题，也不写 `note_features` 原值（`write_raw_counts`），所以 `/judge` 只能替换 `annotate_note:201-248` 里模型题那一半，`code_rows` / `raw_counts` 仍要 TV 侧跑；整段换掉 `v_feature_contrast` 里 `body_len_bucket` 等代码题会断。`ci.yml:1225-1248` 钉着「8 次调用 / 31 行 / 6 组 systemic」，改编排即红。
- **闸一分隔符**：`ingest_gate1_answers.py:147` 改 `split("|")`；已入库的 `NUC_phase1_recv46LaDAdFFc × jev:1.13.0-C` 那 11 行按 `invalid_reason` 标掉；CI 加一条多灰格往返测试。
- **Pattern A**：`sync_comments_from_raw_extra.py:134` 前按 `(?<=\S)\s+(?=\d{1,2}[.、]\s*\S)` 二次切行。注意修了切法再同步，旧的并行行按内容配不上，会被记成 vanished 但保留（`:319-326`），新切出的行以新 id 插入——回填会给新旧两批都判分，评论构成按篇统计会重复；同步后要人工清一次。顺手剥掉「【…】」前缀并据此修正 `comment_role`。
- **`fq_shadow --from-db`**：对全部项目用同一个 `--title-extraction`（`fq_shadow.py:93, 116-117`），而 TV 按项目 mapping 切（`annotate_feature_pass.py:209-210`；`mappings/TGV_phase1.yaml` 是 column 模式）。不按 mapping 切，一致率会被切法差异污染（正是 D-081 提到的「切段差异」）。`extractor=like.llm:*` 若同一篇有多个 llm 模型的 primary 行，`:84-85` 后写覆盖先写。
- **评论三列**：要么补 `comment_type` 题并在 backfill 里回写 comments 三列，要么把 docs/31 §3 ② 改成「答案落账本，不回写」。

**autowriter**

- **覆盖面先定**（§4 #1）：`commit_drafts` 挂判定只覆盖走写作台交付的稿；`_ingest_published_unlocked` 补录路径要不要也挂，或明确「补录稿只靠 TV ① 事后抽取」。
- **插入位置**：`core.py:1062` 的项目写锁与 `:1066` 的 `try` 包住整段，`:1407` `return out` 在两者之内，`:1408-1411` 的 `except` 会对整个 stash 无条件 `_restore_stash` 再 raise。判定若放在 1407 之前且没有自己的 try/except，一次 requests 异常就会把已替换稿的旧指纹放回去（新旧两版指纹并存，下一版判自己撞车），异常 re-raise 又让调用方重试撞上自己刚写的指纹；即使包了 try，锁内多等 8 秒也占同项目其它 commit / ingest 的 90 秒预算。正确位置：`return out` 改赋值，在 `with` 块退出后对 `out["version_ids"]` 判，自带 try/except + `requests.Timeout` 分支记 `judge_status`。`ci.yml` 对 `commit_drafts` 源码只钉 `_gate(`、embedding 两行、`embed_failed`，这样改不会红。
- **判定名单**：调用方自带 `version_id` 的稿不在 `version_ids` 里（`:1106-1111`），要用 `d.get("version_id") or minted_ids[i]` 补进；`identity_error` 的稿没有 versions 行，判了账本里的 `subject_id` 指不到 `versions.id`。
- **接口**：见 §1 #6。`loop.judge_draft` 把 `subject_id` 写死成 `"draft"` / `"draft:pN"`（`loop.py:132, 142, 163`），服务器侧一复用它落账本，所有稿子撞同一主键。
- **延迟**：「commit_drafts 现在约 1–2 秒」在仓库里没有依据（函数内无计时，tests 里也没有）；先加 `elapsed_ms` 攒样本再定 8 秒。上限是 MCP 客户端约 22 秒的实测容忍（`config.py:123-126`）。
- **台账视图**：三列类型一致（UUID ↔ UUID、TEXT ↔ TEXT），跨 schema 有先例（`notes_v1_2_cross_schema_views.sql:41-56`）；但 `tv_note_links` 只 GRANT 给 service_role 且开了 RLS 无策略（`009:88-91`、`010:28`），视图要由 owner 建并 GRANT，否则 dashboard 角色查到空集不报错。

**sanshengliubu**

- **`judge_client` 不能整套照抄 `kimi_client`**：`is_available` 绑在 `backend_configured(模型名)` 上（`jev-1.13.0` 会被 `_model_vendor` 归到 claude vendor）；`_call_kimi_raw` 是 `client.messages.create`；`_estimate_cost_usd` 查不到 jev 会记 0。可复用的只有 `llm_retry.call_with_retry`（注意 `_is_transient` 按异常文本里的 429 / timeout / 5xx 判，HTTPError 文本要保留状态码）和「只抛 NotConfigured / CallFailed、调用方当 skipped」的语义。成本自己按 0.042 美元/M 算再在 orchestrator 调 `accumulate_auxiliary_cost(run_id, cost_usd, …, source="jev_judge")`（照 `:4198-4206` 那三处）。超时按 8 秒，不用 120 秒 + 60 秒墙钟。
- **二审的 state 要装锚点**：`multiplier_gate` 四项里三项要对照 `stop_trigger`（`vibe_critic.md:158-166`）、`reward_type`（`:154`）、`gap_direction`（`:177`）、`advertising_stance` / `product_role`（`:185-193`），这些不在 cell 里，现有 v4-flash 二审的 payload 也没带（`kimi_critic.py:71-83`，既有缺口）；Jev 偏字面，不装进去判的就不是同一件事，影子跑一致率会系统性偏低而不是模型问题。从 `self._direction_index` 与 cell_plan 取（参考 `orchestrator.py:4979-4989`）。
- **分流映射要新写**：「任一 fail 即 fail、weak 最多 borderline」只存在于提示词（`vibe_critic.md:210-213`），orchestrator 只遍历 `gemini_failed` 原样取二审给的 severity（`:4207-4228`）；`_classify_failed_cells:5631-5648` 的 root_cause_kind 回退没有 `template_test`。要决定 Jev 给的 `interest_align / reward_signal = fail` 归不归 strategic——归了就走 `strategic_escalation` 回中书省重跑整轮（`:3183-3193`，「一次几分钱的调用能触发全流水线最贵的重入」），误报代价极不对称。建议影子期只写进 `critic_result["_jev_arbitration"]` 与 stage_log；切换时 Jev 只产生 surface / template / structural_* 三类，strategic 仍由主 critic 定。
- **`ssll_sample` 的 id**：`<run_id>:<cell>:<seed>` 三段都稳定（`batch_sampler.py:283` seed = 1, 21, 41…），但 `run_id` 不在采样函数签名里（`:184-189, 404-409`），要在 orchestrator 拼；更要紧的是全局修订或 cell 级修订会删掉 `batch_sampling` 日志并按同一 seed 序列重采（`:6085-6089, 6309-6324`），新旧两批正文对应同一个 `subject_id`，`run_tag` 相同则账本覆盖、旧 stage_log 已删，「回查到那篇采样」只对最后一次成立。id 里加代际（stage_log id 或 refined_a/b/c）。
- **`comment_seeds` 接回路**：现在工部·构建一次 JSON 出整格，评论只是顺带字段；接 `produce_comments` 要新增 per-slot 的生成 prompt（人设 / 口吻从该 cell 的 system_prompt 取）作为 `prompt_for`、生成端指向 Moonshot、在 async 流程里 `asyncio.to_thread` 包同步 generator、结果写回 `cell.comment_seeds` 并登记新 stage_log 名。这不是接一根管子，是加一个岗位。
- **persona 第三路**（拍板后做）：交集单位是 backend 内 ≥3 个画像一致否决（`:3178-3181`），Jev 要以 `_source="jev"` 对每个 cell 给出 ≥3 条 reactions，`:3197` 的 `srcs >= _backends_seen` 改成计数 ≥ 2。

---

## 6. 小问题（不挡路，顺手修）

- `loop.repair_plan:217` 把 fq 题库名硬编码为 `"feature_questions_v0_1"`；题库换名加载，「未过闸二」守卫失效。
- `mcp_server.judge_draft:60` 用排除法推断项目题库，写手传 `external_triage_v0.1` 会被当项目题库；`repair_plan_for:73-74` 没传 `project`，与 `judge_draft` 行为不一致。
- `comments.thread_repair_plan:325-327` 无法归因时「换分数最差的一位」：`named_count` 超限但每个点名位都被 slot 允许时，问题在 slot 配置，换任何一位都修不了，兜底却去换没点名的位；换位时 `why` 只进 trail，`prompt_for(post, slot)` 拿不到，生成端拿同样的 prompt 大概率给同样的候选，两轮 thread 都在烧调用。
- `comments.produce_comments:383-388`：被换的位若自身也在 plan 里，回复位会被重出两次。
- `comments.judge_comment:181-192` 对缺失答案不对称：choice 题 `None` 直接算硬伤并下发「现在判『None』」的修改单，noul 题 `None` 放行。
- `comments.produce_comments:397` 顶层 `passed` 不看 `needs_review`；brief 硬放背书位时 `same_template` 一触发就把它换掉再出同类，白烧轮次。
- `comment_repair_plan:215` 用 `lstrip("至少")` 按字符集剥前缀；现有 label 无害，label 以「至」「少」开头会误删。
- `loop.EchoGenerator:86-92` docstring 说的「按指令删句」没实现；修补路径返回整段 prompt，`parse_draft` 把修改单当正文，`test_loop` 的 `produce` 因此没有真正测到修补。
- `loop.judge_draft:141-142` 空正文时 fq 按 `text_too_short` 跳题，平台 / 项目题库照常判并可能产生硬伤，没有 `invalid_reason`。
- `loop.py:146`、`comments.py:166` 证据调用与 ops 题库的 usage 没累计，成本估算偏低（calls 是准的）。
- `loop.AnthropicCompatGenerator:66` 不剥尾部 `/v1`（三省六部 `agents/__init__.py:580-582` 会剥），复制带 `/v1` 的中转地址会拼成 `/v1/v1/messages`；读 `ANTHROPIC_API_KEY` 而三省六部用 `MOONSHOT_API_KEY`；无重试。
- `mcp_server._client:37-38` 每次工具调用、`judge_comments` / `judge_thread` 里每条评论都新建 `JevClient`、重读密钥文件。
- `api.py:105-120` 批次里任一 subject 抛错整批 502，已判完的 9 篇也丢；`title_extraction` 非法值抛 `ValueError` 未捕获 → 500。
- `core.apply_evidence:81-85` 收到非编号标签「0」时取 index −1，静默返回最后一句。
- `spans.split_sentences:80` 最多 120 句，清单体长文后段的句子选不到证据。
- `core.postgrest_upsert` 不刷新 `extracted_at`，`rows_to_sql:190` 刷新，两条路口径不同。
- `jev_client.mock_response:116-120` 的确定性依赖 questions 插入顺序（现有调用链不会触发）。
- `external.py:270` 「分诊留下 / 取全文 / 打标」三个计数在同一行同时递增，报告里永远相等，看不出分诊通过率和「取了全文仍太短」浪费了多少钱。
- `external._find_notes:67-95` 识别不到 `items[].note` 包装形态（识别为 0 条但搜索费照扣）；列表里混入非 dict 元素会让 `normalize_note:99` 崩。
- `external-corpus.yml:29` dry_run 路径 `exit 0` 后 `out/` 没有文件，配了 secret 时写库步以 `FileNotFoundError` 失败；`:31` 的 `rc=$?` 回显在 `bash -e` 下永远是 0。
- `external-corpus.yml:20` state 只靠 `actions/cache` 续命：key 永不命中、每次新存，GitHub 淘汰 7 天未访问的缓存，周更正好卡在线上，cron 一延迟就归零；`:30` 也没传 `--known-ids` 做第二道去重。
- `migrations/notes_v1_18:33` 只 `ENABLE RLS`、不加 policy、不 GRANT——与 TV 既有约定一致（`notes_v1_2.sql:1038-1071`），没问题；记一下免得再查。

---

## 7. 建议顺序

1. 修 §1 的十三项（都在本仓，一天量），其中 #3（`bank_sha256`）和 #6（`/judge_draft` 端点）要先定口径再动手；CI 绿后 Railway 起 `/judge`。
2. §2 #1（闸二门禁）、#3（硬约束接到判定）、#8（thread fixture 去互动数）是改题库 / 回路前的必修；#2（暗题）与 #4（数据出境）没有着落之前，MCP 工具只给内部用。
3. TV：`schemas/` 落 v1_17 与改好的 v1_18 并登记 README Step 0 与 sql job；`fq_shadow.py --from-db` 按 mapping 切法跑 300 篇。
4. 写作台：先定覆盖面（§4 #1），再按 §5 的位置挂 HTTP；`commit_drafts` 加 `elapsed_ms`。
5. 三省六部：二审 state 装锚点后影子跑；`sample_one_cell` 保留正文时顺手把 `ssll_sample` id 的代际定下来。
6. 文档：docs/30 / 31 与 `jev-comment-unit` 落到哪个仓、D-071 那段改写、金标准处数与 gold 版本、变量名三处对齐。
