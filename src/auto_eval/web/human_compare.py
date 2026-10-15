"""Frozen post-processing reports, with conservative case/answer identity checks."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import copy
import time
import uuid

from pydantic import Field

from ..judges.compare_protocols import resolve_compare_protocol
from .compare_statistics import Case, SCORE_DIMENSIONS, score_state
from .history import judge_runtime_summary, task_to_snapshot
from .tasks import latest_results_by_index
from .human_baselines import HumanError, HumanStore, StrictModel, atomic_json, digest, read_json
from .human_statistics import build_human_statistics


class TaskMapping(StrictModel):
    task_id: str
    id_field: str = "id"
    product_map: dict[str, str]  # answer1 -> stable baseline product identity
    case_map: dict[str, str] = Field(default_factory=dict)  # source ID -> baseline ID


class ComparisonRequest(StrictModel):
    baseline_id: str
    version: int = Field(ge=1)
    tasks: list[TaskMapping] = Field(min_length=1, max_length=10)
    case_ids: list[str] | None = None
    dimensions: list[str] = Field(default_factory=lambda: list(SCORE_DIMENSIONS))
    compatibility_mode: str = "same_standard"


class GenerateRequest(StrictModel):
    preview_id: str
    config_sha256: str
    confirm_match_ids: list[str] = Field(default_factory=list)
    confirm_binding_ids: list[str] = Field(default_factory=list)
    confirm_reason: str = ""


def _text(value):
    return str(value or "").replace("\r\n", "\n")


def case_identity(item: dict) -> dict:
    return dict(query=_text(item.get("query") or item.get("question")), context=_text(item.get("context")),
                query_hashes=[m["original_sha256"] for m in item.get("query_image_meta",[]) if m.get("original_sha256")])


def response_identity(item: dict, n: int) -> dict:
    source = item.get("source_data") or {}
    def field(key):
        return item.get(key, source.get(key, ""))
    return dict(answer=_text(field(f"answer{n}")), context=_text(field(f"context{n}")),
                response_id=_text(field(f"response_id{n}") or field(f"capture_id{n}")),
                sha256=_text(field(f"response_sha256{n}") or field(f"capture_sha256{n}")),
                evidence=_text(field(f"video{n}") or field(f"screenshot{n}")))


def freeze_run_snapshot(task) -> dict:
    """Must be called without awaiting between the state check and deepcopy."""
    if task.mode != "compare":
        raise HumanError("人工评分对比仅支持 compare 任务")
    if task.status not in ("done", "error", "cancelled", "paused") or task.active_runs or task.repair_status in ("queued", "running"):
        raise HumanError("任务或补跑仍在执行，请结束后生成预览", 409)
    data = copy.deepcopy(task_to_snapshot(task))
    data["results"] = [dict(copy.deepcopy(row),index=index) for index,row in latest_results_by_index(task).items()]
    # Inference settings distinguish repeated experiments even when scores match.
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k,v in value.items() if k not in ("timings", "duration_s", "elapsed_s", "updated_at", "measured_at")}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    data["snapshot_sha256"] = digest(stable({k: data[k] for k in ("items", "results", "evaluation_profile", "protocol_manifest", "judge_runtime")}))
    data["frozen_at"] = time.time()
    return data


def _source_id(item: dict, field: str) -> str:
    if field.startswith("source_data.") and field.count(".") == 1:
        value = (item.get("source_data") or {}).get(field.split(".")[1])
    elif "." not in field:
        value = item.get(field)
    else:
        raise HumanError("题号字段只能选择输入字段或 source_data 内的字段")
    if isinstance(value, bool) or value is None or value == "":
        return ""
    return str(value)


def _binding(item: dict, result: dict, n: int) -> tuple[str, list[str]]:
    if not result:
        return "missing_result", ["模型没有结果"]
    # Compare raw source values only. Visual judge transcriptions are different fields.
    for key in ("query", "context", f"answer{n}", f"context{n}"):
        if key in result and _text(result[key]).strip() != _text(item.get(key) or (item.get("question") if key=="query" else "")).strip():
            return "stale_result", [f"结果与当前输入 {key} 不一致"]
    a,b = item.get("input_manifest_sha256"),result.get("input_manifest_sha256")
    if a and b and a != b:
        return "stale_result", ["结果输入摘要已改变"]
    ah,bh = case_identity(item)["query_hashes"],case_identity(result)["query_hashes"]
    if ah and bh and ah != bh:
        return "stale_result", ["结果提问原图摘要已改变"]
    # A recorded manifest is useful within a run; never use it across products/prompts.
    if a and b and a==b:
        return "verified", ["评测时输入摘要一致"]
    r = response_identity(item,n)
    if not r["evidence"] and not item.get("query_images") and (not item.get("context") or "context" in result) and all(k in result for k in ("query",f"answer{n}",f"context{n}")):
        return "verified", ["纯文本输入与结果所存源文本一致"]
    return "binding_unverified", ["历史结果缺少可验证的输入绑定摘要"]


def _identity(case: dict, item: dict, pid: str, n: int):
    left, right = case, case_identity(item)
    if item.get("session_id"):
        if not case.get("history_prefix_sha256") or not item.get("history_prefix_sha256"):
            return "history_unverified", ["多轮人工基准缺少完整原始历史指纹；不能仅凭题号或相同问题对齐"]
        if case["history_prefix_sha256"] != item["history_prefix_sha256"]:
            return "content_mismatch", ["多轮原始历史版本不同"]
    reasons = []
    for field in ("query","context"):
        if field == "context" and (case.get("context_origin") != "source" or not case.get("context_known",True)):
            continue
        if (field == "context" or left.get(field)) and _text(left.get(field)) != right[field]:
            return "content_mismatch", [f"{field} 原始内容不同"]
    for field in ("session_group", "turn_index"):
        right_value=item.get(field,(item.get("source_data") or {}).get(field))
        if case.get(field) not in (None,"") and right_value is not None and str(case[field])!=str(right_value):
            return "content_mismatch", [f"多轮关系 {field} 不同"]
    if left.get("query_hashes") and right["query_hashes"] and left["query_hashes"] != right["query_hashes"]:
        return "content_mismatch", ["提问原图摘要不同"]
    a,b = case.get("responses",{}).get(pid,{}),response_identity(item,n)
    proven = False
    for field in ("response_id","sha256"):
        if a.get(field) and b.get(field):
            if a[field] != b[field]:
                return "content_mismatch", [f"原始回答 {field} 不同"]
            proven = True
            reasons.append(f"原始回答 {field} 一致")
    for field, origin in (("answer","answer_text_origin"),("context","context_origin")):
        if field in a and a.get(origin) == "source" and _text(a.get(field)) != b[field]:
            return "content_mismatch", [f"原始回答 {field} 不同"]
    if a.get("answer_text_origin") == "source" and a.get("answer") and not a.get("evidence") and not b["evidence"]:
        proven = True
        reasons.append("原始文本回答一致")
    if item.get("query_images") or left.get("query_hashes"):
        if not left.get("query_hashes") or not right["query_hashes"]:
            proven = False
            reasons.append("提问原图身份未核验")
    if not left.get("query"):
        proven = False
    return ("verified" if proven else "identity_unverified"), reasons or ["原始采集信息不足；路径相同不能证明内容相同"]


def _policy(snapshot: dict, row: dict):
    manifest = snapshot.get("protocol_manifest") or {}
    standard = row.get("standard_version") or manifest.get("standard_version")
    revision = row.get("bundle_revision") or manifest.get("bundle_revision")
    if not standard or not revision:
        return None
    try:
        return resolve_compare_protocol("qa_competitor_compare@"+standard,revision)
    except ValueError:
        return None


def align_baseline_to_run(baseline: dict, snapshot: dict, mapping: TaskMapping, scope: list[str], dimensions: list[str], mode: str) -> list[dict]:
    products = {p["product_id"] for p in baseline["products"]}
    if not mapping.product_map or len(set(mapping.product_map.values())) != len(mapping.product_map):
        raise HumanError("请提供一对一产品映射")
    if any(k not in ("answer1","answer2","answer3") or v not in products for k,v in mapping.product_map.items()):
        raise HumanError("产品槽位或基准产品身份无效")
    cases = {c["case_id"]: c for c in baseline["cases"]}
    labels = {(r["case_key"],r["product_id"],r["dimension_id"]):r for r in baseline["labels"]}
    states = {r["case_key"]:r for r in baseline["states"]}
    indexes = defaultdict(list)
    for i,item in enumerate(snapshot["items"]):
        sid = _source_id(item,mapping.id_field)
        if sid:
            indexes[mapping.case_map.get(sid,sid)].append(i)
    results = {int(r["index"]): r for r in snapshot["results"] if isinstance(r.get("index"),int)}
    runtime_summary = judge_runtime_summary(snapshot)
    slots = {v:int(k[-1]) for k,v in mapping.product_map.items()}
    rows = []
    for cid in scope:
        case = cases.get(cid)
        matched = indexes.get(cid,[])
        i = matched[0] if len(matched)==1 else None
        item = snapshot["items"][i] if i is not None else {}
        result = results.get(i,{})
        result_runtime_summary = (judge_runtime_summary({}, result)
                                  if result.get("judge_runtime") or result.get("judge_model") else runtime_summary)
        policy = _policy(snapshot,result)
        compatible = bool(policy and [policy.score_min,policy.score_max]==baseline["score_range"] and (
            policy.standard_version==baseline["human_standard_version"] or mode=="regression"))
        if policy and [policy.score_min,policy.score_max] != baseline["score_range"]:
            raise HumanError("人工与模型分制不兼容，不能进行线性换分或生成数字一致性报告")
        if policy and policy.standard_version != baseline["human_standard_version"] and mode != "regression":
            raise HumanError("评分口径不同，请显式选择相对人工基准的回归比较")
        ck = case["case_key"] if case else "out:"+cid
        state = states.get(ck,{})
        for product in baseline["products"]:
            pid = product["product_id"]
            slot = slots.get(pid)
            product_count = int(item.get("product_count") or (3 if any(item.get(k) for k in ("video3","screenshot3","answer3")) else 2))
            if slot and slot > product_count:
                slot = None
            if len(matched)>1:
                status, reasons = "ambiguous", ["任务中题号重复或映射后存在重复"]
            elif not case or i is None or slot is None:
                status, reasons = "unmatched", ["题号或产品没有对应项"]
            else:
                status, reasons = _identity(case,item,pid,slot)
            binding, binding_reasons = _binding(item,result,slot) if slot and i is not None else ("missing_result",[])
            for dim in dimensions:
                label = labels.get((ck,pid,dim),{})
                human = label.get("score") if label.get("resolution")=="accepted" and label.get("score_status")=="scored" else None
                model_status, model = "标准或冻结修订号缺失/未知", None
                if policy and slot:
                    model_status,model = score_state(Case(i or 0,item,result,"failed" if result.get("error") else "done" if result else "unfinished",
                                                          int(item.get("product_count") or 2),policy.standard_version,policy.bundle_revision),slot,dim,policy)
                mid = digest([snapshot["task_id"],snapshot["snapshot_sha256"],ck,pid,dim])[:32]
                identity_accepted = status=="verified" and binding=="verified"
                row = dict(match_id=mid, task_id=snapshot["task_id"], task_snapshot_sha256=snapshot["snapshot_sha256"],
                           case_id=cid, case_key=ck, task_item_index=i, product_id=pid, task_product_slot=slot, dimension_id=dim,
                           query=case.get("query","") if case else case_identity(item)["query"], category=case.get("category","") if case else "",
                           input_modality="text_image" if item.get("query_images") else "text", evidence_mode=item.get("evidence_mode", "video_frames"),
                           match_status=status, match_reasons=reasons, result_input_binding=binding, binding_reasons=binding_reasons,
                           human_score=human, human_status=label.get("score_status","unlabeled"), human_source=label.get("source",{}),
                           human_reason=label.get("reason",""), model_score=model, model_status=model_status,
                           model_reason=result.get(f"{dim}_rationale") or result.get(f"{dim}_reason") or "",
                           model_gates={g:result.get(f"answer{slot}_{g}_gate") for g in ("response","safety")},
                           human_gates=state.get("gates",{}).get(pid,{}), human_applicable=state.get("applicability",{}).get(dim),
                           model_applicable=result.get(f"{dim}_applicable"), model_input_status=result.get(f"answer{slot}_input_status"),
                           compatible=compatible, identity_accepted=identity_accepted,
                           prompt_sha256=result.get("prompt_sha256",""), evidence=response_identity(item,slot)["evidence"] if slot else "")
                row.update(model_standard=policy.standard_version if policy else "unknown",
                           bundle_revision=policy.bundle_revision if policy else "unknown",
                           **result_runtime_summary)
                finalize_row(row)
                rows.append(row)
    return rows


def finalize_row(row: dict):
    row["identity_accepted"] = row["match_status"] in ("verified","confirmed") and row["result_input_binding"] in ("verified","confirmed")
    row["comparable"] = row["identity_accepted"] and row["compatible"] and row["human_score"] is not None and row["model_score"] is not None
    row["difference"] = row["model_score"]-row["human_score"] if row["comparable"] else None
    row["exclusion"] = "" if row["comparable"] else (
        row["match_status"] if row["match_status"] not in ("verified","confirmed") else
        row["result_input_binding"] if row["result_input_binding"] not in ("verified","confirmed") else
        "标准不可比" if not row["compatible"] else row["human_status"] if row["human_score"] is None else row["model_status"])


class HumanComparisons:
    def __init__(self, store: HumanStore):
        self.store = store
        self.jobs: dict[str,asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(2)

    def create_preview(self, request: ComparisonRequest, snapshots: list[dict]) -> dict:
        if request.compatibility_mode not in ("same_standard","regression"):
            raise HumanError("无效兼容模式")
        if not request.dimensions or any(d not in SCORE_DIMENSIONS for d in request.dimensions) or len(set(request.dimensions))!=len(request.dimensions):
            raise HumanError("请选择六个统计维度中的至少一项；内容准确性仅留档")
        if len({t.task_id for t in request.tasks}) != len(request.tasks):
            raise HumanError("重复任务")
        baseline = self.store.load_baseline(request.baseline_id,request.version)
        first = request.tasks[0]
        anchor = [_source_id(it,first.id_field) for it in snapshots[0]["items"]]
        if not all(anchor) or len(anchor)!=len(set(anchor)):
            raise HumanError("入口任务缺少真实题号或题号重复；请选择正确字段或先修正输入，不可使用自动行号")
        scope = [first.case_map.get(c,c) for c in anchor]
        if len(set(scope))!=len(scope):
            raise HumanError("入口题号映射必须一对一")
        if request.case_ids is not None:
            if not request.case_ids or not set(request.case_ids)<=set(scope):
                raise HumanError("所选题号必须属于入口任务")
            scope = list(dict.fromkeys(request.case_ids))
        rows = []
        for snapshot,mapping in zip(snapshots,request.tasks):
            rows.extend(align_baseline_to_run(baseline,snapshot,mapping,scope,request.dimensions,request.compatibility_mode))
        config = request.model_dump()
        pid = "hp_"+uuid.uuid4().hex[:20]
        preview = dict(preview_id=pid,config=config,config_sha256=digest(config),created_at=time.time(),expires_at=time.time()+86400,
                       baseline=baseline,snapshots=snapshots,rows=rows,scope=scope,
                       out_of_scope=[c["case_id"] for c in baseline["cases"] if c["case_id"] not in scope])
        atomic_json(self.store.path("previews",pid),preview)
        return self.preview_summary(preview)

    @staticmethod
    def preview_summary(preview):
        rows = preview["rows"]
        confirmations = defaultdict(lambda: {"match_ids":[],"binding_ids":[]})
        for row in rows:
            if row["human_score"] is None:
                continue
            key=(row["task_id"],row["case_id"],row["product_id"])
            if row["match_status"]=="identity_unverified" and row["result_input_binding"]!="stale_result":
                confirmations[key]["match_ids"].append(row["match_id"])
            if row["result_input_binding"]=="binding_unverified" and row["match_status"] in ("verified","identity_unverified"):
                confirmations[key]["binding_ids"].append(row["match_id"])
        return dict(preview_id=preview["preview_id"],config_sha256=preview["config_sha256"],scope_count=len(preview["scope"]),
                    human_n=sum(r["human_score"] is not None for r in rows), comparable_n=sum(r["comparable"] for r in rows),
                    matches=dict(Counter(r["match_status"] for r in rows)),bindings=dict(Counter(r["result_input_binding"] for r in rows)),
                    case_matching={s["task_id"]:len({r["case_id"] for r in rows if r["task_id"]==s["task_id"] and
                        r["task_item_index"] is not None and not r["case_key"].startswith("out:")}) for s in preview["snapshots"]},
                    out_of_scope=preview["out_of_scope"], rows=rows[:50], row_count=len(rows),
                    confirmations=[dict(task_id=k[0],case_id=k[1],product_id=k[2],**v) for k,v in confirmations.items()],
                    warnings=["实际 Prompt 信息不全" ] if any(not r["prompt_sha256"] for r in rows) else [],
                    frozen_at=[dict(task_id=s["task_id"],at=s["frozen_at"],sha256=s["snapshot_sha256"]) for s in preview["snapshots"]])

    def prepare_report(self, request: GenerateRequest):
        with self.store.lock:
            preview = read_json(self.store.path("previews",request.preview_id))
            if request.config_sha256!=preview["config_sha256"] or preview["expires_at"]<time.time():
                raise HumanError("预览过期或配置不一致，请重新预览",409)
            if any(r.get("model_standard")=="unknown" for r in preview["rows"]):
                raise HumanError("存在未知评分标准或冻结修订号；请先核实协议，当前仅能查看匹配预览")
            match_ids,binding_ids = set(request.confirm_match_ids),set(request.confirm_binding_ids)
            by_id = {r["match_id"]:r for r in preview["rows"]}
            if match_ids or binding_ids:
                if not request.confirm_reason.strip():
                    raise HumanError("确认历史身份信息时必须填写依据")
            if any(i not in by_id or by_id[i]["match_status"]!="identity_unverified" or by_id[i]["result_input_binding"]=="stale_result" for i in match_ids):
                raise HumanError("只能确认身份信息不足的记录，明确冲突不能忽略")
            if any(i not in by_id or by_id[i]["result_input_binding"]!="binding_unverified" or by_id[i]["match_status"] not in ("verified","identity_unverified") for i in binding_ids):
                raise HumanError("不能确认已过期或冲突的结果绑定")
            rid="hr_"+digest([request.preview_id,sorted(match_ids),sorted(binding_ids),request.confirm_reason])[:24]
            path=self.store.path("reports",rid)
            if path.exists():
                return read_json(path)
            for row in preview["rows"]:
                if row["match_id"] in match_ids:
                    row["match_status"]="confirmed"
                if row["match_id"] in binding_ids:
                    row["result_input_binding"]="confirmed"
                finalize_row(row)
            atomic_json(self.store.path("reports",rid,"input.json"),preview)
            confirmation = dict(match_ids=sorted(match_ids),binding_ids=sorted(binding_ids),reason=request.confirm_reason,
                                confirmed_at=time.time(),confirmation_scope_hash=digest([preview["config"],preview["scope"],
                                    [s["snapshot_sha256"] for s in preview["snapshots"]]]))
            manifest = dict(report_id=rid,schema_version="human-report-1.0",status="queued",created_at=time.time(),
                            task_ids=[t["task_id"] for t in preview["config"]["tasks"]],baseline_id=preview["baseline"]["baseline_id"],
                            version=preview["baseline"]["version"],baseline_name=preview["baseline"]["name"],
                            compatibility_mode=preview["config"]["compatibility_mode"],confirmation=confirmation)
            atomic_json(path,manifest)
            return manifest

    def schedule(self, report_id):
        if report_id in self.jobs:
            return
        self.jobs[report_id]=asyncio.create_task(self._generate(report_id))
        self.jobs[report_id].add_done_callback(lambda _:self.jobs.pop(report_id,None))

    async def _generate(self, report_id):
        async with self.semaphore:
            await asyncio.to_thread(self.generate,report_id)

    def generate(self, report_id):
        from .human_report_export import export_report
        path=self.store.path("reports",report_id)
        manifest=read_json(path)
        if manifest["status"]=="ready":
            return
        try:
            manifest.update(status="generating",error=None)
            atomic_json(path,manifest)
            preview=read_json(self.store.path("reports",report_id,"input.json"))
            baseline=preview["baseline"]
            metrics=build_human_statistics(preview["rows"],manifest["task_ids"],
                                          [p["product_id"] for p in baseline["products"]],preview["config"]["dimensions"])
            report=dict(**manifest,metrics=metrics,rows=preview["rows"],baseline=baseline,
                        config=preview["config"],frozen_at=self.preview_summary(preview)["frozen_at"],
                        model_protocols=[dict(task_id=s["task_id"],protocol_manifest=s.get("protocol_manifest",{}),
                                              evaluation_profile=s.get("evaluation_profile","")) for s in preview["snapshots"]],
                        model_runtimes=[dict(task_id=s["task_id"], **judge_runtime_summary(s))
                                        for s in preview["snapshots"]],
                        accuracy_label_count=sum(r["dimension_id"]=="accuracy" and r["score_status"]=="scored" for r in baseline["labels"]),
                        warnings=self.preview_summary(preview)["warnings"],out_of_scope=preview["out_of_scope"])
            atomic_json(self.store.path("reports",report_id,"report.json"),report)
            output=self.store.path("reports",report_id,"report.xlsx")
            temporary=output.with_suffix(".tmp")
            temporary.write_bytes(export_report(report))
            temporary.replace(output)
            manifest.update(status="ready",metric_version=metrics["metric_version"],finished_at=time.time())
        except Exception as exc:
            manifest.update(status="error",error=str(exc))
        atomic_json(path,manifest)

    def recover(self):
        for path in (self.store.root/"human_reports").glob("*/manifest.json"):
            manifest=read_json(path)
            if manifest["status"] in ("queued","generating"):
                manifest.update(status="error",error="服务重启中断；可基于原冻结输入重新生成")
                atomic_json(path,manifest)

    async def close(self):
        # Do not cancel to_thread: finishing the bounded writes avoids orphan writers.
        await asyncio.gather(*list(self.jobs.values()),return_exceptions=True)
