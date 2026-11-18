# 2026-11-18 封面试点（I-35）

TV 里 427 篇爆帖有 310 篇没有飞书首图（TV `OPEN-ITEMS.md` I-35，`docs/33-first-image-trial.md` 第 7 节）。它们都有发布链接。这个试点要弄清：能不能用 TikHub 的小红书详情接口（`get_image_note_detail`，按 note_id）从发布链接把封面拿回来。owner 11-18 同意先花约 0.7 元试 10 篇，试完带着结果再问剩下的约 21 元。

试点放在 Jev 做：Jev 的 Actions 已经有 TikHub 密钥和 TV 的 Supabase，external-corpus 每周两次在调同一个接口。**不加任何新密钥或环境变量，owner 不用做任何事。**

本分支只有代码和离线测试：没有调用 TikHub、飞书、小红书，没有查生产库，没有写库。

- 命令：`scripts/cover_pilot.py`（I/O 和命令行）、`scripts/cover_pilot_core.py`（纯逻辑）、`scripts/cover_pilot_rehearse.py`（rehearse 用的假世界）
- workflow：`.github/workflows/cover-pilot.yml`，只能手动点
- 测试：`tests/test_cover_pilot.py`、`tests/test_cover_pilot_privacy.py`、`tests/test_cover_pilot_workflow.py`（假服务在 `tests/cover_pilot_fakes.py`）

## 要回答的四件事

1. **图片列表长什么样。** 路径是不是 `data.data[0].note_list[0].images_list`，每一项有哪些键（只记键名和值的类型）。
2. **列表第 0 项是不是封面。** 看 `index` 是不是按列表顺序连续（从 0 或从 1 起都算）；再拿飞书里已经有首图的爆帖当对照，比每一项的宽高比和飞书首图的宽高比。
3. **GitHub runner 能不能直接下载这张图。** 记状态词、要哪一档请求头、content-type 大类、字节头认出来的格式、大小档；隔几分钟再下一次；链接里带不带过期类参数（只记参数名和值的形状）。
4. **没有首图的那批里，删了或隐藏的大概有多少。** 按链接类型记 ok / gone / hidden，给比例和 Wilson 95% 区间（n ≤ 4，只作参考）。

## 怎么选样

全是只读 GET（PostgREST，和 `scripts/known_external_ids.py` 同一个写法），五次读：`notes`（爆 / 大爆，作品ID、巡查状态、已确认存活用 JSON 路径取；PostgREST 不认就退回取整个 raw_extra，只在内存里取这几个键）、`v_note_first_image_source`（不取 `first_file`）、`note_first_image`（含 `file_bytes`）、`first_image_bank_state`、`v_notes_vanished`。任何一次读失败都在花钱之前停（`selection_failed`，退出码 2）。

- **小红书 id**：长链接 `/explore/` 或 `/discovery/item/` 里的 24 位十六进制；否则用 linkcheck 写的「作品ID」格（`<id> ← <链接>`，右边去掉末尾 `/` 要等于现在的发布链接）。作品ID、巡查状态、已确认存活在 raw_extra 顶层和 `_undeclared` 两处都试，取第一个解得出来的（顶层有个空值不挡住 `_undeclared` 里那个好的）。长链 id 和作品ID 对不上的、两篇撞同一个 id 的都排除并计数。
- **目标**：爆帖、小红书、没有飞书首图、没在飞书里删掉、有 id。没有 id 的短链接只计数、不发（展开短链是一次小红书请求）。
- **对照**：爆帖、小红书、有飞书首图，而且 `note_first_image` 有宽高、指纹和现在的首图一致、`status = answered`、项目没被列为先不做、没删、巡查正常且确认存活、有 id。
- **漂移闸**：没有飞书首图的爆帖（所有平台）不在 310 ± 30 之内就不跑。

计数以 `plan` 打印的为准（不花钱）。不另给一段 SQL：早先草稿里那段和代码的规则对不齐（长链 id 没有边界、不排除撞 id、`NOT IN` 遇到 NULL 会出错）。11-18 的计数（`plan` 模式会打印同样的漏斗；对照应是 12，不是 15，`not_a_cover` 的 3 篇不算）：

| 计数 | 预期 |
|---|---|
| 爆帖 / 其中小红书 | 427 / 422 |
| 有飞书首图 / 没有（小红书） | 117 / 310（305） |
| 目标：长链 explore / discovery | 196 / 10 |
| 目标：短链带作品ID（去掉已删的） | 19（18） |
| 目标：短链没有 id（不发） | 80 |
| 能用的目标 | 224 |
| 能用的对照（单图 / 多图） | 12（5 / 7） |

**槽位**（都在 10 次之内，不会因为后备多发一次）：C1–C4 单图对照（不够由多图补），C5–C6 多图对照，T1–T3 目标（T2 优先短链带作品ID），F10 机动。顺序 `C1, C2, T1, C3, T2, C4, T3, C5, C6, F10`：对照打头（最可能还在，先验明外壳形状），目标不相邻。每一组按 `md5(TV note_id + "i35-cover-pilot-1")` 排（和 TV `first_image._rank` 同一个构造），对照里比 3:4 更长的图和不长的图交替、长图先（只有长图分得开列表里的项；但小红书要是把长图裁成 3:4，全押长图会全落空）。F10：C1–C6 里有一篇不是 ok，或 ok 但没宽高 / 没图片列表 / 分析出错，就用第一个对照后备；否则下一个目标。

## 钱

- **详情调用最多 10 次、¥0.75**，常量写在 `cover_pilot_core.py`，命令行和 workflow 都改不了。每次发之前查两道：自己的账（次数、按 ¥0.072 / 次算的元）和 Jev 的 `Budget(0.10 美元, 0.01 美元 / 次)`。先记账再发；发出去失败的照样算钱，不重试、不退。
- **只跑一次**：第一次付费调用之前建 git tag `cover-pilot-i35-run-claimed`（GitHub refs API，原子的，和 D-112 评测同一套语义）。已有这个 tag 就拒，一分钱不花。缺密钥、余额读不到之类在认领之前就拒，不占用这一次。要再跑得有人专门删掉这个 tag。
- **余额**：TikHub 的 `get_user_info`（标明零费用）前后各读一次。账户被停用、读不到、401/403、低于 0.50 美元（给 linkcheck 的巡查留余量）都在花钱之前拒。报告里**只有差值**（之前 − 之后，最后一次付费调用之后紧接着读），标成 `whole_account_shared`：这个账户 linkcheck 的巡查和 Jev 的抓取也在用。绝对余额不打印、不写。
- **抓取时段**：周一、周四 02:00–13:00 UTC 拒跑（external-corpus 定时 03:07 UTC，即北京时间 11:07，在同一个账户上花钱）。测试从 `external-corpus.yml` 的 cron 推出起跑时刻核对这个窗口，那边改了时间这里就红。
- **图片请求**最多 40 次（小红书的图片服务器，不经 TikHub、不计费；含 https→http 的退路和最多 2 跳同域跳转）。

## 状态词和停止规则

详情调用的状态词按这个先后定：网络错 → `network`；HTTP 非 200：401/403 `auth`、402 `quota`、429 `rate_limited`、400/422 及其他 4xx `bad_request`、404 `not_found`、5xx `server`；返回不是 JSON 对象 → `unknown_shape`；外壳 `code` 不是 200 → 照上面映射，映射不到就 `upstream_failed`；`data.success = false` 或 `data.code` 不是 0 / 200 → `upstream_failed`（照样计费，**不当成删了**）；`data.data = []` 或 `note_list = []` → `gone`；`note_list[0].model_type = "error"` → `hidden`（只有作者看得见；它自带的 `image` 键是静态占位图，不碰）；其余形状不对 → `unknown_shape`；都不是 → `ok`。

停：`auth` / `quota` / `rate_limited` 立刻停；第 1 次就 `unknown_shape` 停；连续 2 次失败停（失败 = upstream_failed、bad_request、not_found、server、network、unknown_shape；`gone` / `hidden` 不算也不清零，`ok` 清零）；2 篇 ok、不是视频、却没有 `images_list` 停（`shape_absent`，形状变了再花钱也答不了 Q2、Q3）。到 10 次上限是正常结束。单篇返回里的怪值（字符串宽高、None 项……）只记一个异常类名（`analysis_error`），不停整次运行。

下载：item 0 的 `url_size_large` → `url` → `original` 里第一个 http(s) 链接，`live_photo`、`video*`、`url_multi_level` 一律不碰。过链接闸（http / https、没有账号密码、默认端口、像样的域名、不是 IP 和 localhost）；http 的先试同主机同路径的 https，再试 http。请求头三档：`bare`（urllib 默认 UA）→ `browser_ua`（linkcheck 的桌面 Chrome UA）→ `browser_ua_referer`（再加小红书 Referer），找到能用的就固定下来，之后不再升档；没找到之前每篇都从头试。只有在 2 篇上各档都被明确拒（403 / 404 / 不是图 / 疑似占位图 / 跨域跳转）才停止下载；网络、5xx、太大不算。`ok` 要 HTTP 200、字节头是图片格式，而且字节里读出来的宽高比和 JSON 里这一项的对得上（差 5% 以上记 `placeholder_suspect`，当成没拿到、继续升档）。跳转只跟同一个可注册域、过链接闸的，最多 2 跳。图片请求不带任何密钥和 cookie；`video/*` 在读正文之前就停。

## 结论规则（跑之前定死，跑完不改）

「ok 篇」= 详情调用回 `ok` 的篇，两个池都算。

- **V1 形状**：ok 篇 ≥ 3 且每篇 `images_list` 都是非空的 dict 列表、item 0 有 `index`、`width`、`height` 和至少一个 http(s) 图片链接 → `confirmed`；有不合格的 → `differs`（报告里按缺了什么计数）；ok 篇 1–2 篇且都合格 → `too_few`；没有 ok 篇 → `no_data`。没有图片列表的视频笔记不进分母。
- **V2a 顺序（按 index）**：分母是 ≥ 2 张图的 ok 篇。有 `unsorted`（后面一项的号比前面小）→ `no`；分母 ≥ 3 且**全是**从 0 起、或**全是**从 1 起的连续序号 → `yes`；其余 `unknown`：缺号（`sorted_gaps`，第 0 项还是最小的）、重号（`ties`，比如全是 0，这个字段不带顺序信息）、0 起和 1 起混着来（可能是第一张被丢了），都说明不了，不判「不是」。
- **V2b 宽高比佐证（弱）**：每篇对照的每一项和飞书首图（缩过，宽高比不变，EXIF 已转正）比，容差 |ln 比值| ≤ 0.01，分 `item0_unique_match`（只有第 0 项对得上）/ `item0_shared_match`（第 0 项和别的项都对得上）/ `other_match_only` / `no_match` / `single_image_match` / `single_image_no_match`（小红书只有一张图，对得上 / 对不上）/ `no_dims`。**只拿飞书单图的对照下结论**（多图的飞书第一张本身没确认是封面，I-36；多图的只记数，不动结论）：飞书单图对照里 `other_match_only` ≥ 2 篇 → `contradicts`；能分辨地对上（`item0_unique_match` 而且别的项宽高比确实不一样）≥ 2 篇、没有 `other_match_only`、对不上的（`no_match`、`single_image_no_match`）不过半 → `corroborates`；能分辨的不到 2 篇、而对不上的加说明不了的（并列匹配、只有一张图对上、`no_dims`）过半，或者没有单图对照 → `controls_uninformative`；其余 `inconclusive`。一篇 `other_match_only` 不够判「不是」。
- **V2 第 0 项是封面**：V2a = yes **且** V2b = corroborates → `yes`；V2a = no 或 V2b = contradicts → `no`；其余 `unsettled`。只凭 index 不出 `yes`。固定说法：第 0 项是第一张显示的图（按 index）；「第一张就是封面」是小红书的平台规则（推断）；宽高比证据弱，不比像素。
- **V2 探索项**：飞书附件的字节数（`note_first_image.file_bytes`）等于哪一项的 `origin_img.size`，只记 0 / 1 / 2 / 3_plus / none。`origin_img.size` 的含义没核实过，不进结论。
- **V3 直接下载**：attempted = item 0 至少发出过一次请求的 ok 篇。停止下载了，或 attempted ≥ 3 而成功不到一半 → `no`；attempted ≥ 3 且成功 ≥ 90% → `yes`；50%–90% → `partial`；其余 `not_tested`。一并报要的请求头档位、content-type 和格式。复查：全 ok 且字节相同 `stable`，全 ok 但有字节变了 `stable_bytes_changed`，有不 ok 的 `degraded`，没复查 `skipped`；图片请求上限用完、没去重下的（`not_attempted`）不算 `degraded`，只在计数里看得到，一篇都没复查成就是 `cap_reached`。过期类参数（`sign`、`t`、`expires` 等名字）出现在任一链接里 → `present`，否则 `absent`；不从值里推任何时刻。
- **V4 目标篇**：按链接类型数 ok / gone / hidden / 其他，删了或隐藏的比例和 Wilson 95% 区间，一律标 `indicative_n_le_4`，不判及格。全量预估 = 能用的目标 × ¥0.072（224 篇约 ¥16.1），没有 id 的 80 篇短链不在内。
- **建议**（第一条对上的为准）：没跑完（拒绝 / 出错 / ok 篇 < 4）→ `do_not_ask_yet / run_incomplete`；V1 ≠ confirmed → `shape_differs`；V2 = no → `cover_no`；V3 = no → `download_blocked`；V3 = not_tested → `download_untested`；V2 = unsettled → `cover_unsettled`；否则 `ask_owner_full_run`，带上全量预估、V4 的比例和区间、要的请求头档位、复查结论和过期参数结论。

## 公开仓的纪律

Jev 和 TV 都是公开仓，Actions 日志和产物也是公开的。试点的输出（stdout、step summary、产物 `cover_pilot_report.json` / `.md`）只有计数、固定状态词、键名、形状串、宽高比的分档和统计、余额差。TV note_id、项目代号、发布链接、作品ID 格、小红书 id、`cache_url`、`params`、标题、正文、昵称、用户 id、fileid、trace_id、品牌、任何链接或主机名、绝对余额、密钥一概不打、不写。

- 每一行 stdout 过一道闸：带 `://`、12 位以上十六进制、9 位以上数字、`xsec`、`@` 或任何密钥的整行换成 `[redacted line]`。
- 报告写出之前过 `scrub()`：字符串只许是固定词表、值的形状类、日期、异常类名，键只许是词表、键名或参数名，别的换成 `redacted` 并计数（测试要求代码自己产出的是 0）。
- `HTTPError` / `URLError` 只取状态码和类名，从不格式化；出错只打异常类名；`urllib3` / `requests` 的日志在运行期间挂 NullHandler、升到 ERROR、不往上传；警告全部忽略（ResourceWarning 会带对端 IP）；这些都只在 `main()` 里开、结束时还原。
- 每篇的明细只进产物、而且分了档（图数、d0）；step summary 里只有汇总。
- 测试在每个能带出东西的地方埋金丝雀（含 TV `{项目}_rec…` 形状的 note_id、`cache_url`、`params.note_id`、图片链接、余额），跑一遍完整的付费路径后查 stdout、stderr、summary、产物、日志记录和警告。

## 怎么跑

1. **合并到 main。** `workflow_dispatch` 要文件在 main 上。`railway.json` 没有 watch paths，合并会让 Railway 重启 Jev 服务（服务代码没变）：别在 TV 夜跑或周一、周四 external-corpus 正在调 Jev 的时候合。
2. **Actions → cover-pilot → `plan`**（默认）。只读 TV，看漏斗、10 个槽位、`drift=ok`、`window_now`。
3. **`rehearse`**。整条付费路径在进程里的假服务上空跑一遍，不读任何密钥、不联网、不用掉认领 tag，看 summary、产物和退出码。
4. **`run`，confirm 填 `I-35`**，在 main 上，避开周一、周四 02:00–13:00 UTC（比如周三晚上或周五）。`recheck_minutes` 默认 20。退出码 0 = 完成，3 = 按停止规则提前停（报告照样有效），2 = 一分钱没花就拒绝，1 = 出错（部分报告照样写出）。

跑完把 `cover_pilot_report.json` 里的结论和计数（只有计数）填进下面「结果」，并另开一个 TV 只改文档的 PR 更新 `OPEN-ITEMS.md` I-35 和 `docs/33-first-image-trial.md` 第 7 节，顺手把那里的 426 / 309 改成 427 / 310，并写明 owner 同意的「按相似度比」这次没做。

## 这次不覆盖的

- 没有作品ID 的短链接（约 80 篇）：要先展开短链（一次小红书请求），不在范围里。
- 对照只来自两个项目；「还活着」是 1–4 周前 linkcheck 巡查的结果。
- 视频笔记占多少、封面在哪个键，要跑了才知道（TV 没有媒体类型字段）；报告里只记视频笔记的键名和图片列表长度。
- **owner 11-18 同意的那种「飞书图和小红书图按相似度比」没做**：Jev 拿不到飞书的图。宽高比只是弱佐证；`origin_img.size` 对字节数只是探索项。
- 余额差是整个共用账户的差。

## 和最初设计的出入（评审后改的）

- 第 0 项是封面要有对照的宽高比佐证（`corroborates`），不再只凭 index；判「不是」要 ≥ 2 篇 `other_match_only`；对照多半没信息时是 `controls_uninformative`。
- 第二轮评审（11-18）又改了四处：宽高比佐证只算飞书单图的对照，多图的只记数；只算能分辨的匹配（并列、只有一张图的不算）；小红书只有一张图而对不上的算对不上；顺序只有真的倒了才判「不是」，缺号、重号、0 起 1 起混着都是 `unknown`；复查时请求上限用完不算链接过期。
- 从 1 起的连续 index 也算按顺序。
- 图片链接 http 和 https 都认（http 先试 https），V1 不再要求 https。
- 一篇下不来不停止下载：要 2 篇被明确拒；网络、5xx、太大不算；没找到能用的档位之前每篇都从头试。疑似占位图不算拿到；同域跳转跟最多 2 跳。
- 单篇的怪数据只记类名不停整次运行；最后一次付费调用之后的余额在 `finally` 里读；加了 `shape_absent` 停止规则和不花钱的 `rehearse` 模式。
- 能用的对照预期是 12 不是 15；作品ID 等三格两处都试、取第一个解得出来的。
- 报告清洗改成只认固定词表；日志 / 警告 / 信号的设置从 import 挪进 `main()`；抓取时段的测试从 external-corpus 的 cron 推出来。
- 结论里 V3 = not_tested 记 `download_untested`（不再混进 `download_blocked`）；每篇的明细不进 step summary。
- 图片请求上限从 24 提到 40（多了 http 退路和同域跳转）。

## 结果

（跑完填：只填 `cover_pilot_report.json` 里的计数和结论词。）
