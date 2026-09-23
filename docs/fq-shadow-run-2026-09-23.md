# 特征层影子跑 · feature_questions_v0_1 fq-v0.1 × jev-1.13.0 · 50 篇

调用 100 次，输入 373,644 / 输出 58,669 token，单次平均 521 ms（最慢 853 ms）。歧义格 121/999（12.1%）。

证据选句：要证据 334 格，选「没有」26 格；与现行抽取器同答「是」且有证据的 237 格里，两边证据重合（子串或 4 字片段 60% 以上）181 格（76%）。

| 题 | 型 | Jev vs 现行 | Jev vs D-081 表 | 现行 vs D-081 表 | Jev 歧义 | Jev NULL | 一致时 p | 不一致时 p |
|---|---|---|---|---|---|---|---|---|
| title_is_question | noul | 46/46 (100%) | 49/49 (100%) | 46/46 (100%) | 1 | 1 | 0.94 | – |
| opening_type | choice | 36/47 (77%) | 44/50 (88%) | 37/47 (79%) | 9 | 0 | 0.91 | 0.59 |
| has_specific_time | noul | 41/46 (89%) | 46/50 (92%) | 43/46 (93%) | 4 | 0 | 0.88 | 0.70 |
| has_specific_place | noul | 40/46 (87%) | 47/50 (94%) | 43/46 (93%) | 12 | 0 | 0.82 | 0.69 |
| has_direct_quote | noul | 45/46 (98%) | 50/50 (100%) | 45/46 (98%) | 3 | 0 | 0.91 | 0.60 |
| has_body_sensation | noul | 44/46 (96%) | 49/50 (98%) | 45/46 (98%) | 7 | 0 | 0.84 | 0.68 |
| ending_asks_reader | noul | 41/46 (89%) | 47/50 (94%) | 42/46 (91%) | 5 | 0 | 0.94 | 0.65 |
| invites_sharing | noul | 46/46 (100%) | 47/50 (94%) | 43/46 (93%) | 7 | 0 | 0.87 | – |
| asks_for_help | noul | 40/45 (89%) | 49/50 (98%) | 41/45 (91%) | 2 | 0 | 0.90 | 0.74 |
| withholds_product_name | noul | 45/47 (96%) | 50/50 (100%) | 45/47 (96%) | 0 | 0 | 0.95 | 0.76 |
| divisive_claim | noul | 44/47 (94%) | 49/50 (98%) | 45/47 (96%) | 9 | 0 | 0.78 | 0.61 |
| product_role | choice | 38/46 (83%) | 43/50 (86%) | 33/46 (72%) | 13 | 0 | 0.86 | 0.58 |
| efficacy_promise | noul | 46/47 (98%) | 49/50 (98%) | 47/47 (100%) | 1 | 0 | 0.96 | 0.52 |
| narrator_identity | noul | 40/47 (85%) | 49/50 (98%) | 39/47 (83%) | 5 | 0 | 0.86 | 0.69 |
| own_experience | noul | 44/47 (94%) | 50/50 (100%) | 44/47 (94%) | 8 | 0 | 0.89 | 0.56 |
| comparison_group | noul | 44/47 (94%) | 50/50 (100%) | 44/47 (94%) | 2 | 0 | 0.87 | 0.77 |
| calls_out_reader_group | noul | 41/47 (87%) | 46/50 (92%) | 39/47 (83%) | 8 | 0 | 0.83 | 0.59 |
| turning_point | noul | 34/45 (76%) | 44/50 (88%) | 31/45 (69%) | 13 | 0 | 0.82 | 0.65 |
| judged_by_others | noul | 40/42 (95%) | 48/50 (96%) | 38/42 (90%) | 5 | 0 | 0.89 | 0.70 |
| negative_outcome_happened | noul | 40/45 (89%) | 48/50 (96%) | 38/45 (84%) | 7 | 0 | 0.88 | 0.72 |
| **合计** | | 835/921 (91%) | 954/999 (95%) | 828/921 (90%) | 121 | 1 | | |

## 与现行抽取器不一致的格子（篇 · 题 · 现行 → Jev · Jev 概率 · 歧义）

- NRT_phase2_recuXYC07aiPXm · asks_for_help · 否 → 是 · 0.83 · 
- NRT_phase3_recv1A3EXVZLGU · asks_for_help · 是 → 否 · 0.57 · 歧义
- NUC_phase1_recv3rSop52dZR · asks_for_help · 否 → 是 · 0.76 · 
- NUC_phase1_recv59erAaKllf · asks_for_help · 否 → 是 · 0.88 · 
- OKMAN_phase1_recvoEvW6RrpbZ · asks_for_help · 否 → 是 · 0.66 · 
- NRT_phase2_recuYyYJm2MuYC · calls_out_reader_group · 是 → 否 · 0.67 · 
- NRT_phase2_recuZkIQtVBW1l · calls_out_reader_group · 是 → 否 · 0.55 · 歧义
- NUC_phase1_recv2Z8m1lnHwZ · calls_out_reader_group · 是 → 否 · 0.61 · 歧义
- NUC_phase1_recv2lNktp6h13 · calls_out_reader_group · 是 → 否 · 0.51 · 歧义
- SPX_phase1_recvqVAd4Gs0Lw · calls_out_reader_group · 否 → 是 · 0.55 · 歧义
- SPX_phase1_recvqVAd4GvnCd · calls_out_reader_group · 是 → 否 · 0.67 · 
- NRT_phase2_recuYyYJm2MuYC · comparison_group · 否 → 是 · 0.86 · 
- NUC_phase1_recv59erAaKllf · comparison_group · 否 → 是 · 0.6 · 歧义
- SPX_phase1_recvtWVFOWlNeJ · comparison_group · 是 → 否 · 0.85 · 
- NUC_phase1_recv4LUeQWvDEd · divisive_claim · 否 → 是 · 0.74 · 
- NUC_phase1_recv59erAaKllf · divisive_claim · 否 → 是 · 0.54 · 歧义
- SPX_phase1_recvqVAd4GvnCd · divisive_claim · 是 → 否 · 0.56 · 歧义
- SPX_phase1_recvsxkitAgg8c · efficacy_promise · 否 → 是 · 0.52 · 歧义
- NRT_phase2_recuYs7CP28GTg · ending_asks_reader · 是 → 否 · 0.62 · 歧义
- NUC_phase1_recv3rSop52dZR · ending_asks_reader · 是 → 否 · 0.53 · 歧义
- OKMAN_phase1_recvmpA0N0zZng · ending_asks_reader · 是 → 否 · 0.63 · 歧义
- OKMAN_phase1_recvrTMpirGoN0 · ending_asks_reader · 否 → 是 · 0.9 · 
- OKMAN_phase1_recvrd83rmeBC5 · ending_asks_reader · 是 → 否 · 0.55 · 歧义
- NRT_phase2_recuZkIQtVBW1l · has_body_sensation · 否 → 是 · 0.81 · 
- NUC_phase1_recv4LUeQWvDEd · has_body_sensation · 是 → 否 · 0.54 · 歧义
- SPX_phase1_recvqVAd4GvnCd · has_direct_quote · 否 → 是 · 0.6 · 歧义
- NRT_phase2_recuYs7CP28GTg · has_specific_place · 否 → 是 · 0.88 · 
- NRT_phase2_recuZkJtsfhvnx · has_specific_place · 是 → 否 · 0.54 · 歧义
- NRT_phase3_recv0wDwqPB9P5 · has_specific_place · 是 → 否 · 0.79 · 
- NUC_phase1_recv59erAaKllf · has_specific_place · 是 → 否 · 0.5 · 歧义
- SPX_phase1_recvsxkitAgg8c · has_specific_place · 否 → 是 · 0.77 · 
- SPX_phase1_recvtWVFOWlNeJ · has_specific_place · 否 → 是 · 0.64 · 歧义
- NRT_phase2_recuXYC07aiPXm · has_specific_time · 是 → 否 · 0.62 · 歧义
- NUC_phase1_recv2Z8m1lnHwZ · has_specific_time · 否 → 是 · 0.69 · 
- NUC_phase1_recv4LUeQWvDEd · has_specific_time · 是 → 否 · 0.73 · 
- OKMAN_phase1_recvngg4GKkBDr · has_specific_time · 是 → 否 · 0.78 · 
- SPX_phase1_recvtWVFOW0mGs · has_specific_time · 否 → 是 · 0.69 · 
- NRT_phase2_recuXYC07aiPXm · judged_by_others · 否 → 是 · 0.63 · 歧义
- NUC_phase1_recv2lNktp6h13 · judged_by_others · 否 → 是 · 0.77 · 
- NRT_phase2_recuYyYJm2KerZ · narrator_identity · 否 → 是 · 0.77 · 
- NRT_phase3_recv0xer3f2I22 · narrator_identity · 否 → 是 · 0.7 · 
- NRT_phase3_recv4TC4JgofIx · narrator_identity · 否 → 是 · 0.55 · 歧义
- NUC_phase1_recv2Z8m1lnHwZ · narrator_identity · 否 → 是 · 0.76 · 
- NUC_phase1_recv3JWU20d8TP · narrator_identity · 否 → 是 · 0.72 · 
- NUC_phase1_recv3K4xf09gDe · narrator_identity · 否 → 是 · 0.76 · 
- SPX_phase1_recvqVAd4Gs0Lw · narrator_identity · 否 → 是 · 0.58 · 歧义
- NRT_phase3_recv0xer3flcF3 · negative_outcome_happened · 否 → 是 · 0.87 · 
- NUC_phase1_recv2Z8m1l4wUd · negative_outcome_happened · 否 → 是 · 0.83 · 
- NUC_phase1_recv2Z8m1lnHwZ · negative_outcome_happened · 否 → 是 · 0.51 · 歧义
- NUC_phase1_recv59erAaKllf · negative_outcome_happened · 是 → 否 · 0.56 · 歧义
- OKMAN_phase1_recvqtis0iHKYk · negative_outcome_happened · 否 → 是 · 0.85 · 
- NRT_phase2_recuZ8Lij7vGAj · opening_type · 具体事件 → 数据事实 · 0.5 · 歧义
- NRT_phase3_recv0xer3flcF3 · opening_type · 具体事件 → 数据事实 · 0.43 · 歧义
- NUC_phase1_recv2Z8m1lpGw1 · opening_type · 具体事件 → 观点断言 · 0.58 · 歧义
- NUC_phase1_recv4LUeQWvDEd · opening_type · 感叹情绪 → 观点断言 · 0.61 · 
- NUC_phase1_recv59erAaKllf · opening_type · 具体事件 → 身份自述 · 0.71 · 
- OKMAN_phase1_recvnBz7VKNxqT · opening_type · 感叹情绪 → 观点断言 · 0.53 · 歧义
- OKMAN_phase1_recvqtis0iHKYk · opening_type · 感叹情绪 → 观点断言 · 0.62 · 
- OKMAN_phase1_recvrTMpirGoN0 · opening_type · 观点断言 → 提问 · 0.57 · 歧义
- SPX_phase1_recvqVAd4Gs0Lw · opening_type · 具体事件 → 身份自述 · 0.62 · 
- SPX_phase1_recvqVAd4GvnCd · opening_type · 身份自述 → 具体事件 · 0.54 · 歧义
- SPX_phase1_recvtWVFOW0mGs · opening_type · 感叹情绪 → 具体事件 · 0.76 · 
- OKMAN_phase1_recvoEvW6RrpbZ · own_experience · 是 → 否 · 0.53 · 歧义
- OKMAN_phase1_recvqtis0iHKYk · own_experience · 否 → 是 · 0.55 · 歧义
- SPX_phase1_recvqVAd4Gs0Lw · own_experience · 否 → 是 · 0.59 · 歧义
- NRT_phase2_recuZ8Lij7vGAj · product_role · 解决方案 → 主角 · 0.55 · 歧义
- NRT_phase3_recv0wDwqPB9P5 · product_role · 解决方案 → 主角 · 0.61 · 
- NRT_phase3_recv1vrL72SFzE · product_role · 顺带一提 → 未出现 · 0.58 · 歧义
- NUC_phase1_recv2Z8m1lpGw1 · product_role · 未出现 → 解决方案 · 0.43 · 歧义
- NUC_phase1_recv2lNktp6h13 · product_role · 只暗示 → 解决方案 · 0.83 · 
- NUC_phase1_recv3K4xf09gDe · product_role · 未出现 → 解决方案 · 0.31 · 歧义
- NUC_phase1_recv59erAaKllf · product_role · 主角 → 解决方案 · 0.87 · 
- SPX_phase1_recvqVAd4Gs0Lw · product_role · 未出现 → 主角 · 0.42 · 歧义
- NRT_phase2_recuXp4TjZJ1mu · turning_point · 否 → 是 · 0.66 · 
- NRT_phase2_recuYs7CP28GTg · turning_point · 否 → 是 · 0.53 · 歧义
- NRT_phase2_recuYyYJm2KerZ · turning_point · 是 → 否 · 0.51 · 歧义
- NRT_phase3_recv0wDwqPB9P5 · turning_point · 否 → 是 · 0.52 · 歧义
- NRT_phase3_recv1ARtq6nE06 · turning_point · 否 → 是 · 0.8 · 
- NRT_phase3_recv1vrL72SFzE · turning_point · 否 → 是 · 0.79 · 
- NUC_phase1_recv2xd6lzC7a0 · turning_point · 否 → 是 · 0.69 · 
- NUC_phase1_recv3K4xf09gDe · turning_point · 否 → 是 · 0.83 · 
- NUC_phase1_recv3rSop52dZR · turning_point · 否 → 是 · 0.59 · 歧义
- OKMAN_phase1_recvngg4GKFMep · turning_point · 否 → 是 · 0.59 · 歧义
- SPX_phase1_recvsxkitAfnoK · turning_point · 否 → 是 · 0.64 · 歧义
- NUC_phase1_recv2lNktp6h13 · withholds_product_name · 是 → 否 · 0.69 · 
- OKMAN_phase1_recvoEvW6RrpbZ · withholds_product_name · 是 → 否 · 0.83 · 

## 证据核对（按题）

| 题 | 两边证据重合 |
|---|---|
| title_is_question | 17/18 |
| has_specific_time | 22/26 |
| has_specific_place | 14/21 |
| has_direct_quote | 13/15 |
| has_body_sensation | 10/12 |
| ending_asks_reader | 17/22 |
| invites_sharing | 2/2 |
| asks_for_help | 17/24 |
| divisive_claim | 3/3 |
| narrator_identity | 10/17 |
| own_experience | 20/32 |
| comparison_group | 3/3 |
| calls_out_reader_group | 3/3 |
| turning_point | 5/8 |
| judged_by_others | 5/7 |
| negative_outcome_happened | 20/24 |