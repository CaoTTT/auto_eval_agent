# 问答闲聊 V2 实施与使用

依据 `38ebddd` 提交的 [Prompt 与输出格式重设计](qa-chat-compare-v1-prompt-output-redesign.md)，在 `feat/qa-chat-compare-v1` 分支新增 V2。此改动不修改已冻结的 V1，也不改变默认评测标准。

## 选择与确认

拉取本分支并重启后，在“垂域视觉对比评测”的评测标准中选择 **问答闲聊V2·1-5分（候选）**，再导入原有双产品或三产品数据并批跑。长截图、录屏、提问图片和原始多轮上下文继续使用已有入口。

| 项目 | V1 | V2 |
| --- | --- | --- |
| 可选协议 | `qa_chat_compare@1.0` | `qa_chat_compare@1.1` |
| 协议包修订 | `1.0.0` | `1.1.0` |
| 结果 Schema | `qa-chat-result-1` | `qa-chat-result-2` |
| 评分标准 | 两项 Gate、六维、1—5 分 | 同一标准及分制 |
| 模型输出字段数，双/三产品 | 102 / 149 | 56 / 78 |
| 理由与证据 | 逐产品逐维 | 每维共享容器，原文标明各产品归属 |

V2 用新的可选协议版本区分输出合同；没有更改评分锚点，也不把 V1 的 `1.0.0` 原地升级。原来的默认仍为 `qa_competitor_compare@0.2-simplified`。

可以从三处确认已注册：

- `src/auto_eval/judges/compare_protocols.py` 的 V2 注册项绑定新 SYSTEM、USER 和独立 observation 模型。
- `src/auto_eval/judges/visual_compare_prompt_qa_chat_v2.py` 包含设计附录的完整 Prompt、原表锚点和 `output_example(2/3)`。
- 运行中服务的 `GET /api/config` → `evaluation_profiles` 包含 `id=qa_chat_compare@1.1`、`bundle_revision=1.1.0`、`result_schema_version=qa-chat-result-2`；新任务响应及导出也冻结这组身份。

开发环境启动须使用本工作树源码，例如 PowerShell：

```powershell
Set-Location C:\workspace\auto_eval_qa_chat_compare
$env:PYTHONPATH = '.\src'
& C:\workspace\venv\Scripts\python.exe -m uvicorn auto_eval.web.server:app --host 127.0.0.1 --port 8054
```

## 实现边界

模型只输出精确 56/78 键。活动产品的 Gate 与理由、逐产品六维适用性/核验/分数、每维共享证据/理由均必填；允许 null 的字段也不能漏键。严格拒绝重复键、多余键、缩写键、缺 Gate、字符串分数、浮点分数和布尔分数。原始输出、原始及修复键对、结构诊断分别留存；合法 JSON 的结构错误不会触发额外模型修复调用。

数组或字符串包装不能被截取成内部对象接受。本地尾逗号修复保留理由/证据字符串中的标点。确需调用既有 JSON 语法修复时，先确认原文所有必需键及其值均可确定且符合合同，再核对修复结果逐键逐值保持一致；修复器不能新增缺失的 Gate/分数，也不能改写已有判定、理由或证据。缺少内容或原值无法确定时保留失败审计，需携原始图文补跑。

`qa_chat_v2_schema.py` 独立处理 V2，不向 V1 模型填默认值。程序按“可信采集失败 → 本产品 Gate 阻断 → 明确不适用 → 无法核验 → 正常评分”的优先级派生状态。一个产品空分不会清除其他产品；Gate unclear 和核验 partial 的合法整数分仍可统计。排名、复核、请求身份和指纹由程序生成。

当前上传/媒体预处理只能确认文件可用，没有给出可信的“内容完整采集”结论，因此 V2 默认保存 `answerN_input_status=null`、`trusted_collection_metadata={}`，界面显示“未知（未提供采集结论）”。不会从文件存在、成功解码、产品自述或 Excel 扩展列推断 complete/failed；未知也不会排除本维已有充分证据的有效分。

将来接入采集器时，程序可通过 `VisualCompareJudge.evaluate(..., trusted_collection_metadata=...)` 或 runner 内部同名参数提供结论，例如 `{"answer1":{"input_status":"failed","source":"collector-run-id"}}`。非空结论必须有来源，元数据纳入输入指纹及审计；普通输入数据字段不能替代这个参数。采集准备本身失败仍沿用现有技术失败路径，不伪装成产品质量 Gate fail。

网页、单题 API、CSV/XLSX 和人机报告明确标注“维度共享”理由/证据，保留整段原文，不从中文分句反推状态，也不复制成假逐产品事实。V2 不显示未采集的冲突、置信度、总体理由、逐产品问题摘要等列。各产品适用分母、共同有效交集和会话等权统计沿用原有规则。

V1 历史任务、恢复执行和失败补跑继续原 Prompt、Schema、请求身份及解析行为。缺修订、未知修订、混版本或结果 Schema 与协议冲突时保留原始数据，禁止正式统计/续跑。V2 失败补跑额外在 `retry_runs` 留存 `original_result` 与 `attempt_result`，不因成功替换逐题结果而丢失原始失败输出。

V1 与 V2 人工基准严格隔离；带旧冻结身份的模板/基准不能仅修改导入版本后冒充 V2。请下载 V2 空白模板重新标注，或在明确审核范围后建立新的不可变基准并记录来源。

## 验证与示例

自动化验证覆盖设计验收矩阵中的键集合、重复键/错键、严格分值、状态优先级、各产品独立性、未知采集、多轮隔离、统计/导出/人机、历史冻结与补跑。测试使用假模型，不调用真实裁判。

2026-10-16 最终验证：

- `C:\workspace\venv\Scripts\python.exe -m pytest -q -m 'not integration' --basetemp=C:\workspace\t\q2release`：**1164 passed**，93.47 秒。环境为 `PYTHONPATH=src`、`PYTHONIOENCODING=utf-8`、`NO_PROXY=127.0.0.1,localhost`；Windows asyncio 测试需允许本机回环通信。24 条 warning 来自既有 FastAPI 生命周期弃用与 openpyxl 默认样式提示。
- `tests/*.cjs`：**20 组通过**，包含 V2、V1、历史切换、筛选与人机界面。
- 合成浏览器预览无页面 JavaScript 错误；实际 `api_config()` 返回 V1/V2 各自正确注册身份，默认协议保持原值。
- 独立复验确认数组包装被拒绝、字符串标点保留、缺 Gate/分值不能由修复器补造、修复器改分被拒绝；V1 Prompt/Schema/多轮适配器无改动。

合成界面和导出示例位于 [examples/qa-chat-v2](examples/qa-chat-v2)：双/三产品界面、共享理由展开、V2 人工模板界面、结果 Excel 与空白模板。可通过 `tools/preview_qa_chat_v2.py` 重新生成；不读取真实任务数据。

尚未进行设计文档建议的同批原始图文真实模型对照实验，因此不宣称漏键率、人机一致率、输出 Token 或延迟已经改善。该协议继续以候选选项提供，不能把键数减少当作效果实验结果。

回退时选择原 V1 或原有标准新建任务；已创建任务保持其冻结身份。此实现不自动将旧任务切换成 V2，也不执行部署。
