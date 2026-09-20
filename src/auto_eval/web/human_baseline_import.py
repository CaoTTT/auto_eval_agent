"""Deterministic wide-sheet import with physical column addresses and provenance."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
import zipfile

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from ..judges.compare_protocols import resolve_compare_protocol
from .compare_statistics import DIMENSION_NAMES, SCORE_DIMENSIONS
from .human_baselines import HumanError, HumanScoreLabel, ImportMapping, digest

MAX_UPLOAD = 20 * 1024 * 1024
MAX_ROWS = 20000
MAX_COLUMNS = 512


@contextmanager
def workbooks(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > 4000 or sum(i.file_size for i in archive.infolist()) > 160 * 1024 * 1024:
                raise HumanError("工作簿解压后过大", 400)
        formulas = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            try:
                yield formulas, cached
            finally:
                cached.close()
        finally:
            formulas.close()
    except HumanError:
        raise
    except Exception as exc:
        raise HumanError(f"无法解析 XLSX：{type(exc).__name__}", 400) from exc


def _check_sheet(ws):
    if ws.sheet_state != "visible":
        raise HumanError("不能导入隐藏工作表", 400)
    if (ws.max_row or 0) > MAX_ROWS + 30 or (ws.max_column or 0) > MAX_COLUMNS:
        raise HumanError(f"工作表上限为 {MAX_ROWS} 行、{MAX_COLUMNS} 列", 400)


def _headers(ws, row: int) -> list[str]:
    return [str(c.value or "") for c in next(ws.iter_rows(min_row=row, max_row=row, max_col=ws.max_column or 1))]


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def suggest_mapping(headers: list[str], sheet_name: str, header_row: int = 1, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    cols = {h: get_column_letter(i + 1) for i, h in enumerate(headers) if headers.count(h) == 1}
    products = metadata.get("products") or [{"product_id": "product1", "display_name": "产品1"},
                                          {"product_id": "product2", "display_name": "产品2"}]
    if any("产品3_" in h for h in headers) and len(products) == 2:
        products.append({"product_id": "product3", "display_name": "产品3"})
    labels, responses, gates = [], {}, {}
    for n, product in enumerate(products, 1):
        pid = product["product_id"]
        responses[pid] = {field: cols[name] for field, name in (
            ("answer", f"源回答_产品{n}"), ("context", f"源背景_产品{n}"),
            ("response_id", f"采集ID_产品{n}"), ("sha256", f"源摘要_产品{n}"),
            ("evidence", f"证据引用_产品{n}")) if name in cols}
        gates[pid] = {g: cols[f"人工{label}Gate_产品{n}"] for g, label in (("response", "响应"), ("safety", "安全"))
                      if f"人工{label}Gate_产品{n}" in cols}
        for dim, label in DIMENSION_NAMES.items():
            mapping = {field: cols[name] for field, name in (
                ("score", f"人工分_产品{n}_{label}"), ("review", f"人工复核分_产品{n}_{label}"),
                ("status", f"人工评分状态_产品{n}_{label}"), ("reason", f"人工批注_产品{n}_{label}")) if name in cols}
            if any(mapping.get(k) for k in ("score", "review", "status")):
                labels.append(dict(product_id=pid, dimension_id=dim, **mapping))
    # The legacy adapter is only suggested when its physical headers still look human-authored.
    legacy = [("understanding", "AW", "AX", "", ""), ("accuracy", "BJ", "BK", "", ""),
              ("service_closure", "BU", "BV", "", ""), ("scenario_fulfillment", "CF", "CG", "", ""),
              ("intuitive_efficiency", "CU", "CV", "CZ", "DA"),
              ("evidence_quality", "DK", "DM", "DR", "DS"), ("guided_recommendation", "ED", "EE", "", "")]
    if not labels and sheet_name == "合并标注结果-review后":
        addresses = [c for spec in legacy for c in spec[1:] if c]
        if all(column_index_from_string(c) <= len(headers) and
               re.search(r"人工|review|复核", headers[column_index_from_string(c) - 1], re.I) for c in addresses):
            products = [{"product_id": "p_xiaoyi", "display_name": "小艺"}, {"product_id": "p_doubao", "display_name": "豆包"}]
            for dim, a, b, ar, br in legacy:
                labels.extend([dict(product_id=products[n]["product_id"], dimension_id=dim, score=s, review=r)
                               for n, (s, r) in enumerate(((a, ar), (b, br)))])
            responses = {products[0]["product_id"]: dict(evidence="C", context="E", answer="F"),
                         products[1]["product_id"]: dict(evidence="D", context="G", answer="H")}
            gates = {}
    return dict(sheet_name=sheet_name, header_row=header_row, case_id=cols.get("case_id", cols.get("query_id", "")),
                query=cols.get("query", cols.get("题目", "")), context=cols.get("公共背景", ""),
                query_hashes=cols.get("提问原图摘要", ""), category=cols.get("场景", ""), products=products,
                session_group=cols.get("会话ID", ""), turn_index=cols.get("轮次", ""),
                history_prefix_sha256=cols.get("历史前缀指纹", ""),
                labels=labels, responses=responses, gates=gates,
                applicability={d: cols[f"人工是否适用_{name}"] for d, name in DIMENSION_NAMES.items() if f"人工是否适用_{name}" in cols},
                answer_text_origin="source" if metadata.get("template_version") else "unknown",
                context_origin="source" if metadata.get("template_version") else "unknown",
                na_status="na_unspecified", human_standard_version=metadata.get("human_standard_version", ""),
                policy_note="", purpose="regression", header_signature=digest(headers))


def inspect_workbook(path: Path, sheet_name: str = "", header_row: int = 1) -> dict:
    with workbooks(path) as (wb, cached):
        sheets = [s.title for s in wb if s.sheet_state == "visible" and s.title != "填写说明"]
        if not sheets:
            raise HumanError("没有可导入的可见工作表", 400)
        selected = sheet_name or ("合并标注结果-review后" if "合并标注结果-review后" in sheets else sheets[0])
        if selected not in sheets or not 1 <= header_row <= 30:
            raise HumanError("工作表或表头行无效", 400)
        ws = wb[selected]
        _check_sheet(ws)
        headers = _headers(ws, header_row)
        metadata = {}
        if "填写说明" in wb:
            for row in cached["填写说明"].iter_rows(max_row=40, max_col=2, values_only=True):
                if row[0] == "模板元信息":
                    try:
                        metadata = json.loads(row[1])
                    except (ValueError, TypeError):
                        pass
        return dict(sheets=sheets, sheet_name=selected, header_row=header_row,
                    columns=[dict(column=get_column_letter(i + 1), header=h) for i, h in enumerate(headers)],
                    sample=[[str(c or "") for c in row] for row in cached[selected].iter_rows(
                        min_row=header_row + 1, max_row=header_row + 3, max_col=len(headers), values_only=True)],
                    suggested_mapping=suggest_mapping(headers, selected, header_row, metadata))


def _number(value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("分数必须为整数")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+(?:\.0+)?", value.strip()):
        raise ValueError("分数格式错误")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or not minimum <= number <= maximum:
        raise ValueError(f"分数必须在 {minimum}—{maximum} 之间")
    return int(number)


def parse_human_labels(path: Path, mapping: ImportMapping) -> dict:
    protocol = resolve_compare_protocol("qa_competitor_compare@" + mapping.human_standard_version)
    catalog = "catalog_" + uuid.uuid4().hex[:16]
    cases, labels, states, issues = [], [], [], []
    all_ids = []
    total_rows = 0
    with workbooks(path) as (wb, cache):
        if mapping.sheet_name not in wb:
            raise HumanError("工作表不存在", 400)
        ws, cs = wb[mapping.sheet_name], cache[mapping.sheet_name]
        _check_sheet(ws)
        headers = _headers(ws, mapping.header_row)
        if mapping.header_signature and digest(headers) != mapping.header_signature:
            raise HumanError("表头已改变，不能复用原列地址；请重新映射", 409)
        physical_columns = [mapping.case_id, mapping.query, mapping.context, mapping.query_hashes, mapping.category,
                            mapping.session_group, mapping.turn_index, mapping.history_prefix_sha256,
                            *mapping.applicability.values()]
        for label in mapping.labels:
            physical_columns.extend(getattr(label, k) for k in ("score", "review", "status", "reason"))
        for fields in (*mapping.responses.values(), *mapping.gates.values()):
            physical_columns.extend(fields.values())
        for col in filter(None, physical_columns):
            if not re.fullmatch(r"[A-Z]{1,3}", col) or column_index_from_string(col) > len(headers):
                raise HumanError(f"无效物理列地址：{col}")
        for label in mapping.labels:
            for col in (label.score, label.review):
                if col and re.search(r"模型|一致率|胜负|Gate|适用性", headers[column_index_from_string(col) - 1], re.I):
                    raise HumanError(f"{col} 列不是人工数字评分列")
        mapping = mapping.model_copy(update={"header_signature": digest(headers)})
        formula_rows = ws.iter_rows(min_row=mapping.header_row + 1, max_col=len(headers))
        cached_rows = cs.iter_rows(min_row=mapping.header_row + 1, max_col=len(headers))
        for row_no, (raw, cached) in enumerate(zip(formula_rows, cached_rows), mapping.header_row + 1):
            if all(_blank(c.value) for c in raw):
                continue
            total_rows += 1
            if total_rows > MAX_ROWS:
                raise HumanError("数据行数超过上限", 400)
            def get(col):
                if not col:
                    return None
                i = column_index_from_string(col) - 1
                cell = raw[i]
                if cell.data_type == "f":
                    if cached[i].value is None:
                        raise ValueError(f"{col}{row_no} 公式无缓存；请另存计算结果")
                    return cached[i].value
                if cell.data_type == "e":
                    raise ValueError(f"{col}{row_no} Excel 错误值")
                return cell.value
            def issue(message, col="", pid="", dim=""):
                issues.append(dict(sheet=mapping.sheet_name, row=row_no, column=col, case_id=case_id,
                                   product_id=pid, dimension_id=dim, message=str(message)))
            case_id = ""
            try:
                value = get(mapping.case_id)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("题号必须以非空文本保存（数值题号请在 Excel 中显式转为文本，保留前导零）")
                case_id = value
                all_ids.append(case_id)
                case_key = catalog + ":" + case_id
                query_hashes = get(mapping.query_hashes)
                query_hashes = json.loads(query_hashes) if query_hashes else []
                if not isinstance(query_hashes, list) or not all(isinstance(h, str) for h in query_hashes):
                    raise ValueError("提问原图摘要须为 JSON 字符串数组")
                case = dict(case_key=case_key, case_id=case_id, query=str(get(mapping.query) or ""),
                            context=str(get(mapping.context) or ""), query_hashes=query_hashes,
                            context_known=bool(mapping.context),
                            session_group=str(get(mapping.session_group) or ""),
                            history_prefix_sha256=str(get(mapping.history_prefix_sha256) or ""),
                            turn_index="" if get(mapping.turn_index) is None else str(get(mapping.turn_index)),
                            context_origin=mapping.context_origin, category=str(get(mapping.category) or ""), responses={})
                for product in mapping.products:
                    pid = product.product_id
                    response = {k: str(get(c) or "") for k, c in mapping.responses.get(pid, {}).items()}
                    response["answer_text_origin"] = mapping.answer_text_origin
                    response["context_origin"] = mapping.context_origin
                    response["response_id"] = response.get("response_id") or ""
                    case["responses"][pid] = response
                cases.append(case)
            except (ValueError, TypeError) as exc:
                issue(exc, mapping.case_id)
                continue
            shared, gates = {}, {}
            try:
                for dim, col in mapping.applicability.items():
                    v = get(col)
                    token = str(v).strip().lower() if not _blank(v) else "unknown"
                    if token not in ("true", "false", "是", "否", "适用", "不适用", "unknown"):
                        raise ValueError(f"{col}{row_no} 适用性值无效")
                    shared[dim] = None if token == "unknown" else token in ("true", "是", "适用")
                for pid, cols in mapping.gates.items():
                    gates[pid] = {}
                    for gate, col in cols.items():
                        v = get(col)
                        token = str(v).strip().lower() if not _blank(v) else "unknown"
                        token = {"通过": "pass", "不通过": "fail", "不确定": "unclear"}.get(token, token)
                        if token not in ("pass", "fail", "unclear", "unknown"):
                            raise ValueError(f"{col}{row_no} Gate 值无效")
                        gates[pid][gate] = token
                states.append(dict(case_key=case_key, applicability=shared, gates=gates,
                                   source=dict(sheet=mapping.sheet_name, row=row_no,
                                               applicability=mapping.applicability, gates=mapping.gates)))
            except ValueError as exc:
                issue(exc)
                cases.pop()
                continue
            start = len(labels)
            for col in mapping.labels:
                try:
                    review, explicit = get(col.review), get(col.status)
                    use_review = not _blank(review)
                    try:
                        base = get(col.score)
                    except ValueError:
                        if not use_review:
                            raise
                        base = str(raw[column_index_from_string(col.score)-1].value)
                    value, chosen = (review, col.review) if use_review else (base, col.score)
                    status = "unlabeled" if _blank(value) else (mapping.na_status if str(value).strip().upper() in ("N/A", "NA") else "scored")
                    score = _number(value, protocol.score_min, protocol.score_max) if status == "scored" else None
                    if not _blank(explicit):
                        explicit = {"不适用": "not_applicable", "无法核验": "unverifiable", "Gate阻断": "gate_blocked",
                                    "未标注": "unlabeled", "已评分": "scored"}.get(str(explicit).strip(), str(explicit).strip())
                        if explicit not in ("scored", "not_applicable", "unverifiable", "gate_blocked", "unlabeled", "na_unspecified"):
                            raise ValueError("人工评分状态无效")
                        if (score is not None and explicit != "scored") or (score is None and explicit == "scored"):
                            raise ValueError("数字评分与显式状态冲突")
                        if status not in ("unlabeled", "na_unspecified", explicit):
                            raise ValueError("NA 转换规则与显式状态冲突")
                        status = explicit
                    pg = gates.get(col.product_id, {})
                    blocked = pg.get("response") == "fail" or (protocol.require_response_pass and pg.get("response") == "unclear") or (
                        not protocol.require_response_pass and pg.get("safety") == "fail")
                    if score is not None and (shared.get(col.dimension_id) is False or blocked):
                        raise ValueError("人工数字分与适用性/Gate 冲突")
                    if status == "not_applicable" and shared.get(col.dimension_id) is True:
                        raise ValueError("不适用标签与共享适用性冲突")
                    source = dict(sheet=mapping.sheet_name, row=row_no, base_cell=f"{col.score}{row_no}" if col.score else "",
                                  review_cell=f"{col.review}{row_no}" if col.review else "", selected_cell=f"{chosen}{row_no}" if chosen else "",
                                  base_raw=base, review_raw=review, formula=any(raw[column_index_from_string(c)-1].data_type == "f" for c in (col.score, col.review) if c))
                    labels.append(HumanScoreLabel(case_key=case_key, product_id=col.product_id,
                        response_id=case["responses"][col.product_id].get("response_id") or digest([case_key, col.product_id])[:24],
                        dimension_id=col.dimension_id, score_status=status, score=score,
                        annotation_stage="review" if use_review else "base", reason=str(get(col.reason) or ""), source=source).model_dump())
                except (ValueError, TypeError) as exc:
                    issue(exc, col.review or col.score or col.status, col.product_id, col.dimension_id)
            for dim in DIMENSION_NAMES:
                group = [r for r in labels[start:] if r["dimension_id"] == dim]
                if any(r["score_status"] == "not_applicable" for r in group) and any(r["score_status"] == "scored" for r in group):
                    for r in group:
                        r["resolution"] = "pending_conflict"
                    issue("不同产品的不适用与数字分冲突", dim=dim)
    duplicate_ids = {key for key, count in Counter(all_ids).items() if count > 1}
    duplicate_keys = {c["case_key"] for c in cases if c["case_id"] in duplicate_ids}
    for key in sorted(duplicate_ids):
        issues.append(dict(sheet=mapping.sheet_name, case_id=key, column=mapping.case_id, message="重复题号；所有重复行均排除"))
    cases = [c for c in cases if c["case_key"] not in duplicate_keys]
    labels = [r for r in labels if r["case_key"] not in duplicate_keys]
    states = [r for r in states if r["case_key"] not in duplicate_keys]
    result = dict(mapping=mapping.model_dump(), cases=cases, labels=labels, states=states, issues=issues,
                  source_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  summary=dict(total_rows=total_rows, case_count=len(cases), label_count=len(labels),
                               statuses=dict(Counter(r["score_status"] for r in labels)), issue_count=len(issues)))
    result["preview_sha256"] = digest(result)
    return result


def build_human_template(snapshot: dict, standard: str) -> bytes:
    from .human_compare import case_identity, response_identity
    from .human_report_export import tables_workbook
    from .compare_statistics import StatisticsTable
    protocol = resolve_compare_protocol("qa_competitor_compare@" + standard)
    items = snapshot.get("items") or []
    count = max((int(i.get("product_count") or (3 if i.get("video3") or i.get("screenshot3") or i.get("answer3") else 2)) for i in items), default=2)
    names = (snapshot.get("options") or {}).get("product_names") or []
    products = [dict(product_id=f"product{n}", display_name=names[n-1] if n <= len(names) and names[n-1] else f"产品{n}") for n in range(1, count+1)]
    headers = ["case_id", "query", "公共背景", "提问原图摘要", "场景", "会话ID", "轮次", "历史前缀指纹"]
    for n in range(1, count+1):
        headers.extend(f"{name}_产品{n}" for name in ("源回答", "源背景", "采集ID", "源摘要", "证据引用"))
        for dim in SCORE_DIMENSIONS:
            headers.extend(f"{name}_产品{n}_{DIMENSION_NAMES[dim]}" for name in ("人工分", "人工复核分", "人工评分状态", "人工批注"))
        headers.extend((f"人工响应Gate_产品{n}", f"人工安全Gate_产品{n}"))
    headers.extend(f"人工是否适用_{DIMENSION_NAMES[d]}" for d in SCORE_DIMENSIONS)
    table = StatisticsTable("人工标注模板", tuple(headers))
    for item in items:
        identity = case_identity(item)
        source = item.get("source_data") or {}
        row = [str(item.get("id") or source.get("query_id") or ""), identity["query"], identity["context"],
               json.dumps(identity["query_hashes"], ensure_ascii=False), item.get("category") or source.get("category") or "",
               item.get("session_group",source.get("session_group","")),item.get("turn_index",source.get("turn_index","")), item.get("history_prefix_sha256", "")]
        for n in range(1, count+1):
            response = response_identity(item, n)
            row.extend(response.get(k, "") for k in ("answer", "context", "response_id", "sha256", "evidence"))
            row.extend([None] * (len(SCORE_DIMENSIONS)*4 + 2))
        row.extend([None]*len(SCORE_DIMENSIONS))
        table.rows.append(row)
    meta = dict(template_version="human-wide-1.0", products=products, human_standard_version=standard)
    notes = StatisticsTable("填写说明", ("项目", "说明"), rows=[
        ["模板元信息", json.dumps(meta, ensure_ascii=False)], ["人工标准", protocol.display],
        ["分值范围", f"{protocol.score_min}—{protocol.score_max} 的整数"],
        ["填写规则", "题号须为文本；留空=未标注；review 非空优先（包括 0 和 N/A）。N/A 默认原因未知。"],
        ["状态可选值", "scored / not_applicable / unverifiable / gate_blocked / unlabeled / na_unspecified"],
        ["身份核对", "请核对产品、题号和原始回答；缺失真实题号须先补充。填写说明中的产品 ID 可在导入映射中修正。"],
        ["Gate/适用性", "Gate：pass/fail/unclear；适用性：true/false；留空=未知。"],
    ])
    return tables_workbook({"人工标注": [table], "填写说明": [notes]}, plain=True)
