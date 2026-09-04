# -*- coding: utf-8 -*-
"""0829 小艺 vs 豆包 对比评测统计
- 不同维度的平均分对比 + 核心质量总分对比
- 产品1 是产品2 的多少百分比（产品1均分 / 产品2均分 * 100%）
- 整体 / 按分类 / 按领域 / 按子领域
- 跳过该维度有 NA 的行；跳过分类为空的行
- 额外新增 4 个 sheet，按各维度"是否适用=True"过滤后再统计（不含核心质量总分）
- 输出到 Excel 文件，分 sheet 展示
"""
import pandas as pd
import numpy as np
import os

SRC = "agent_diff/data/0829_小艺_豆包_对比评测_批跑.xlsx"
SHEET = "逐题结果"

# 维度 -> (产品1列, 产品2列, 是否适用列或None)
DIMENSIONS = [
    ("理解需求",   "产品1理解需求分",   "产品2理解需求分",   "理解需求是否适用"),
    ("呈现易读",   "产品1呈现易读分",   "产品2呈现易读分",   "呈现易读是否适用"),
    ("内容准确性", "产品1内容准确性分", "产品2内容准确性分", "内容准确性是否适用"),
    ("支撑决策",   "产品1支撑决策分",   "产品2支撑决策分",   "支撑决策是否适用"),
    ("高效闭环",   "产品1高效闭环分",   "产品2高效闭环分",   "高效闭环是否适用"),
    ("核心质量总分", "产品1核心质量总分", "产品2核心质量总分", None),
]
DIMENSIONS_NO_TOTAL = DIMENSIONS[:-1]

GROUP_COLS = [
    ("分类",   ["分类"]),
    ("领域",   ["分类", "领域"]),
    ("子领域", ["分类", "领域", "子领域"]),
]
COLS = ["维度", "产品1均分", "产品2均分", "差值(1-2)", "产品1/产品2(%)", "有效题数"]


def compute_block(df, dims=None):
    """对给定 df 计算各维度的产品1/产品2均分及百分比。
    dims=None 时使用全部维度（含核心质量总分），仅跳过 NA。
    """
    if dims is None:
        dims = DIMENSIONS
    rows = []
    for dim, c1, c2, _ in dims:
        sub = df[[c1, c2]].dropna()
        if sub.empty:
            rows.append([dim, 0, 0, 0, 0, 0])
            continue
        m1 = sub[c1].mean()
        m2 = sub[c2].mean()
        pct = (m1 / m2 * 100) if m2 else np.nan
        rows.append([dim, round(m1, 2), round(m2, 2), round(m1 - m2, 2),
                     round(pct, 2), len(sub)])
    return pd.DataFrame(rows, columns=COLS)


def compute_block_applicable(df):
    """按各维度自己的"是否适用=True"过滤后再统计（不含核心质量总分）。
    每个维度的有效题数可能不同。
    """
    rows = []
    for dim, c1, c2, applicable_col in DIMENSIONS_NO_TOTAL:
        sub = df[df[applicable_col] == True][[c1, c2]].dropna()
        if sub.empty:
            rows.append([dim, 0, 0, 0, 0, 0])
            continue
        m1 = sub[c1].mean()
        m2 = sub[c2].mean()
        pct = (m1 / m2 * 100) if m2 else np.nan
        rows.append([dim, round(m1, 2), round(m2, 2), round(m1 - m2, 2),
                     round(pct, 2), len(sub)])
    return pd.DataFrame(rows, columns=COLS)


def build_grouped_sheet(df, group_cols, compute_fn):
    """按多列分组，拼接所有分组为一个 DataFrame，带分组标签列。"""
    grouped = df.dropna(subset=group_cols)
    for c in group_cols:
        grouped = grouped[grouped[c].astype(str).str.strip() != ""]
    all_blocks = []
    for keys, sub_df in grouped.groupby(group_cols, sort=True):
        b = compute_fn(sub_df)
        # keys 可能是 tuple 或单值
        if not isinstance(keys, tuple):
            keys = (keys,)
        # 从后往前 insert(0)，保证最终列顺序为 group_cols 的顺序
        for col_name, key_val in reversed(list(zip(group_cols, keys))):
            b.insert(0, col_name, key_val)
        all_blocks.append(b)
    return pd.concat(all_blocks, ignore_index=True) if all_blocks else pd.DataFrame()


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "stat_result_v2.xlsx")

    df = pd.read_excel(SRC, sheet_name=SHEET)
    print(f"原始数据: {df.shape[0]} 行")
    df = df[df["分类"].notna() & (df["分类"].astype(str).str.strip() != "")]
    print(f"分类非空: {df.shape[0]} 行")

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        # ---- 原始 4 个 sheet（不过滤是否适用）----
        overall = compute_block(df)
        overall.to_excel(writer, sheet_name="整体对比", index=False)
        print("  整体对比 -> 已写入")

        for label, col in GROUP_COLS:
            sheet_df = build_grouped_sheet(df, col, compute_block)
            sheet_name = f"按{label}对比"
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)
            print(f"  {sheet_name} -> 已写入 ({len(sheet_df)} 行)")

        # ---- 新增 4 个 sheet（按是否适用=True 过滤，不含核心质量总分）----
        overall_app = compute_block_applicable(df)
        overall_app.to_excel(writer, sheet_name="整体对比(适用)", index=False)
        print("  整体对比(适用) -> 已写入")

        for label, col in GROUP_COLS:
            sheet_df = build_grouped_sheet(df, col, compute_block_applicable)
            sheet_name = f"按{label}对比(适用)"
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)
            print(f"  {sheet_name} -> 已写入 ({len(sheet_df)} 行)")

    print(f"\n完整结果已保存至: {out_path}")


if __name__ == "__main__":
    main()
