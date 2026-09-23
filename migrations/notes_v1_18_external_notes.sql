-- ════════════════════════════════════════════════════════════════════
-- truth_vault v1.18 · 外部笔记表（docs/31 §3 位置 ⑧）
-- ════════════════════════════════════════════════════════════════════
-- 存外部公开笔记的正文与互动数；判定答案在 note_feature_answers(subject_type='external_note', subject_id=note_id)。
-- 互动数只在这张表里，参考分布按它分组；任何进 Jev 的 state 都不带它（Mode A）。
-- 不进 v_l2_labels、不进书架、不进 reference_samples 的自动通道；要给三省六部用时带 source='tikhub' 另走一条。
-- 幂等：IF NOT EXISTS。RLS 与 TV 其余表同（ENABLE，service_role 写）。
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS truth_vault.external_notes (
    note_id        TEXT PRIMARY KEY,          -- 平台 note_id
    platform       TEXT NOT NULL DEFAULT 'xiaohongshu',
    category       TEXT NOT NULL,             -- config 里的品类名
    keyword        TEXT NOT NULL,             -- 搜到它的关键词
    sort_type      TEXT,                      -- popularity_descending / general / …（哪一半来的）
    title          TEXT,
    body           TEXT NOT NULL,
    author         TEXT,
    author_id      TEXT,
    liked          INT,
    collected      INT,
    comments       INT,
    shares         INT,
    publish_time   TEXT,                      -- 平台给什么存什么，不推算
    voice          TEXT,                      -- 分诊：普通用户 / 达人测评 / 商家或品牌 / 媒体或科普
    ad_like        BOOLEAN,
    has_product    BOOLEAN,
    run_id         TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'tikhub',
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tv_external_notes_cat ON truth_vault.external_notes (category, fetched_at DESC);
ALTER TABLE truth_vault.external_notes ENABLE ROW LEVEL SECURITY;

-- 参考分布视图：每题每取值在「高互动组（同品类互动数前四分之一）」和其余里的占比
-- 分位数按品类先 GROUP BY 算成一张 CTE 再 JOIN：PostgreSQL 不允许有序集聚合（percentile_cont）配 OVER 窗口
-- （报错 "OVER is not supported for ordered-set aggregate percentile_cont"）。PG 16 实跑验证过能建、能查，share 每组求和为 1。
-- 分组带 question_version / extractor：题目改版或模型升级后新旧两套行不混在一个分母里（与 v_feature_contrast 同口径）。
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
SELECT g.category, a.question_id, a.question_version, a.extractor, a.answer, g.grp, COUNT(*) AS n,
       ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER (PARTITION BY g.category, a.question_id, a.question_version, a.extractor, g.grp), 3) AS share
FROM truth_vault.note_feature_answers a
JOIN grp g ON g.note_id = a.subject_id
WHERE a.subject_type = 'external_note' AND a.run_tag = 'external' AND a.answer IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6;
