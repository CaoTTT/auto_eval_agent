# -*- coding: utf-8 -*-
"""将 query_corpus_90_first10.json 转为 Excel 格式"""
import json
import pandas as pd
from pathlib import Path

# 输入输出路径
INPUT_FILE = Path("agent_diff/data/input_data/query_corpus_90_first10.json")
OUTPUT_DIR = Path("agent_diff/data/observe_data")
OUTPUT_FILE = OUTPUT_DIR / "query_corpus_90_first10.xlsx"

def main():
    # 确保输出目录存在
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 读取 JSON
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = data.get("questions", [])
    print(f"读取到 {len(questions)} 条问题")

    # 转换为 DataFrame 行
    rows = []
    for q in questions:
        rows.append({
            "id": q["id"],
            "序列号": q["id"],
            "query": q["question"],
            "is_start": True,
            "is_end": True,
        })

    df = pd.DataFrame(rows)

    # 写入 Excel
    df.to_excel(OUTPUT_FILE, index=False)
    print(f"已写入: {OUTPUT_FILE}")
    print(f"共 {len(df)} 行")

if __name__ == "__main__":
    main()
