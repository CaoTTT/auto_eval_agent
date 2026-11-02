"""Categorical human GSB references, without numeric score conversion."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from itertools import combinations
import json
import re
import uuid

from openpyxl.utils import column_index_from_string, get_column_letter

from .human_baselines import HumanError, digest

STANDARD_ID = "chitchat_gsb_compare"
OUTCOMES = ("G", "S", "B")
STATUS_LABELS = {"evaluated": "已评", "insufficient_evidence": "待补证据",
                 "needs_review": "待复核", "unlabeled": "未标注"}


def reverse_gsb(value):
    return {"G": "B", "B": "G", "S": "S"}.get(value)


def pair_title(a, b):
    return f"产品{a}对产品{b}"


def suggest_gsb_mapping(headers, sheet_name, header_row, metadata):
    cols = {h: get_column_letter(i + 1) for i, h in enumerate(headers) if headers.count(h) == 1}
    products = metadata.get("products") or [dict(product_id=f"product{n}", display_name=f"产品{n}") for n in (1, 2)]
    pairs = []
    for a, b in combinations(range(1, len(products) + 1), 2):
        title = pair_title(a, b)
        fields = {key: cols[header] for key, header in (
            ("gsb", f"人工GSB_{title}"), ("review", f"人工复核GSB_{title}"),
            ("status", f"人工状态_{title}"), ("reason", f"人工理由_{title}"),
            ("serious_issue", f"人工严重问题_{title}")) if header in cols}
        if any(fields.get(key) for key in ("gsb", "review", "status")):
            pairs.append(dict(product_a=products[a - 1]["product_id"], product_b=products[b - 1]["product_id"], **fields))
    responses = {p["product_id"]: {field: cols[name] for field, name in (
        ("answer", f"源回答_产品{n}"), ("context", f"源背景_产品{n}"),
        ("response_id", f"采集ID_产品{n}"), ("sha256", f"源摘要_产品{n}"),
        ("evidence", f"证据引用_产品{n}")) if name in cols} for n, p in enumerate(products, 1)}
    return dict(sheet_name=sheet_name, header_row=header_row, case_id=cols.get("case_id", ""),
                query=cols.get("query", ""), context=cols.get("公共背景", ""),
                query_hashes=cols.get("提问原图摘要", ""), category=cols.get("场景", ""),
                session_group=cols.get("会话ID", ""), turn_index=cols.get("轮次", ""),
                history_prefix_sha256=cols.get("历史前缀指纹", ""), products=products,
                labels=[], pair_labels=pairs, responses=responses, gates={}, inputs={}, applicability={},
                answer_text_origin="source" if metadata.get("template_version") else "unknown",
                context_origin="source" if metadata.get("template_version") else "unknown",
                human_standard_id=metadata.get("human_standard_id", ""),
                human_standard_version=metadata.get("human_standard_version", ""),
                human_bundle_revision=metadata.get("human_bundle_revision", ""),
                human_result_schema_version=metadata.get("human_result_schema_version", ""),
                policy_note="", purpose="regression", header_signature=digest(headers))


def parse_gsb_labels(path, mapping):
    from .human_baseline_import import workbooks, _headers, _check_sheet, _workbook_metadata, _blank, MAX_ROWS
    from .human_baselines import human_protocol
    protocol = human_protocol(mapping.human_standard_version, mapping.human_standard_id, mapping.human_bundle_revision)
    cases, labels, states, issues, seen = [], [], [], [], []
    catalog = "catalog_" + uuid.uuid4().hex[:16]
    total = 0
    with workbooks(path) as (wb, cache):
        if mapping.sheet_name not in wb:
            raise HumanError("工作表不存在", 400)
        ws, cs = wb[mapping.sheet_name], cache[mapping.sheet_name]
        _check_sheet(ws)
        headers = _headers(ws, mapping.header_row)
        meta = _workbook_metadata(cache)
        if meta.get("human_standard_version"):
            identity = (meta.get("human_standard_id"), meta.get("human_standard_version"),
                        meta.get("human_bundle_revision"), meta.get("human_result_schema_version"))
            if identity != (protocol.standard_id, protocol.standard_version, protocol.bundle_revision, protocol.result_schema_version):
                raise HumanError("模板或基准的冻结标准与GSB标准不同；不得迁移旧数字评分")
        if mapping.header_signature and mapping.header_signature != digest(headers):
            raise HumanError("表头已改变，请重新映射", 409)
        columns = [getattr(mapping, key) for key in ("case_id", "query", "context", "query_hashes", "category",
                   "session_group", "turn_index", "history_prefix_sha256")]
        columns += [v for fields in mapping.responses.values() for v in fields.values()]
        columns += [getattr(pair, k) for pair in mapping.pair_labels for k in ("gsb", "review", "status", "reason", "serious_issue")]
        for column in filter(None, columns):
            if not re.fullmatch(r"[A-Z]{1,3}", column) or column_index_from_string(column) > len(headers):
                raise HumanError(f"无效物理列地址：{column}")
        for pair in mapping.pair_labels:
            for col in filter(None, (pair.gsb, pair.review)):
                title = headers[column_index_from_string(col) - 1]
                if not re.search(r"人工|复核|review", title, re.I) or re.search(r"模型|一致率|净胜率", title):
                    raise HumanError(f"{col} 列不是独立人工GSB标注列，不能导入模型结论或数字评分")
        for row_no, (raw, cached) in enumerate(zip(ws.iter_rows(min_row=mapping.header_row + 1, max_col=len(headers)),
                                                  cs.iter_rows(min_row=mapping.header_row + 1, max_col=len(headers))), mapping.header_row + 1):
            if all(_blank(c.value) for c in raw):
                continue
            total += 1
            if total > MAX_ROWS:
                raise HumanError("数据行数超过上限", 400)
            def get(col):
                if not col:
                    return None
                i = column_index_from_string(col) - 1
                if raw[i].data_type == "e":
                    raise ValueError(f"{col}{row_no} Excel错误值")
                if raw[i].data_type == "f":
                    if cached[i].value is None:
                        raise ValueError(f"{col}{row_no} 公式无缓存")
                    return cached[i].value
                return raw[i].value
            cid = ""
            def issue(exc, col="", pair=""):
                issues.append(dict(sheet=mapping.sheet_name, row=row_no, column=col, case_id=cid,
                                   product_id=pair, dimension_id="overall", message=str(exc)))
            try:
                cid = get(mapping.case_id)
                if not isinstance(cid, str) or not cid.strip():
                    raise ValueError("题号必须是非空文本，不能使用自动行号")
                seen.append(cid)
                hashes = json.loads(get(mapping.query_hashes) or "[]")
                if not isinstance(hashes, list) or not all(isinstance(x, str) for x in hashes):
                    raise ValueError("提问原图摘要须为JSON字符串数组")
                ck = catalog + ":" + cid
                case = dict(case_key=ck, case_id=cid, query=str(get(mapping.query) or ""),
                            context=str(get(mapping.context) or ""), query_hashes=hashes,
                            context_known=bool(mapping.context), context_origin=mapping.context_origin,
                            category=str(get(mapping.category) or ""), session_group=str(get(mapping.session_group) or ""),
                            turn_index=str(get(mapping.turn_index) or ""), history_prefix_sha256=str(get(mapping.history_prefix_sha256) or ""),
                            responses={})
                for product in mapping.products:
                    fields = mapping.responses.get(product.product_id, {})
                    case["responses"][product.product_id] = {**{k: str(get(c) or "") for k, c in fields.items()},
                        "answer_text_origin": mapping.answer_text_origin, "context_origin": mapping.context_origin}
                cases.append(case)
                states.append(dict(case_key=ck, applicability={}, gates={}, inputs={}))
            except (ValueError, TypeError) as exc:
                issue(exc, mapping.case_id)
                continue
            for pair in mapping.pair_labels:
                try:
                    base, review = get(pair.gsb), get(pair.review)
                    selected = pair.review if not _blank(review) else pair.gsb
                    value = review if not _blank(review) else base
                    if not _blank(value) and (not isinstance(value, str) or value.strip() not in OUTCOMES):
                        raise ValueError("GSB只接受文本G、S、B，不接受数字评分或NA")
                    gsb = None if _blank(value) else value.strip()
                    explicit = get(pair.status)
                    status = str(explicit).strip() if not _blank(explicit) else ("evaluated" if gsb else "unlabeled")
                    status = {v: k for k, v in STATUS_LABELS.items()}.get(status, status)
                    if status not in STATUS_LABELS or (status == "evaluated") != (gsb is not None):
                        raise ValueError("GSB结论与判定状态冲突；缺证/待复核不能填写S")
                    reason = str(get(pair.reason) or "")
                    if status != "unlabeled" and not reason.strip():
                        raise ValueError("已评、缺证或待复核须填写决定性理由")
                    serious = get(pair.serious_issue)
                    serious = None if _blank(serious) else str(serious).strip()
                    if serious not in (None, "none", "A", "B", "both"):
                        raise ValueError("严重问题仅none/A/B/both，A/B按该产品对方向")
                    if status == "evaluated" and ((serious == "A" and gsb != "B") or (serious == "B" and gsb != "G")):
                        raise ValueError("单方存在严重问题时应判该方更差，严重问题标记与GSB结论冲突")
                    labels.append(dict(case_key=ck, product_a=pair.product_a, product_b=pair.product_b,
                        dimension_id="overall", status=status, gsb=gsb, reason=reason, serious_issue=serious,
                        annotation_stage="review" if not _blank(review) else "base", resolution="accepted",
                        source=dict(sheet=mapping.sheet_name, row=row_no, selected_cell=f"{selected}{row_no}" if selected else "",
                                    base_raw=base, review_raw=review,
                                    formula=any(raw[column_index_from_string(c) - 1].data_type == "f" for c in (pair.gsb, pair.review) if c))))
                except (ValueError, TypeError) as exc:
                    issue(exc, pair.review or pair.gsb or pair.status, f"{pair.product_a}/{pair.product_b}")
    duplicates = {key for key, count in Counter(seen).items() if count > 1}
    for cid in sorted(duplicates):
        issues.append(dict(sheet=mapping.sheet_name, case_id=cid, column=mapping.case_id, message="重复题号；所有重复行均排除"))
    invalid = {case["case_key"] for case in cases if case["case_id"] in duplicates}
    cases = [case for case in cases if case["case_key"] not in invalid]
    labels = [label for label in labels if label["case_key"] not in invalid]
    states = [state for state in states if state["case_key"] not in invalid]
    result = dict(mapping=mapping.model_dump(), cases=cases, labels=labels, states=states, issues=issues,
        source_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        summary=dict(total_rows=total, case_count=len(cases), label_count=len(labels),
                     statuses=dict(Counter(label["status"] for label in labels)), issue_count=len(issues)))
    result["preview_sha256"] = digest(result)
    return result


def build_gsb_template(snapshot, protocol, baseline=None):
    from .compare_statistics import StatisticsTable
    from .human_report_export import tables_workbook
    from .human_compare import case_identity, response_identity
    items = snapshot.get("items") or []
    count = len(baseline["products"]) if baseline else max((int(i.get("product_count") or 2) for i in items), default=2)
    names = (snapshot.get("options") or {}).get("product_names") or []
    products = baseline["products"] if baseline else [dict(product_id=f"product{n}", display_name=names[n - 1] if n <= len(names) else f"产品{n}") for n in range(1, count + 1)]
    headers = ["case_id", "query", "公共背景", "提问原图摘要", "场景", "会话ID", "轮次", "历史前缀指纹"]
    for n in range(1, count + 1):
        headers += [f"{key}_产品{n}" for key in ("源回答", "源背景", "采集ID", "源摘要", "证据引用")]
    for a, b in combinations(range(1, count + 1), 2):
        headers += [f"{field}_{pair_title(a, b)}" for field in ("人工GSB", "人工复核GSB", "人工状态", "人工理由", "人工严重问题")]
    table = StatisticsTable("人工标注模板", tuple(headers))
    cases = baseline["cases"] if baseline else items
    for case in cases:
        identity = case if baseline else case_identity(case)
        row = [str(case.get("case_id") or case.get("id") or (case.get("source_data") or {}).get("query_id") or ""),
               identity.get("query", ""), identity.get("context", ""), json.dumps(identity.get("query_hashes", []), ensure_ascii=False),
               case.get("category", ""), case.get("session_group") or case.get("session_id", ""), case.get("turn_index", ""), case.get("history_prefix_sha256", "")]
        for n, product in enumerate(products, 1):
            response = case.get("responses", {}).get(product["product_id"], {}) if baseline else response_identity(case, n)
            row += [response.get(key, "") for key in ("answer", "context", "response_id", "sha256", "evidence")]
        for a, b in combinations(range(1, count + 1), 2):
            label = next((v for v in (baseline or {}).get("labels", []) if v["case_key"] == case["case_key"]
                and {v["product_a"], v["product_b"]} == {products[a - 1]["product_id"], products[b - 1]["product_id"]}), {}) if baseline else {}
            gsb = label.get("gsb")
            serious = label.get("serious_issue")
            if label and label["product_a"] != products[a - 1]["product_id"]:
                gsb = reverse_gsb(gsb)
                serious = {"A": "B", "B": "A"}.get(serious, serious)
            row += [gsb, None, label.get("status"), label.get("reason"), serious]
        table.rows.append(row)
    metadata = dict(template_version="human-gsb-wide-1.0", products=products,
        human_standard_id=protocol.standard_id, human_standard_version=protocol.standard_version,
        human_bundle_revision=protocol.bundle_revision, human_result_schema_version=protocol.result_schema_version,
        evaluation_profile=protocol.id, dimensions=["overall"], dimension_names={"overall": "整体GSB"},
        score_dimensions=[], score_range=None, allowed_outcomes=list(OUTCOMES))
    notes = StatisticsTable("填写说明", ("项目", "说明"), rows=[
        ["模板元信息", json.dumps(metadata, ensure_ascii=False)],
        ["判定方向", "每个产品对前者为A、后者为B；G=A更好，S=整体相近，B=A更差。"],
        ["填写规则", "结论只填G/S/B，复核非空优先。缺证/待复核留空并填写状态与理由，不能填写S。"],
        ["状态", "evaluated / insufficient_evidence / needs_review / unlabeled"],
        ["严重问题", "none/A/B/both；按该产品对方向填写，仅记录可见严重问题。"],
        ["身份要求", "题号必须为文本；多轮保留历史前缀指纹。按同题同采集回答对齐，不以路径相同证明内容相同。"],
        ["标准隔离", "仅复用原始输入，不得复制旧数字评分或模型GSB作为人工标注。"]])
    return tables_workbook({"人工标注": [table], "填写说明": [notes]}, plain=True)


def finalize_gsb_row(row):
    row["identity_accepted"] = row["match_status"] in ("verified", "confirmed") and row["result_input_binding"] in ("verified", "confirmed")
    row["comparable"] = bool(row["identity_accepted"] and row["compatible"] and row.get("human_status") == "evaluated"
        and row.get("model_score_status") == "evaluated" and row.get("human_gsb") in OUTCOMES and row.get("model_gsb") in OUTCOMES)
    row["agreement"] = row["human_gsb"] == row["model_gsb"] if row["comparable"] else None
    row["reversal"] = {row["human_gsb"], row["model_gsb"]} == {"G", "B"} if row["comparable"] else None
    row["difference"] = None
    row["exclusion"] = "" if row["comparable"] else (row["match_status"] if row["match_status"] not in ("verified", "confirmed") else
        row["result_input_binding"] if row["result_input_binding"] not in ("verified", "confirmed") else
        "标准不可比" if not row["compatible"] else row["human_status"] if row.get("human_gsb") not in OUTCOMES else row["model_status"])


def align_gsb_baseline(baseline, snapshot, mapping, scope, dimensions, mode):
    from .human_baselines import baseline_protocol
    from .human_compare import _source_id, _identity, _binding, _policy
    from .protocol_identity import recover_protocol_identity
    from .history import judge_runtime_summary
    from .compare_statistics import Case
    from .chitchat_gsb_statistics import gsb_pair_state
    policy_h = baseline_protocol(baseline)
    pids = [p["product_id"] for p in baseline["products"]]
    if not mapping.product_map or len(set(mapping.product_map.values())) != len(mapping.product_map):
        raise HumanError("请提供一对一产品映射")
    if any(key not in ("answer1", "answer2", "answer3") or value not in pids for key, value in mapping.product_map.items()):
        raise HumanError("产品槽位或基准产品身份无效")
    slots = {pid: int(key[-1]) for key, pid in mapping.product_map.items()}
    cases = {c["case_id"]: c for c in baseline["cases"]}
    labels = {(r["case_key"], frozenset((r["product_a"], r["product_b"]))): r for r in baseline["labels"]}
    indexes = defaultdict(list)
    for index, item in enumerate(snapshot["items"]):
        cid = _source_id(item, mapping.id_field)
        if cid:
            indexes[mapping.case_map.get(cid, cid)].append(index)
    results = {r["index"]: r for r in snapshot["results"] if type(r.get("index")) is int}
    recovered = recover_protocol_identity(snapshot)
    rows = []
    def combine(values):
        bad = next(((state, why) for state, why in values if state not in ("verified", "identity_unverified", "binding_unverified")), None)
        if bad:
            return bad
        state = next((state for state, _ in values if state != "verified"), "verified")
        return state, [reason for _, reasons in values for reason in reasons]
    for cid in scope:
        case = cases.get(cid)
        matches = indexes.get(cid, [])
        index = matches[0] if len(matches) == 1 else None
        item = snapshot["items"][index] if index is not None else {}
        result = results.get(index, {})
        policy = _policy(snapshot, result, recovered)
        if policy and (policy.id, policy.bundle_revision, policy.result_schema_version) != (policy_h.id, policy_h.bundle_revision, policy_h.result_schema_version):
            raise HumanError("GSB人工与模型必须使用相同完整标准、冻结修订号和结果Schema，不能跨家族或版本迁移")
        ck = case["case_key"] if case else "out:" + cid
        for pa, pb in combinations(pids, 2):
            a, b = slots.get(pa), slots.get(pb)
            count = int(item.get("product_count") or 2)
            if len(matches) > 1:
                status, reasons = "ambiguous", ["任务题号重复或映射后重复"]
            elif not case or index is None or not a or not b or max(a, b) > count:
                status, reasons = "unmatched", ["题号或产品对无对应项"]
            else:
                status, reasons = combine([_identity(case, item, pid, slot) for pid, slot in ((pa, a), (pb, b))])
            binding, why = combine([_binding(item, result, slot) for slot in (a, b)]) if index is not None and a and b else ("missing_result", [])
            label = labels.get((ck, frozenset((pa, pb))), {})
            human = label.get("gsb") if label.get("resolution") == "accepted" and label.get("status") == "evaluated" else None
            if label and label["product_a"] != pa:
                human = reverse_gsb(human)
            model_case = Case(index or 0, item, result, "failed" if result.get("error") else "done" if result else "unfinished",
                              count, policy.standard_version if policy else "", policy.bundle_revision if policy else "",
                              policy.standard_id if policy else "", "" if recovered.get("executable") else "协议身份不完整")
            model_status, pair = gsb_pair_state(model_case, min(a, b), max(a, b), policy) if a and b and policy else ("unknown_protocol", None)
            pair = pair or {}
            model = pair.get("gsb") if model_status == "evaluated" else None
            if a and b and a > b:
                model = reverse_gsb(model)
            if model not in OUTCOMES:
                model = None
            model_serious = pair.get("serious_issue")
            if a and b and a > b:
                model_serious = {"A": "B", "B": "A"}.get(model_serious, model_serious)
            human_serious = label.get("serious_issue")
            if label and label["product_a"] != pa:
                human_serious = {"A": "B", "B": "A"}.get(human_serious, human_serious)
            row = dict(comparison_kind="gsb", task_id=snapshot["task_id"], task_snapshot_sha256=snapshot["snapshot_sha256"],
                match_id=digest([snapshot["task_id"], snapshot["snapshot_sha256"], ck, pa, pb])[:32],
                case_id=cid, case_key=ck, task_item_index=index, product_id="gsb:" + digest([pa, pb])[:24],
                product_a=pa, product_b=pb, task_product_slot=None, task_product_slots=[a, b], dimension_id="overall",
                query=case.get("query", "") if case else item.get("query", ""), category=case.get("category", "") if case else "",
                input_modality="text_image" if item.get("query_images") else "text", evidence_mode=item.get("evidence_mode", "video_frames"),
                match_status=status, match_reasons=reasons, result_input_binding=binding, binding_reasons=why,
                human_gsb=human, model_gsb=model, human_score=None, model_score=None,
                human_status=label.get("status", "unlabeled"), model_status=STATUS_LABELS.get(model_status, model_status or "未完成"),
                model_score_status=model_status, human_reason=label.get("reason", ""), model_reason=pair.get("reason", ""),
                human_source=label.get("source", {}), human_serious_issue=human_serious, model_serious_issue=model_serious,
                human_standard_id=STANDARD_ID, model_standard_id=policy.standard_id if policy else "unknown",
                model_standard=policy.standard_version if policy else "unknown", bundle_revision=policy.bundle_revision if policy else "unknown",
                model_result_schema_version=policy.result_schema_version if policy else result.get("result_schema_version"),
                compatible=bool(policy), prompt_sha256=result.get("prompt_sha256", ""),
                human_applicable=None, model_applicable=None, model_gates={}, human_gates={}, model_reason_scope="pair",
                model_evidence=[], model_evidence_scope="merged_in_reason", evidence="",
                evidence_a=(case or {}).get("responses", {}).get(pa, {}).get("evidence", ""),
                evidence_b=(case or {}).get("responses", {}).get(pb, {}).get("evidence", ""),
                **judge_runtime_summary(snapshot, result))
            finalize_gsb_row(row)
            rows.append(row)
    return rows


def gsb_human_statistics(rows, task_ids):
    pair_ids = list(dict.fromkeys(r["product_id"] for r in rows))
    metrics = []
    ratio = lambda a, b: a / b if b else None
    for pair in pair_ids:
        valid = {task: {r["case_key"]: r for r in rows if r["task_id"] == task and r["product_id"] == pair and r["comparable"]} for task in task_ids}
        common = set.intersection(*(set(v) for v in valid.values())) if valid else set()
        for task in task_ids:
            group = [r for r in rows if r["task_id"] == task and r["product_id"] == pair]
            hn = sum(r["human_status"] == "evaluated" and r.get("human_gsb") in OUTCOMES for r in group)
            for scope in (("own", "common") if len(task_ids) > 1 else ("own",)):
                selected = [valid[task][key] for key in sorted(common if scope == "common" else valid[task])]
                n = len(selected)
                exact = sum(r["agreement"] for r in selected)
                reversal = sum(r["reversal"] for r in selected)
                hd = {token: sum(r["human_gsb"] == token for r in selected) for token in OUTCOMES}
                md = {token: sum(r["model_gsb"] == token for r in selected) for token in OUTCOMES}
                exemplar = group[0]
                metrics.append(dict(task_id=task, product_id=pair, product_a=exemplar["product_a"], product_b=exemplar["product_b"],
                    dimension_id="overall", scope=scope, human_n=hn, n=n, coverage=ratio(n, hn), exact_n=exact, exact=ratio(exact, n),
                    reversal_n=reversal, reversal=ratio(reversal, n), human_distribution=hd, model_distribution=md,
                    human_net_win=ratio(hd["G"] - hd["B"], n), model_net_win=ratio(md["G"] - md["B"], n),
                    confusion={f"{h},{m}": sum(r["human_gsb"] == h and r["model_gsb"] == m for r in selected) for h in OUTCOMES for m in OUTCOMES},
                    sample_ids=[r["match_id"] for r in selected], excluded=dict(Counter(r["exclusion"] or "未进入共同样本" for r in group if r not in selected))))
    return dict(metric_version="human-statistics-chitchat-gsb-1.0", comparison_kind="gsb", gsb_agreement=metrics,
                scores=[], pairs=[], ranks=[], states=[], overview={task: dict(label_rows=sum(r["task_id"] == task for r in rows)) for task in task_ids},
                policy="按同题同产品对的有效GSB比较；未判不补S；多轮逐轮对齐，不据轮次数宣称独立会话样本")


def export_gsb_human_report(report):
    from .compare_statistics import StatisticsTable
    from .human_report_export import tables_workbook
    metrics = StatisticsTable("GSB一致性", ("任务", "产品A", "产品B", "样本口径", "可比数", "人工有效数", "覆盖率", "完全一致率", "G与B反向率", "人工GSB分布", "模型GSB分布", "人工净胜率", "模型净胜率", "混淆矩阵"),
                              ("text", "text", "text", "text", "count", "count", "percent", "percent", "percent", "text", "text", "percent", "percent", "text"))
    for m in report["metrics"].get("gsb_agreement", []):
        metrics.rows.append([m[key] if key not in ("human_distribution", "model_distribution", "confusion") else json.dumps(m[key], ensure_ascii=False)
            for key in ("task_id", "product_a", "product_b", "scope", "n", "human_n", "coverage", "exact", "reversal", "human_distribution", "model_distribution", "human_net_win", "model_net_win", "confusion")])
    fields = ("case_id", "query", "task_id", "product_a", "product_b", "human_gsb", "model_gsb", "agreement", "reversal", "human_status", "model_status", "human_reason", "model_reason", "human_serious_issue", "model_serious_issue", "match_status", "result_input_binding", "exclusion", "evidence_a", "evidence_b")
    details = StatisticsTable("逐题GSB对比", ("题号", "问题", "任务", "产品A", "产品B", "人工GSB", "模型GSB", "是否一致", "是否G与B反向", "人工状态", "模型状态", "人工理由", "模型理由", "人工严重问题", "模型严重问题", "身份状态", "结果绑定", "排除原因", "A证据", "B证据"))
    details.rows = [[r.get(key) for key in fields] for r in report["rows"]]
    notes = StatisticsTable("口径与版本", ("项目", "说明"), rows=[
        ["人工协议", json.dumps(report.get("human_protocol", {}), ensure_ascii=False)],
        ["模型协议", json.dumps(report.get("model_protocols", []), ensure_ascii=False)],
        ["统计口径", report["metrics"].get("policy", "")],
        ["方向", "产品A相对产品B：G胜、S近似、B负；不从数字分数换算。"]])
    return tables_workbook({"GSB一致性": [metrics], "逐题GSB对比": [details], "口径与版本": [notes]})
