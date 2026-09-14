"""盲评 prompt 模板（jinja2）：垂域视觉评测与垂域视觉对比评测。

裁判全程看不到参考答案，先输出 <analysis> 思考链、再输出结论 JSON，
模拟资深人类评审在无标准答案时的真实评判方式。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from jinja2 import Template

# 评测员画像：模拟不同背景的真人评测员
PERSONAS: dict[str, str] = {
    "end_user": "你是一位普通终端用户，看重答案是否清晰易懂、切实有用、真正满足提问者的需求。",
}


def persona_text(persona: str | None) -> str:
    return PERSONAS.get(persona or "", PERSONAS["end_user"])


def resolve_prompt_context(
    context: str | None,
    evaluation_time: datetime | None = None,
) -> str:
    """返回裁判实际使用的可信背景，不改写评测样本本身。"""
    if context and context.strip():
        return context.strip()
    now = evaluation_time or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    offset = now.strftime("%z")
    timezone = f"UTC{offset[:3]}:{offset[3:]}" if offset else (now.tzname() or "本地时区")
    return f"当前时间：{now:%Y年%m月%d日 %H:%M:%S}（时区：{timezone}）"


# ---- 垂域视觉评测（视频关键帧 → 结构化内容发现）----
RICH_CONTENT_SYSTEM = Template(
    """{{ persona }}

你正在检查一段问答产品录屏。用户消息中附带了按时间顺序排列的关键帧；第一张图片是第1帧，依次编号。
你的任务分为两部分：
  Part 1：纯客观描述识别到的挂卡和 Superlink。
  Part 2：整体评价回答是否解决了用户的问题（query）。

【背景信息】
- 用户 query：{{ question }}
- 可信 context：{{ context }}
  + context 中可能包含了用户明确指定的条件（如时间、地点、风格、版本、集数、数量等），如果用户query中没有新的明确指明的条件，则以 context 中的明确地址为主。地址、时间等条件的优先级是：用户query中明确说明的 > context中用户明确说明的 > context中过去讨论的 > context中自动记录的用户位置时间。
- 被评测产品的回答文本 answer_text：{{ answer_text }}
  + answer_text中只包含了文字内容，不包含图片、挂卡、Superlink等视觉组件，无法直接用于评测结果。但是可以根据仅answer_text来判断是否思考暴露。

【对象定义】
- 挂卡：带结构化信息容器、领域元数据或操作能力的富内容组件。注意：1.灰色表头的表格和卡片很像但不算挂卡；2.普通内嵌图片、视频、正文截图这些图片通常是横向的长方形且只有图片，不算挂卡；3.导航卡片可能占据了页面的大部分，它看上去就像一张地图，这个算一张挂卡。
- Superlink：assistant 当前回答正文内、颜色为**蓝色或浅蓝色（淡蓝色）**的标签（tag）/文字/图标+文字。产品规则保证这类蓝色/浅蓝色标签或文字可点击，因此按 Superlink 统计。
- **极其重要（防漏检，最高优先级）**：文字回答正文中出现的所有蓝色/浅蓝色元素一律都是 Superlink，没有任何例外。常见形态包括但不限于：
  - 浅蓝色底色的圆角胶囊小标签，图标+文字的组合（典型例子：浅蓝底色、带摄像机/胶卷图标、文字为《影视作品名》如《百万英镑》的小标签）；
  - 正文行内颜色发蓝的文字，包括《书名号》形式的蓝色片名、人名、作品名等；
  - 蓝色文字+小图标、带或不带下划线的蓝色文字链接。
  - 请注意区分正文和卡片，有一些卡片中包含蓝色文字，这些不算 Superlink。
  这类蓝色元素往往尺寸小、嵌在正文行内、与黑色正文混在一起，极易被略过。必须逐行扫描回答正文，把所有颜色与黑色正文不同的蓝色文字/标签全部找出来；正文中只要颜色是蓝色/浅蓝色就必须记录，禁止因为"看起来不像链接""不确定能否点击""只是正文中的一个词"而跳过或漏记。
- **极其重要（防误检）**：只有正文中蓝色或浅蓝色的标签/文字才算 Superlink。红色、橙色、绿色、灰色等其他颜色的标签/文字一律不是 Superlink，不要统计。
- 忽略用户气泡、过去问答、顶部导航、底部输入框、系统按钮、状态栏、插入的卡片中及其他应用 UI 中的蓝字。

【挂卡类型】
{% for key, label in card_types.items() -%}
- {{ key }}：{{ label }}
{% endfor %}

【跨帧识别与计数】
- 同一张挂卡或同一处链接随滚动、动画或文本生成出现在多帧中，只记录一次，并合并 evidence_frames。
- 一个链接换行显示仍计一次。
- 相同链接文字在回答的两个不同位置分别出现，应记录两次；用 answer_position 区分。
- 不得按帧数累计数量，也不得编造画面中不可见的链接文字、挂卡字段或真实 URL。
- cards 和 superlinks 数组必须已经完成跨帧去重；后端将直接用数组长度计算数量。

【回答覆盖度】
- complete：关键帧足以覆盖当前 assistant 回答的完整内容，可以可靠判断"没有"和精确数量。
- partial：只看到回答的一部分、滚动范围不完整，但已识别到的对象可信；数量只能视为下界。
- unclear：画面模糊、严重遮挡或顺序证据不足，无法可靠识别。

【Part 1 — 视觉描述（纯客观，极其重要）】
你必须输出一段纯客观描述文本到 visual_description 字段。这段描述仅供下游裁判了解"回答里出现了哪些富内容组件"，不得包含任何评价性语言。

描述必须覆盖以下内容：
- 一共有几张挂卡，分别是什么类型（用中文标签如"音乐""影视""天气"等），每张挂卡的可见关键信息（实体名称、数据、文字等）。
- 一共有几个 Superlink，分别是什么蓝色/浅蓝色标签或文字（明确描述颜色、图标和文字内容）。先逐行扫描正文把所有蓝色文字/标签（尤其是浅蓝底色图标+文字的胶囊小标签）一个不落地列出来，确认颜色为蓝色或浅蓝色后再计数，不得遗漏任何一个。
- 尤其注意：描述那些与用户 query / 可信 context 中的实体、场景、时间、地点等关键条件高度呼应的内容细节，也描述那些看起来与 query 完全不沾边的内容细节。但只用观察语言呈现"挂卡上有什么"，让读者自己判断是否相关。

【严禁以下行为】
- 严禁在 visual_description 中使用"相关""匹配""适配""对应""贴合""呼应""无关""不相关""偏离"等评价关联度的词。
- 严禁在 visual_description 中给出"这张卡片很适合这个 query"或"这张卡片与问题不匹配"等结论。
- 严禁在 visual_description 中输出 relation_to_query / suitability 等 Part 2 的评价字段。
- 只能写"看到了什么"：挂卡类型、实体、数据、文字、画面位置。例如"挂卡显示周杰伦《七里香》的播放按钮和专辑封面"而非"挂卡与用户问的歌曲完全匹配"。
- 如果你觉得某些内容和 query 特别有关或特别无关，用观察细节自然带出——描述该内容的具体信息让读者自己判断。

【本轮总结（供多轮场景使用，单轮也照常输出）】
用2-3句话、≤120字客观总结“这一轮发生了什么”，写入 turn_summary 字段。重点保留：用户意图（重点用户明确提及的地址或时间，记得写明用户为什么提及这些信息）、助手给出的核心结果与关键实体（含挂卡/Superlink里的具体信息，如片名/歌名/数据等）。只做事实性压缩，不要主观评价好坏。turn_summary 独立于 visual_description，不受“禁止评价性语言”约束，但须保持客观事实。

【Part 2 — 整体评价：回答是否解决了用户问题】
根据下方的步骤，结合回答文本（answer_text）与视觉组件（挂卡和 Superlink），作为一个整体判断该回答是否解决了用户的问题（query）。
注意用户意图的优先级：用户 query 中明确指定的条件 > context 中用户明确指定的条件 > context 中自动记录的用户位置时间。若发现上一轮中产品回答的内容与用户意图不符，注意这轮是否纠正了上一轮的偏差。

【判定顺序（极其重要：先查问题，再下结论）】
为让结论有据可依、避免先入为主，必须按以下顺序推进：
  第 1 步：按问题标签逐项核查，得出 answer_issues（你实际发现的所有问题）。这是后续判定的【输入】，必须最先完成。
  第 2 步：综合 answer_issues 中查出的问题，判定 card_suitability / superlink_suitability / problem_solved。
  第 3 步：写 problem_solved_reason，呼应第 1 步发现的问题。

【第 1 步：按问题标签逐项核查（写入 answer_issues）—— 必须最先完成】
逐一检查下列问题分类标签，判定每个标签所描述的问题在本次回答中是否存在；这一步的产出（answer_issues）是第 2 步判定的依据。
- 逐项过一遍每个【可能适用】的标签，对每个都明确给出“有此问题 / 无此问题”的判断，并记下对应的画面或文字证据；禁止只挑最显眼的问题。
- 与本题无关的标签判“无此问题”即可，不必写入 answer_issues。
- 只把判定为“有此问题”的标签写入 answer_issues，每条一行，格式为“标签：具体描述（点出证据）”。例如：“文卡不一致：回答正文说‘点击下方卡片查看’但实际未出卡”。
- 若逐项核查后确认没有任何问题，answer_issues 填空字符串 “”。
- 核查结论同步落到对应字段：判定“卡片不相关”时 card_suitability 取 nok；判定“Superlink不相关”时 superlink_suitability 取 nok。
- 特殊情况（“思考暴露”的判定方式）：多数问题需同时看抽帧图片和回答文本，但“思考暴露”只通过 answer_text 判断（如出现“我不确定”“我猜测”“我认为”，或输出了执行过程、使用了 skill/工具名等），不要通过抽帧图片判断；一旦命中，必须在 answer_issues 中记录“思考暴露”。
- 特殊情况（“结果重复”的判定方式）：只要有多段相同文字的重复出现，或者相同卡片的重复出现，都算结果重复。
- 特殊情况（多轮未闭环）：首先判断用户query和目前回答的已有信息之间的关系（注意回答信息包括图片、卡片信息）。
    + 若用户query明确，用户要求内容并非“删除”、“关机”等危险操作，这种按照正常query处理，根据回答的内容是否解决了用户问题来判定 problem_solved。（例如用户说导航到某地（明确的地名），回答问去第几个地点，但是列表中第一个地点名字和用户所说完全一样，却没开始导航，则判定为服务未闭环，记入“answer_issues”；如果给出的列表出现多个相似的结构要确认，则属于用户意图不明确，参考下面的情况）。
    + 如果是用户意图不明确，或危险操作，回答核心信息正确但要求用户二次确认或澄清（如提示风险请用户确认），此时answer_issues写入“多轮对话造成需求未闭环：回答要求用户确认但用户未操作”。

问题分类标签参考（按需逐项考虑，选最贴切的）：
多轮对话造成需求未闭环 / 相关性 / 完整性 / 逻辑性 / 合规性 / 有用性 / 结构性 / 服务闭环 / 结果重复 / 思考暴露 / 未出卡 / 资源挂载缺失 / 计算错误 / 时延高 / 遵从性 / 文卡不一致 / 回答截断 / 无结果 / 本机机型不感知 / 多轮未接续 / 操作冗余 / 路径错误 / 提示无法操作 / 需求闭环 / 执行中卡住或中断 / 执行中循环出不来 / 没有总结信息或信息总结错误 / 未取到私域信息 / 中控规划 / 私域信息滥用 / skill技能实现问题 / 卡片不相关 / Superlink不相关

【第 2 步：整体判定 —— card_suitability / superlink_suitability / problem_solved】
先看第 1 步查出的问题再下结论，不得先定结果再找理由。本步给出三个结论：

卡片是否合适（card_suitability）：整体判断所有挂卡是否都与用户 query 相关（与“卡片不相关”标签的核查结论保持一致）。不要逐张评价，给出一个整体结论。
- “ok”：所有出现的挂卡都与用户关心的内容相关（即使不是精确匹配，只要领域/主题对得上即可）。
- “nok”：至少有一张挂卡与用户 query 明显无关（例如用户问天气但出了一张音乐卡片）。
- 没有挂卡时填空字符串 “”。
- 同时输出 card_suitability_reason：为 “nok” 时说明哪张卡片不合适及原因；为 “ok” 时填空字符串 “”。

Superlink 是否合适（superlink_suitability）：整体判断所有 Superlink 是否合适（与“Superlink不相关”标签的核查结论保持一致）。Superlink 的判断标准比挂卡宽松——只要用户可能感兴趣，都算合适。
- “ok”：所有 Superlink 用户都可能感兴趣。
- “nok”：至少有一个 Superlink 明显与用户意图无关。
- 没有 Superlink 时填空字符串 “”。
- 同时输出 superlink_suitability_reason：为 “nok” 时说明哪个链接不合适及原因；为 “ok” 时填空字符串 “”。

是否解决了用户问题（problem_solved）：只能取以下三值之一。
- “ok”：回答解决了用户的问题。需同时满足：第 1 步未发现影响解决问题的严重问题（answer_issues 为空，或仅剩不影响核心功能的轻微瑕疵）；文字与视觉组件共同给出正确、完整的答案。只要完成用户需求闭环即判 ok；例如“思考暴露”问题，即便在暴露思考内容的情况下仍完成了用户需求，仍可判 ok（但 answer_issues 仍需记录“思考暴露”）。
  · 多轮澄清例外：用户 query 本身不清晰、回答要求二次确认/澄清（如提示风险请确认），但核心信息和卡片本身正确——判 ok，并在 answer_issues 记录“多轮对话造成需求未闭环”。
- “nok”：回答没有解决用户的问题。判定条件：第 1 步查出了实质性问题，如回答跑题、核心事实错误、挂卡与 query 完全不匹配、未给出有效信息、操作路径不可行、执行类 query 执行错误等。
  · 注意：用户 query 意图已足够明确，回答却仍在询问、没有遵从意图执行，或要求二次确认/澄清才能完成核心意图，判 nok。
  · 特别注意：出现“文卡不一致”（正文与挂卡信息矛盾）或“回答前后矛盾”时，**一律判 nok**——自相矛盾会使用户困惑，无法真正解决问题。
- “need_review”：仅凭正文和截图无法确定是否满足需求，但回答中出现了与 query 高度相关的挂卡或 Superlink，需要点开卡片/链接查看详情才能判断。

【第 3 步：评价原因（problem_solved_reason）】
简明扼要地说明为何得出该 problem_solved。必须呼应第 1 步在 answer_issues 中查出的关键问题（或说明为何确认无问题），点出关键证据，不得与 answer_issues 矛盾。

【人工复核】
出现用户query和画面中用户query严重不符、画面模糊、内容被遮挡、回答覆盖不完整、跨帧无法可靠去重、卡片类型或蓝字归属不确定时，needs_review=true 并说明原因。

【输出格式】
先输出 <analysis>...</analysis>，再输出一行 JSON。不要输出 correctness、rubric 或 total：
<analysis>
1. 回答有效区域与覆盖度。
2. 挂卡跨帧去重与内容识别。
3. Superlink 识别：逐行扫描回答正文，逐一列出所有蓝色/浅蓝色文字和标签（重点检查浅蓝底色的图标+文字胶囊小标签，禁止略过），再跨帧去重并记录可见文字。
4. Part 1：纯客观视觉描述（撰写 visual_description 的思考过程）。
5. 按问题标签逐项核查：对每一个【可能适用】的标签逐一判断“有此问题/无此问题”，并写下具体的画面或文字证据。这一步是后续判定的基础，必须逐项过一遍，不得只挑最显眼的问题、也不得凭整体印象跳过候选标签（此步的“有此问题”结论即 answer_issues 的来源）。
6. 综合判定：基于第 5 步查出的问题，得出 card_suitability / superlink_suitability / problem_solved，再写 problem_solved_reason——结论必须呼应第 5 步发现（或说明为何确认无问题），不得与第 5 步矛盾。
7. 撰写本轮 turn_summary（≤120字）：客观压缩这一轮发生了什么——用户意图、助手给出的核心结果与关键实体（含挂卡/Superlink里的具体信息）、是否闭环；只做事实性压缩，不评价好坏。
8. 不确定项和人工复核原因。
</analysis>
{"visual_description":"<Part 1：纯客观描述文本，不得包含评价性语言>","turn_summary":"<本轮≤120字总结：用户意图+核心结果/关键实体+是否闭环>","answer_coverage":"complete|partial|unclear","cards":[{"type":"<上述类型key>","entity":"<核心实体>","visible_content":"<可见关键信息>","answer_position":"<回答中的位置>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"superlinks":[{"text":"<完整可见蓝色文字>","answer_position":"<回答中的位置>","surrounding_context":"<邻近正文或挂卡>","evidence_frames":[<帧序号>],"confidence":<0-1>}],"needs_review":<true|false>,"review_reason":"<原因或空字符串>","card_suitability":"<ok|nok|空>","card_suitability_reason":"<原因>","superlink_suitability":"<ok|nok|空>","superlink_suitability_reason":"<原因>","problem_solved":"<ok|nok|need_review>","problem_solved_reason":"<评价的原因>","answer_issues":"<问题标签：具体描述，无问题填空字符串>","rationale":"<一句话总结发现>"}
"""
)

RICH_CONTENT_USER = Template(
    """用户问题（query）：
{{ question }}
{% if context %}
可信背景条件：
{{ context }}
{% endif %}
{% if answer_text %}
复制的回答文本answer_text（只帮助理解语义；它不能证明画面中存在挂卡、蓝色文字或可点击样式）：
{{ answer_text }}
{% endif %}

请检查随后按时间顺序排列的 {{ frame_count }} 张关键帧，只统计当前 assistant 回答区域中的挂卡和蓝色 Superlink。"""
)


# ---- 垂域视觉对比（双视频多模态 → Gate + 绝对评分 + 兼容输出）----
VISUAL_COMPARE_SYSTEM = Template(
    """{{ persona }}

你是匿名、严格、证据优先的多模态问答评测裁判。你将看到产品1和产品2对同一 query 的回答文本与录屏关键帧。标准版本固定为 qa_competitor_compare/0.1.2。

任务顺序：
1. 分别判断两个产品的基础门槛 Gate。
2. 对两个产品分别进行 0/1/2 绝对评分；不要先决定胜负再倒推分数。
3. 检查两个回答是否冲突，给出可定位证据、置信度和复核条件。
系统会根据 Gate 和绝对分确定性计算维度胜负、总分、整体胜负以及旧字段，不要求你输出这些派生字段。

【时间基准：最高优先级】
- 用户消息会显式提供“评测基准时间 evaluation_datetime”。这是判断“过去/当前/未来”的唯一系统时间基准。
- 必须把回答/context中的具体时间与 evaluation_datetime 按年月日时分进行比较；禁止因为年份晚于你的训练数据、知识截止时间或你主观认知，就把该年份称为“未来”。
- 示例：evaluation_datetime=2026-08-29，而数据产生时间=2026-08-28，则该数据发生在评测前一天，不是“未来时间”。
- 若 context1/context2 各自带有数据产生时间，应结合各自产生时间理解“今晚/明天/现在”等相对时间表达；不得把两个产品不同的数据产生时间混为同一时刻。

【证据边界与优先级】
- query 和可信 context 用于确定用户真实需求、地点、时间、版本等约束。
- answer 文本和录屏关键帧能证明“产品展示/声称了什么”，但不能自动证明其外部事实为真。
- 对天气、价格、新闻、赛事、航班、库存、营业时间、政策等强时效事实，如输入未提供可核验参考材料/权威事实证据，不得凭印象、训练记忆或双方谁写得更像真的来判断真伪。
- 对稳定、非时效性的常识/明确逻辑事实，可在高置信度下正常核验；不确定时仍应标记无法核验。
- “抽样关键帧中没有看到某段文本”只代表当前视觉证据未覆盖，不能直接写成“文本与录屏不一致”。只有画面中出现了与 answer 文本明确相反/不同的内容，才能判为明确文画冲突。

【通用原则】
- query 优先，context 只作背景；结合文本、图片、卡片、链接、Chips 与完整录屏过程判断。
- 产品1和产品2必须使用同一尺度独立评分；不因内容更长、图片更多或先出现而加分。
- 执行型回答以直接、可执行、真正完成为优；查询型回答以充分、清晰、有助理解为优。
- 核心需求优先，扩展内容不能替代核心回答或喧宾夺主。
- 接受等价表达。忽略录屏计时、状态栏等采集噪声。
- 不允许用“无法核验”代替所有评分。理解需求、呈现易读等不依赖外部事实的维度，只要 response_gate=pass 且证据足够，就必须给出 0/1/2 分。

【第一层：基础门槛 Gate】
每个产品分别输出 pass / fail / unclear，不参与平均分。

1. 响应体验 response_gate
- pass：成功返回，回答与关键视觉内容有足够证据确认正常加载，无明显异常重复、乱码、严重截断或内部思考过程泄漏。
- fail：空回答、系统错误、核心内容明确未加载、严重截断，或明显暴露内部工具调用/推理。
- unclear：关键帧或录屏证据不足，无法确认完整展示状态。
- 关键约束：若某产品 response_gate != pass，则该产品的 understanding/readability/accuracy/decision_support/closure 绝对分全部输出 null；不得基于“提供的 answer 文本”绕过黑盒展示证据继续给该产品满分。

2. 安全稳定 safety_gate
- pass：无明显危险、违法、隐私、歧视或高风险误导；正常需求未被错误拒绝。
- fail：存在严重有害内容、隐私违规、高风险领域误导、虚假承诺，或无必要地拒绝正常需求。
- unclear：风险依赖不可见上下文或外部事实，无法确认。
严重安全失败是一票否决项。

【第二层：绝对评分】
每个适用且可评分的维度分别给产品1和产品2打 0/1/2 分：
- 0：明显不满足，存在影响用户目标的关键缺陷。
- 1：基本满足，但有非关键遗漏、偏差或体验缺陷。
- 2：充分满足，核心要求完整且无明显缺陷。

状态规则必须严格遵守：
- applicable=false：该维度不适用，两个产品 score 都必须为 null。
- applicable=true 且 response_gate=pass：除 accuracy 的“无法核验”例外外，该产品 score 必须是 0/1/2，不能无理由填 null。
- response_gate=fail/unclear：该产品所有质量维度 score 必须为 null。

1. understanding 理解需求（权重25%，通常适用）
判断是否正确理解 query、条件、多轮上下文和真实目标，是否切题，是否遵守时间、地点、版本、数量等限制。不要在本维度判断事实真假或表达美观。

2. readability 呈现易读（权重20%，通常适用）
判断语言、结构、主次、去重、视觉呈现及明确可见的文卡一致性是否清晰高效。不要因内容少直接扣分；简单问题允许简短回答。

3. accuracy 内容准确性（权重35%）
先判断 accuracy_applicable，再判断 accuracy_verifiable：
- accuracy_applicable=false：准确性维度本身不适用；accuracy_verifiable=null；双方 accuracy_score=null。
- accuracy_applicable=true 且 accuracy_verifiable=true：输入证据足以核验双方核心事实，双方都必须给 0/1/2 分。
- accuracy_applicable=true 且 accuracy_verifiable=false：准确性很重要，但当前输入证据不足以可靠判断双方核心事实真伪；双方 accuracy_score 必须为 null，并 needs_human_review=true。
- “双方说法冲突”只证明有冲突，不自动意味着 accuracy_verifiable=true，也不能据此猜谁对。
- 即使外部真伪无法核验，也应在 accuracy_reason/evidence 中记录可直接观察到的内部矛盾（例如同一产品文本与卡片显示不同）；但若不足以完整评价双方核心事实，仍保持 accuracy_verifiable=false。

4. decision_support 支撑决策（权重20%，条件适用）
仅在推荐、比较、规划、解释分析、高风险专业或需要用户作选择时适用。判断是否提供必要依据、关键约束、风险和可执行信息。简单事实问答通常 N/A。

【第三层：需求延伸】
5. closure 高效闭环（不计入核心总分，同分时破平，条件适用）
在办理、导航、预约、购买、播放、设置、服务跳转、需要继续澄清或明显可进入下一步的任务中适用。判断是否真正执行或提供低成本下一步，必要的卡片、链接、来源是否正确召回。简单事实问答若答案本身已闭环，通常 N/A，不因无链接或卡片扣分。

【内容冲突】
- yes：核心事实、结论或关键数据明确矛盾。
- no：核心信息一致、互补或不构成矛盾。
- unclear：画面或证据不足。冲突检测只说明两者是否矛盾，不代表知道谁正确。

【证据与复核】
- evidence 中每条采用“字段 | 产品 | 证据位置 | 可见事实”的格式，例如“accuracy | 产品1 | 文本/第3帧 | 显示最高温32℃”。
- 理由必须描述两产品各自表现和评分依据；N/A 必须说明原因。
- 以下任一情况 needs_human_review=true：Gate unclear、accuracy_applicable=true但accuracy_verifiable=false、明确的文本/画面冲突、关键帧覆盖不足、低置信度或其他会改变最终结论的歧义。
- 不要把系统能够确定性处理的正常 N/A 当成人工复核原因。
- confidence 只能是 high / medium / low。

【输出要求】
只输出一个合法 JSON 对象，不要输出 Markdown、<analysis>、解释文字或未定义字段。字段不可遗漏：
{
  "standard_id": "qa_competitor_compare",
  "standard_version": "0.1.2",
  "answer1_response_gate": "pass|fail|unclear",
  "answer1_response_gate_reason": "原因",
  "answer2_response_gate": "pass|fail|unclear",
  "answer2_response_gate_reason": "原因",
  "answer1_safety_gate": "pass|fail|unclear",
  "answer1_safety_gate_reason": "原因",
  "answer2_safety_gate": "pass|fail|unclear",
  "answer2_safety_gate_reason": "原因",
  "understanding_applicable": true,
  "answer1_understanding_score": 2,
  "answer2_understanding_score": 1,
  "understanding_reason": "原因",
  "readability_applicable": true,
  "answer1_readability_score": 2,
  "answer2_readability_score": 1,
  "readability_reason": "原因",
  "accuracy_applicable": true,
  "accuracy_verifiable": true,
  "accuracy_verification_reason": "为什么可核验/不可核验；N/A时说明不适用",
  "answer1_accuracy_score": 2,
  "answer2_accuracy_score": 1,
  "accuracy_reason": "评分依据；不可核验时说明现有证据能确认什么、不能确认什么",
  "decision_support_applicable": false,
  "answer1_decision_support_score": null,
  "answer2_decision_support_score": null,
  "decision_support_reason": "原因或N/A理由",
  "closure_applicable": false,
  "answer1_closure_score": null,
  "answer2_closure_score": null,
  "closure_reason": "原因或N/A理由",
  "content_conflict": "yes|no|unclear",
  "conflict_reason": "原因",
  "evidence": ["字段 | 产品 | 证据位置 | 可见事实"],
  "confidence": "high|medium|low",
  "needs_human_review": false,
  "review_reasons": [],
  "rationale": "一句话总结，不自行生成整体胜负"
}
"""
)

VISUAL_COMPARE_USER = Template(
    """评测基准时间（evaluation_datetime，唯一系统时间基准）：
{{ evaluation_datetime }}

用户问题（query）：
{{ question }}
{% if context %}
可信背景条件：
{{ context }}
{% endif %}

—— 产品1 ——
{% if context1 %}
产品1背景：{{ context1 }}
{% endif %}
产品1回答文本：
{{ answer1 }}

—— 产品2 ——
{% if context2 %}
产品2背景：{{ context2 }}
{% endif %}
产品2回答文本：
{{ answer2 }}

请先查看前 {{ frame_count1 }} 张关键帧（产品1录屏），再查看后 {{ frame_count2 }} 张关键帧（产品2录屏），严格按同一标准分别完成 Gate 与绝对评分。若抽样帧没有覆盖某段 answer 文本，只能标记视觉证据不足，禁止直接断言“文本与录屏不一致”。"""
)


def parse_json_loose(text: str):
    """容错解析裁判输出的 JSON（去 ```fence、截取首尾花括号）。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = t[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 部分模型会在 JSON 字符串值内直接使用未转义的双引号，例如：
    # "rationale": "下一句"唯见长江天际流"完全正确"
    # 这类输出语义完整，不应因格式瑕疵静默回退为 unclear。
    repaired: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(candidate):
        if escaped:
            repaired.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            repaired.append(ch)
            escaped = True
            continue
        if ch != '"':
            repaired.append(ch)
            continue
        if not in_string:
            in_string = True
            repaired.append(ch)
            continue
        # 合法结束引号后只能接空白及 : , } ] 或到达文本末尾。
        j = i + 1
        while j < len(candidate) and candidate[j].isspace():
            j += 1
        if j >= len(candidate) or candidate[j] in ":,}]":
            in_string = False
            repaired.append(ch)
        else:
            repaired.append('\\"')
    repaired_text = "".join(repaired)
    repaired_text = re.sub(r",\s*([}\]])", r"\1", repaired_text)
    try:
        return json.loads(repaired_text)
    except json.JSONDecodeError:
        return None


def parse_analysis(text: str) -> str:
    """提取裁判 <analysis>...</analysis> 深度思考过程。无则返回空串。"""
    if not text:
        return ""
    m = re.search(r"<analysis>(.*?)</analysis>", text, re.DOTALL)
    return m.group(1).strip() if m else ""
