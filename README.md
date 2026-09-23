# judge —— BYWOOD 内容生产的共用判定服务（Jev）

一句话：同一个模型（Jev）、同一套题库纪律、同一张账本（`truth_vault.note_feature_answers`），在内容生产的写前、写中、写后
和数据飞轮的特征层、评论层、外部语料上做同一件事——把文字变成带概率、带证据句的闭集事实。它不写字，不解释理由，不判「会不会爆」。
逻辑与拍板见 docs/00-decisions.md 和 truth-vault 的 docs/31；**Jev 具体怎么介入、评论怎么写，看 docs/01-how-jev-is-used.md（一页）**。

## 仓库里有什么

| 目录 | 内容 |
|---|---|
| `judge/` | `jev_client`（调用、重试、mock）· `banks`（两种题库格式 → 一种内部表示；歧义判定）· `spans`（TV 同款切片、切句）· `core`（判一篇 / 判一个 state、证据选句、账本行、SQL / PostgREST 写入）· `loop`（稿子的生产回路：brief 编译成项目题库、best-of-k、判 → 修改单 → 修补）· `comments`（评论的生产回路：评论位、best-of-k、修改单、评论区成组判、换位）· `external`（外部语料：TikHub 抓取、预算、去重、分诊、打标）· `api`（HTTP `/judge`）· `mcp_server`（给写手用的 MCP 工具） |
| `banks/` | `vendor/feature_questions_v0_1.yaml` TV 的 fq 题库原样 vendor（SHA256SUMS 记校验和，测试钉住）· `comment_reader_v0.4` 读者侧评论 7 题（v0.3 + 按篇 / 按项目的占位符，途鸽用例逐字等同 v0.3）· `comment_thread_v0.3` 评论区 4 题（同理） · `comment_ops_v0.1` 运营侧 comment_intent（草案）· `platform_health_v0.1` 大健康平台层（草案）· `human_feel_para_v0.1` 人感段级（草案）· `external_triage_v0.1` 外部语料分诊 4 题（草案）· `gold/` 金标准 |
| `scripts/` | `run_gold.py` 金标准评测 · `fq_shadow.py` 特征层影子跑（与现行抽取器 / D-081 表比对，出 SQL）· `backfill_comments.py` 评论回填 · `external_corpus.py` 外部语料定时抓取（`--dry-run` 算钱、`--probe` 钉字段、`--rows` 出两张表的行）· `apply_rows.py` 把 rows.json 经 PostgREST 写进 TV（TV 没有、也不该有执行任意 SQL 的 RPC） |
| `config/` | `external_corpus.yaml`：品类、关键词、排序、页数、每次 / 每月上限、预算——量级和频率都在这里改 |
| `migrations/` | `notes_v1_17_judge_subjects.sql`（账本加 comment / ssll_sample / external_note，prob 口径统一）· `notes_v1_18_external_notes.sql`（外部笔记表 + 参考分布视图 `v_external_reference`） |
| `fixtures/` | gate1 的 50 篇笔记 + Opus 答案 + D-081 Jev 表；33 条运营评论；评论用例 |
| `docs/` | 实跑报告（特征层 50 篇、评论 33 条、评论金标准）与拍板记录 |
| `tests/` | 28 个测试，全 mock（评论回路用按关键词给答案的假 Jev），不联网 |

## 跑起来

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY=...                     # Jev 密钥，只在服务端
python3 -m pytest -q                            # 全 mock
python3 scripts/run_gold.py banks/comment_reader_v0.4.yaml fixtures/comment_cases.json --gold banks/gold/comment_reader_gold_v0.2_proposed.yaml   # 占位符从用例文件的 fill 填
python3 scripts/fq_shadow.py --notes fixtures/fq_notes_gate1_50.json --opus fixtures/fq_opus_gate1_50.tsv --gate1 fixtures/fq_jev_d081_A_gate1_50.tsv --out fq.md --sql fq.sql --run-tag shadow-$(date +%F)
uvicorn judge.api:app --port 8080              # HTTP：POST /judge（一个题库、一批 subject，并行）· POST /judge_draft（一篇稿、多层题库、带修改单）· GET /banks · GET /health（不鉴权）
python3 -m judge.mcp_server                     # 写手的 MCP 工具：judge_draft · repair_plan_for · judge_comments · comment_repair_plan_for · judge_thread · list_banks
python3 scripts/external_corpus.py --dry-run   # 外部语料：先看这次要花多少钱；--probe 花一次请求钉字段；正式跑由 .github/workflows/external-corpus.yml 每周一触发
```

环境变量：`TYPESAFE_API_KEY`（必需）· `JUDGE_API_KEY`（HTTP 鉴权，必需；不配则服务拒绝所有请求，本地开发显式设 `JUDGE_ALLOW_ANONYMOUS=1`）· `JUDGE_WORKERS`（一批 subject 并行几路，默认 4）·
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`（与 truth-vault 同名；直接写账本，没有就只出 SQL / rows.json）· `JUDGE_MOCK=1`（不联网的假 Jev，线上不要留）· `TIKHUB_API_KEY`（外部语料，每次请求 0.01 美元）。
账本里的 `bank_sha256` 用 TV 同款规范化摘要（剔掉 `status:` / `frozen_sha256:` 两行再算），与 TV 自己写的行同口径，冻结题库不会把行劈成两批；`banks/vendor/SHA256SUMS` 钉的是整文件。

## 评论怎么写

`judge/comments.py`：评论位（Slot）是目标画像——要做什么、能不能点名、要不要接住帖子、读者能拿到什么；默认一组 = 提问 ×2 + 补充经验 ×1 + 贴主回复答疑 ×1，
brief 可换。每一位生成端出 k 条，逐条过读者侧 7 题 + 运营侧 2 题，取硬伤最少的一条；不过就下修改单（题号 + 概率 + 要改成什么 + 定义）让生成端只改这一条；
一组选定后整体过评论区 4 题，不过就换位重出。`produce_comments(client, generator, post, slots, prompt_for, reader=…, thread=…, ops=…)`。
默认组里没有背书位：读者侧题库把「亲历背书 / 旁观推荐」路由进高风险复核，回路里只会换掉它们，不会把它们修得更像。
题干里按篇变的部分（帖子要点、主体是机构还是产品）由 `fill_for(post)` 填占位符；漏填会报错，不会拿别的帖子的要点去判。

## 外部语料怎么控量

三道闸都在 `config/external_corpus.yaml`：`budget_usd_per_run`（一次运行的硬上限，到了就停并写进报告）、`max_keep_per_category_per_run` / `_per_month`（每品类留多少篇全文）、`pages_per_sort`（每个关键词搜几页）。
钱花在两处：搜索按页（0.01 美元 / 页）、取全文按篇（0.01 美元 / 篇）。分诊（Jev 四题）只看搜索页的摘要，不花抓取费，所以「先分诊再取全文」是省钱的关键。
默认配置：5 品类 × 4 词 × 2 排序 × 2 页 = 80 页，每品类每次最多 40 篇全文，最坏 2.8 美元 / 次、每周一次约 12 美元 / 月、每品类每月 160 篇。
去重靠 `state/external_corpus_state.json`（GitHub Actions 用 cache 保留）和 `--known-ids`（账本里已有的 external note_id）；一条笔记只在有了结局（分诊拒绝 / 太短 / 入账本）后才记为「见过」，因上限、预算或报错没看的下次还会看。
翻页按 TikHub 的规矩带首页返回的 `search_id` / `search_session_id`；单条笔记或单页搜索出错只记进报告、不中止整次运行。写库走 `scripts/apply_rows.py`（PostgREST upsert，外部笔记表在前、账本行在后），产物先上传再写库。`--mock` 的 state 默认落到临时目录。

## 三个仓库怎么接

- **truth-vault**：跑 `migrations/notes_v1_17_judge_subjects.sql`；特征层用 `scripts/fq_shadow.py --from-db` 影子跑，过线后 worker 的 `/annotate-features` 改调 `/judge`（extractor = `jev:1.13.0`，run_tag = primary），Opus 路径留作备份；`annotate_feature_pass` 的续跑判据从 `llm:%` 改成按 extractor 传入。
- **autowriter / deskcore**：`commit_drafts` 入库后 HTTP 调 `/judge`（bank = feature_questions_v0_1 + platform + 项目题库），答案落 `note_feature_answers(aw_version)`，返回里带修改单；判失败只记状态，不影响入库。deskcore 保持零 LLM。写手侧另可直接挂 `judge.mcp_server`。
- **sanshengliubu**：网感循环的二审改调 `/judge`（advisory，照 `kimi_client` 的写法）；`sample_one_cell` 保留正文后用 `feature_questions_v0_1 + human_feel_para` 判 5 篇 / cell；`comment_seeds` 走 `comments.produce_comments`（工部·构建出候选，本仓判和换位）。

## 纪律（改题前先读）

代码先切句再给 Jev；每题留出口；定义里每个词都会被执行；金标准对着定义标、两人独立、不随模型答案改；
题干或定义一改 → bank_version 升一号 → 重跑金标准；模型版本固定；同样输入连跑两遍看稳不稳；
概率用「所选答案的概率」，阈值从数据定；题干用中文；人只判分歧和抽检；state 里不放任何表现数据（Mode A）。
