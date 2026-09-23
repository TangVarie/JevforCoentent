-- ════════════════════════════════════════════════════════════════════
-- truth_vault v1.17 · 判定账本扩两类 subject（docs/31 §2.4）
-- ════════════════════════════════════════════════════════════════════
-- note_feature_answers.subject_type 现在只允许 'note' / 'aw_version'（notes_v1_13:20）。
-- 加三类：
--   comment        → truth_vault.comments.comment_id（评论题库：读者侧 7 题 + 运营侧 comment_intent）
--   ssll_sample    → 三省六部批量采样的一篇（sample_one_cell 落库后才有 id；id 形如 <run_id>:<cell>:<seed>）
--   external_note  → 外部语料（SocialDataX 抓的公开笔记，id 用平台 note_id）；不进 v_l2_labels，只做参考分布与闸二外部复核
-- 幂等：CHECK 约束先删后建；不改主键、不改列。跑两遍结果一样。
-- 这份文件由 judge 仓提供，在 TV 的 schemas/ 里落一份同名文件后按 TV 的部署顺序执行（v1_16 之后）。
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE truth_vault.note_feature_answers
    DROP CONSTRAINT IF EXISTS note_feature_answers_subject_type_check;
ALTER TABLE truth_vault.note_feature_answers
    ADD CONSTRAINT note_feature_answers_subject_type_check
    CHECK (subject_type IN ('note', 'aw_version', 'comment', 'ssll_sample', 'external_note'));

COMMENT ON COLUMN truth_vault.note_feature_answers.subject_type IS
    'note → notes.note_id; aw_version → autowriter.versions.id::text; comment → comments.comment_id; ssll_sample → 三省六部采样 id; external_note → 外部公开笔记（v1.17）';

-- prob 口径（v1.17 起统一）：所选答案的概率。是非题 = max(p, 1-p)，选择题 = 第一名概率。
-- 之前 gate1-* 里 bool 题存的是 P(是)，choice 题存的是第一名，只有低把握格才有值；不回改，按 run_tag 区分。
COMMENT ON COLUMN truth_vault.note_feature_answers.prob IS
    'v1.17 起：所选答案的概率（是非题 max(p,1-p)，选择题第一名）。歧义不落库，读取时按题库阈值算。gate1-* 旧口径见 docs/31 §4.2';
