# 知识问答评估候选模板与输出设计

基于 `competitive_assessment` 的 `6ecac36208d3146c89013d02fd72239fe22e0192` 新增独立候选模板，保持现有测评数据格式、输入内容和图片分组不变。依据《测试部标准-知识问答领域语料.xlsx》的“评测标准”B3:E4，并参考垂域、教育办公补充要求。

本次交付是**完整Prompt模板及独立输出校验**。没有注册新的可运行协议，没有修改页面选择、旧协议、输入解析、裁判参数、统计或导出。不能把本候选输出直接交给旧结果模型：旧流程含Gate阻断及整数分处理，会错误清空0分或截断1.5分。候选口径确认后再做运行接入。

## 当前仓库的输入和输出

当前链路为 `web/parse_input.py::parse_jsonl` → `web/runner.py` → `judges/visual_compare_judge.py::VisualCompareJudge.evaluate`。裁判单次调用，无联网和工具。

外部数据继续使用 `query`（兼容`question`）、逐产品非空`contextN`、`answerN`及`screenshotN`或`videoN`，支持2/3产品。`id`由程序管理。当前导入要求各产品背景非空；确无额外背景时如实填写“无额外背景”，不编造条件。截图齐全优先采用长截图，否则走原有录屏校验；同一条记录不混用两种证据。可选题图和多轮结构保持原有规则。

单轮模板可获得的变量全部沿用现有接口：

| 类别 | 既有字段 |
| --- | --- |
| 共享任务 | `question`、`product_count`、`evaluation_datetime`、`evidence_mode` |
| 产品背景与辅助文本 | `context1/2/3`、`answer1/2/3` |
| 录屏帧数 | `frame_count1/2/3` |
| 长截图块数与边界 | `image_count1/2/3`、`split_manifest1/2/3` |
| 系统角色 | `persona` |

图片仍由现有适配器编码和追加，题图不是产品配图。多轮`assemble_conversation`会替换整个USER，因此四维评分、时间规则、状态、输出合同全部写在SYSTEM，适用于当前目标轮和各产品自己的历史。

**不增加`reference_answer`、`test_time`等输入参数。**这些新字段目前只会进入原始导出数据，不会自动传给裁判。参考答案、验收点及测试时点继续放在对应`contextN`，例如：

```json
{
  "id": "knowledge-case-001",
  "query": "一个小时等于多少分钟？",
  "product_count": 2,
  "context1": "[产品已知信息]用户咨询一般时间换算。\n[仅供裁判核对]参考答案：一小时等于60分钟。验收点：数值和单位正确。",
  "context2": "[产品已知信息]用户咨询一般时间换算。\n[仅供裁判核对]参考答案：一小时等于60分钟。验收点：数值和单位正确。",
  "answer1": "60分钟。",
  "answer2": "1小时等于60分钟。",
  "screenshot1": "/existing/evidence/product1.png",
  "screenshot2": "/existing/evidence/product2.png"
}
```

实际图片路径按现有平台配置提供。`[仅供裁判核对]`不能反推产品知道；`contextN`不能串用给其他产品。动态题需写明产品真实测试时间及出处，不能用本次`evaluation_datetime`、上传时间或模型正文自称日期补足；`task_start_time/task_end_time`只是录屏截取秒数。

原输出也不是统一的一个模型：稳定版、闲聊V2、时效协议有各自Schema和后处理。它们大体使用`answerN_*`扁平键及维度理由/证据。本候选沿用该风格，而不沿用旧Gate或1—5分规则。

## 四个评分维度

| 输出键 | 维度 | 允许分数 | 主要检查指标 |
| --- | --- | --- | --- |
| `accuracy` | 准确性 | 0、1、1.5、2 | 意图与指令、上下文、核心覆盖、事实概念数据及计算过程、时效地域、幻觉与伪造来源、前后一致 |
| `clarity_and_structure` | 回复清晰易懂与呈现形式结构化 | 0、1、2 | 语言术语、结论定位、可理解推导、层级顺序、排版重点及已知学段适配 |
| `content_quality` | 丰富程度、简洁度与专业性 | 0、1、2 | 内容范围与深度、必要条件例外风险、专业方法边界、信息增益、冗余偏题及引导语 |
| `multimedia_value` | 富媒体增益 | 0、1、2 | 来源可追溯与支撑、图视频实际帮助、卡片链接匹配及有证据的闭环作用 |

检查指标用于维内判定，不各自出分。不输出总分、维度均分、胜负、排名、内容冲突或旧Gate；这些不属于当前已经明确的新标准。题号、产品身份、产品数、标准版本与采集状态由程序管理，不让模型重复生成。

准确性1.5只用于核心及必要条件全对、非必要扩展有轻微错误；其他维度不允许1.5。准确性0是有效评分，不阻断其他维度，也不把空分变成0。

## 状态与字段合同

每维只有共享`DIM_evidence`字符串数组、非空`DIM_reason`，以及逐产品`answerN_DIM_status`和`answerN_DIM_score`。共享的是字段容器，理由必须逐段覆盖所有活动产品，各产品独立定分。双产品24键，三产品32键，没有条件性增减键。

| status | score | 含义 |
| --- | --- | --- |
| `scored` | 本维允许的数值 | 证据足以确定该维档位，0也是有效分 |
| `not_applicable` | `null` | 仅富媒体允许；任务无需且没有实际需要评价的媒体影响 |
| `insufficient_evidence` | `null` | 关键截图、背景、时间、上文或执行记录缺失 |
| `unverifiable` | `null` | 主张可见，但在现有无工具条件下真伪或支撑关系不可确定 |

前三维始终检查，不能借“不适用”跳过。无法判断不使用中间分代替。不要求模型输出`pending/未评/跳过`等程序流程状态。

## 双产品实际输出示例

以下是假设截图分别清楚展示“60分钟”和“1小时等于60分钟”、无媒体时的构造示例，不是对实际产品的判定。JSON中的定位应在真实评估时替换为实际可见证据。

<!-- output-example -->
```json
{
  "accuracy_evidence": ["产品1 当前轮正文：60分钟。", "产品2 当前轮正文：1小时等于60分钟。"],
  "accuracy_reason": "产品1：数值与单位正确，完整回答核心问题，给2分；产品2：时间换算正确且表达完整，给2分。",
  "answer1_accuracy_status": "scored",
  "answer1_accuracy_score": 2,
  "answer2_accuracy_status": "scored",
  "answer2_accuracy_score": 2,
  "clarity_and_structure_evidence": ["产品1 当前轮正文仅一句答案。", "产品2 当前轮用一句话表达等式关系。"],
  "clarity_and_structure_reason": "产品1：答案简短易找，无需额外标题，给2分；产品2：结论明确且便于理解，给2分。",
  "answer1_clarity_and_structure_status": "scored",
  "answer1_clarity_and_structure_score": 2,
  "answer2_clarity_and_structure_status": "scored",
  "answer2_clarity_and_structure_score": 2,
  "content_quality_evidence": ["产品1 直接给出所问时长。", "产品2 直接说明小时与分钟的换算。"],
  "content_quality_reason": "产品1：完整简洁，无必要扩展，给2分；产品2：内容范围适当，无冗余，给2分。",
  "answer1_content_quality_status": "scored",
  "answer1_content_quality_score": 2,
  "answer2_content_quality_status": "scored",
  "answer2_content_quality_score": 2,
  "multimedia_value_evidence": [],
  "multimedia_value_reason": "产品1：简单时间换算无需富媒体，且未提供需要评价的媒体，不适用；产品2：纯文字已充分完成任务，无媒体评价对象，不适用。",
  "answer1_multimedia_value_status": "not_applicable",
  "answer1_multimedia_value_score": null,
  "answer2_multimedia_value_status": "not_applicable",
  "answer2_multimedia_value_score": null
}
```

三产品只在每维追加`answer3_DIM_status`与`answer3_DIM_score`，共享理由和证据同时覆盖产品3；没有其他结构变化。

## 候选中采用的执行口径

这些是根据前次标准审阅形成的候选方案，不能表述为原工作簿已统一规定：

1. 按主表将清晰易懂与结构化合并成四维，暂不沿用示例表拆分后的第五维。
2. 以核心影响区分准确性0/1；1.5严格限非必要扩展。无足够依据不默认满分。确证0可直接0；否则未知可能改变档位时空分，保留已知问题。
3. 不设置准确性Gate，其他维度可独立评分；风险信息按实际准确性或内容充分性影响评价，不创建未定义的拟人化分数。
4. 必要隐含需求有任务边界，简单完整答案可满分；标题加粗和媒体不是形式打卡项。
5. 富媒体按任务必要性及实际帮助定档，只有该维可不适用。三类媒体按严重实质影响合并，不机械平均；统一处理通用与教育办公媒体条款的冲突。
6. 静态截图不能确认隐藏来源全文、死链和播放成功；非决定性缺证不让整维空分。复制文本缺字及切片边界不作产品缺陷。
7. 不新增输入，参考与时间继续通过现有逐产品背景；新标准不把本次评估调用时间当产品测试时间。

## 文件与使用方式

- `src/auto_eval/judges/visual_compare_prompt_knowledge_qa.py`：完整SYSTEM、原输入结构USER、双/三产品模板生成器。
- `src/auto_eval/judges/knowledge_qa_schema.py`：独立严格Schema、精确字段集合和JSON解析校验；拒绝重复键、额外字段、字符串/布尔分数、非法小数和状态矛盾，不语义补写评分。
- `tests/test_knowledge_qa_contract.py`、`tests/test_knowledge_qa_prompt.py`：数值边界、状态、字段完整性、原输入渲染和原JSONL导入测试，不调用真实裁判。

```python
from auto_eval.judges.visual_compare_prompt_knowledge_qa import (
    render_system_prompt, VISUAL_COMPARE_USER,
)
from auto_eval.judges.knowledge_qa_schema import parse_output

system = render_system_prompt(product_count=2, evidence_mode="long_screenshot")
user = VISUAL_COMPARE_USER.render(**existing_render_variables)
# 图片仍需按现有请求方式追加；不能只给以上文字就声称完成了视觉评估。
observation = parse_output(raw_model_json, product_count=2)
result = observation.model_dump()
```

这段示例演示模板与契约的独立使用，不表示平台已经有该协议的运行入口。后续接入需适配协议注册和版本、严格解析/结果分流、0与1.5分统计、历史导出、UI及人工基准校验；须维持新旧协议结果隔离。结构测试不能证明裁判评分效果，需另用经过审核的标杆样本验证。
