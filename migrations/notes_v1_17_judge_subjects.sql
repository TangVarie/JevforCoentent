-- ════════════════════════════════════════════════════════════════════
-- truth_vault v1.17 · 判定账本扩三类 subject（judge 仓 docs/31 §2.4; TV D-085）
-- ════════════════════════════════════════════════════════════════════
-- note_feature_answers.subject_type 现在只允许 'note' / 'aw_version'（notes_v1_13:20）。
-- 加三类：
--   comment        → truth_vault.comments.comment_id（评论题库：读者侧 7 题 + 运营侧 comment_intent）
--   ssll_sample    → 三省六部批量采样的一篇（sample_one_cell 保留正文后才有 id；id 形如 <run_id>:<cell>:<代际>:<seed>，
--                    代际区分全局 / cell 级修订后按同一 seed 重采的那一批，否则新旧两批正文会写到同一个 subject_id 上）
--   external_note  → 外部语料（TikHub 抓的公开小红书笔记，id 用平台 note_id；正文与互动数在 notes_v1_18 的
--                    external_notes）；不进 v_l2_labels，只做参考分布与闸二外部复核
-- 幂等：CHECK 约束先删后建；不改主键、不改列。跑两遍结果一样。
-- content_scores 的同名 CHECK 不跟着扩：打分器今天只打 note / aw_version，要给别的主体打分时另起迁移。
--
-- 部署: 必须在 notes_v1_13 之后（改的就是 v1_13 建的表，早跑 → relation does not exist）。
--   TV 的部署链里 v1_13 排在 v1_16 之后、是当前链的最后一环（scripts/README.md Step 0；编号 13
--   落仓晚于 14/15/16，不改号），所以「v1_16 之后」不够，要写成「v1_13 之后」。之后接 notes_v1_18。
--   psql -d <shared_supabase> -f notes_v1_17_judge_subjects.sql
-- 这份文件由 judge 仓提供，TV 的 schemas/ 落同名一份。TV 改过的只有注释：头注释（供应商、部署顺序）
-- 与 prob 列 COMMENT 里旧口径的出处（judge 仓的 docs/31 不在 TV 里）；约束与列一字未动。
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
-- TV 侧写 prob 的只有 scripts/ingest_gate1_answers.py（annotate_feature_pass 恒写 NULL），D-085 起按本口径写。
COMMENT ON COLUMN truth_vault.note_feature_answers.prob IS
    'v1.17 起：所选答案的概率（是非题 max(p,1-p)，选择题第一名）。歧义不落库，读取时按题库阈值算。旧口径：run_tag gate1-20260928 的 Jev 三张表 bool 题存 P(是)（TV D-085；judge 仓 docs/31 §4.2）';
