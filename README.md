# judge —— BYWOOD 内容生产的共用判定服务（Jev）

> **评论线云端交接**：[完整中文交接、180条对照及完整Excel/交接包](docs/handoff/2026-10-15/README.md)，可直接在GitHub阅读。

> **接手当前进度（2026-10-15）先读 [HANDOFF.md](HANDOFF.md)**：本轮检查、已合并改动、生产验证、未完成事项和下一步执行顺序。

评论线本轮工程与固定验证已收尾，C4新40与AI参考225/280未达预注册标准，生产评论继续关闭；见[收尾报告与接手边界](docs/comment-line-closeout-2026-10-15.md)。人员返回及C3历史结果见[原返回说明](docs/comment-holdout-return-2026-10-15.md)。

一句话：同一个模型（Jev）、同一套题库纪律、同一张账本（`truth_vault.note_feature_answers`），在内容生产的写前、写中、写后
和数据飞轮的特征层、评论层、外部语料上做同一件事——把文字变成带概率、带证据句的闭集事实。它不写字，不解释理由，不判「会不会爆」。
逻辑与拍板见 docs/00-decisions.md 和本仓的 docs/31（它不在 truth-vault 里；它引用的 docs/30 是一份没有入库的草稿）；**Jev 具体怎么介入、评论怎么写，看 docs/01-how-jev-is-used.md（一页）**。

## 仓库里有什么

| 目录 | 内容 |
|---|---|
| `judge/` | `jev_client`（调用、重试、mock）· `banks`（两种题库格式 → 一种内部表示；歧义判定）· `spans`（TV 同款切片、切句）· `core`（判一篇 / 判一个 state、证据选句、账本行、SQL / PostgREST 写入）· `loop`（稿子的生产回路：brief 编译成项目题库、best-of-k、判 → 修改单 → 修补）· `comments`（评论的生产回路：评论位、best-of-k、修改单、评论区成组判、换位）· `external`（外部语料：TikHub 抓取、预算、去重、分诊、打标）· `draft`（一篇稿子判哪几层、哪些硬约束：`/judge_draft` 用，MCP 经 HTTP 间接用）· `policy`（数据出境：处方药未发布稿不出境；项目层看 `project_layer_cleared`，10-20 起 `"*"` 全部放行）· `rules`（`/compile_rules`：写作台的自由文本规则编成写后检查）· `hidden`（暗题：人感题库每季度 1/3 不进任何给写手看的输出）· `api`（HTTP `/judge` · `/judge_draft` · `/compile_rules`）· `mcp_server`（内部 / 运维机器用的 stdio MCP：HTTP 服务的薄客户端，不持 Jev 密钥、不读题库；写手的判稿入口在写作台 deskcore，见 docs/31 §5.4） |
| `banks/` | `vendor/feature_questions_v0_1.yaml` TV 的 fq 题库原样 vendor（SHA256SUMS 记校验和，测试钉住）· `comment_reader_v0.6` 读者侧评论 7 题（C4 原样，D-112 owner 10-16 验收：四道门槛题 + 三道参考题；写手评论工具和评论回填默认用它）· `comment_reader_v0.4` 上一版读者侧 7 题（v0.3 + 按篇 / 按项目的占位符，途鸽用例逐字等同 v0.3）· `comment_thread_v0.4` 评论区 4 题（v0.3 + praise_share 的出口「评论太少」；v0.3 留给金标准）· `comment_ops_v0.2` 运营侧 comment_intent（草案；蓝词植入改成只看文字能判的定义，state 不带蓝词清单）· `platform_health_v0.1` 大健康平台层（草案）· `human_feel_para_v0.2` 人感段级（草案；para_function 加出口「说不清」）· `external_triage_v0.1` 外部语料分诊 4 题（草案）· `gold/` 金标准 |
| `scripts/` | `run_gold.py` 金标准评测 · `fq_shadow.py` 特征层影子跑（与现行抽取器 / D-081 表比对，出 SQL）· `backfill_comments.py` 评论回填 · `external_corpus.py` 外部语料定时抓取（`--dry-run` 算钱、`--probe` 钉字段、`--rows` 出两张表的行）· `apply_rows.py` 把 rows.json 经 PostgREST 写进 TV（TV 没有、也不该有执行任意 SQL 的 RPC）· `known_external_ids.py` 从 TV 拉已入库的外部笔记 id 做第二道去重 |
| `config/` | `external_corpus.yaml`：品类、关键词、排序、页数、每次 / 每月上限、预算——量级和频率都在这里改 · `data_policy.yaml`：处方药项目、合同已清出境的项目、放行项目层的项目 · `hidden_rotation.yaml`：暗题的比例与手工指定 |
| `migrations/` | `notes_v1_17_judge_subjects.sql`（账本加 comment / ssll_sample / external_note，prob 口径统一）· `notes_v1_18_external_notes.sql`（外部笔记表 + 参考分布视图 `v_external_reference`） |
| `fixtures/` | gate1 的 50 篇笔记 + Opus 答案（覆盖其中 47 篇，所以报告里有 921 与 999 两个分母）+ D-081 Jev 表；33 条运营评论（运营前缀【贴主回复】等已剥掉、角色已改正）；评论用例 |
| `docs/` | 实跑报告（特征层 50 篇、评论 33 条、评论金标准）与拍板记录 |
| `tests/` | 合成工程测试（本轮功能基线200项），全 mock（评论回路用按关键词给答案的假 Jev；MCP 薄客户端的 urllib 打到进程内 TestClient），不联网 |

## 跑起来

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY=...                     # Jev 密钥，只在服务端
python3 -m pytest -q                            # 全 mock
python3 scripts/run_gold.py banks/comment_reader_v0.4.yaml fixtures/comment_cases.json --gold banks/gold/comment_reader_gold_v0.2_proposed.yaml   # 占位符从用例文件的 fill 填
python3 scripts/fq_shadow.py --notes fixtures/fq_notes_gate1_50.json --opus fixtures/fq_opus_gate1_50.tsv --gate1 fixtures/fq_jev_d081_A_gate1_50.tsv --out fq.md --sql fq.sql --run-tag shadow-$(date +%F)
uvicorn judge.api:app --port 8080              # HTTP：POST /judge（一个题库、一批 subject，并行）· POST /judge_draft（一篇稿、多层题库、带修改单）· POST /compile_rules（规则编成写后检查）· GET /banks · GET /health（不鉴权）
JUDGE_URL=https://… JUDGE_API_KEY=… python3 -m judge.mcp_server   # 内部 / 运维机器的 MCP 工具（上面那个 HTTP 服务的薄客户端）：judge_draft · repair_plan_for · list_banks。写手不用它：这三个工具 2026-10-09 起由写作台 deskcore 转发（写手只配 deskcore 那一个 MCP）；
                                                #   旧 stdio MCP 的 judge_comments · comment_repair_plan_for · judge_thread 仍为显式本机路径；写手通过 deskcore 转发已实现的评论HTTP（默认关闭），见 HANDOFF
python3 scripts/external_corpus.py --dry-run   # 外部语料：先看这次要花多少钱；--probe 花一次请求钉字段；正式跑由 .github/workflows/external-corpus.yml 每周一触发
```

金标准报告头会写明对的是哪一版金标准；`cg-v0.2-proposed` 是提议版、未经勘误，命中率不等于对勘误版的命中率。影子跑从库里取用 `--from-db --mappings ../truth-vault/mappings`，切标题按每个项目的 mapping 走。

环境变量（服务端）：`TYPESAFE_API_KEY`（必需，**只放在服务端**，写手机器不配；也可用 `TYPESAFE_KEY_FILE` 指向密钥文件，`TYPESAFE_BASE_URL` 换端点）· `JUDGE_API_KEY`（管理 key，必需；不配则服务拒绝所有请求，本地开发显式设 `JUDGE_ALLOW_ANONYMOUS=1`）· `JUDGE_WRITER_API_KEYS`（发给写手机器的 key，逗号分隔可多把；只够 `GET /banks` 和 `POST /judge_draft`，后者 write 一律 403、不回账本行；`/judge` 只认管理 key）· `JUDGE_WORKERS`（一批 subject 并行几路，默认 4）·
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`（与 truth-vault 同名；直接写账本，没有就只出 SQL / rows.json）· `JUDGE_MOCK=1`（不联网的假 Jev；它判的行 extractor 是 `mock:<模型>`，任何写库请求一律拒绝）· `TIKHUB_API_KEY`（外部语料，每次请求 0.01 美元；`TIKHUB_BASE_URL` 换端点）·
`JUDGE_BANKS_DIR` / `JUDGE_POLICY_CONFIG` / `JUDGE_HIDDEN_CONFIG`（换题库目录与两份配置的位置）·
`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`（或 `MOONSHOT_API_KEY`）（`loop.AnthropicCompatGenerator` 的生成端；末尾带不带 `/v1` 都行；`/compile_rules` 用它，没配则 503）·
`JUDGE_COMPILE_MODEL`（`/compile_rules` 的模型，默认 `claude-sonnet-5-5`）· `JUDGE_COMPILE_EFFORT`（默认 `low`；中转站不认 `output_config` 时设空串）·
`JUDGE_COMPILE_BATCH`（每次调用编几条，默认 10）· `JUDGE_COMPILE_WORKERS`（并行几路，默认 6）· `JUDGE_COMPILE_TIMEOUT_SEC` / `JUDGE_COMPILE_RETRIES`（单次调用超时与 429/5xx 重试，默认 90 秒 / 1 次）。
环境变量（`judge.mcp_server` 所在机器的 `.mcp.json` env；2026-10-09 审计 A-09。写手机器**不再**起它——写手通过 deskcore 的 `judge_draft` / `repair_plan_for` / `list_banks` 判稿，deskcore 用服务端的管理 key 转发）：`JUDGE_URL`（判定服务地址）+ `JUDGE_API_KEY`（填服务端 `JUDGE_WRITER_API_KEYS` 里的一把写手 key，**不是**服务端的管理 key；鉴权头 `X-Judge-Key`）两个必需，没配则启动记一条警告、工具返回 `{"error": …}` ·
`JUDGE_PROJECT` / `JUDGE_CATEGORY`（默认项目代号与品类；`project` 两处都没有在本地就拒绝，其余数据出境规则由服务端执行并回显在 `policy`）· `JUDGE_HTTP_TIMEOUT_SEC`（默认 180；429 / 5xx 退避重试最多 2 次）·
`JUDGE_ALLOW_LOCAL_JEV=1`（旧stdio MCP三个评论工具的本机路径默认拒绝；内部机器需合法持有vendor密钥及本地banks。写手使用deskcore转发的HTTP接口，服务开关仍关闭，见HANDOFF）。写手机器上**没有** `TYPESAFE_API_KEY`，MCP 不读 `banks/`、不 import `judge.hidden`。
账本里的 `bank_sha256` 用 TV 同款规范化摘要（剔掉 `status:` / `frozen_sha256:` 两行再算），与 TV 自己写的行同口径，冻结题库不会把行劈成两批；`banks/vendor/SHA256SUMS` 钉的是整文件。

## 数据出境、暗题、闸二门禁

- **数据出境**（docs/00 #7，`judge/policy.py` + `config/data_policy.yaml`）：公开内容（note / external_note / comment）直接跑；未发布稿（aw_version / ssll_sample）必须带 `project`，
  通用层（fq、人感、评论、三省六部二审、外部分诊）+ 平台层照跑；项目层（brief / 内联 `project_bank` / 项目题库 / `/compile_rules` 的规则原文）只有 `project_layer_cleared` 里的项目才跑，否则整层去掉并在响应的 `policy.dropped_banks` 回显（`/compile_rules` 直接 403）。
  **2026-10-20 起 `project_layer_cleared: ["*"]`**：owner 拍板项目层对所有项目放行（原话「全部放行，一次到位」），`*` = 所有项目，显式列项目代号照旧认；YAML 里 `*` 必须带引号。
  处方药闸不受影响、仍先执行：处方药项目（`rx_categories` / `rx_projects`）的未发布稿在合同确认、加进 `rx_cleared` 之前一律 403，一次 Jev 都不调（`rx_cleared` 不认通配）。规则只在服务端执行一遍：MCP 是 `/judge_draft` 的薄客户端，只把 `project` / `category` 传过去，403 的 detail 原样带回写手。
- **Jev 格式题的 `scope`**（2026-10-20）：Jev 格式题（含内联 `project_bank`）可选写 `scope: title | body | full`。只在 `/judge_draft` 的平台层 / 项目层起作用：题干前加「只看【标题】」之类的提示；
  答「是」的证据，`title` 只在标题里找、`full` 在「标题（自成一句）+ 正文各句」里找；稿子没标题时 `title` 题不问、记 `no_title`（不算 unjudged，同 fq）。不写 `scope` 的题行为不变：证据只从正文句里选。
- **暗题**（docs/00 #4，`judge/hidden.py` + `config/hidden_rotation.yaml`）：人感题库每季度按 sha256(题号|季度) 自动换 1/3；暗题照判、照落账本、照算硬伤，
  但不出现在修改单、`/judge_draft` 的 profile / detail / plan / ledger_rows、`/banks` 与 MCP `list_banks` 的题号列表里。
  `judge_paras=on_fail`（默认）时篇级没硬伤也要把暗题逐段判一遍（暗题防的就是对着公开题库写、篇级全过的稿子）；`never` 一次段级调用都不发，暗题也不判。
  段级结果（含暗题）以 `subject_id = <稿 id>:p<段号>` 写进账本；`/judge` 的 ledger_rows 照带全部题，调用方不得转给写手。
- `write=true` 时账本**可能写到一半**（PostgREST 一批一个事务，批之间没有）。2026-10-09 起 `/judge` / `/judge_draft` 不再因此 500：
  回 200，`written` 是真写进去的行数，`write_error` 说清写了多少、第几批炸的；`/judge` 写失败时无论 `return_rows` 都回 `ledger_rows`
  供补写（upsert 幂等，重跑不重复）。调用方要看 `write_error`，别只看状态码。TV 的 `verify_supabase_state.sql` #87 从账本侧反查半写。
- **闸二门禁**（docs/28 §7）：没过闸二的 fq 目标题不进修改单，只进响应的 `recorded`；fq 硬伤只给依据句，不给题干和定义。

## 规则编译：`POST /compile_rules`（2026-10-20）

写作台把存下的自由文本规则一条条编译成「稿子写完之后怎么查」，不再把每条规则塞进写手模型的简报；结果只作建议给写手看、不拦稿。逻辑在 `judge/rules.py`，端点在 `judge/rules_api.py`。

- **鉴权与出境**：只认管理 key（写手 key 403，同 `/judge`）。规则原文是项目层内容，按未发布稿过 `policy.decide`：处方药没清出境 403、项目层没放行 403，都在调生成端之前。
- **请求**：`{"project": "<必填>", "category": "<可选>", "rules": [{"id": "<规则 id>", "text": "<原文，≤4000 字>", "severity": "hard" | "soft", "applicability": "<可选，≤100 字>"}]}`，1–60 条，id 不重复，多余字段 422。
  `applicability` 是写作台 `memories.applicability` 原样：部位（「标题」「正文」）决定 `where` / `scope`；方向词（产品向 / 流量向）写作台自己按方向筛编出来的检查，编译不管。
- **响应**（合成示例）：
  ```json
  {"rules": [
     {"id": "r-001", "sha": "cfb4acd4e688", "verdict": "code",
      "code": {"kind": "forbidden_word", "words": ["最便宜", "第一名"], "where": ["title"]}, "questions": [],
      "coverage": "full", "uncovered": "", "why": "标题禁用词，代码能查"},
     {"id": "r-006", "sha": "e9c631367736", "verdict": "questions",
      "code": {"kind": "forbidden_word", "words": ["闭眼入"], "where": ["title", "body"]},
      "questions": [{"qid": "r006_e9c631_1", "ask": "这篇稿子读起来像不像在推销？", "pass_if": "否", "scope": "full", "note": "语气部分"}],
      "coverage": "full", "uncovered": "", "why": "词表走代码，语气要读懂"},
     {"id": "r-007", "sha": "…", "verdict": "code", "code": {"kind": "max_len", "max": 20, "where": "title"}, "questions": [],
      "coverage": "partial", "uncovered": "正文要带 #品牌话题 没查（一条只能填一个 code）", "why": "…"},
     {"id": "r-003", "sha": "09426db7df02", "verdict": "not_checkable", "code": null, "questions": [], "coverage": null, "uncovered": "", "why": "交付命名，稿子里查不到"},
     {"id": "r-009", "sha": "…", "verdict": null, "code": null, "questions": [], "coverage": null, "uncovered": "", "why": "", "error": "模型输出不合格（已重试一次）：…"}],
   "project_bank": {"bank_version": "rules-9c83b9eca62a", "ambiguity": {…}, "questions": [{"id": "r006_e9c631_1", "type": "noul", "scope": "full", …}]},
   "errors": 1, "model": "claude-sonnet-5-5", "calls": 1, "usage": {"input_tokens": …, "output_tokens": …}, "latency_ms": …, "policy": {…}}
  ```
  - `sha` = 规则原文（UTF-8 原样）sha256 前 12 位；带 `applicability` 时是「原文 + `\x00` + applicability（去首尾空白）」的。
    写作台的缓存键是 **（规则 id, sha）**，不是 sha 一样：题号里有规则 id，两条原文相同、id 不同的规则只按 sha 复用，会把前一条的题号带进后一条，拼出的题库题号重复（`/judge_draft` 422）。原文和适用范围都没变才不用重编。
  - `verdict`：`code`（禁用词 / 词能不能出现在哪 / 必带话术 / 字数范围，形状同写作台 `deskcore/rulecheck.py`，写作台自己跑——Jev 不会数数、比大小）·
    `questions`（1–3 道只看一篇稿就能答的是非题；分得开时 `code` 也一起给）· `not_checkable`（流程、交付、写作方向菜单）。`where` ⊆ title / body / tags / comments；`max_len` 的 max、min 至少一个。
  - `code` 的执行语义按写作台 `rulecheck`：词表是不分大小写的「包含」（纯英文数字按词边界），所以 prompt 不让模型把单字、常见词放进禁用词（单个汉字直接打回）；
    `forbidden_regex` 是 Python `re.search`、不加 flag（`.` 不跨换行），量词只套单个字符或定长的组（同写作台 `rulecheck._assert_regex_shape` + `validator` 的嵌套量词检查，查不过的打回重编，不会编出写作台存不进去的正则）。
    数话题标签个数的正则假定 tags 串里没有重复：写作台 `rulecheck._parts` 把写手的 tags 和正文里的 #话题 直接拼在一起，两处都写了同一组话题会数成两倍，要在写作台那边先去重。
  - `coverage`（verdict 是 code / questions 时）：一条规则只能填一个 `code`、最多 3 道题；装不下的要求模型写进 `uncovered`、`coverage` 记 `partial`。
    **只有 `coverage: full` 的规则才能从写手简报里拿掉**；`partial` 的原文留在简报里（或请写手把规则拆成几条），`not_checkable` 的编译不替代它。
  - `qid` = `<规则 id 只留小写字母数字的前 8 位>_<sha 前 6 位>_<n>_<这道题本身的指纹 6 位>`：带 sha，规则一改题号就变；带题目指纹（问法 + pass_if + scope），重编出来问法变了题号也变，账本里不同问法的答案不会互相覆盖（Jev 格式题的 question_version 永远是 1）；与本服务所有题库的题号、同一请求里别的规则都不撞，撞了那条带 `error`。
  - 单条编不出（输出不是 JSON、形状不对、漏了这条）先带着「哪里不对」重试一次，仍不行只让那一条带 `error`；一条都没编出 → 502。生成端没配密钥 → 503。
    生成端回 4xx / 5xx 时 `error` 带返回体前 300 字（密钥打码，比如中转站不认 `output_config` 时设 `JUDGE_COMPILE_EFFORT=""`）；模型拒答 / 输出被截断报成「模型拒答」「输出被截断」，不重试。
- **送回 `/judge_draft`**：**单独发一次**，不和存档那次评分合在一起（TV docs/32 §E.3 / E.4：存档的判定预算只剩约 1.8 秒，影子题的行不能和 `primary` 混）——
  存档返回之后异步发 `{"banks": [], "project_bank": <拼好的题库>, "project_bank_name": "desk_rules", "run_tag": "shadow-rules", "judge_paras": "never", …}`：
  只判规则题、不重复花 fq / 平台 / 人感的钱，也不判段。写作台缓存的是每条规则的结果，
  可以按需拼：`judge/rules.py` 的 `to_project_bank(规则结果列表, taken=…)` 就是拼法（每题 noul、criteria 是 / 否、`scope` 原样；
  `evidence` 跟着 `pass_if`：`pass_if: 否` 的题 `on_true`，答「是」= 违反时给出依据句，就是要改的那句；`pass_if: 是` 的题 `never`，不找依据句）。
  **不要**把这些题写进 `hard_rules` 或 `brief`：那样会进 `passed`；它们只作建议，写作台对照每题的 `pass_if` 和 `/judge_draft` 返回的 `detail.desk_rules[qid]`（答案、概率、证据句、歧义）自己出提示。
  `detail.desk_rules[qid].invalid_reason` 不为空时这道题当「没判出来」，不报违反也不报守住：`evidence_not_found` = 答了「是」（违反）却找不到依据句（账本里 answer 为 NULL）；`no_title` = 标题题、稿子没标题。
- **延迟与费用**：每次调用编 `JUDGE_COMPILE_BATCH`（默认 10）条，`JUDGE_COMPILE_WORKERS`（默认 6）路并行，60 条是一轮调用的时间。
  最坏耗时 = 2 次调用（格式不合格重试一次）×（`JUDGE_COMPILE_RETRIES` + 1）× `JUDGE_COMPILE_TIMEOUT_SEC` + 退避，默认 2 × 2 × 90 秒 + 1 秒 ≈ 361 秒；
  调用方的 HTTP 超时设 ≥ 400 秒（改了这两个 env 就按公式重算），端点没有整体截止时间，调用方先断开服务端也会把这一轮跑完、照样花钱。
  **编译不能放在写手工具的调用里**（写作台 MCP 工具的预算封顶 20 秒，`JUDGE_DRAFT_TOOL_TIMEOUT_SEC`）：放在写作台后台（规则存下 / 改了之后按（规则 id, sha）补编），
  写手工具和送回 `/judge_draft` 只用已经缓存的编译结果，没编好的规则这一轮照旧留在简报里。
  按（规则 id, sha）缓存后只有新增或改过的规则才花钱。送回 `/judge_draft` 时多一次 Jev 调用（+ `pass_if: 否` 的题答「是」时一次证据调用），约 0.5–1 秒。
- `JUDGE_MOCK=1`：用 `MockRuleGenerator`（不联网、按关键词给确定的结果），只测流程，不代表编译质量。

## 评论怎么写

`judge/comments.py`：评论位（Slot）是目标画像——要做什么、能不能点名、要不要接住帖子、读者能拿到什么；默认一组 = 提问 ×2 + 补充经验 ×1 + 贴主回复答疑 ×1，
brief 可换。每一位生成端出 k 条，逐条过读者侧 7 题 + 运营侧 2 题，取硬伤最少的一条；不过就下修改单（题号 + 概率 + 要改成什么 + 定义）让生成端只改这一条；
一组选定后整体过评论区 4 题（没有摩擦也算不过），不过就换位重出；问题出在评论位配置本身、换谁都修不了的，只记进轨迹、不换位。`produce_comments(client, generator, post, slots, prompt_for, reader=…, thread=…, ops=…)`。
默认组里没有背书位：读者侧题库把「亲历背书 / 旁观推荐」路由进高风险复核，回路里只会换掉它们，不会把它们修得更像。
题干里按篇变的部分（帖子要点、主体是机构还是产品）由 `fill_for(post)` 填占位符；漏填会报错，不会拿别的帖子的要点去判。

## 外部语料怎么控量

三道闸都在 `config/external_corpus.yaml`：`budget_usd_per_run`（一次运行的硬上限，到了就停并写进报告）、`max_keep_per_category_per_run` / `_per_month`（每品类留多少篇全文）、`pages_per_sort`（每个关键词搜几页）。
钱花在两处：搜索按页（0.01 美元 / 页）、取全文按篇（0.01 美元 / 篇）。分诊（Jev 四题）只看搜索页的摘要，不花抓取费，所以「先分诊再取全文」是省钱的关键。
分诊通过的一律取全文再打 fq：TikHub 搜索页的摘要截在 60 字，拿它打标等于只看半句话。详情接口没有公开 schema，正文从整棵返回里找（id 对得上的优先）；每次运行的前 3 份原始返回进产物 `kept.json` 的 `detail_samples`，开头 5 篇都解析不出正文就整次停、job 红。
默认配置：5 品类 × 4 词 × 2 排序 × 2 页 = 80 页，每品类每次最多 40 篇全文，最坏 2.8 美元 / 次、每周一次约 12 美元 / 月、每品类每月 160 篇。
去重靠 `state/external_corpus_state.json`（GitHub Actions 用 cache 保留）和 `--known-ids`（账本里已有的 external note_id）；一条笔记只在有了结局（分诊拒绝 / 太短 / 入账本）后才记为「见过」，因上限、预算或报错没看的下次还会看。从库里删掉一批外部笔记后要重抓，手动触发时设 `fresh_state=true`（不恢复 cache 里的 state）。
翻页按 TikHub 的规矩带首页返回的 `search_id` / `search_session_id`；单条笔记或单页搜索出错只记进报告、不中止整次运行。写库走 `scripts/apply_rows.py`（PostgREST upsert，外部笔记表在前、账本行在后），产物先上传再写库。`--mock` 的 state 默认落到临时目录。

## 三个仓库怎么接

- **truth-vault**：两份迁移已落进 TV 的 `schemas/`（排在 notes_v1_13 之后）；特征层用 `scripts/fq_shadow.py --from-db` 影子跑，过线后 worker 的 `/annotate-features` 改调 `/judge`（extractor = `jev:1.13.0`，run_tag = primary），Opus 路径留作备份；`annotate_feature_pass` 的续跑判据从 `llm:%` 改成按 extractor 传入。
- **autowriter / deskcore**：`commit_drafts` 写锁释放后 HTTP 调 `/judge_draft`（带 project；层与硬约束按数据出境规则定），答案落 `note_feature_answers(aw_version)`，返回里带修改单；判失败只记状态，不影响入库。deskcore 保持零 LLM。写手的 `judge_draft` / `repair_plan_for` / `list_banks` 也由 deskcore 转发到同一个 `/judge_draft`（2026-10-09 autowriter#95：写手不装本仓、不持 judge 密钥）；`judge.mcp_server` 这个 stdio 薄客户端只给内部 / 运维机器。
- **sanshengliubu**：网感循环的二审影子跑 `/judge`（题库 `ssll_critic_v0.1`，advisory，只写进 `_jev_arbitration` 与 stage_log，不改分流）；`sample_one_cell` 保留正文后用 `feature_questions_v0_1 + human_feel_para` 判 5 篇 / cell；`comment_seeds` 走 `comments.produce_comments`（工部·构建出候选，本仓判和换位）。

## 纪律（改题前先读）

代码先切句再给 Jev；每题留出口；定义里每个词都会被执行；金标准对着定义标、两人独立、不随模型答案改；
题干或定义一改 → bank_version 升一号 → 重跑金标准；模型版本固定；同样输入连跑两遍看稳不稳；
概率用「所选答案的概率」，阈值从数据定；题干用中文；人只判分歧和抽检；state 里不放任何表现数据（Mode A）。
