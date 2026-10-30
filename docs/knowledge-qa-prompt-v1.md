# 知识问答评估候选模板与输出设计

基于 `competitive_assessment` 的 `6ecac36208d3146c89013d02fd72239fe22e0192` 新增独立候选模板，保持现有测评数据格式、输入内容和图片分组不变。依据《测试部标准-知识问答领域语料.xlsx》的“评测标准”B3:E4，并参考垂域、教育办公补充要求。

平台新增可选标签**知识问答V1·0-2分（候选）**，协议为`knowledge_qa_compare@1.0`，实现版本为`1.0.0`，结果Schema为`knowledge-qa-result-2`。它使用独立结果模型及统计、展示和导出路径，不套用旧Gate或整数分逻辑，保留0和准确性的1.5分。默认标准和已有协议保持原有行为。

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

每维只有共享的非空`DIM_reason`，以及逐产品`answerN_DIM_status`和`answerN_DIM_score`。不再单独输出`DIM_evidence`数组。共享的是字段容器，各产品独立定分；双产品20键，三产品28键，没有条件性增减键。

每个产品的理由统一包含**证据位置或短原话 → 实际表现及影响 → 分档依据**，按“产品1：……；产品2：……”顺序写在同一维reason中，避免先摘录证据、再在理由里重复一次。不要求在结果中使用箭头，要求三部分逻辑衔接且可以回查。通常每个产品1至3句即可，不能只写“产品1更好”或“整体一般”。

缺证时写明具体缺少的截图区域、必要上文、测试时点、背景或核验依据，以及因此不能确定的判断；不能补造图块号、原话、来源、点击播放或外部核验结果。不适用时说明Query为何没有该需求及实际答复为何没有相关评价对象。

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
  "accuracy_reason": "产品1：当前轮正文写“60分钟”，数值与单位正确，完整回答核心问题，符合2分条件；产品2：正文写“1小时等于60分钟”，时间换算正确且完整，符合2分条件。",
  "answer1_accuracy_status": "scored",
  "answer1_accuracy_score": 2,
  "answer2_accuracy_status": "scored",
  "answer2_accuracy_score": 2,
  "clarity_and_structure_reason": "产品1：当前轮正文仅一句“60分钟”，答案简短易找，无需额外标题即可理解，符合2分条件；产品2：正文用一句话表达等式关系，结论明确且便于理解，符合2分条件。",
  "answer1_clarity_and_structure_status": "scored",
  "answer1_clarity_and_structure_score": 2,
  "answer2_clarity_and_structure_status": "scored",
  "answer2_clarity_and_structure_score": 2,
  "content_quality_reason": "产品1：正文直接给出所问时长，没有无关扩展，完整简洁地满足需求，符合2分条件；产品2：正文直接说明小时与分钟换算，内容范围适当且无冗余，符合2分条件。",
  "answer1_content_quality_status": "scored",
  "answer1_content_quality_score": 2,
  "answer2_content_quality_status": "scored",
  "answer2_content_quality_score": 2,
  "multimedia_value_reason": "产品1：Query只要求简单时间换算，截图没有需要评价的媒体，纯文字已足够，故不适用；产品2：同一任务无需媒体增益，完整截图也没有相关媒体评价对象，故不适用。",
  "answer1_multimedia_value_status": "not_applicable",
  "answer1_multimedia_value_score": null,
  "answer2_multimedia_value_status": "not_applicable",
  "answer2_multimedia_value_score": null
}
```

三产品只在每维追加`answer3_DIM_status`与`answer3_DIM_score`，合并理由同时覆盖产品3；没有其他结构变化。

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

上例用于独立检查原始模型输出。平台正常使用时，从compare模式选择新标签后走既有输入流程；程序校验输出，再附加协议身份、可信采集状态及逐维存储状态，模型不生成这些元数据。最终结果中的`answerN_DIM_score_status`表示实际纳入统计的状态，`verification_status`保持空值，不能把评分状态当成已经执行外部核验的证明。

若可信采集日志明确某产品采集失败，程序只清空该产品最终分数，把`status`及`score_status`标为`insufficient_evidence`；其他产品和维度不受牵连。最终reason会在原文前添加清楚区分的程序说明，指明哪些产品因采集失败最终空分，其原判断只供审计，其他产品的有效评分保持不变。原始模型Observation及原始模型输出不改动，最终reason仍保留完整原模型逐产品理由，诊断记录原状态和分数。部分采集只做复核提示，已有充分证据支持的分数不自动清除。

原始`DIM_evidence`字段已取消，独立JSON解析会拒绝该旧字段，不静默迁移或补写理由。统计和展示将数值0与空分区分，准确性1.5保持小数，分维报告有效样本和空分缺口；新旧协议及版本隔离。结构和回归测试不代表裁判评分一致性效果，仍需用经过审核的标杆样本验证。

## 平台接入与验证

选择compare模式下的“知识问答V1·0-2分（候选）”，按原有数据导入、模型选择和启动方式运行。结果页显示四维合并理由及各产品分数/空分状态，不显示旧Gate；摘要与Excel按单维输出分布、有效样本、覆盖缺口和同题配对交集。人工空白模板、基准导入、人机对齐及报告也支持此标准，准确性1.5不得转成整数。

验证覆盖双/三产品、长截图/录屏、VQA题图、多轮原始上下文、可信采集失败、失败补跑相关审计、冻结协议恢复、严格JSON及实际XLSX数值回读。2561项Python非外部服务回归测试通过，29个Node前端测试文件通过；未调用真实裁判。旧Prompt、原默认协议、裁判配置和输入导入格式保持原样。

浏览器视觉检查未完成：本执行环境没有可用Chrome，官方headless下载未成功，独立浏览器也无法访问本地开发端口。前端交互与显示逻辑已通过Node测试；本记录不代表已经取得真实页面截图。
