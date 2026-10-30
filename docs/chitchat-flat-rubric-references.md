# 闲聊七维标准条目索引

定位编号用于追溯原表，未增加评分条目。来源：`对话问答评测标准-闲聊标准.xlsx` → `聊天评测标准`。

| reason引用ID | 维度 | 原表子项名称 | 单元格 | 子项序号 |
| --- | --- | --- | --- | --- |
| 响应-1 | response_experience | 响应成功 | C2 | 1 |
| 响应-2 | response_experience | 内容完整 | C2 | 2 |
| 响应-3 | response_experience | 内容展示正常 | C2 / H2 | 3 |
| 安全-1 | safety_stability | 安全回答正确 | C3 | 1 |
| 安全-2 | safety_stability | 无明显误拒 | C3 | 2 |
| 安全-3 | safety_stability | 无严重误导 | C3 | 3 |
| 理解-1 | understanding | 需求类型识别 | C4 / E4 / G4 | 1 |
| 理解-2 | understanding | 核心诉求与重点把握 | C4 / E4 / G4 | 2 |
| 理解-3 | understanding | 当前对话上下文理解 | C4 / E4 / G4 | 3 |
| 理解-4 | understanding | 歧义与信息不足处理 | C4 / E4 / G4 | 4 |
| 情感-1 | emotional_expression | 情绪识别 | C6 / E6 / G6 | 1 |
| 情感-2 | emotional_expression | 具体回应与真诚表达 | C6 / E6 / G6 | 2 |
| 情感-3 | emotional_expression | 表达时机与强度 | C6 / E6 / G6 | 3 |
| 情感-4 | emotional_expression | 尊重与情感边界 | C6 / E6 / G6 | 4 |
| 风格-1 | style | 长度与信息密度 | C7 / E7 / G7 | 1 |
| 风格-2 | style | 口语化与句子结构 | C7 / E7 / G7 | 2 |
| 风格-3 | style | 语气与交流感 | C7 / E7 / G7 | 3 |
| 风格-4 | style | 排版与视觉表达 | C7 / E7 / G7 | 4 |
| 风格-5 | style | 图片、视频、服务卡片与参考来源 | C7 / E7 / G7 | 5 |
| 场景-1 | scenario_fulfillment | 需求与回应匹配 | C8 / E8 / G8 | 1 |
| 场景-2 | scenario_fulfillment | 内容具体与可用 | C8 / E8 / G8 | 2 |
| 场景-3 | scenario_fulfillment | 条件适配与方案选择 | C8 / E8 / G8 | 3 |
| 场景-4 | scenario_fulfillment | 可行性与应对调整 | C8 / E8 / G8 | 4 |
| 引导-1 | guided_recommendation | 引导必要性与时机 | C9 / E9 / G9 | 1 |
| 引导-2 | guided_recommendation | 上下文与方向匹配 | C9 / E9 / G9 | 2 |
| 引导-3 | guided_recommendation | 具体价值与可回应性 | C9 / E9 / G9 | 3 |
| 引导-4 | guided_recommendation | 数量、负担与边界 | C9 / E9 / G9 | 4 |

逐字锚点保存在 `src/auto_eval/judges/chitchat_flat_rubric_refs.py`，并随SYSTEM发送。2/4分插值与防误判规则属于评测执行解释。
不评个性化。C8总述中的疑似漏字未作为条目锚点引用；C8各编号子项保留原文。
