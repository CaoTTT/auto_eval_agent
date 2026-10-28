# 推理一致性配置

本次调整用于尽量降低同输入重复评估的波动。它不保证远端模型逐字相同，也不通过结果缓存代替真实推理。

当前默认模型配置为采购渠道 `bailian_qwen38_flash`。六个可选配置覆盖 Qwen3.5-397B-A17B / Qwen3.8-Flash 的采购、xiaohang、zimo 渠道，共用基础 `judge_2` 的采样参数与输出上限。离线配置检查只能确认请求携带参数，不能确认采购网关实际支持或执行所有参数。

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

## 当前共用采样组合

在 `config/judges.yaml` 的 `judge_2` 下配置：

```yaml
temperature: 0
top_p: 1
top_k: 1
seed: 42
frequency_penalty: 0
n: 1
max_tokens: 65536
enable_thinking: false
total_timeout_s: 1800
read_timeout_s: 300
```

- `temperature=0` 尽量采用最高概率输出；`top_k=1` 显式将候选集限制为一个 token（取决于服务端是否执行该参数）。`top_p=1` 不额外裁剪概率质量，避免依赖平台缺省值。
- 固定 `seed=42`，重试时不重新抽种子；服务端是否执行该参数仍需确认，不能仅以 HTTP 成功推断它生效。
- `frequency_penalty=0` 保留现有发送配置；它不是独立的确定性保证。`presence_penalty` 当前缺省不发送，百炼独立部署示例则显式设为 0。
- `n=1` 只生成一个答案；代码仅消费第一个 choice，因此配置也仅接受 1。
- `max_tokens=65536` 使用两种模型官方输出上限中的较小值，作为统一的输出预算；采购网关可能有更低限制。它是上限，不要求生成满 65536 token，也不保证任何无界输出都能完整结束。
- 基础 `enable_thinking=false` 会被所选 profile 覆盖。采购配置不支持思考开关，实际请求省略该参数；xiaohang / zimo 配置默认开启，可在页面显式关闭。关闭原生思考不会取消单视觉 prompt 要求在正文中输出的 `<analysis>`。
- 百炼文档说明 Qwen3.8-Flash 在思考模式下会把低于 0.6 的温度调到 0.6，因此客户端发送 0 不代表所有模型和模式都按 0 执行。

`top_k` 经 OpenAI Python SDK 的 `extra_body` 发送，序列化后位于 HTTP JSON 顶层；同时保留 `enable_thinking`、图片高清开关等已有扩展。其他标准采样参数直接传给 SDK。

参数为 `null` 或缺省时不发送新增可选项，以兼容不同网关。其他模型不一定支持该组合；参数不支持时应显式修改配置，不会通过静默删除采样参数来继续评测。采样参数应写在基础 `judges` 配置中，写在 `judge_model_profiles` 或任务 `options` 中不会覆盖基础采样参数。

## 输出空间与超时

`config/visual_modes/rich_content.yaml` 中 `long_screenshot.output_reserve_tokens` 和 `query_images.output_reserve_tokens` 同步为 65536。这两处不自动读取 `judge.max_tokens`，后续修改时仍须一起调整。当前保守上下文窗口为 262144，预留输出后输入预算相应减少；单视觉等未覆盖统一准入的调用路径仍需另行完善预算检查。Pydantic 模型的旧缺省值保留兼容性，当前部署使用 YAML 的显式值。

单次模型请求总超时为 1800 秒，流式读取连续无数据超时为 300 秒；单题评估默认超时为 3600 秒，为生成和重试、修复留出更长时间。正常请求完成后立即返回，不会等待满超时。显式任务超时及 `runs/web_settings.json` 保存的全局设置优先，不会被新默认值覆盖；已有部署若保存了 900 秒，应在全局设置中改为 3600 秒。排队等待不计入单题的有效执行时间。

这些预算只降低长度上限或超时引起的不完整风险。单视觉分析、卡片与 Superlink 数组，以及各对比协议的理由、证据仍无硬长度上限。当前已有正文但 `finish_reason=length` 的响应不会被统一标记为截断失败，JSON 语法修复也不能可靠补回尚未生成的内容；明确拦截截断和约束输出长度属于后续修复项。

## 配置冻结与评估时间

修改 YAML 后重启 Web 服务，并新建任务。新任务冻结所有采样字段；已有任务的恢复、失败补跑和增量运行继续使用原 `judge_runtime`。旧快照中未记录的新字段保持不发送，不会被新 YAML 默认值悄悄替换。修改当前 YAML 不会升级旧任务的采样配置。

对比协议的提示词包含评估时间。此前每次调用使用当前时刻，即使题目相同，实际提示词也会随重试变化。Web 链路现在使用持久化的 `Task.created_at` 作为任务基准时间，固定为 UTC+08:00，并在对比结果 `evaluation_datetime` 中记录。初次评估、单题重试、恢复和补跑使用同一基准。直接调用 `VisualCompareJudge.evaluate()` 时，应显式传入相同的 `evaluation_time`。

新建任务具有新的创建时间，因此跨任务并不自动保证相同提示词。`timeliness_compare` 协议例外：它使用材料中的测试时间，不把运行时钟注入提示词。对其他含评估时间的协议，时间基准、题目、系统提示词、上下文、模型、思考模式、图片内容与顺序都相同，才属于完整的同输入。缺少原始创建时间的历史快照也无法恢复当时的精确时间。

迁移边界：升级前的旧结果可能使用逐次调用的实际时刻，不能把任务创建时间当成那些旧请求的原始时刻。时间冻结保证适用于新版本的调用；旧任务在升级后补跑会采用创建时间，可能与其历史输出的时间基准不同。严格重放历史请求须采用原调用记录中的时间和完整提示词。

## 审计与验证

开启 `AUTO_EVAL_JUDGE_TRACE` 后，评审和修复日志的 `inference_parameters` 记录实际请求参数；每个 `llm_rounds` 记录服务端返回的 `response_model` 与可选 `system_fingerprint`。供应商不返回指纹时该值为 null，不能用 null 断言后端相同。日志不记录密钥或图片 Base64。

离线测试验证配置校验、HTTP 请求中的参数、修复/重试/usage 降级的一致性、任务配置冻结和时间基准。它们不调用收费模型，不能证明服务端接受或执行所有参数，更不能替代真实样本的多次复跑统计。

真正验证时，固定完整输入后重复调用，分别比较原始文本、结构化判定与分数；不要把“参数被接受”当作“参数已生效”，也不要把少量样本一致当作百分百保证。服务商的模型升级、路由、并行计算或参数忽略仍可能产生差异。

## 官方参考

- [Qwen3.5-397B-A17B](https://help.aliyun.com/zh/model-studio/qwen3-5-397b-a17b)：官方最大输出 65536。
- [Qwen3.8-Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)：官方最大输出 131072。
- [百炼 OpenAI 兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)：百炼模型参数及 `extra_body` 的使用。
- [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat/object)：固定 seed 为尽力确定性，不保证完全一致；服务端指纹用于辅助观察后端变化。
