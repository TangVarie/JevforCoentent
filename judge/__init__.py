"""judge —— BYWOOD 内容生产的共用判定服务（Jev）。

一句话：同一个模型、同一套题库纪律、同一张账本。这个包只做三件事：
  1. 把题库（TV 的 fq 格式 / 本仓的 Jev 格式）变成 Jev 请求；
  2. 把 Jev 的答案变成带概率、带歧义标记、带证据句的闭集事实；
  3. 把事实写成 truth_vault.note_feature_answers 的行（SQL 或 PostgREST）。
它不写字，不解释理由，不判「会不会爆」。
"""

__version__ = "0.1.0"
