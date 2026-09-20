"""Versioned compare conversations; history contains inputs, never judge results."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

VERSION = "compare-conversation-1"
INPUT_VERSION = "2.0"
FIELDS = {"session_id", "turn_index", "screenshot_scope", "conversation_mode",
          *(f"screenshot_scope{n}" for n in (1, 2, 3))}
DIAGNOSTIC_FIELDS = {"session_id", "session_group", "turn_index", "history_turn_count",
    "effective_input_modality", "conversation_adapter_version", "input_schema_version",
    "history_prefix_sha256", "limits_profile_version", "image_findings", "image_warning_refs",
    "image_warning_count", "image_blocking_count", "input_diagnostic_status",
    "request_budget_report", "conversation_asset_manifest"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def is_conversation(item: dict) -> bool:
    return "session_id" in item or "turn_index" in item


def normalize_turn(item: dict, *, external: bool = False) -> dict:
    if not is_conversation(item):
        return {}
    sid, turn = item.get("session_id"), item.get("turn_index")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("conversation_structure_invalid: session_id 必须是非空字符串")
    if type(turn) is not int or turn < 1:
        raise ValueError("conversation_structure_invalid: turn_index 必须是从 1 开始的整数")
    if not isinstance(item.get("id"), str) or not item["id"].strip():
        raise ValueError("conversation_structure_invalid: 多轮必须提供稳定 id")
    if item.get("conversation_mode", "shared_script") != "shared_script":
        raise ValueError("仅支持各产品共享问题和题图的 shared_script")
    if any(item.get(f"{key}{n}") for key in ("query", "question", "query_images") for n in (1, 2, 3)):
        raise ValueError("多轮各产品必须共享问题和题图")
    media = item.get("media") or []
    def video(value):
        if isinstance(value, dict):
            return str(value.get("type", "")).startswith("video") or any(video(v) for v in value.values())
        return isinstance(value, str) and value.lower().split("?", 1)[0].endswith((".mp4", ".mov", ".avi", ".webm", ".mkv"))
    if item.get("evidence_mode") == "video_frames" or any(
        item.get(key) for key in ("video_path", *(f"video{n}" for n in (1, 2, 3)), *(f"video{n}_path" for n in (1, 2, 3)))
    ) or any(video(v) for v in media):
        raise ValueError("multiturn_video_unsupported: 多轮仅支持当前轮长截图，不能混用或回退录屏")
    if external and any(item.get(f"frames{n}") or item.get(f"screenshot_meta{n}") for n in (1, 2, 3)):
        raise ValueError("multiturn_untrusted_evidence: 请提交原始 screenshot 路径，不能提交派生图片")
    count = item.get("product_count") or (3 if item.get("screenshot3") else 2)
    if type(count) is not int or count not in (2, 3):
        raise ValueError("product_count 必须是 2 或 3")
    scopes = {}
    for n in range(1, count + 1):
        scope = item.get(f"screenshot_scope{n}", item.get("screenshot_scope"))
        if scope != "current_turn":
            raise ValueError("screenshot_scope_unresolved: 必须声明 screenshot_scope=current_turn；累计截图暂不支持")
        if not isinstance(item.get(f"screenshot{n}"), str) or not item[f"screenshot{n}"].strip():
            raise ValueError(f"multiturn_evidence_missing: 产品{n}缺少本轮长截图")
        scopes[f"screenshot_scope{n}"] = scope
    return {"session_id": sid.strip(), "turn_index": turn, "session_group": "compare:" + sid.strip(),
            "input_schema_version": INPUT_VERSION, "conversation_mode": "shared_script",
            "screenshot_scope": "current_turn", "evidence_mode": "long_screenshot", **scopes}


def validate_conversations(items: list[dict], *, external: bool = False) -> None:
    groups = {}
    ids = {}
    for item in items:
        if item.get("id"):
            ids.setdefault(item["id"], []).append(item)
        if is_conversation(item):
            item.update(normalize_turn(item, external=external))
            groups.setdefault(item["session_id"], []).append(item)
    for key, matches in ids.items():
        if len(matches) > 1 and any(is_conversation(it) for it in matches):
            raise ValueError(f"conversation_structure_invalid: id 重复 {key}")
    for sid, turns in groups.items():
        indices = sorted(it["turn_index"] for it in turns)
        if indices != list(range(1, len(turns) + 1)):
            raise ValueError(f"conversation_structure_invalid: 会话 {sid} 轮次必须从 1 连续且唯一")
        counts = {it.get("product_count") or (3 if it.get("screenshot3") else 2) for it in turns}
        mappings = {digest(it.get("product_mapping")) for it in turns}
        if len(counts) != 1 or len(mappings) != 1:
            raise ValueError(f"conversation_structure_invalid: 会话 {sid} 产品数量或映射不一致")


@dataclass(frozen=True)
class TurnEvaluationInput:
    session_id: str
    target_turn: int
    turns: tuple[dict, ...]
    history_prefix_sha256: str


class ConversationIndex:
    def __init__(self, items: list[dict]):
        copies = deepcopy(items)
        validate_conversations(copies)
        self.groups = {}
        for item in copies:
            if is_conversation(item):
                self.groups.setdefault(item["session_id"], []).append(item)
        for turns in self.groups.values():
            turns.sort(key=lambda it: it["turn_index"])

    def prefix(self, session_id: str, target_turn: int) -> TurnEvaluationInput:
        turns = tuple(deepcopy(it) for it in self.groups[session_id] if it["turn_index"] <= target_turn)
        if len(turns) != target_turn:
            raise ValueError("conversation_history_missing: 无法重建完整原始前缀")
        keys = {"id", "query", "context", "query_images", "product_count", *FIELDS,
                *(f"{p}{n}" for p in ("answer", "context", "screenshot") for n in (1, 2, 3))}
        identity = [{k: it[k] for k in sorted(keys) if k in it} for it in turns]
        return TurnEvaluationInput(session_id, target_turn, turns, digest(identity))
