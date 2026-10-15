# 问答闲聊 V1 实施与验收

## 分支与依据

- 实现分支：`feat/qa-chat-compare-v1`。
- 按本次开发要求，基于远端 `feat/multi-turn-compare` 的 `efa97b13bc3cced6055230d709b9bfa1d7f9c901` 创建。
- 设计依据：远端实际分支名为 `docs/qa-chat-compare-v1-design`；最新提交 `469e3cc6916ddf66e31fd6f67191244a661b8037`。完整设计已保存为同目录 `qa-chat-compare-v1-design.md`。
- 设计报告的参考基线为 `competitive_assessment`。本实现遵循用户指定的源分支，保留该源分支全部四个旧协议；没有引入另一分支独有的稳定 plus，也没有删除或修改已有旧 Prompt。

## 使用入口

在“垂域视觉对比”的协议选择中选“问答闲聊V1·1-5分（候选）”。API 沿用 `/api/eval`，设置 `mode=compare`、`evaluation_profile=qa_chat_compare@1.0`。

协议包修订为 `1.0.0`，结果版本为 `qa-chat-result-1`。原默认协议仍为 `qa_competitor_compare@0.2-simplified`。任务创建后固定协议和配置，修改新建任务的选择不会重新解释已加载的结果。

新协议只评两项 Gate 和理解需求、个性化适配、情感表达恰当、风格恰当、场景化满足、引导推荐六维。分数为严格整数 1—5，不生成跨维总分或总体冠军。公共及产品背景仍使用现有 context/contextN，通过 `[产品已知信息]`、`[产品N已知信息]` 和 `[仅供裁判核对]` 表明归属。

支持双/三产品、单轮录屏、单轮长截图、题图，以及原始证据长截图多轮。多轮沿用完整前缀、原图与预算检查，不引入额外会话裁判调用。

## 实现边界

| 环节 | 实现 |
| --- | --- |
| 核心协议 | `qa_chat_schema.py` 独立模型；`visual_compare_prompt_qa_chat.py` 包含完整原表锚点、Gate、背景归属与输出形状；注册及裁判入口分路 |
| 状态 | 每产品、每维独立保存适用性、核验、评分状态、证据、理由和分数；输入失败优先于 Gate，Gate fail 只阻断对应产品 |
| 合法分 | 统一 `compare_statistics.score_state`；unclear Gate、partial 输入/证据及复核标记不自动剔除合法分 |
| 汇总 | 单产品分母独立；产品对取共同有效交集；三方排名只使用三方共同有效样本；会话等权包含独立题并先对齐共同轮 |
| 恢复 | `protocol_identity.py` 检查任务、manifest、options 与实际结果；chat 不凭当前实现补写历史 revision |
| 不可执行历史 | 原始 JSON、字段及诊断可读；禁止续跑和正式评分统计；缓存均分从正式汇总及 SSE 隐去，原始汇总明确另存 |
| 展示与导出 | 从冻结协议取维度；产品级理由和空分原因可见；CSV/XLSX 分数保持数字或空白；单题 API 带完整任务身份上下文 |
| 人工链路 | 空白模板、列映射、严格导入、不可变基准版本、人机对齐、状态指标及冻结报告；新旧家族不能生成混合数字一致性 |

旧 Schema、配置、模型选择、媒体准备/切片算法及共享多轮提示文字未修改。`request_metadata()` 保留旧协议进入多轮请求指纹的原字段集，避免新增展示元数据改变旧请求身份。业务 session_id 和旧 CSV 辅助轮次不会自动触发多轮。

## 验证结果

日期：2026-10-15。所有 Python 执行均使用 `C:\workspace\venv\Scripts\python.exe`。

| 检查 | 实际结果 |
| --- | --- |
| 指定基线的原测试集 | 901 passed |
| 最终分支完整非集成回归 | 1013 passed，22 warnings，88.61 秒 |
| SSE 最终防护及运行/旧补跑/暂停恢复/计时补充回归 | 58 passed |
| 全部前端 `.cjs` | 19 组通过，含新增普通结果与人工 UI 检查 |
| 实现补丁格式 | 排除原样引入的设计文档后，`git diff --check HEAD^ HEAD -- . ':(exclude)docs/qa-chat-compare-v1-design.md'` 通过 |
| 合成 Excel | 验证六维列、数值类型、空分、状态、双产品无第三产品评分列、业务额外源字段保留 |
| 浏览器 | Chrome 实际渲染双/三产品、旧模式和人工模板入口，无页面脚本错误 |

回归覆盖非法整数类型、活动产品漏字段、Gate fail/unclear、产品间 NA/无法核验不对称、个性化理由隔离、双三产品及录屏/长图 fake 调用、多轮前缀与未来轮隔离、冻结恢复/冲突、缓存和单题/SSE导出、各对与会话手算样例、人工模板往返/跨家族隔离/冻结报告。

运行命令（在仓库根目录的 PowerShell）：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
$env:NO_PROXY='127.0.0.1,localhost'
& C:\workspace\venv\Scripts\python.exe -m pytest -q -m 'not integration' --basetemp=C:\workspace\t\qlast
Get-ChildItem tests -Filter *.cjs | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw $_.Name }
}
```

本机沙箱限制会令 asyncio 的本地 socketpair 初始化阻塞；测试在沙箱外运行。采用短 basetemp 避免 Windows 路径长度限制。现有 FastAPI 生命周期弃用警告与 Excel 默认样式警告不影响上述结果。

设计源文件与远端 Git blob 均为 `8180f5b3625c360e441d2f392092b333b63c8a06`，按原样保留；其开头四处 Markdown 双空格硬换行会被默认 `diff --check` 标为行尾空格，不属于实现新增的代码空白问题。

一次中间全套复验中，旧测试 `test_concurrent_case_capture_does_not_mix_requests` 出现过单个 Case 失败；此前完整基线及完整分支均通过，随后独立 8 次与最终 1013 项全套全部通过。该间歇问题未稳定复现，不能断言具体根因；保留了断言中的实际结果诊断。没有为通过测试改动证据文件快照或媒体算法。

## 合成验收材料

- `examples/qa-chat/three-products.png`：三产品及无法核验、NA。
- `examples/qa-chat/two-products.png`：双产品显示。
- `examples/qa-chat/legacy-mode.png`：旧 0—3 分协议显示。
- `examples/qa-chat/human-template.png`：新协议人工入口。
- `examples/qa-chat/synthetic-results.xlsx`：合成评分及统计导出。
- `examples/qa-chat/blank-human-template.xlsx`：空白新六维人工模板。
- `examples/qa-chat/synthetic-input.json`：上述展示样例的合成题目元数据，不是可直接调用真实模型的媒体输入数据集。

可用指定解释器运行 `tools/preview_qa_chat.py` 重新生成材料；需要开发依赖、Playwright 及本机 Chrome。预览使用临时本地静态服务及模拟 API，不调用真实裁判、不写业务运行快照。

## 尚未执行

没有调用真实裁判模型、使用真实人工标注集测量效果，也没有部署服务器或合入任何已有分支。模拟与回归结果用于验证程序契约和兼容性，不能代替真实模型评分效果验收。
