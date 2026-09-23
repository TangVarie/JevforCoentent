"""scripts/feature_bank.py — 内容特征层的纯逻辑（docs/28 §4-§5, D-065 / D-070）。

只用标准库 + pyyaml, **不 import supabase / anthropic**, 让 CI 守卫和本地自检能在裸环境里跑
（同 deskcore/fingerprint.py 的取舍）。库 I/O 和 LLM 调用在 annotate_feature_pass.py。

这里住着:
  · 问题库的加载 / 校验和 / 结构校验（守卫 1）
  · 标题 / 正文切片: mapping 的 title_extraction (column / markers / none), 09-19 实查 17 张表
  · 四段输入 spans: title / first_sentence / last_para / body(≤1,500 字, 记截断) / full
  · 代码算的 8 个特征 + 3 道占位题（守卫 5）
  · 分组提示词: 一次调用只问一组、同组 ≤ 4 题（守卫 4）; Mode A 防泄漏（守卫 2）
  · 答案校验: 闭集 + 证据是原文子串 + 截断片段的「否」不算数（守卫 3）
  · 下发给写作台的规律列表: aw_instruction=never 的永不出现（守卫 7）
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

import yaml

BANK_PATH = Path(__file__).resolve().parent.parent / "prompts" / "feature_questions_v0_1.yaml"

MAX_GROUP_SIZE = 4          # docs/28 §5.3: 同组最多 4 题
BODY_CAP = 1500             # 与 essence 同一截断
EVIDENCE_MAX_CHARS = 30     # 证据片段上限（去空白后）
MIN_BODY_CHARS = 20         # 短于这个的正文不问模型
BOOL_VALUES = ("是", "否")
SPANS = ("title", "first_sentence", "last_para", "body", "full")
TRUNCATABLE_SPANS = ("body", "full")
LAYERS = ("surface", "essence")
HYPOTHESES = ("+", "-", "?")
TITLE_EXTRACTION_MODES = ("column", "markers", "none")
CODE_EXTRACTOR = "code:v1"

# invalid_reason 闭集（docs/28 §5.1 / 问题库答题契约）
INVALID_EVIDENCE_NOT_FOUND = "evidence_not_found"
INVALID_EVIDENCE_TOO_LONG = "evidence_too_long"
INVALID_OUT_OF_VOCAB = "out_of_vocab"
INVALID_MISSING = "missing"
INVALID_NO_TITLE = "no_title"
INVALID_TEXT_TOO_SHORT = "text_too_short"
INVALID_SPAN_TRUNCATED = "span_truncated"
INVALID_NO_BRAND_DICT = "no_brand_dict"
INVALID_JSON = "json_parse_failed"
INVALID_API = "api_error"

# ── Mode A 防泄漏（照搬 annotate_essence_pass.build_mode_a_prompt 的两道, D-028）──
PERFORMANCE_KEYWORDS = (
    "tier", "大爆", "爆贴", "爆款", "爆率", "impressions", "reads", "interactions",
    "互动数", "阅读数", "曝光", "评论数", "performance", "实际表现", "点赞",
)
TEMPLATE_LEAK_PLACEHOLDERS = (
    "{performance", "{tier", "{interactions", "{reads", "{impressions", "{comments",
)

SYSTEM_TEMPLATE = """你在给一条小红书笔记做事实标注。只回答下面几道题，每题只看题目指定的那一段。
不要评价这条笔记好不好，也不要猜它的数据表现。每道题相互独立，不要用一道题的答案去推另一道。

{questions_block}

只输出 JSON，不要 markdown 包装、不要解释：
{{"answers":[{answers_example}]}}
规则：
- bool 题只答「是」或「否」；choice 题只从给出的选项里选一个，原样抄选项名。
- 需要证据时，evidence 必须从题目指定的那一段【原样抄】，不超过 {evidence_max} 字；不需要证据时 evidence 留空字符串。
- 拿不准就按题目里「算否」的标准答「否」，不要为了凑证据而硬答「是」。"""

SPAN_LABELS = {
    "title": "标题",
    "first_sentence": "正文第一句",
    "last_para": "正文最后一段",
    "body": "正文",
    "full": "标题+正文",
}


# ═══════════════════════════════════════════════════════════════════════
# 问题库
# ═══════════════════════════════════════════════════════════════════════

# 规范化时剔掉的两行: 二者都是「冻结」这个动作自己写的, 不是题目内容。
# 都是顶格 key（^ 锚在行首、不吃缩进）, 所以选项里的 status:/frozen_sha256: 不会误伤。
_META_LINES = re.compile(rb"(?m)^(?:frozen_sha256|status):.*(?:\r?\n|$)")


def bank_digest(raw: bytes) -> str:
    """问题库的**规范化**校验和: 把 `frozen_sha256:` 和 `status:` 两行剔掉之后再 hash。

    ⚠️ 为什么不直接 hash 整个文件 (codex review on #141, P1): 冻结那天要把算出来的
    digest 写回同一个文件的 `frozen_sha256:` 字段、并把 `status: draft` 改成
    `status: frozen`, 写回去文件字节就变了 —— 要求「文件 sha256 == 文件里记的那个
    sha256」是自指的, 算不出来。于是冻结这一步会让 validate_bank 永远报错、每次抽取
    直接退出 2。剔掉这两行之后, 冻结前后的 digest 完全一样: 冻结这个动作算得出来,
    落进 note_feature_answers.bank_sha256 的值也不会因为「冻结」把答案劈成两批。
    digest 管的是**题目内容**, 生命周期状态不在里头。
    """
    return hashlib.sha256(_META_LINES.sub(b"", raw)).hexdigest()


def load_bank(path: Path | str = BANK_PATH) -> dict:
    """读问题库, 附上规范化 sha256（跑的时候记进每一行, D-041 的纪律）。"""
    raw = Path(path).read_bytes()
    bank = yaml.safe_load(raw)
    bank["_sha256"] = bank_digest(raw)
    bank["_path"] = str(path)
    return bank


def llm_questions(bank: dict) -> list[dict]:
    return [q for q in (bank.get("questions") or []) if not q.get("retired")]


def question_index(bank: dict) -> dict[str, dict]:
    return {q["id"]: q for q in llm_questions(bank)}


def validate_bank(bank: dict) -> list[str]:
    """结构校验（守卫 1）。返回错误列表, 空 = 合法。

    判据来自 docs/28 §4.1 六条规矩 + §5.3 分组约束 + §4.3 冻结纪律:
      id 唯一; 每题在且只在一个 call_group 里; 组 ≤ 4 题; bool 题有正反例; choice 题的
      hypothesis 键与选项一致; scope / layer / hypothesis 闭集; 冻结后 sha256 不变。
    """
    errs: list[str] = []
    if not bank.get("bank_version"):
        errs.append("缺 bank_version")
    groups: dict[str, list] = bank.get("call_groups") or {}
    if not groups:
        errs.append("缺 call_groups")
    qs = llm_questions(bank)
    ids = [q.get("id") for q in qs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errs.append(f"question id 重复: {dupes}")
    membership: dict[str, list[str]] = {}
    for g, members in groups.items():
        if not isinstance(members, list) or not members:
            errs.append(f"call_group {g} 不是非空列表")
            continue
        if len(members) > MAX_GROUP_SIZE:
            errs.append(f"call_group {g} 有 {len(members)} 题, 超过 {MAX_GROUP_SIZE}")
        for m in members:
            membership.setdefault(m, []).append(g)
    for q in qs:
        qid = q.get("id") or "<无 id>"
        for key in ("id", "version", "family", "group", "layer", "type", "scope", "ask", "hypothesis"):
            if q.get(key) in (None, ""):
                errs.append(f"{qid}: 缺 {key}")
        if not isinstance(q.get("version"), int) or q.get("version", 0) < 1:
            errs.append(f"{qid}: version 必须是 ≥1 的整数")
        if q.get("layer") not in LAYERS:
            errs.append(f"{qid}: layer={q.get('layer')!r} 不在 {LAYERS}")
        if q.get("scope") not in SPANS:
            errs.append(f"{qid}: scope={q.get('scope')!r} 不在 {SPANS}")
        where = membership.get(qid, [])
        if len(where) != 1:
            errs.append(f"{qid}: 应在且只在一个 call_group 里, 实际在 {where}")
        elif where[0] != q.get("group"):
            errs.append(f"{qid}: group={q.get('group')} 与 call_groups 登记的 {where[0]} 不一致")
        if q.get("type") == "bool":
            if not q.get("yes_examples") or not q.get("no_examples"):
                errs.append(f"{qid}: bool 题必须有 yes_examples 和 no_examples")
            if q.get("hypothesis") not in HYPOTHESES:
                errs.append(f"{qid}: bool 题 hypothesis={q.get('hypothesis')!r} 不在 {HYPOTHESES}")
        elif q.get("type") == "choice":
            opts = q.get("options") or []
            values = [o.get("value") if isinstance(o, dict) else o for o in opts]
            if len(values) < 2 or len(set(values)) != len(values):
                errs.append(f"{qid}: choice 题选项要 ≥2 且不重复")
            hyp = q.get("hypothesis")
            if not isinstance(hyp, dict) or set(hyp) != set(values):
                errs.append(f"{qid}: choice 题 hypothesis 的键必须与选项一致")
            elif any(v not in HYPOTHESES for v in hyp.values()):
                errs.append(f"{qid}: choice 题 hypothesis 取值不在 {HYPOTHESES}")
            # 选项级指令 (codex review on #141): choice 题的 aw_instruction 只对某些取值成立
            # (opening_type 的那句只对「具体事件」)。没声明 = 过了闸也不自动下发。
            vals = q.get("aw_instruction_values")
            if vals is not None:
                if not isinstance(vals, list) or not vals:
                    errs.append(f"{qid}: aw_instruction_values 必须是非空列表")
                elif any(v not in values for v in vals):
                    errs.append(f"{qid}: aw_instruction_values {vals} 里有不在选项里的值")
        else:
            errs.append(f"{qid}: type={q.get('type')!r} 不是 bool / choice")
        if q.get("type") == "bool" and q.get("aw_instruction_values") is not None:
            errs.append(f"{qid}: aw_instruction_values 只用于 choice 题 (bool 题的指令对应「是」)")
    for m in membership:
        if m not in ids:
            errs.append(f"call_groups 里的 {m} 不是问题库里的题")
    code_ids = [c.get("id") for c in (bank.get("code_features") or [])]
    plc_ids = [p.get("id") for p in (bank.get("placebo") or [])]
    all_ids = ids + code_ids + plc_ids
    clash = sorted({i for i in all_ids if all_ids.count(i) > 1})
    if clash:
        errs.append(f"模型题 / 代码特征 / 占位题 id 互相撞了: {clash}")
    for p in bank.get("placebo") or []:
        if str(p.get("hypothesis")) != "0":
            errs.append(f"占位题 {p.get('id')} 的 hypothesis 必须是 '0'")
    if bank.get("status") == "frozen":
        want = bank.get("frozen_sha256")
        if not want:
            errs.append("status=frozen 但没记 frozen_sha256")
        elif bank.get("_sha256") and want != bank["_sha256"]:
            errs.append("status=frozen 但问题库规范化 sha256 与 frozen_sha256 不符 —— 冻结后改题要升 version 并记 DECISIONS "
                        f"(现在是 {bank['_sha256']})")
    return errs


# ═══════════════════════════════════════════════════════════════════════
# 切片: 标题 / 正文
# ═══════════════════════════════════════════════════════════════════════

_BRACKET_TITLE = re.compile(r"【\s*标\s*题\s*】")
_BRACKET_BODY = re.compile(r"【\s*正\s*文\s*】")
_COLON_TITLE = re.compile(r"标题\s*[：:]")
_COLON_BODY = re.compile(r"(?:^|\n)\s*正文\s*[：:]")
_LEADING_TAG = re.compile(r"^\s*【[^】\n]{1,12}】\s*")   # 「【粉饼贴】标题：…」这种前缀


def split_title_body(raw: str, mode: str, title_col: Optional[str] = None
                     ) -> tuple[Optional[str], str, str]:
    """按 mapping 的 title_extraction 切出 (title, body, how)。

    mode:
      column  — 标题在独立列 (TGV)。正文若以标题开头就把它去掉。
      markers — 文案里带标记。同一个解析器认两种写法（一张表里可能混用, 09-19 实查:
                HXZ_QD / NUC / WTG 两种都有）:
                  「【标题】…【正文】…」   「标题：…\\n正文：…」（冒号全角半角都有, 前面可带【xx】）
                切不出来 → title None（标题类题目记 NULL, 不猜）。
      none    — 没有标题。
    how 记录实际用了哪条路（column / bracket / colon / none）, 闸一登记切法用。
    """
    raw = raw or ""
    if mode not in TITLE_EXTRACTION_MODES:
        raise ValueError(f"title_extraction={mode!r} 不在 {TITLE_EXTRACTION_MODES}")
    if mode == "column":
        title = (title_col or "").strip() or None
        body = raw
        if title and body.lstrip().startswith(title):
            body = body.lstrip()[len(title):]
        return title, _strip_markers(body), "column"
    if mode == "none":
        return None, _strip_markers(raw), "none"

    m_t = _BRACKET_TITLE.search(raw)
    if m_t:
        rest = raw[m_t.end():]
        m_b = _BRACKET_BODY.search(rest)
        if m_b:
            title, body = rest[:m_b.start()], rest[m_b.end():]
        else:
            title, body = _first_line_split(rest)
        title = title.strip() or None
        return title, _strip_markers(body), "bracket"

    head = _LEADING_TAG.sub("", raw, count=1)
    m_t = _COLON_TITLE.match(head.lstrip())
    if m_t:
        rest = head.lstrip()[m_t.end():]
        m_b = _COLON_BODY.search(rest)
        if m_b:
            title, body = rest[:m_b.start()], rest[m_b.end():]
        else:
            title, body = _first_line_split(rest)
        title = title.strip() or None
        return title, _strip_markers(body), "colon"
    return None, _strip_markers(raw), "none"


def _first_line_split(text: str) -> tuple[str, str]:
    """没有正文标记时: 第一行非空文本当标题, 其余当正文。"""
    lines = text.lstrip("\n").split("\n", 1)
    if len(lines) == 1:
        return lines[0], ""
    return lines[0], lines[1]


def _strip_markers(text: str) -> str:
    text = _BRACKET_TITLE.sub("", text)
    text = _BRACKET_BODY.sub("", text)
    text = re.sub(r"(?:^|\n)\s*正文\s*[：:]\s*", "\n", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════
# 话题标签 / 表情 / 句子
# ═══════════════════════════════════════════════════════════════════════

_HASHTAG_CLOSED = re.compile(r"#([^#\s\[\]]{1,40}?)(?:\[话题\])?#")
_HASHTAG_BARE = re.compile(r"#([^\s#\[\]]{1,40})")
_MENTION = re.compile(r"@[^\s@]{1,30}")
# 表情: 剥掉变体选择符后, 把「基字 + 肤色修饰 + ZWJ 连接的后续基字」整串算【一个】。
# ⚠️ 原来的写法把 U+FE0F 当成独立字符匹配, 于是「❤️」「☀️」各记 2 个、一家三口的 ZWJ
# 串记 3 个 —— 分档直接从 1-3 跳到 >=4, 污染闸二 (codex review on #141)。
_EMOJI_BASE = "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF]"
_SKIN = "[\U0001F3FB-\U0001F3FF]"
_VARIATION = re.compile("[\uFE0E\uFE0F]")
_EMOJI_SEQ = re.compile(f"{_EMOJI_BASE}(?:{_SKIN})?(?:\u200d{_EMOJI_BASE}(?:{_SKIN})?)*")
_BRACKET_EMOJI = re.compile(r"\[[^\[\]\n]{1,10}R\]")   # 小红书方括号表情 [哭惹R]
_EMOJI = re.compile(f"{_EMOJI_SEQ.pattern}|{_BRACKET_EMOJI.pattern}")   # 只用于剥除, 不用于计数
_SENTENCE_END = re.compile(r"[。！？!?…\n]")
_WS = re.compile(r"\s+")


def extract_hashtags(text: str) -> tuple[list[str], str]:
    """返回 (标签列表, 去掉标签后的文本)。同时认「#…#」「#…[话题]#」和裸「#词」三种。"""
    tags: list[str] = []

    def _grab(m):
        tags.append(m.group(1).strip())
        return " "
    cleaned = _HASHTAG_CLOSED.sub(_grab, text)
    cleaned = _HASHTAG_BARE.sub(_grab, cleaned)
    return tags, cleaned


def count_mentions(text: str) -> int:
    return len(_MENTION.findall(text or ""))


def count_emoji(text: str) -> int:
    """可见表情个数: 变体选择符不单独算, ZWJ 串 / 肤色修饰算一个, 方括号表情算一个。"""
    t = _VARIATION.sub("", text or "")
    return len(_EMOJI_SEQ.findall(t)) + len(_BRACKET_EMOJI.findall(t))


def visible_len(text: str) -> int:
    return len(_WS.sub("", text or ""))


def first_sentence(body: str) -> str:
    """去掉开头的标签、表情后, 到第一个 。！？!?… 或换行为止; 不足 6 字就并入下一句。"""
    _, text = extract_hashtags(body or "")
    text = _EMOJI.sub("", text).strip()
    parts = [p.strip() for p in _SENTENCE_END.split(text)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    out = parts[0]
    i = 1
    while visible_len(out) < 6 and i < len(parts):
        out = out + parts[i]
        i += 1
    return out


def last_paragraph(body: str) -> str:
    """去掉末尾的标签和 @ 之后, 最后一个非空段落; 不足 10 字就往上再并一段。"""
    _, text = extract_hashtags(body or "")
    text = _MENTION.sub("", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    if not paras:
        return ""
    out = paras[-1]
    i = len(paras) - 2
    while visible_len(out) < 10 and i >= 0:
        out = paras[i] + "\n" + out
        i -= 1
    return out


# ═══════════════════════════════════════════════════════════════════════
# spans
# ═══════════════════════════════════════════════════════════════════════

def build_spans(raw_content: str, *, mode: str, title_col: Optional[str] = None) -> dict:
    """代码先切好四段 + 元信息。模型只看题目 scope 指定的那段（docs/28 §5.1）。"""
    title, body, how = split_title_body(raw_content, mode, title_col)
    truncated = len(body) > BODY_CAP
    body_capped = body[:BODY_CAP]
    full = (f"{title}\n{body_capped}" if title else body_capped)
    return {
        "title": title,                     # None = 拿不到
        "first_sentence": first_sentence(body_capped),
        # ⚠️ 结尾那段必须从【没截断】的正文取 (codex review on #141): 从 body_capped 取
        # 等于把中间某一段当成结尾, 而 last_para 不在 TRUNCATABLE_SPANS 里 —— 模型对着
        # 中段答 ending_asks_reader 还会被判成有效答案。这一段本来就短, 不受 1,500 字约束。
        "last_para": last_paragraph(body),
        "body": body_capped,
        "full": full,
        "_body_untruncated": body,
        "_truncated": truncated,
        "_title_how": how,
    }


# ═══════════════════════════════════════════════════════════════════════
# 代码算的特征 + 占位题
# ═══════════════════════════════════════════════════════════════════════

_DIGIT = re.compile(r"[0-9０-９]")


def _bucket(n: int, edges: list[tuple[int, str]], last: str) -> str:
    for upper, label in edges:
        if n <= upper:
            return label
    return last


def brand_dictionary(project: dict, note: dict, mapping: Optional[dict] = None) -> list[str]:
    """品牌词 / 产品词词典: projects.brand + product + 笔记 target_blue_keywords + mapping.brand_aliases。
    占位（(未填) / 待确认 / 内部代号 TGV 这种）交给运营 Q3 补 aliases, 这里只做形状过滤。"""
    cands: list[str] = []
    for v in (project.get("brand"), project.get("product")):
        if isinstance(v, str):
            cands.append(v)
    for v in (note.get("target_blue_keywords") or []):
        if isinstance(v, str):
            cands.append(v)
    for v in ((mapping or {}).get("brand_aliases") or []):
        if isinstance(v, str):
            cands.append(v)
    out = []
    for c in cands:
        c = c.strip()
        if len(c) < 2 or c.startswith("(") or "待" in c and len(c) <= 4:
            continue
        if c not in out:
            out.append(c)
    return out


def _find_brand(text: str, words: list[str]) -> int:
    """第一个品牌词出现的位置（字符下标）, 不区分大小写; 没有 → -1。"""
    low = (text or "").lower()
    best = -1
    for w in words:
        i = low.find(w.lower())
        if i >= 0 and (best < 0 or i < best):
            best = i
    return best


def code_features(spans: dict, brand_words: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """8 个代码特征 → {id: (answer, invalid_reason)}。分档固定, 不按项目分位（docs/28 §4.4）。"""
    body = spans["_body_untruncated"]
    title = spans["title"]
    tags, body_wo_tags = extract_hashtags(body)
    blen = visible_len(body_wo_tags)
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    out["body_len_bucket"] = (_bucket(blen, [(99, "<100"), (199, "100-199"), (399, "200-399")], ">=400"), None)
    if title is None:
        out["title_len_bucket"] = (None, INVALID_NO_TITLE)
        out["title_has_digit"] = (None, INVALID_NO_TITLE)
    else:
        out["title_len_bucket"] = (_bucket(visible_len(title), [(10, "<=10"), (15, "11-15")], ">=16"), None)
        out["title_has_digit"] = ("是" if _DIGIT.search(title) else "否", None)
    qm = body_wo_tags.count("？") + body_wo_tags.count("?")
    out["body_question_marks"] = (_bucket(qm, [(0, "0"), (1, "1")], ">=2"), None)
    em = count_emoji(body)
    out["emoji_bucket"] = (_bucket(em, [(0, "0"), (3, "1-3")], ">=4"), None)
    out["hashtag_bucket"] = (_bucket(len(tags), [(0, "0"), (3, "1-3")], ">=4"), None)
    if not brand_words:
        out["brand_in_title"] = (None, INVALID_NO_BRAND_DICT)
        out["brand_first_position"] = (None, INVALID_NO_BRAND_DICT)
    else:
        if title is None:
            out["brand_in_title"] = (None, INVALID_NO_TITLE)
        else:
            out["brand_in_title"] = ("是" if _find_brand(title, brand_words) >= 0 else "否", None)
        pos = _find_brand(body_wo_tags, brand_words)
        n = max(len(body_wo_tags), 1)
        if pos < 0:
            out["brand_first_position"] = ("未出现", None)
        elif pos / n < 0.2:
            out["brand_first_position"] = ("前20%", None)
        elif pos / n >= 0.8:
            out["brand_first_position"] = ("后20%", None)
        else:
            out["brand_first_position"] = ("中间", None)
    return out


def raw_counts(spans: dict) -> dict[str, int]:
    """数值原值, 写 note_features 现有列。"""
    body = spans["_body_untruncated"]
    tags, _ = extract_hashtags(body)
    return {
        "title_len": visible_len(spans["title"]) if spans["title"] is not None else None,
        "body_len": visible_len(extract_hashtags(body)[1]),
        "hashtag_count": len(tags),
        "mention_count": count_mentions(body),
    }


def placebo_answer(note_id: str, k: int) -> str:
    """sha256(note_id + ':fq-placebo-k') 最后一位十六进制 < 8 记「是」。确定性, 与内容无关。"""
    h = hashlib.sha256(f"{note_id}:fq-placebo-{k}".encode("utf-8")).hexdigest()
    return "是" if int(h[-1], 16) < 8 else "否"


def placebo_answers(bank: dict, note_id: str) -> dict[str, str]:
    out = {}
    for i, p in enumerate(bank.get("placebo") or [], start=1):
        out[p["id"]] = placebo_answer(note_id, i)
    return out


# ═══════════════════════════════════════════════════════════════════════
# 提示词（分组）
# ═══════════════════════════════════════════════════════════════════════

def _render_question(n: int, q: dict) -> str:
    label = SPAN_LABELS[q["scope"]]
    lines = [f"{n}. {q['id']} · 看【{label}】· {q['ask']}"]
    if q["type"] == "bool":
        ye = " / ".join(q.get("yes_examples") or [])
        ne = " / ".join(q.get("no_examples") or [])
        lines.append(f"   算「是」：{q.get('yes_if', '')} 例：{ye}")
        lines.append(f"   算「否」：{q.get('no_if', '')} 例：{ne}")
    else:
        lines.append("   选项：")
        for o in q.get("options") or []:
            if isinstance(o, dict):
                lines.append(f"   - {o['value']}：{o.get('means', '')} 例：{o.get('example', '')}")
            else:
                lines.append(f"   - {o}")
    ev = q.get("evidence")
    if ev:
        lines.append(f"   证据：{ev}")
    return "\n".join(lines)


def hygiene_check(system_prompt: str, user_template_parts: list[str]) -> None:
    """Mode A 两道检查（守卫 2）: 指令 / 题目文本里不许有表现类关键词, 模板里不许有表现类占位符。
    笔记正文本身不查 —— 那是内容, 不是上下文。"""
    for ph in TEMPLATE_LEAK_PLACEHOLDERS:
        assert ph not in SYSTEM_TEMPLATE and all(ph not in p for p in user_template_parts), (
            f"feature 模板含表现类占位符 {ph!r} —— 违反 D-028 Mode A")
    for kw in PERFORMANCE_KEYWORDS:
        assert kw not in system_prompt, (
            f"feature 提示词（指令/题目）泄漏表现类关键词 {kw!r} —— 违反 D-028 Mode A")


def render_call(bank: dict, question_ids: list[str], spans: dict) -> dict:
    """把【一组】题渲染成一次调用。question_ids 必须同属一个 call_group 且 ≤ 4（守卫 4）。

    返回 {system, user, question_ids, spans_used, skipped: {qid: reason}}。
    skipped 是这次不问的题（拿不到标题 / 正文太短）, 调用方直接记 NULL。
    """
    idx = question_index(bank)
    qs = [idx[q] for q in question_ids]
    groups = {q["group"] for q in qs}
    if len(groups) != 1:
        raise ValueError(f"一次调用只能问一个组, 收到 {sorted(groups)} (docs/28 §5.3 防晕轮)")
    if len(qs) > MAX_GROUP_SIZE:
        raise ValueError(f"一次调用最多 {MAX_GROUP_SIZE} 题, 收到 {len(qs)}")
    (group,) = groups
    declared = set(bank["call_groups"][group])
    stray = [q["id"] for q in qs if q["id"] not in declared]
    if stray:
        raise ValueError(f"{stray} 不在 call_group {group} 里")

    skipped: dict[str, str] = {}
    asked: list[dict] = []
    for q in qs:
        sc = q["scope"]
        if sc == "title" and spans["title"] is None:
            skipped[q["id"]] = INVALID_NO_TITLE
        elif visible_len(spans[sc] or "") < (MIN_BODY_CHARS if sc in ("body", "full") else 2):
            skipped[q["id"]] = INVALID_TEXT_TOO_SHORT
        else:
            asked.append(q)
    if not asked:
        return {"system": "", "user": "", "question_ids": [], "spans_used": [], "skipped": skipped}

    qblock = "\n".join(_render_question(i + 1, q) for i, q in enumerate(asked))
    example = ", ".join(
        '{"id":"%s","answer":"%s","evidence":""}' % (q["id"], "是" if q["type"] == "bool" else "<选项>")
        for q in asked
    )
    system = SYSTEM_TEMPLATE.format(questions_block=qblock, answers_example=example,
                                    evidence_max=EVIDENCE_MAX_CHARS)
    spans_used = []
    for q in asked:
        if q["scope"] not in spans_used:
            spans_used.append(q["scope"])
    user_parts = [f"【{SPAN_LABELS[s]}】\n{spans[s]}" for s in spans_used]
    hygiene_check(system, [f"【{SPAN_LABELS[s]}】\n" for s in spans_used])
    return {
        "system": system,
        "user": "\n\n".join(user_parts) + "\n\n按上面的题目输出 JSON。",
        "question_ids": [q["id"] for q in asked],
        "spans_used": spans_used,
        "skipped": skipped,
    }


def plan_calls(bank: dict, single: bool = False) -> list[list[str]]:
    """本轮要发的调用清单: 默认按 call_groups 一组一次; single=True 每题单问（闸一对比用）。"""
    if single:
        return [[q["id"]] for q in llm_questions(bank)]
    return [list(members) for members in bank["call_groups"].values()]


# ═══════════════════════════════════════════════════════════════════════
# 答案校验
# ═══════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return _WS.sub("", s or "")


_EXEMPT_RE = re.compile(r"选「(.+?)」以外")


def evidence_required(q: dict, answer: str) -> bool:
    """什么时候必须给证据: bool 答「是」; choice 除非题目说「不需要」或选了豁免值（如 product_role 的 未出现）。"""
    ev = (q.get("evidence") or "").strip()
    if q["type"] == "bool":
        return answer == "是"
    if ev.startswith("不需要"):
        return False
    m = _EXEMPT_RE.search(ev)
    if m and answer == m.group(1):
        return False
    return True


def closed_set(q: dict) -> tuple[str, ...]:
    if q["type"] == "bool":
        return BOOL_VALUES
    return tuple(o["value"] if isinstance(o, dict) else o for o in q.get("options") or [])


def validate_answers(bank: dict, question_ids: list[str], parsed: Any, spans: dict
                     ) -> dict[str, dict]:
    """逐题校验模型输出（守卫 3）。返回 {qid: {answer, evidence, invalid_reason}}。

    · 答案不在闭集 → out_of_vocab
    · 需要证据而证据不是 scope 那段的原样子串（去空白比）→ evidence_not_found; 超 30 字 → evidence_too_long
    · 片段被截断且 scope 为 body/full 且答「否」→ span_truncated（「否」不能来自没看全的片段）
    · 缺题 → missing
    """
    idx = question_index(bank)
    got: dict[str, dict] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("answers"), list):
        for a in parsed["answers"]:
            if isinstance(a, dict) and a.get("id") in question_ids:
                got[a["id"]] = a
    out: dict[str, dict] = {}
    for qid in question_ids:
        q = idx[qid]
        a = got.get(qid)
        if a is None:
            out[qid] = {"answer": None, "evidence": None, "invalid_reason": INVALID_MISSING}
            continue
        ans = str(a.get("answer") if a.get("answer") is not None else "").strip()
        ev = str(a.get("evidence") or "").strip()
        if ans not in closed_set(q):
            out[qid] = {"answer": None, "evidence": ev or None, "invalid_reason": INVALID_OUT_OF_VOCAB}
            continue
        if evidence_required(q, ans):
            nev = _norm(ev)
            if not nev or nev not in _norm(spans[q["scope"]] or ""):
                out[qid] = {"answer": None, "evidence": ev or None, "invalid_reason": INVALID_EVIDENCE_NOT_FOUND}
                continue
            if len(nev) > EVIDENCE_MAX_CHARS:
                out[qid] = {"answer": None, "evidence": ev, "invalid_reason": INVALID_EVIDENCE_TOO_LONG}
                continue
        if spans.get("_truncated") and q["scope"] in TRUNCATABLE_SPANS and ans == "否":
            out[qid] = {"answer": None, "evidence": None, "invalid_reason": INVALID_SPAN_TRUNCATED}
            continue
        out[qid] = {"answer": ans, "evidence": ev or None, "invalid_reason": None}
    return out


def correction_note(results: dict[str, dict]) -> str:
    """重问一次时附的修正说明（只列没过的题）。"""
    lines = []
    for qid, r in results.items():
        reason = r.get("invalid_reason")
        if reason in (None, INVALID_SPAN_TRUNCATED):
            continue
        if reason == INVALID_OUT_OF_VOCAB:
            lines.append(f"- {qid}: 答案不在允许的取值里, 只能从题目给的选项/是否里选")
        elif reason == INVALID_EVIDENCE_NOT_FOUND:
            lines.append(f"- {qid}: evidence 不是指定那一段的原样片段。要么从原文一字不改地抄, 要么改答「否」")
        elif reason == INVALID_EVIDENCE_TOO_LONG:
            lines.append(f"- {qid}: evidence 超过 {EVIDENCE_MAX_CHARS} 字, 只抄最关键的一小段")
        elif reason == INVALID_MISSING:
            lines.append(f"- {qid}: 漏答了")
    return "\n".join(lines)


def retryable(results: dict[str, dict]) -> list[str]:
    return [qid for qid, r in results.items()
            if r.get("invalid_reason") not in (None, INVALID_SPAN_TRUNCATED)]


# ═══════════════════════════════════════════════════════════════════════
# 写作台下发（守卫 7）
# ═══════════════════════════════════════════════════════════════════════

def instructions_for_desk(bank: dict, validation_rows: list[dict]) -> list[dict]:
    """把 feature_validation 里 validated 的行翻成写作台能照做的话。

    ⚠️ aw_instruction=never 的题（efficacy_promise, 合规观察项）**永不出现**在返回里,
    不管闸二把它判成什么（docs/28 §4.2, D-065 续 第 1 条）。方向为「?」的新发现也要 owner 看过
    才下发, 这里只标 needs_owner_review, 不擅自过滤。

    另外两道闸（codex review on #141）:
      · **版本要对得上**: 结论行的 question_version / bank_version 必须等于当前问题库里的值。
        改过题的旧结论不许套到新题干上 —— 旧答案是按旧边界答的, 新指令是按新题干写的。
        缺字段一律不下发（fail-closed）。
      · **choice 题按取值下发**: 一道 choice 题只有一句 aw_instruction, 但闸二是【按取值】
        下结论的。`aw_instruction_values` 声明这句话对哪些取值成立（opening_type 只对
        「具体事件」）; 没声明就不自动下发。bool 题只认「是」。
    """
    idx = question_index(bank)
    bank_version = bank.get("bank_version")
    out: list[dict] = []
    for row in validation_rows:
        if row.get("status") != "validated":
            continue
        q = idx.get(row.get("question_id"))
        if q is None:
            continue
        if row.get("question_version") is None or int(row["question_version"]) != int(q["version"]):
            continue
        if row.get("bank_version") != bank_version:
            continue
        instr = q.get("aw_instruction")
        if not instr or str(instr).strip().lower() == "never":
            continue
        answer = row.get("answer")
        if q["type"] == "bool":
            if answer != "是":
                continue
        elif answer not in (q.get("aw_instruction_values") or []):
            continue
        out.append({
            "question_id": q["id"],
            "answer": row.get("answer"),
            "instruction": instr,
            "summary": row.get("summary"),
            "needs_owner_review": row.get("hypothesis") == "?",
        })
    return out
