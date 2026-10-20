# 推理一致性配置

本次调整用于尽量降低同输入重复评估的波动。它不保证远端模型逐字相同，也不通过结果缓存代替真实推理。

截至 2026-10-20，仓库保留的默认模型 `Qwen/Qwen3.5-397B-A17B` 已被 SiliconFlow 列入 [2026-09-11 下线公告](https://docs.siliconflow.cn/docs/release-notes/overview)。本次保留模型名称以免暗中更换评测口径；部署前须使用实际可用的模型/服务配置，并确认它支持所配参数。百炼部署可参考独立示例。离线测试不证明此远端模型仍可调用。

## 实际调用链与配置位置

| 位置 | 作用 |
| --- | --- |
| `config/judges.yaml` | 当前 Web 评估实际使用的服务地址、模型、采样参数与思考开关 |
| `src/auto_eval/config.py` → `JudgeConfig` | 参数定义与范围校验；新增字段必须在这里声明，否则单写 YAML 不会传到模型 |
| `src/auto_eval/judge_profiles.py` | 基础裁判配置叠加所选模型与思考设置，生成任务 `judge_runtime` 快照 |
| `src/auto_eval/web/runner.py` | 从任务快照恢复裁判，调度单视觉与对比评估 |
| `src/auto_eval/judges/base.py` → `JudgeClient.complete` / `repair_json` / `_request_kwargs` | 组装实际请求；评审与 JSON 修复共用采样参数，修复保留 `temperature=0` |
| `src/auto_eval/llm_stream.py` → `_collect_stream` | 真正执行 `await client.chat.completions.create(**request)`，并聚合流式结果 |

正常视觉、长截图、多轮会话和对比评估都通过上述链路。请求失败重试保持采样参数；网关不支持 `stream_options.include_usage` 时只撤去 usage 设置。

`config/models.yaml` 是历史被测模型配置，当前 `load_config()` 不加载它。旧 `runners/openai_compat.py`、`rubric_judge.py` 分类器、`llm_call.py` 片段和旧 demo/probe 不属于当前 Web 视觉评测调用链；其中部分依赖已删除的旧类型。不要通过修改这些历史路径来配置现行评估。独立采样探针故意测试不同组合，不能拿其硬编码参数推断线上配置。

## 默认 SiliconFlow 组合

在 `config/judges.yaml` 的 `judge_2` 下配置：

```yaml
temperature: 0
top_p: 1
top_k: 1
seed: 42
frequency_penalty: 0
n: 1
max_tokens: 8192
enable_thinking: false
```

- `temperature=0` 尽量采用最高概率输出；`top_k=1` 显式将候选集限制为一个 token（取决于服务端是否执行该参数）。`top_p=1` 不额外裁剪概率质量，避免依赖平台缺省值。
- 固定 `seed=42`，重试时不重新抽种子。SiliconFlow 当前公开接口未列出 seed 的保证，因此保留已有 seed 配置作为尽力设置，不能仅以 HTTP 成功推断它生效。
- `frequency_penalty=0` 固定为无词频惩罚；这是明确生成策略，不是独立的确定性保证。当前 SiliconFlow 文档未列出 `presence_penalty`，默认不发送它。
- `n=1` 只生成一个答案；代码仅消费第一个 choice，因此配置也仅接受 1。
- `max_tokens=8192` 固定输出上限，配合当前图片上下文预留预算。长回答仍可能被截断；它本身不会增加确定性。修改输出预算后需重新验证图片输入上限/上下文余量。
- `enable_thinking=false` 显式关闭原生思考，减少未固定的服务端默认行为。它可能影响复杂问题的评判质量；若启用思考，应在新任务中固定为同一值，并重新校准评判质量。

`top_k` 经 OpenAI Python SDK 的 `extra_body` 发送，序列化后位于 HTTP JSON 顶层；同时保留 `enable_thinking`、图片高清开关等已有扩展。其他标准采样参数直接传给 SDK。

参数为 `null` 或缺省时不发送新增可选项，以兼容不同网关。其他模型不一定支持该组合；参数不支持时应显式修改配置，不会通过静默删除采样参数来继续评测。代码支持可选 `presence_penalty`，百炼示例使用 `presence_penalty: 0`，保持其既有 `enable_thinking: true`，不向百炼强加 SiliconFlow 的词频参数。

## 配置冻结与评估时间

修改 YAML 后重启 Web 服务，并新建任务。新任务冻结所有采样字段；已有任务的恢复、失败补跑和增量运行继续使用原 `judge_runtime`。旧快照中未记录的新字段保持不发送，不会被新 YAML 默认值悄悄替换。修改当前 YAML 不会升级旧任务的采样配置。

对比协议的提示词包含评估时间。此前每次调用使用当前时刻，即使题目相同，实际提示词也会随重试变化。Web 链路现在使用持久化的 `Task.created_at` 作为任务基准时间，固定为 UTC+08:00，并在对比结果 `evaluation_datetime` 中记录。初次评估、单题重试、恢复和补跑使用同一基准。直接调用 `VisualCompareJudge.evaluate()` 时，应显式传入相同的 `evaluation_time`。

新建任务具有新的创建时间，因此跨任务并不自动保证相同提示词。时效性评估应把“截至哪个时刻”视为输入的一部分：时间基准、题目、系统提示词、上下文、模型、思考模式、图片内容与顺序都相同，才属于完整的同输入。缺少原始创建时间的历史快照也无法恢复当时的精确时间。

迁移边界：升级前的旧结果可能使用逐次调用的实际时刻，不能把任务创建时间当成那些旧请求的原始时刻。时间冻结保证适用于新版本的调用；旧任务在升级后补跑会采用创建时间，可能与其历史输出的时间基准不同。严格重放历史请求须采用原调用记录中的时间和完整提示词。

## 审计与验证

开启 `AUTO_EVAL_JUDGE_TRACE` 后，评审和修复日志的 `inference_parameters` 记录实际请求参数；每个 `llm_rounds` 记录服务端返回的 `response_model` 与可选 `system_fingerprint`。供应商不返回指纹时该值为 null，不能用 null 断言后端相同。日志不记录密钥或图片 Base64。

离线测试验证配置校验、HTTP 请求中的参数、修复/重试/usage 降级的一致性、任务配置冻结和时间基准。它们不调用收费模型，不能证明服务端接受或执行所有参数，更不能替代真实样本的多次复跑统计。

真正验证时，固定完整输入后重复调用，分别比较原始文本、结构化判定与分数；不要把“参数被接受”当作“参数已生效”，也不要把少量样本一致当作百分百保证。服务商的模型升级、路由、并行计算或参数忽略仍可能产生差异。

## 官方参考

- [SiliconFlow Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post)：采样、扩展与输出参数。
- [百炼 OpenAI 兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)：百炼模型参数及 `extra_body` 的使用。
- [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat/object)：固定 seed 为尽力确定性，不保证完全一致；服务端指纹用于辅助观察后端变化。
