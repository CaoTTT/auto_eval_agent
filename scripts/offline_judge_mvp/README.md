# 离线判定卡 MVP 工具

输入候选测评 Excel，模型逐题起草判定依据，导出专家审核表，再将明确批准的记录输出为 JSONL。完整方案见 [设计文档](../../docs/offline-judge-basis-mvp-design.md)。本工具独立于现有 Web 评估链路，尚未实现产品回答评估或最小输入投影。

## 环境与路径

Python 3.10+，解析、API 起草和批准导入只用标准库。工作簿导出需受支持的 Node 与 `@oai/artifact-tool`，包装器可使用 `CODEX_PRIMARY_RUNTIME_NODE` 和 `CODEX_PRIMARY_RUNTIME_NODE_MODULES`，或传 `--node`、`--node-modules`。普通 Python 环境不会自动具备导出依赖。

以下命令从仓库根目录执行。原始样本、草稿、审核 Excel 和批准库存放在本地忽略的 `data/offline_judge_mvp/`。程序会创建输出父目录。不要提交 `.env`、密钥、本地数据或生成结果。

## 1. 导入

```bash
python scripts/offline_judge_mvp/pipeline.py import \
  --input /path/to/input.xlsx \
  --output data/offline_judge_mvp/raw_samples.json \
  --prefix B20261012_S
```

默认首个工作表、第 1 行表头，可指定 `--sheet`、`--header-row`。至少有 `query`、`Query` 或 `问题` 列。其余原始字段保留；格式化时间和日期按显示语义解释。原始公式、图片和富数据明确拒绝，不采用缓存结果。只支持 XLSX。

样本 ID 在当前批次内按有效原始行生成；跨批次使用独立前缀。Query 相同但条件不同，可以保留不同样本。多轮关联标记保留，影响判题的实际前文仍需补齐。

## 2. 模型起草

通过运行环境设置 `JUDGE_API_KEY`，不要在命令行或代码中写真实密钥。base URL 和模型名必须使用实际部署的配置。

```bash
python scripts/offline_judge_mvp/pipeline.py generate \
  --samples data/offline_judge_mvp/raw_samples.json \
  --output data/offline_judge_mvp/drafts.jsonl \
  --base-url YOUR_CONFIGURED_COMPATIBLE_BASE_URL \
  --model YOUR_DEPLOYED_MODEL \
  --api-key-env JUDGE_API_KEY
```

OpenAI 兼容接口，逐题串行，成功逐条落盘。默认 JSON mode，不支持时加 `--no-json-mode`。输入、提示词 hash、模型和 endpoint 配置均匹配才复用已有草稿。默认有限重试 3 次，可设 `--attempts 1..5`；失败写邻近 `.errors.jsonl`，不自动伪造完整覆盖。缺密钥时不发请求。

模型只起草：原答案不是已核验事实，开放题不是参考白名单，缺条件可澄清或明确假设。没有置信度、分数、错误严重度或模型自动批准。

## 3. 校验与导出

```bash
python scripts/offline_judge_mvp/pipeline.py validate \
  --samples data/offline_judge_mvp/raw_samples.json \
  --drafts data/offline_judge_mvp/drafts.jsonl

python scripts/offline_judge_mvp/export_review.py \
  --samples data/offline_judge_mvp/raw_samples.json \
  --drafts data/offline_judge_mvp/drafts.jsonl \
  --out data/offline_judge_mvp/review.xlsx \
  --source-name input.xlsx
```

校验结构和整批覆盖不等于核验事实。工作簿含“专家审核”“模型草稿”“原始输入”“填写说明”。第 4 行表头、第 5 行起逐题；所有状态初始待审核，事实与条件未确认，审核人／日期／实际核验依据留空。长原答案分段保存，可以按样本 ID 无损还原。

专家优先审核 M 题意条件、N 最终答案、O 核心规则、P/Q 允许变体与覆盖、T/U/V/W 时效与依据，然后确认判定方式、按需规则及完成度要求。黄色可编辑，灰色为对照提示，颜色不是工作表锁定。

## 4. 专家确认后入库

```bash
python scripts/offline_judge_mvp/pipeline.py approved-import \
  --samples data/offline_judge_mvp/raw_samples.json \
  --drafts data/offline_judge_mvp/drafts.jsonl \
  --review data/offline_judge_mvp/review_approved.xlsx \
  --output data/offline_judge_mvp/approved_cards.jsonl
```

只接受固定审核表名称和表头；允许排序，拒绝串题、重复／缺失／未知 ID 和来源标签修改。只发布“通过／修改后通过”，并要求事实和条件已确认、审核人和有效日期、必要判定字段、核验依据完整；覆盖与时效不能待确认。动态题另要求时效范围及有效证据日期。

修改 L—U 后须“修改后通过”并写意见；新增 V/W 证据可保持“通过”。任一批准行不合格，整个发布失败。顶端展示公式在第 4 行之前可忽略；表头／数据区公式仍拒绝。

输出 `judge_card_mvp.v1` JSONL 和统计报告，含原样本、模型候选、专家改动及文件 hash。当前 card ID 仅批次内、版本固定 1.0，无外部数据库或自动版本管理。日期格式与字段完整性校验不证明事实正确或时效语义匹配；专家仍需解决影响判题的实质问题。

## 验证

```bash
python -m unittest discover -s tests -p test_offline_judge_basis_mvp.py -v
node --test tests/test_offline_review_data.mjs
```

也可使用仓库的 pytest 运行同一文件。测试不调用真实模型或网络。真实 API、运行时投影、产品回答评估和平台 UI 集成需后续验证。
