# auto_eval_agent V0.2 三产品最小兼容改造

## 改动范围

运行代码只涉及四处：

1. `src/auto_eval/schema.py`
   - 评分从 `0/1/2` 改为 `1–5/null`。
   - 增加产品3、输入状态、V0.2七维字段和分维度排名组。
   - 保留旧五维字段，避免旧页面和历史接口因缺字段报错。

2. `src/auto_eval/judges/visual_compare_prompt_v02.py`
   - 新增独立的完整 V0.2 Prompt，不覆盖原 `prompts.py`，避免影响其他模式。
   - 采用“证据 → 主要问题 → 评分理由 → 分数”的生成顺序。

3. `src/auto_eval/judges/visual_compare_judge.py`
   - `evaluate()` 增加可选的 `context3/answer3/frames3/product_count`。
   - 根据绝对分确定性生成三产品 `rank_groups`。
   - V0.2 未定义权重，暂不生成总分和整体第一名。
   - 准确性保留模型诊断结果，但暂不参与聚合，也不因无法核验单独触发复核。

4. `src/auto_eval/web/history.py`
   - Excel/CSV 导出增加第三产品、输入状态、V0.2七维分数、理由、证据和排名组。

测试更新为 `tests/test_visual_compare_v02.py`。

## 调用方最小改动

原双产品调用可以不改，仍按 `product_count=2` 执行。三产品调用只需补充四个参数：

```python
result = await compare_judge.evaluate(
    question=question,
    context=context,
    context1=context1,
    answer1=answer1,
    frames1=frames1,
    context2=context2,
    answer2=answer2,
    frames2=frames2,
    context3=context3,
    answer3=answer3,
    frames3=frames3,
    product_count=3,
)
```

调用方保存逐题结果时，还需要像产品1、产品2一样补充：

```python
result.update({
    "context3": context3,
    "answer3": answer3,
})
```

上传解析、第三段视频抽帧和上述调用位置不在本次用户提供的压缩包中，因此没有猜测修改其文件。把完整仓库中的 Runner/输入解析文件接入时，只需要沿用现有产品2逻辑复制一份产品3路径。

## 三种位置顺序

Judge 单次调用只评价当前输入顺序。位置实验由调用层分别执行：

```text
Run 1：A-B-C
Run 2：B-C-A
Run 3：C-A-B
```

每次结果先把 `product1/product2/product3` 映射回真实产品，再聚合：

- Gate、适用性：多数规则。
- 绝对分：中位数。
- 排名：从聚合后的绝对分重新生成。
- 任一次产品输入失败：该 Query 不进入位置一致性比较。
- `applicable=false`：不进入该维度覆盖率和分数统计分母。
- accuracy：暂时保留原始输出，不进入汇总排名。

## 兼容字段说明

旧页面字段仍存在，但只能表达产品1和产品2：

- `relevance` ← `understanding_winner`
- `safety` ← 产品1/产品2安全 Gate 对比
- `need_closure` ← `service_closure_winner`
- `personalization` ← `scenario_fulfillment_winner`
- `content_quality`、`overall_winner`：V0.2 无正式权重，固定为 `null`

三产品正式结果应读取七个 `*_rank_groups` 字段，不应再使用旧 `answer1/answer2/tie` 字段生成三产品结论。
