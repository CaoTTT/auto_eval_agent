"""Versioned human references. Never writes to evaluation inputs or calls a judge."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..judges.compare_protocols import DIMENSIONS, resolve_compare_protocol


class HumanError(ValueError):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.status = status


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    if not path.is_file():
        raise HumanError("记录不存在", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise HumanError("无效记录编号")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Product(StrictModel):
    product_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=100)


class LabelColumn(StrictModel):
    product_id: str
    dimension_id: str
    score: str = ""
    review: str = ""
    status: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def validate_dimension(self):
        if self.dimension_id not in DIMENSIONS or not (self.score or self.review or self.status):
            raise ValueError("标签必须指定有效维度以及分数或状态列")
        return self


class ImportMapping(StrictModel):
    sheet_name: str
    header_row: int = Field(default=1, ge=1, le=30)
    case_id: str
    query: str = ""
    context: str = ""
    query_hashes: str = ""
    category: str = ""
    session_group: str = ""
    turn_index: str = ""
    products: list[Product] = Field(min_length=2, max_length=3)
    labels: list[LabelColumn] = Field(min_length=1, max_length=21)
    # Product -> field -> physical column (answer, context, response_id, sha256, evidence).
    responses: dict[str, dict[str, str]] = Field(default_factory=dict)
    applicability: dict[str, str] = Field(default_factory=dict)
    gates: dict[str, dict[str, str]] = Field(default_factory=dict)
    answer_text_origin: Literal["source", "unknown", "judge_transcription"] = "unknown"
    context_origin: Literal["source", "unknown", "judge_transcription"] = "unknown"
    na_status: Literal["na_unspecified", "not_applicable", "unverifiable", "gate_blocked"] = "na_unspecified"
    human_standard_version: str
    policy_note: str = Field(min_length=1, max_length=2000)
    purpose: Literal["development", "regression", "holdout"] = "regression"
    header_signature: str = ""

    @model_validator(mode="after")
    def validate_mapping(self):
        ids = [p.product_id for p in self.products]
        if len(set(ids)) != len(ids):
            raise ValueError("产品身份不能重复")
        keys = [(c.product_id, c.dimension_id) for c in self.labels]
        if len(set(keys)) != len(keys) or any(p not in ids for p, _ in keys):
            raise ValueError("标签产品无效或产品维度映射重复")
        if any(p not in ids for p in (*self.responses, *self.gates)):
            raise ValueError("回答或 Gate 的产品无效")
        if any(d not in DIMENSIONS for d in self.applicability):
            raise ValueError("适用性维度无效")
        if any(k not in ("response", "safety") for g in self.gates.values() for k in g):
            raise ValueError("Gate 类型无效")
        if any(k not in ("answer", "context", "response_id", "sha256", "evidence")
               for fields in self.responses.values() for k in fields):
            raise ValueError("回答身份字段无效")
        resolve_compare_protocol("qa_competitor_compare@" + self.human_standard_version)
        return self


class HumanScoreLabel(StrictModel):
    case_key: str
    product_id: str
    response_id: str
    dimension_id: str
    score_status: Literal["scored", "not_applicable", "unverifiable", "gate_blocked", "unlabeled", "na_unspecified"]
    score: int | None = Field(default=None, strict=True)
    annotation_stage: Literal["base", "review"]
    resolution: Literal["accepted", "pending_conflict"] = "accepted"
    reason: str = ""
    source: dict

    @model_validator(mode="after")
    def validate_score(self):
        if (self.score_status == "scored") != (self.score is not None):
            raise ValueError("仅 scored 标签可包含数字分")
        return self


class HumanStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()

    def path(self, kind: str, record_id: str, filename: str = "manifest.json") -> Path:
        return self.root / ("human_" + kind) / safe_id(record_id) / filename

    def load_baseline(self, baseline_id: str, version: int) -> dict:
        if version < 1:
            raise HumanError("无效基准版本")
        folder = self.path("baselines", baseline_id, f"v{version}")
        manifest = read_json(folder / "manifest.json")
        return {**manifest, **{key: read_json(folder / f"{key}.json") for key in ("cases", "labels", "states")}}

    def list_baselines(self, search: str = "") -> list[dict]:
        rows = [read_json(p) for p in (self.root / "human_baselines").glob("*/v*/manifest.json")]
        return sorted((r for r in rows if search.lower() in (r["name"] + r["baseline_id"]).lower()),
                      key=lambda r: r["created_at"], reverse=True)

    def publish_baseline(self, import_id: str, preview_sha256: str, name: str,
                         baseline_id: str = "", expected_version: int = 0,
                         valid_only: bool = False, save_mapping: bool = True) -> dict:
        with self.lock:
            preview = read_json(self.path("imports", import_id, "preview.json"))
            if preview["preview_sha256"] != preview_sha256:
                raise HumanError("导入预览已改变，请重新检查", 409)
            if preview["issues"] and not valid_only:
                raise HumanError("存在解析冲突；修正后导入，或明确选择仅保存有效记录")
            if not name.strip():
                raise HumanError("请填写基准名称")
            baseline_id = baseline_id or "hb_" + uuid.uuid4().hex[:16]
            versions = [r["version"] for r in self.list_baselines() if r["baseline_id"] == baseline_id]
            latest = max(versions, default=0)
            if expected_version != latest:
                raise HumanError("基准版本已改变，请重新选择版本", 409)
            mapping = preview["mapping"]
            if latest:
                old = self.load_baseline(baseline_id, latest)
                if old["products"] != mapping["products"]:
                    raise HumanError("升级基准必须保留产品身份；产品变化请创建新基准")
            version = latest + 1
            cases = preview["cases"]
            labels = [r for r in preview["labels"] if r["resolution"] == "accepted"]
            if not any(r["score_status"] == "scored" for r in labels):
                raise HumanError("至少需要一条有效人工数字评分")
            folder = self.path("baselines", baseline_id, f"v{version}")
            protocol = resolve_compare_protocol("qa_competitor_compare@" + mapping["human_standard_version"])
            manifest = dict(schema_version="human-baseline-1.0", baseline_id=baseline_id, version=version,
                            name=name.strip(), created_at=time.time(), products=mapping["products"],
                            human_standard_id=protocol.standard_id, human_standard_version=protocol.standard_version,
                            human_policy_note=mapping["policy_note"], score_range=[protocol.score_min, protocol.score_max],
                            purpose=mapping["purpose"], mapping=mapping, mapping_profile_id="map_" + digest(mapping)[:20],
                            case_catalog_sha256=digest(cases), labels_sha256=digest(labels),
                            source_file_sha256=preview["source_file_sha256"], sheet_name=mapping["sheet_name"],
                            case_count=len(cases), label_count=len(labels), excluded_records=preview["issues"])
            for key, value in (("cases", cases), ("labels", labels), ("states", preview["states"])):
                atomic_json(folder / f"{key}.json", value)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "source.xlsx").write_bytes(self.path("imports", import_id, "source.xlsx").read_bytes())
            if save_mapping:
                atomic_json(self.root / "human_mappings" / (manifest["mapping_profile_id"] + ".json"), mapping)
            # Publish last: readers never see an incomplete version.
            atomic_json(folder / "manifest.json", manifest)
            return manifest
