# 问答对比评测：长截图视觉证据改造实施方案

## 1. 文档状态

- 方案状态：已完成需求讨论，待代码实现。
- 方案基线：`feat/evaluation-protocol-versioning`，基线提交 `0619ba0`。
- 方案分支：`feat/long-screenshot-evaluation`。
- 当前分支只保存实施方案，不包含功能代码改动。
- 目标模式：`compare` 问答类双/三产品对比评测。
- 当前重点标准：`qa_competitor_compare/0.2-simplified`，同时保证协议切换至 v0.3 时证据语义正确。

## 2. 已确认的关键事实与决策

### 2.1 实际部署模型

线上服务与 GitHub 仓库中的默认 `config/judges.yaml` 不同：

- 实际服务商：阿里云百炼（Model Studio / MaaS）。
- 实际 OpenAI 兼容地址：`https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- 实际模型：`qwen3.5-397b-a17b`。
- GitHub 默认配置仍可能显示 SiliconFlow；分析线上行为时不得将仓库默认配置误认为实际部署配置。
- 代码实现只能增加对百炼参数的配置支持，不得提交真实 API Key、完整私有地址或线上部署配置。

参考：

- [qwen3.5-397b-a17b 模型说明](https://help.aliyun.com/en/model-studio/qwen3-5-397b-a17b)
- [百炼 OpenAI Chat Completions 兼容接口](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions)
- [百炼错误码与图片硬限制](https://help.aliyun.com/en/model-studio/error-code)

### 2.2 视觉证据方案

每个产品的视觉证据从“录屏采样关键帧序列”扩展为“最终完整回答长截图”：

1. 长截图未超过百炼接口限制时，直接发送原图。
2. 原图路径下不得调用现有 `encode_frame()`，不得缩放、转 JPEG 或降低质量。
3. 长截图超过单图限制时，切成尽可能少的图片块。
4. 切片保持原始宽度、上下连续、无重叠、无遗漏。
5. 优先在行间、段落间或组件间空白处切分。
6. 找不到安全切分点时，不停止：选择风险最低的位置强制切分并打标。
7. 最终只发送原图或切片之一，不同时发送原图和切片。
8. 旧录屏关键帧模式继续兼容。
9. 评分 JSON、2/3 产品结构、Excel 正式评分列不变。

## 3. 目标输入协议

### 3.1 双产品长截图

```json
{
  "id": "case-001",
  "query": "用户问题",
  "context": "可选公共背景",
  "product_count": 2,
  "screenshot1": "/data/product1.png",
  "screenshot2": "/data/product2.png",
  "answer1": "产品1纯文本回答",
  "answer2": "产品2纯文本回答",
  "context1": "可选产品1背景",
  "context2": "可选产品2背景"
}
```

### 3.2 三产品长截图

```json
{
  "id": "case-002",
  "query": "用户问题",
  "product_count": 3,
  "screenshot1": "/data/product1.png",
  "screenshot2": "/data/product2.png",
  "screenshot3": "/data/product3.png",
  "answer1": "产品1纯文本回答",
  "answer2": "产品2纯文本回答",
  "answer3": "产品3纯文本回答"
}
```

### 3.3 兼容和校验规则

- `screenshot1/2/3` 存在时，规范化为 `evidence_mode=long_screenshot`。
- `video1/2/3` 存在时，规范化为 `evidence_mode=video_frames`。
- 一个产品同时提供 `videoN` 和 `screenshotN`：输入错误。
- 同一 Case 内不同产品混用视频和长截图：输入错误，避免证据质量和可观测范围不一致。
- `product_count=2` 时禁止提供产品3的截图、视频、回答或背景字段。
- 产品数量未显式提供时，`screenshot3` 也应参与三产品推断。
- `answerN` 是无 Markdown 标记的纯文本，只用于对齐 Query 和辅助理解主要回答，不作为视觉展示、引用、加粗、链接或排版的主要证据。
- 为降低改造范围，处理完成后的原图或切片路径可继续写入现有 `frames1/2/3` 字段；同时增加 `evidence_mode` 消除语义歧义。

## 4. 目标处理流程

```text
读取 screenshotN
    ↓
校验路径、格式、尺寸、像素、文件大小和长宽比
    ↓
计算原图 Base64 Data URL 大小与视觉 Token 估算
    ↓
满足单图限制 ──→ 原始文件字节直接 Base64 ──┐
    │                                        │
    └─ 超限 → 计算最少切片数                │
                  ↓                          │
            检测候选切分位置                 │
                  ↓                          │
       safe / fallback / risky 全局最优切分  │
                  ↓                          │
            保存无重叠 PNG 切片              │
                  └──────────────────────────┘
                             ↓
               检查整次请求上下文预算
                             ↓
         按产品、按从上到下顺序构造多模态消息
                             ↓
       百炼调用：vl_high_resolution_images=true
                             ↓
                  现有评分解析与后处理
```

## 5. 百炼图片限制与本地策略

### 5.1 配置值

第一版按以下限制实现，并通过配置暴露：

| 限制项 | 默认值 | 本地行为 |
|---|---:|---|
| 高分辨率模式单图最大像素 | `16,777,216` | 超过则切片 |
| Base64 Data URL 最大长度 | `< 10 MiB` | 达到或超过则切片 |
| 最小边 | `> 10 px` | 不满足则输入失败 |
| 最大长宽比 | `200:1` | 原图超限时可通过切片解决 |
| 支持格式 | PNG/JPEG/JPG/WebP 等 | 第一版至少支持 PNG/JPEG/WebP |
| Qwen3.5 最大输入长度 | `260,096 tokens` | 请求前进行保守预算 |
| Qwen3.5 上下文窗口 | `262,144 tokens` | 不依赖服务端静默截断 |

### 5.2 原图直传判定

同时满足以下条件时走原图直传：

```text
width > 10
height > 10
max(width, height) / min(width, height) <= 200
width * height <= 16,777,216
data_url_encoded_bytes < 10 MiB
```

原图直传要求：

- 只读取原始文件字节并进行 Base64 编码。
- MIME 类型与原文件一致。
- 不经 Pillow `save()`。
- 不改变颜色模式、尺寸、编码格式和压缩质量。
- Base64 解码后的字节 SHA-256 必须与源文件一致。

### 5.3 超限行为

- 超过 `16,777,216` 像素时，不依赖百炼服务端自动缩小，平台主动切片。
- 像素未超限但 Base64 超过 10 MiB 时，同样切片。
- 切片后每一块必须重新执行像素、编码大小、最小边和长宽比校验。
- 输入文件损坏、无法解码或格式不支持时直接输入失败，不调用模型。

## 6. 长截图切片算法

### 6.1 理论最少切片数

原图宽度为 `W`、高度为 `H`，单图像素上限为 `Pmax`：

```text
Hmax = floor(Pmax / W)
Npixel = ceil(H / Hmax)
```

文件编码大小与内容有关，不能仅靠高度精确预测；先以 `Npixel` 为下界，裁切并编码验证，若某块仍超过 10 MiB，再增加候选边界或切片数量。

### 6.2 候选边界检测

第一版不引入 PaddleOCR 等重型依赖，复用项目已有 Pillow 和 NumPy：

1. 将长截图按需缩小为仅用于边界检测的灰度预览，原图像素不变。
2. 计算每个水平行带的前景密度、灰度变化和边缘密度。
3. 对行信号做小窗口平滑，识别连续低信息带。
4. 检测明显连通区域是否跨过候选边界。
5. 候选位置映射回原图坐标。
6. 空白带中心、段落间距、卡片间距作为 `safe` 候选。

候选优先级：

```text
章节间空白
> 卡片间空白
> 段落/列表行间
> 图片低显著性背景
> 卡片内部无文字区域
> 表格行间
> 普通正文文字
> 标题、价格、数字、公式、按钮或关键结论
```

### 6.3 全局选择目标

切分不能只做局部贪心。将顶部、底部和所有候选边界构成有向图；两个边界之间形成满足单图限制的切片时建立可行边。通过动态规划求字典序最优路径：

1. 切片数量最少；
2. 所有边界总风险最低；
3. 切片高度尽量均衡；
4. 其他条件相同时边界尽量靠后。

### 6.4 边界风险评分

建议初始权重：

```text
疑似文字区域穿越        +1000
标题/数字/公式穿越       +800
按钮/Superlink/Chips     +600
表格/卡片关键区域穿越    +400
图片显著区域穿越         +150
水平边缘密度             +50 × normalized_density
空白带宽度奖励           -50 × normalized_width
距理论目标位置的偏差     +small_distance_penalty
```

权重只用于候选排序，应集中定义、可测试，后续用真实长截图校准。

### 6.5 找不到安全边界时

不得停止切片：

1. 在当前切片允许的最大范围内生成降级候选位置。
2. 计算所有位置的风险分。
3. 选择风险最低的位置；风险相同则选择更靠后的位置。
4. 未明显穿过文字但穿过组件背景：标记 `fallback`。
5. 疑似穿过文字、表格关键行或关键组件：标记 `risky`。
6. 仍保持零重叠、零间隙。

### 6.6 切片像素规则

```text
part1 = rows [0, y1)
part2 = rows [y1, y2)
...
partN = rows [yN-1, H)
```

必须保证：

- 任意相邻切片：`previous.end_y == next.start_y`。
- `overlap_pixels == 0`。
- 首块从 0 开始，末块在 H 结束。
- 拼接所有切片后，像素矩阵与原始解码图像完全一致。
- 切片保存为 PNG，避免再次引入 JPEG 有损压缩。
- 若源文件是 JPEG，未超限时仍发送原 JPEG；需要切片时按解码后的像素裁切并保存 PNG，原文件保持不变。

## 7. 切片状态与可追溯元数据

每个产品保存独立预处理元数据，但不得把 Base64 写入任务快照或 trace：

```json
{
  "evidence_mode": "long_screenshot",
  "algorithm_version": "long-screenshot-v1",
  "original_path": "/data/product1.png",
  "original_sha256": "...",
  "original_width": 1080,
  "original_height": 20000,
  "original_pixels": 21600000,
  "original_file_bytes": 6250100,
  "original_data_url_bytes": 8333500,
  "split_status": "fallback",
  "split_count": 2,
  "has_overlap": false,
  "estimated_image_tokens": 21094,
  "slices": [
    {
      "path": ".../product1/part_001_of_002.png",
      "start_y": 0,
      "end_y": 15120,
      "width": 1080,
      "height": 15120,
      "pixels": 16329600,
      "data_url_bytes": 6500000
    }
  ],
  "boundaries": [
    {
      "y": 15120,
      "status": "fallback",
      "risk_score": 0.28,
      "reason": "允许高度附近不存在完整空白带",
      "crossed_text": false,
      "crossed_elements": ["card_background"]
    }
  ]
}
```

状态定义：

| 状态 | 定义 | 评测处理 |
|---|---|---|
| `original` | 原图未超限，未切片 | 正常评测 |
| `safe` | 所有边界位于低风险空白/行间 | 正常评测 |
| `fallback` | 至少一条边界穿过组件，但没有明显穿过文字 | 正常评测，记录警告 |
| `risky` | 至少一条边界疑似穿过文字或关键组件 | 继续评测，并强制人工复核标记 |

如果任一产品为 `risky`，代码后处理必须：

```text
needs_human_review = true
review_reasons += ["长截图存在高风险切分边界，可能影响局部证据识别"]
```

不能依赖模型主动返回该标记。

## 8. 上下文预算

Qwen3.5 系列高分辨率图片可按 32×32 像素约对应一个视觉 Token 做预估：

```text
image_tokens ≈ ceil(width / 32) × ceil(height / 32)
```

请求前统计：

```text
所有产品切片视觉 Token
+ 系统 Prompt 保守估算
+ 用户文本保守估算
+ 输出预留
< max_input_tokens
```

要求：

- 切片没有重叠，避免重复视觉 Token 和重复内容误判。
- 切片不会减少总像素 Token，只避免单图上限触发服务端缩小。
- 记录请求返回的 `usage.image_tokens`，用于后续成本、耗时和预算校准。
- 如果整次请求仍超过上下文预算，不得静默删除图片或回答底部；该 Case 标记 `context_budget_exceeded` 并进入人工复核。
- “多阶段分块理解再汇总”会改变评测协议，不纳入本次改造。

## 9. 百炼请求参数

### 9.1 配置模型

在 `JudgeConfig` 增加可选字段，默认关闭以兼容非百炼网关：

```python
vl_high_resolution_images: bool = False
```

实际部署的 `judges.yaml` 设置：

```yaml
base_url: https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
model: qwen3.5-397b-a17b
vl_high_resolution_images: true
```

不得把真实 API Key 或未脱敏的私有地址提交到仓库。

### 9.2 请求体

只有包含图片的主评测调用增加：

```python
extra_body={
    "vl_high_resolution_images": True,
}
```

规则：

- 开启高分辨率模式后不同时设置 `max_pixels`。
- JSON 修复调用没有图片，不发送该参数。
- 与未来可能配置的 `enable_thinking` 等 `extra_body` 字段做字典合并，不能覆盖。
- 现有流式调用、usage 收集、重试和超时机制保持不变。

## 10. 多模态消息组织

当前实现把完整文本放在最前，再连续追加所有图片。长截图模式应显式标记产品和切片顺序：

```text
[完整评测任务文字]

[文字] 产品1最终回答长截图，共2块；以下从上到下连续、无重叠。
[图片] 产品1 第1/2块（顶部）
[图片] 产品1 第2/2块（底部）

[文字] 产品2最终回答长截图，共1块。
[图片] 产品2 第1/1块
```

建议让 `JudgeClient.complete()` 支持完整的多模态 `content_parts`，每个图片 part 保留以下逻辑元数据：

- `product_no`
- `part_no`
- `part_count`
- `position=top|middle|bottom|whole`
- `split_status`
- 本地 `ref_path`

发送给百炼的标准图片 part 仍为：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/png;base64,..."
  }
}
```

trace 中将 Data URL 脱敏为本地引用和切片编号，避免日志膨胀。

## 11. Prompt 证据规则改造

需要同时适配：

- `visual_compare_prompt_v02_simplified.py`
- `visual_compare_prompt_v03.py`

新增模板输入：

```text
evidence_mode
image_count1 / image_count2 / image_count3
split_manifest1 / split_manifest2 / split_manifest3
```

### 11.1 长截图通用规则

1. 每个产品输入的是最终回答长截图，而不是流式生成过程的时间序列。
2. 多张图时，它们是同一张长截图从上到下的连续切片。
3. 切片没有重叠，不代表内容重复。
4. 切片边界是评测预处理行为，不是产品的截断、重复、排版或渲染问题。
5. `fallback` 边界不得作为产品扣分证据。
6. `risky` 边界应结合相邻两块连续理解；确实无法确认时标记证据部分可验证，不得猜测。
7. 纯文本回答只用于对齐 Query 和辅助理解主要回复，不用于判断界面引用、超链、加粗、Markdown 或排版。
8. 最终长截图中实际可见的文字、图片、卡片、Superlink、Chips、表格、公式和排版才是主要视觉证据。
9. 静态长截图不能验证点击、跳转和后续加载过程，不得因未点击扣分。
10. 若长截图本身疑似截取不全，应优先判断输入证据不完整，不得直接认定产品回答生成中断。

### 11.2 响应体验 Gate

长截图模式调整为：

- 响应成功：最终截图中可见正常回答，无系统报错、空回答或 `NO_REPLY`。
- 内容完整：核心结论、句子和步骤在截图内没有明显异常中断；若无法区分产品中断与截图不全，使用 `unclear`，不得直接判失败。
- 展示正常：最终截图中可见文字、表格、公式、图片和卡片没有明确渲染失败、重复或异常截断。
- 静态截图无法证明链接点击、视频播放或折叠内容加载结果，不得据此扣分。
- 图片切片边界不得被误认为产品展示截断。

### 11.3 已确认的有理有据规则

- 显示“找到 26 篇资料”等搜索数量，能够证明存在搜索过程。
- 来源列表处于折叠状态或没有展开，不能因为评测未打开折叠列表而扣分。
- 但如果正文没有引用、引用标签或可见来源对应关系，仍应按“有理有据”标准评价正文缺少引用支撑。
- 产品纯文本中没有参考链接、加粗符号或 Markdown 标记，不得据此扣分；必须查看长截图的最终渲染效果。

### 11.4 输出兼容

- 不修改现有 JSON 字段名、枚举、评分范围和字段顺序。
- 不修改 v0.2/v0.3 的维度定义和后处理排名规则。
- 只改变证据说明、图片顺序说明和 Gate 的可观测边界。

## 12. 代码改动清单

### 12.1 新增文件

#### `src/auto_eval/long_screenshot.py`

负责：

- 图片探测与限制校验；
- 原始字节 Data URL 编码；
- 水平信号和候选边界检测；
- 最少切片动态规划；
- `safe/fallback/risky` 分类；
- 无重叠 PNG 裁切；
- 视觉 Token 估算；
- 元数据生成。

#### `tests/test_long_screenshot.py`

负责长截图算法和编码单元测试。

### 12.2 修改文件

| 文件 | 具体修改 |
|---|---|
| `src/auto_eval/config.py` | 增加 `LongScreenshotConfig` 和 `JudgeConfig.vl_high_resolution_images` |
| `config/visual_modes/rich_content.yaml` | 增加 `long_screenshot` 配置块 |
| `src/auto_eval/web/parse_input.py` | 接受 `screenshot1/2/3`，推断并校验 `evidence_mode` |
| `src/auto_eval/web/runner.py` | 将 `needs_video_prepare` 抽象为视觉证据准备，分流视频与长截图 |
| `src/auto_eval/web/video_prepare.py` | 保留现有视频逻辑；必要时只抽取可复用路径解析方法 |
| `src/auto_eval/judges/visual_compare_judge.py` | 接收证据模式、切片元数据，原图/切片不调用 `encode_frame()`，构造带标签的图片序列 |
| `src/auto_eval/judges/base.py` | 支持完整 `content_parts`、百炼高分辨率 `extra_body`、trace 脱敏 |
| `src/auto_eval/judges/visual_compare_prompt_v02_simplified.py` | 增加长截图证据规则和条件模板 |
| `src/auto_eval/judges/visual_compare_prompt_v03.py` | 同步长截图证据语义，保证协议切换正确 |
| `src/auto_eval/web/history.py` | 历史任务识别原图/切片、保存预处理元数据、支持 Case 证据查看 |
| `src/auto_eval/web/server.py` | 若实现页面上传，新增图片上传接口 |
| `src/auto_eval/web/static/app.js` | 若实现页面上传，增加截图字段、上传和预览 |
| `src/auto_eval/web/static/index.html` | 更新输入示例和界面提示 |
| 现有 compare 测试 | 增加双/三产品、协议切换和旧视频回归覆盖 |

`src/auto_eval/web/operation_media.py` 当前没有被实际 runner 导入，不应为了形式同步复制改动；避免维护两套实现。

## 13. Runner 内部字段建议

长截图准备完成后，每条 item 可包含：

```json
{
  "evidence_mode": "long_screenshot",
  "screenshot1": "/source/product1.png",
  "screenshot2": "/source/product2.png",
  "frames1": ["/prepared/product1/part_001.png"],
  "frames2": ["/prepared/product2/part_001.png", "/prepared/product2/part_002.png"],
  "screenshot_meta1": {},
  "screenshot_meta2": {},
  "frame_count": 3,
  "media": ["/source/product1.png", "/source/product2.png"]
}
```

`framesN` 和 `frame_count` 暂时作为兼容字段保留；用户可见文案统一称为“视觉证据图片/长截图切片”，不能称为录屏关键帧。

## 14. 历史、导出与页面

### 14.1 历史任务

- 将历史数据读取中的“视频流”抽象为“视觉证据流”。
- 旧任务没有 `evidence_mode` 时，根据 `videoN/framesN` 回退判断为 `video_frames`。
- 新任务能够查看原始长截图、切片数量、边界状态和处理告警。
- 持久化保存相对路径和元数据，不保存 Base64。

### 14.2 Excel

- 正式评分列和双/三产品列规则保持不变。
- `fallback` 只保存在历史详情与 trace，不污染评分列。
- `risky` 通过现有 `needs_review/review_reasons` 体现。
- 如需输出完整技术诊断，后续单独增加“调试导出”，不在本次修改正式评分表结构。

### 14.3 页面上传（第二阶段也可）

新增 `/api/upload/image`：

- 支持 PNG/JPEG/WebP；
- 流式写盘，避免一次性读取大图；
- 校验文件头、真实 MIME 和图片尺寸，不只看扩展名；
- 上传阶段只保存原图，不缩放；
- 上传上限与百炼单图 10 MiB 不绑定，因为超限原图仍可在评测阶段切片；
- 页面为每个产品提供视频或长截图二选一；
- 历史 Case 可预览原图和按顺序预览切片。

## 15. 日志与可观测性

每个产品记录：

- 原图尺寸、像素、格式、字节数和 SHA-256；
- 是否原图直传；
- 切片数量、每块尺寸和编码字节数；
- 每条边界的 `safe/fallback/risky`；
- 估算视觉 Token；
- API 返回的 `usage.image_tokens`；
- 图片准备耗时、模型调用耗时；
- 是否触发人工复核。

不得记录：

- 图片 Base64 全文；
- API Key；
- 未脱敏线上私有配置。

## 16. 测试计划

### 16.1 长截图算法

1. 未超限 PNG 原图不重新编码，字节哈希一致。
2. 未超限 JPEG 原图不转格式。
3. 超像素限制时得到理论最少切片数。
4. Base64 超限时能够继续增加切片。
5. 优先选择连续空白行间。
6. 没有安全位置时产生 `fallback`。
7. 疑似穿过文字时产生 `risky`。
8. 所有切片无重叠、无间隙。
9. 所有切片拼回后像素矩阵与原图一致。
10. 每块像素、Base64大小、最小边和长宽比均满足配置。
11. 相同原图和配置重复运行得到相同边界。

### 16.2 输入与执行

1. 双产品 `screenshot1/2` 正常解析。
2. 三产品 `screenshot1/2/3` 正常解析。
3. `screenshot3` 能触发三产品推断。
4. 视频和截图混用被拒绝。
5. 长截图模式不调用视频探测和抽帧。
6. 旧视频 JSONL 和历史任务继续可用。

### 16.3 模型请求

1. 图片按产品和切片顺序发送。
2. 主评测请求包含 `vl_high_resolution_images=true`。
3. 非百炼或配置关闭时不发送该扩展参数。
4. JSON 修复请求不发送图片高分辨率参数。
5. trace 不包含 Base64。
6. 双/三产品图片不会串位。

### 16.4 Prompt与结果

1. v0.2简化版长截图提示正确。
2. v0.3长截图提示正确。
3. 视频模式仍使用关键帧时序规则。
4. `fallback`边界不会被当作产品截断或重复问题。
5. `risky`强制追加人工复核原因。
6. 模型输出 JSON 字段和Excel正式评分列不变。

### 16.5 真实百炼集成验证

使用脱离单元测试的手工集成检查，禁止在测试代码中写入凭据：

- 一张未超限原图；
- 一张需要2块安全切片的图片；
- 一张没有安全边界的fallback图片；
- 一张产生risky边界的图片；
- 双产品和三产品各一条；
- 核对请求成功、`usage.image_tokens`、耗时和输出解析。

## 17. 验收标准

同时满足以下条件才算完成：

- 未超限图片确实按原文件字节发送。
- 超限图片按最少数量、零重叠、零间隙切片。
- 无安全点时继续最低风险切片并正确打标。
- 每块均满足百炼单图硬限制。
- 图片与产品、切片上下顺序明确且可追踪。
- Prompt不会把切片边界当作产品缺陷。
- 纯文本回答仍只作为Query对齐参考。
- `risky`自动触发人工复核。
- 旧视频模式、2/3产品、协议版本切换不回归。
- 评分JSON和Excel正式评分结构不变。
- 全量单元测试通过。
- 至少完成一轮真实百炼冒烟验证。

## 18. 推荐实施顺序

1. 增加配置模型和 `long_screenshot.py`。
2. 完成长截图原图直传、候选检测和最少切片算法。
3. 修改 JSONL 输入解析和 runner 视觉证据分流。
4. 修改多模态消息组织和百炼高分辨率参数。
5. 修改 v0.2/v0.3 Prompt 的条件化证据规则。
6. 增加后处理人工复核标记。
7. 完成历史任务兼容。
8. 完成后端和算法测试。
9. 根据使用入口决定是否同批实现前端图片上传与预览。
10. 运行全量测试和真实百炼冒烟验证。

## 19. Codex执行指令

将以下内容作为新 Codex 会话的首条任务：

```text
请在 auto_eval_agent 仓库的 feat/long-screenshot-evaluation 分支实现
docs/long-screenshot-evaluation-implementation-plan.md。

开始前：
1. 完整阅读仓库根目录 AGENTS.md 和方案文档；
2. 核对当前代码与方案，存在会影响输入格式、输出结构或线上部署的冲突时先向我提问；
3. 只修改方案涉及的代码，不清理无关历史代码；
4. 保持现有2/3产品、评分JSON和Excel正式评分结构不变；
5. 保持旧录屏关键帧模式和历史任务兼容；
6. 实际部署使用阿里云百炼 compatible-mode API，模型为 qwen3.5-397b-a17b；
   GitHub仓库中的SiliconFlow judges.yaml不是实际线上配置，不要混淆，也不要提交真实线上凭据；
7. 长截图未超限时必须按原文件字节发送；超限时按最少数量、无重叠、优先安全边界、
   无安全边界则最低风险强制切分并标记fallback/risky；
8. 先实现和测试，再向我汇报修改文件、关键diff、测试结果和仍需确认的问题；
9. 未经我确认不要提交或推送功能代码。
```

## 20. 非目标

本次不处理：

- 自动生成产品分享长截图的采集脚本；
- 多阶段“分块理解—摘要—再比较”的新评测协议；
- 修改评分维度、分数档位或JSON Schema；
- 修改准确性维度是否纳入统计的既有口径；
- 修改线上 API Key、私有部署地址或发布流程；
- 删除旧录屏关键帧能力。
