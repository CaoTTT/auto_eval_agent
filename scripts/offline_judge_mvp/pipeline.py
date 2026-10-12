#!/usr/bin/env python3
"""Offline judge-card MVP. Python standard library; XLSX read only.

Exporting the expert workbook is delegated to export_review.mjs (artifact-tool).
No invocation of this module can silently approve model-generated cards.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
N = {"m": MAIN}
REVIEW_COLUMNS = [
    "样本ID", "Query", "审核状态", "事实确认", "条件确认", "审核意见", "审核人", "审核日期",
    "原垂域", "原答案类型", "原答案摘要", "判定方式", "题意与适用条件", "最终答案", "核心准确性规则",
    "允许变体", "参考覆盖范围", "按需检查规则", "任务完成要求", "时效类型", "时效范围", "核验依据",
    "证据对应日期", "模型待确认问题",
]
DRAFT_FIELDS = [
    "sample_id", "judge_mode", "question_intent", "applicable_conditions", "suggested_answer",
    "accuracy_rules", "accepted_variants", "reference_coverage", "conditional_rules", "task_completion",
    "temporal_type", "temporal_scope", "issues", "evidence_to_verify",
]
ENUMS = {
    "judge_mode": {"确定答案核对", "开放规则核验", "条件澄清"},
    "reference_coverage": {"核心答案穷尽", "不穷尽", "待确认"},
    "temporal_type": {"稳定", "动态", "待确认"},
}
STATUSES = {"待审核", "通过", "修改后通过", "待补充", "剔除"}
APPROVED = {"通过", "修改后通过"}
BUILTIN_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%",
    14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0",
}


class PipelineError(ValueError):
    """Actionable input, draft, or approval error."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load_records(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        records = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise PipelineError(f"{path.name} 第 {line_no} 行 JSON 无效：{e.msg}") from e
    else:
        records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
        raise PipelineError("输入必须为 JSON 对象数组，或每行一个对象的 JSONL")
    return records


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_jsonl(path: str | Path, records: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")
    temp.replace(path)


def query_of(sample: dict[str, Any]) -> str:
    for key in ("query", "Query", "问题"):
        if key in sample:
            return str(sample[key])
    raise PipelineError(f"样本 {sample.get('_sample_id', '?')} 缺少 query 字段")


def index_samples(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for sample in samples:
        sid = sample.get("_sample_id")
        if not isinstance(sid, str) or not sid:
            raise PipelineError("每个原始样本必须有非空 _sample_id")
        if sid in result:
            raise PipelineError(f"重复原始样本ID：{sid}")
        if not query_of(sample).strip():
            raise PipelineError(f"{sid} 的 Query 为空")
        result[sid] = sample
    return result


def normalize_number(value: str | int | float | Decimal, format_code: str = "General", date_1904: bool = False) -> str:
    """Restore common Excel date/time semantics without losing literal strings.

    Unsupported complex formats are not simulated. Dates are canonical ISO and
    times are 24-hour values rather than locale-dependent display strings.
    """
    try:
        number = Decimal(str(value))
    except InvalidOperation as e:
        raise PipelineError(f"非有效数字单元格：{value!r}") from e
    if not number.is_finite():
        raise PipelineError("单元格含非有限数字")
    fmt = format_code or "General"
    stripped = re.sub(r'"[^"]*"|\\.', "", fmt).lower().split(";")[0]
    elapsed = "[h]" in stripped
    # Ignore colors and locale codes, but retain elapsed-time units above.
    cleaned = re.sub(r"\[[^]]*\]", "", stripped)
    date_tokens = bool(re.search(r"[yd]", cleaned)) or "mmm" in cleaned
    time_tokens = elapsed or bool(re.search(r"[hs]", cleaned)) or ":" in cleaned
    if date_tokens or time_tokens:
        seconds = int((number * Decimal(86400)).to_integral_value())
        day_serial = int(number // 1)
        if date_tokens:
            if not date_1904 and day_serial == 60:
                raise PipelineError("Excel 日期序列60代表不存在的1900-02-29，请人工确认")
            base = dt.datetime(1904, 1, 1) if date_1904 else dt.datetime(1899, 12, 31)
            corrected = seconds if date_1904 or day_serial < 60 else seconds - 86400
            timestamp = base + dt.timedelta(seconds=corrected)
            return timestamp.strftime("%Y-%m-%d %H:%M:%S" if time_tokens else "%Y-%m-%d")
        if elapsed:
            hours, rest = divmod(seconds, 3600)
        else:
            hours, rest = divmod(seconds % 86400, 3600)
        minutes, secs = divmod(rest, 60)
        if cleaned.startswith("mm:ss") and not elapsed:
            return f"{minutes:02d}:{secs:02d}"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if "s" in cleaned else f"{hours:02d}:{minutes:02d}"
    if "%" in cleaned:
        decimals = len(cleaned.partition(".")[2].split("%")[0]) if "." in cleaned else 0
        return f"{number * 100:.{decimals}f}%"
    if re.fullmatch(r"0{2,}", cleaned):
        if number != number.to_integral_value():
            raise PipelineError("带前导零的格式包含小数，请人工确认显示含义")
        return str(int(number)).zfill(len(cleaned))
    # General numeric values should not acquire binary-floating artifacts.
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def col_index(address: str) -> int:
    match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", address)
    if not match:
        raise PipelineError(f"无效单元格坐标：{address}")
    column = 0
    for letter in match.group(1).upper():
        column = column * 26 + ord(letter) - 64
    return column - 1


def rel_targets(z: ZipFile, rels_path: str) -> dict[str, str]:
    if rels_path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(rels_path))
    return {x.attrib["Id"]: x.attrib["Target"] for x in root.findall(f"{{{PKG_REL}}}Relationship")
            if x.attrib.get("TargetMode") != "External"}


def resolve_target(base: str, target: str) -> str:
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))


def read_xlsx(
    path: str | Path,
    sheet_name: str | None = None,
    reject_images: bool = True,
    ignore_formulas_before_row: int | None = None,
) -> dict[int, dict[int, str]]:
    """Return ordered {row_no: {column_index: literal/display value}}."""
    try:
        with ZipFile(path) as z:
            workbook = ET.fromstring(z.read("xl/workbook.xml"))
            sheets = workbook.findall("m:sheets/m:sheet", N)
            if sheet_name is None:
                sheet = sheets[0] if sheets else None
            else:
                sheet = next((s for s in sheets if s.attrib.get("name") == sheet_name), None)
            if sheet is None:
                raise PipelineError(f"工作表不存在：{sheet_name or '首个工作表'}")
            targets = rel_targets(z, "xl/_rels/workbook.xml.rels")
            target = targets.get(sheet.attrib.get(f"{{{REL}}}id"))
            if not target:
                raise PipelineError("无法定位工作表 XML")
            worksheet_path = resolve_target("xl", target)
            root = ET.fromstring(z.read(worksheet_path))
            if reject_images and (root.find("m:drawing", N) is not None or root.find("m:legacyDrawing", N) is not None):
                raise PipelineError(f"工作表 {sheet.attrib['name']} 含嵌入图片/对象：请先人工转写图片答案并保留原图，再导入")
            if any(name.startswith("xl/cellimages") or name.startswith("xl/richData/") for name in z.namelist()):
                raise PipelineError("检测到单元格图片或富数据：请先人工转写，不可将图片答案当空白")
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                for item in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", N):
                    shared.append("".join(t.text or "" for t in item.findall(".//m:t", N)))
            styles = ["General"]
            if "xl/styles.xml" in z.namelist():
                style_root = ET.fromstring(z.read("xl/styles.xml"))
                custom = {int(n.attrib["numFmtId"]): n.attrib["formatCode"] for n in style_root.findall("m:numFmts/m:numFmt", N)}
                styles = [custom.get(int(x.attrib.get("numFmtId", 0)), BUILTIN_FORMATS.get(int(x.attrib.get("numFmtId", 0)), "General"))
                          for x in style_root.findall("m:cellXfs/m:xf", N)] or ["General"]
            properties = workbook.find("m:workbookPr", N)
            date_1904 = properties is not None and properties.attrib.get("date1904") in {"1", "true"}
            rows = {}
            for row in root.findall("m:sheetData/m:row", N):
                row_no = int(row.attrib["r"])
                values = {}
                for cell in row.findall("m:c", N):
                    address = cell.attrib.get("r", "")
                    column = col_index(address)
                    if cell.find("m:f", N) is not None:
                        if ignore_formulas_before_row is not None and row_no < ignore_formulas_before_row:
                            # Decorative/status formulas above a review-table
                            # header are not review evidence. Never consume their
                            # potentially stale cached values.
                            continue
                        raise PipelineError(f"{sheet.attrib['name']}!{address} 含公式：请人工确认并粘贴为值后导入；不采用可能过期的缓存结果")
                    ctype = cell.attrib.get("t", "n")
                    value = cell.find("m:v", N)
                    raw = "" if value is None or value.text is None else value.text
                    if ctype == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//m:t", N))
                    elif ctype == "s":
                        try:
                            text = shared[int(raw)]
                        except (IndexError, ValueError) as e:
                            raise PipelineError(f"{address} 共享字符串索引无效") from e
                    elif ctype in {"str", "d"}:
                        text = raw
                    elif ctype == "b":
                        text = "TRUE" if raw == "1" else "FALSE"
                    elif ctype == "e":
                        raise PipelineError(f"{address} 是 Excel 错误值 {raw}")
                    elif not raw:
                        text = ""
                    else:
                        style_id = int(cell.attrib.get("s", "0"))
                        if style_id >= len(styles):
                            raise PipelineError(f"{address} 格式索引不存在")
                        text = normalize_number(raw, styles[style_id], date_1904)
                    values[column] = text
                rows[row_no] = values
            return rows
    except (BadZipFile, KeyError, ET.ParseError) as e:
        raise PipelineError(f"无法读取 XLSX：{e}") from e


def import_samples(
    path: str | Path, sheet_name: str | None = None, header_row: int = 1, prefix: str = "S",
) -> list[dict[str, Any]]:
    rows = read_xlsx(path, sheet_name)
    headers = rows.get(header_row)
    if not headers:
        raise PipelineError(f"第 {header_row} 行没有表头")
    names = [x for x in headers.values() if x]
    if len(names) != len(set(names)):
        raise PipelineError("原始表含重复表头，请先明确列名")
    query_column = next((column for column, name in headers.items() if name in {"query", "Query", "问题"}), None)
    if query_column is None:
        raise PipelineError("原始表缺少 query / Query / 问题 列")
    samples = []
    for row_no in sorted(rows):
        if row_no <= header_row:
            continue
        values = rows[row_no]
        if not any(str(x).strip() for x in values.values()):
            continue
        if not values.get(query_column, "").strip():
            raise PipelineError(f"原始第 {row_no} 行有内容但 Query 为空，请处理后导入")
        record = {name: values.get(column, "") for column, name in headers.items() if name}
        record["_source_row"] = row_no
        record["_sample_id"] = f"{prefix}{len(samples) + 1:03d}"
        samples.append(record)
    if not samples:
        raise PipelineError("未找到有效样本")
    return samples


def validate_draft(draft: Any, expected_id: str | None = None) -> list[str]:
    errors = []
    if not isinstance(draft, dict):
        return ["草稿不是 JSON 对象"]
    for field in DRAFT_FIELDS:
        if field not in draft:
            errors.append(f"缺少字段 {field}")
            continue
        if field == "issues":
            if not isinstance(draft[field], list) or any(not isinstance(x, str) or not x.strip() for x in draft[field]):
                errors.append("issues 必须是非空字符串组成的数组，可用 []")
        elif not isinstance(draft[field], str):
            errors.append(f"{field} 必须是字符串")
        elif field not in {"conditional_rules", "task_completion", "temporal_scope"} and not draft[field].strip():
            errors.append(f"{field} 不可为空；不确定时写明待确认")
        if field in ENUMS and draft.get(field) not in ENUMS[field]:
            errors.append(f"{field} 枚举值无效")
    extras = set(draft) - set(DRAFT_FIELDS) - {"_generation_meta"}
    if extras:
        errors.append("未知字段：" + ", ".join(sorted(extras)))
    if "_generation_meta" in draft and not isinstance(draft["_generation_meta"], dict):
        errors.append("_generation_meta 必须是对象")
    if expected_id is not None and draft.get("sample_id") != expected_id:
        errors.append(f"sample_id 应为 {expected_id}")
    if draft.get("temporal_type") == "动态" and not str(draft.get("temporal_scope", "")).strip():
        errors.append("动态题必须说明 temporal_scope 或待补条件")
    return errors


def validate_drafts(samples: list[dict[str, Any]], drafts: list[dict[str, Any]]) -> list[str]:
    index = index_samples(samples)
    seen = set()
    errors = []
    for record in drafts:
        sid = record.get("sample_id", "?")
        if sid in seen:
            errors.append(f"重复草稿ID：{sid}")
        seen.add(sid)
        if sid not in index:
            errors.append(f"草稿ID不在原始样本中：{sid}")
        errors.extend(f"{sid}: {error}" for error in validate_draft(record))
    errors.extend(f"缺少草稿：{sid}" for sid in index if sid not in seen)
    return errors


def parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise PipelineError(f"模型输出不是单个有效 JSON：{e.msg}") from e
    if not isinstance(result, dict):
        raise PipelineError("模型输出必须是一个 JSON 对象")
    return result


def request_card(
    sample: dict[str, Any], prompt: str, base_url: str, model: str,
    key: str, timeout: float, json_mode: bool,
) -> dict[str, Any]:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    payload = {
        "model": model, "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(sample, ensure_ascii=False)},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise PipelineError("模型接口缺少 choices[0].message.content") from e
    if not isinstance(text, str) or not text.strip():
        raise PipelineError("模型接口返回空 content")
    record = parse_model_json(text)
    errors = validate_draft(record, sample["_sample_id"])
    if errors:
        raise PipelineError("；".join(errors))
    return record


def generate(args: argparse.Namespace) -> None:
    samples = load_records(args.samples)
    index_samples(samples)
    key = os.environ.get(args.api_key_env)
    if not key:
        raise PipelineError(f"环境变量 {args.api_key_env} 未设置，未发起任何模型请求。可先使用随包 ChatGPT 草稿示例。")
    prompt = Path(args.prompt).read_text(encoding="utf-8")
    config = {"model": args.model, "base_url": args.base_url.rstrip("/"), "prompt_hash": digest(prompt), "json_mode": args.json_mode, "temperature": 0}
    output = Path(args.output)
    old = load_records(output) if output.exists() else []
    existing = {}
    for draft in old:
        sid = draft.get("sample_id")
        if sid in existing:
            raise PipelineError(f"续跑文件含重复ID：{sid}")
        existing[sid] = draft
    completed = []
    failures = []
    if args.attempts < 1 or args.attempts > 5:
        raise PipelineError("--attempts 范围为 1 至 5")
    for number, sample in enumerate(samples, 1):
        sid = sample["_sample_id"]
        input_hash = digest(sample)
        prev = existing.get(sid)
        meta = (prev or {}).get("_generation_meta", {})
        if prev and not validate_draft(prev, sid) and meta.get("input_hash") == input_hash and meta.get("config") == config:
            completed.append(prev)
            print(f"[{number}/{len(samples)}] {sid} 复用匹配草稿", flush=True)
            continue
        error = None
        for attempt in range(1, args.attempts + 1):
            try:
                draft = request_card(sample, prompt, args.base_url, args.model, key, args.timeout, args.json_mode)
                draft["_generation_meta"] = {"origin": "configured_api", "model": args.model, "input_hash": input_hash,
                                              "config": config, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                completed.append(draft)
                error = None
                break
            except urllib.error.HTTPError as e:
                error = f"HTTP {e.code}；请检查模型、额度及 JSON mode 支持情况"
                # Credentials and request bodies never enter error logs.
                if e.code not in {408, 429, 500, 502, 503, 504}:
                    break
            except (PipelineError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                error = str(e)
            if attempt < args.attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        if error:
            failures.append({"sample_id": sid, "input_hash": input_hash, "error": error, "status": "generation_failed"})
            print(f"[{number}/{len(samples)}] {sid} 失败：{error}", flush=True)
        else:
            print(f"[{number}/{len(samples)}] {sid} 草稿生成，待专家审核", flush=True)
        write_jsonl(output, completed)
        write_jsonl(args.errors or str(output.with_name(output.stem + ".errors.jsonl")), failures)
    # All-resumed runs also refresh their error report; an old failed run must
    # not leave stale failures after a completely successful resume.
    write_jsonl(output, completed)
    write_jsonl(args.errors or str(output.with_name(output.stem + ".errors.jsonl")), failures)
    if failures:
        raise PipelineError(f"{len(failures)} 条失败；成功草稿已保存，可修正后续跑。未自动批准任何记录。")


def draft_review_fields(draft: dict[str, Any]) -> dict[str, str]:
    conditions = draft["question_intent"] + "\n适用条件：" + draft["applicable_conditions"]
    return {
        "判定方式": draft["judge_mode"], "题意与适用条件": conditions, "最终答案": draft["suggested_answer"],
        "核心准确性规则": draft["accuracy_rules"], "允许变体": draft["accepted_variants"], "参考覆盖范围": draft["reference_coverage"],
        "按需检查规则": draft["conditional_rules"], "任务完成要求": draft["task_completion"], "时效类型": draft["temporal_type"],
        "时效范围": draft["temporal_scope"],
    }


def review_rows_from_xlsx(path: str | Path) -> list[dict[str, Any]]:
    sheet = read_xlsx(path, "专家审核", reject_images=False, ignore_formulas_before_row=4)
    header = sheet.get(4, {})
    actual = [header.get(i, "") for i in range(len(REVIEW_COLUMNS))]
    compatible = list(actual)
    if len(compatible) > 10 and compatible[10] == "原答案":
        compatible[10] = "原答案摘要"
    if compatible != REVIEW_COLUMNS:
        raise PipelineError("专家审核第4行表头或列顺序不匹配；请使用导出的24列模板")
    records = []
    for row_no in sorted(sheet):
        if row_no < 5:
            continue
        row = {name: sheet[row_no].get(i, "") for i, name in enumerate(REVIEW_COLUMNS)}
        if any(x.strip() for x in row.values()):
            row["_review_row"] = row_no
            records.append(row)
    return records


def is_iso_date(value: Any) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return False
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def approval_cards(
    samples: list[dict[str, Any]], drafts: list[dict[str, Any]],
    review_rows: list[dict[str, Any]], review_file_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Fail closed for approved invalid rows. Pending rows never become cards."""
    sample_index = index_samples(samples)
    draft_errors = validate_drafts(samples, drafts)
    if draft_errors:
        raise PipelineError("草稿校验失败：" + "；".join(draft_errors))
    draft_index = {d["sample_id"]: d for d in drafts}
    seen = set()
    rows_by_id = {}
    errors = []
    for row in review_rows:
        sid = row.get("样本ID", "")
        if sid in seen:
            errors.append(f"重复审核样本ID：{sid}")
        seen.add(sid)
        if sid not in sample_index:
            errors.append(f"未知审核样本ID：{sid}")
            continue
        sample = sample_index[sid]
        if row.get("Query") != query_of(sample):
            errors.append(f"{sid} Query 与原始样本不一致，禁止串题或修改原始Query")
        # Main sheet K may contain only an excerpt. The full source answer is
        # always taken from the original samples snapshot, never this summary.
        for column, raw_key in [("原垂域", "垂域"), ("原答案类型", "答案类型")]:
            if str(row.get(column, "")) != str(sample.get(raw_key, "")):
                errors.append(f"{sid} {column} 被修改；请修改定稿字段，保留原始快照")
        status = row.get("审核状态", "")
        if status not in STATUSES:
            errors.append(f"{sid} 审核状态无效：{status!r}")
        rows_by_id[sid] = row
        if status not in APPROVED:
            continue
        for column in ["审核人", "审核日期", "判定方式", "题意与适用条件", "最终答案", "核心准确性规则", "允许变体", "参考覆盖范围", "时效类型", "核验依据"]:
            if not str(row.get(column, "")).strip():
                errors.append(f"{sid} 已批准但缺少 {column}")
        for column in ["事实确认", "条件确认"]:
            if row.get(column) != "已确认":
                errors.append(f"{sid} 已批准但 {column} 必须为已确认")
        if not is_iso_date(row.get("审核日期", "")):
            errors.append(f"{sid} 审核日期必须为有效 YYYY-MM-DD")
        for column, field in [("判定方式", "judge_mode"), ("参考覆盖范围", "reference_coverage"), ("时效类型", "temporal_type")]:
            if row.get(column) not in ENUMS[field]:
                errors.append(f"{sid} {column} 枚举无效")
        if row.get("时效类型") == "待确认":
            errors.append(f"{sid} 时效类型待确认，不能进入批准库")
        if row.get("参考覆盖范围") == "待确认":
            errors.append(f"{sid} 参考覆盖范围待确认，不能进入批准库")
        if row.get("时效类型") == "动态":
            if not str(row.get("时效范围", "")).strip():
                errors.append(f"{sid} 动态题缺少时效范围")
            if not is_iso_date(row.get("证据对应日期", "")):
                errors.append(f"{sid} 动态题证据对应日期必须为有效 YYYY-MM-DD")
        source_fields = draft_review_fields(draft_index[sid])
        changed = any(row.get(k, "") != v for k, v in source_fields.items())
        if changed and status != "修改后通过":
            errors.append(f"{sid} 已修改定稿字段，请选择修改后通过")
        if status == "修改后通过" and not str(row.get("审核意见", "")).strip():
            errors.append(f"{sid} 修改后通过需填写审核意见")
    # Missing rows must not silently reduce a 50-row batch to an untraceable subset.
    errors.extend(f"审核表缺少样本：{sid}" for sid in sample_index if sid not in seen)
    if errors:
        raise PipelineError("审核导入校验失败：\n" + "\n".join(errors))
    cards = []
    for sid, sample in sample_index.items():
        row = rows_by_id[sid]
        if row["审核状态"] not in APPROVED:
            continue
        draft = draft_index[sid]
        model_fields = draft_review_fields(draft)
        changes = {k: {"model": model_fields[k], "expert": row.get(k, "")} for k in model_fields if row.get(k, "") != model_fields[k]}
        card = {
            "schema_version": "judge_card_mvp.v1", "card_id": "card_" + sid, "version": "1.0", "sample_id": sid,
            "query": query_of(sample), "domain": sample.get("垂域", ""), "judge_mode": row["判定方式"],
            "question_intent_and_conditions": row["题意与适用条件"], "reference_answer": row["最终答案"],
            "accuracy_rules": row["核心准确性规则"], "accepted_variants": row["允许变体"],
            "reference_coverage": row["参考覆盖范围"], "conditional_rules": row.get("按需检查规则", ""),
            "task_completion": row.get("任务完成要求", ""), "temporal_type": row["时效类型"],
            "temporal_scope": row.get("时效范围", ""), "verification_basis": row["核验依据"],
            "evidence_as_of": row.get("证据对应日期") or None,
            "review": {"status": row["审核状态"], "facts_confirmed": True, "conditions_confirmed": True,
                       "reviewer": row["审核人"], "reviewed_at": row["审核日期"], "notes": row.get("审核意见", "")},
            "provenance": {"raw_sample": copy.deepcopy(sample), "model_draft": copy.deepcopy(draft),
                           "expert_changes": changes, "expert_verification_basis": row["核验依据"],
                           "expert_evidence_as_of": row.get("证据对应日期") or None,
                           "sample_hash": digest(sample), "review_file_hash": review_file_hash},
        }
        cards.append(card)
    return cards


def approved_import(args: argparse.Namespace) -> None:
    samples = load_records(args.samples)
    drafts = load_records(args.drafts)
    review = review_rows_from_xlsx(args.review)
    file_hash = hashlib.sha256(Path(args.review).read_bytes()).hexdigest()
    cards = approval_cards(samples, drafts, review, file_hash)
    write_jsonl(args.output, cards)
    report = {"total_samples": len(samples), "approved": len(cards), "not_approved": len(samples) - len(cards),
              "statuses": {status: sum(r.get("审核状态") == status for r in review) for status in sorted(STATUSES)},
              "review_file_hash": file_hash, "output": str(args.output)}
    write_json(args.report or str(Path(args.output).with_suffix(".report.json")), report)
    print(json.dumps(report, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判定卡离线建设 MVP：导入 → 起草 → 专家审核 → 批准入库")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="只读导入相似原始 XLSX；按显示语义处理时间日期")
    imp.add_argument("--input", required=True)
    imp.add_argument("--output", required=True)
    imp.add_argument("--sheet")
    imp.add_argument("--header-row", type=int, default=1)
    imp.add_argument("--prefix", default="S")
    gen = sub.add_parser("generate", help="真实配置 API 逐题起草，匹配输入/提示词/模型配置后续跑")
    gen.add_argument("--samples", required=True)
    gen.add_argument("--output", required=True)
    gen.add_argument("--base-url", required=True)
    gen.add_argument("--model", required=True)
    gen.add_argument("--api-key-env", default="JUDGE_API_KEY")
    gen.add_argument("--prompt", default=str(Path(__file__).parent / "prompts/generate_card.txt"))
    gen.add_argument("--timeout", type=float, default=90)
    gen.add_argument("--attempts", type=int, default=3)
    gen.add_argument("--errors")
    gen.add_argument("--json-mode", action=argparse.BooleanOptionalAction, default=True)
    val = sub.add_parser("validate", help="检查草稿字段、ID及覆盖情况")
    val.add_argument("--samples", required=True)
    val.add_argument("--drafts", required=True)
    app = sub.add_parser("approved-import", help="严格校验专家审核表，只发布明确批准且完成确认的卡")
    app.add_argument("--samples", required=True)
    app.add_argument("--drafts", required=True)
    app.add_argument("--review", required=True)
    app.add_argument("--output", required=True)
    app.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            records = import_samples(args.input, args.sheet, args.header_row, args.prefix)
            write_json(args.output, records)
            print(f"已导入 {len(records)} 条；样本ID按本批原始行序稳定生成。")
        elif args.command == "generate":
            generate(args)
        elif args.command == "validate":
            errors = validate_drafts(load_records(args.samples), load_records(args.drafts))
            if errors:
                raise PipelineError("\n".join(errors))
            print("草稿结构与样本覆盖校验通过；事实与规则仍需专家确认。")
        elif args.command == "approved-import":
            approved_import(args)
    except (PipelineError, OSError, json.JSONDecodeError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
