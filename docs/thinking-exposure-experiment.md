# 思考暴露优化独立实验版本

本次将 `6c05c20` 中对原有模板的思考暴露修改独立为可选实验版本。
原有三个协议及通用模板恢复为 `8b650fc` 的原始内容；稳定版继续默认选中。

| 页面选项 | standard_version | bundle_revision | 模板文件 |
| --- | --- | --- | --- |
| V0.2 简化版（稳定） | `0.2-simplified` | `0.2.1` | `visual_compare_prompt_v02_simplified.py` |
| V0.3（实验） | `0.3` | `0.3.1` | `visual_compare_prompt_v03.py` |
| V0.2 简化版·评分校准（实验） | `0.2-simplified-calibrated` | `0.2.2` | `visual_compare_prompt_v02_calibrated.py` |
| V0.2 简化版·思考暴露优化（实验） | `0.2-simplified-thinking-exposure` | `0.2.3` | `visual_compare_prompt_v02_thinking_exposure.py` |

模板均位于 `src/auto_eval/judges/`。新实验版的 `evaluation_profile` 为
`qa_competitor_compare@0.2-simplified-thinking-exposure`。

## 实验范围

- 新版采用独立的 System/User 模板与长截图规则，思考暴露定义来自
  `internal_process_rules.py`；其他原有版本不引用这份定义。
- 实验版保留 `6c05c20` 中 V0.2 简化版的思考暴露规则及相关维度边界，
  仅修改协议标识；不叠加评分校准版的加粗/来源优化，也未加入后续 review
  尚未实施的收紧建议。
- 沿用 V0.2 的 1—5 分、输出字段及 Gate 语义；响应 Gate 为 fail 或 unclear
  时，该产品后续分数仍为 null。
- 同时兼容两/三产品、录屏/长截图、文字/VQA 提问。输入方式沿用原有流程。
- 历史恢复、失败补跑及导出识别新协议；统计按标准、修订和产品数量分别计算。
  新实验版达标阈值为 4 分，低质阈值为 2 分，与原 V0.2 一致。

## 如何对照测试

在同一数据集分别新建两个任务：一个选择 V0.2 简化版（稳定），另一个选择
V0.2 简化版·思考暴露优化（实验）。保持模型、参数及输入证据相同，比较思考
暴露判定、响应 Gate、各维度有效评分及可比覆盖率。不要只比较剩余有效样本
的均分或胜率；Gate 阻断会改变统计样本。

既有任务不能通过补跑选项切换协议。实验结果确认后，再决定是否替换稳定版；
本次不会自动替换或合并。

历史结果不会重写。`6c05c20` 发布后、本次恢复前生成的结果可能使用优化过的
旧版模板，但仍带旧协议标识；本次不自动给这些历史结果改标签。旧任务的
Prompt 正文未被完整归档，其补跑会读取当前对应模板，因此本轮对照应新建任务。
