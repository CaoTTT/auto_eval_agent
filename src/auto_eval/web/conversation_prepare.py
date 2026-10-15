"""One preparation path for full runs, resume, retry and append."""
from __future__ import annotations

import hashlib
from copy import deepcopy

from ..conversation import ConversationIndex, VERSION, digest
from ..image_limits import effective_profile, inspect_asset
from ..long_screenshot import prepare_long_screenshot
from ..paths import RUNS_DIR, resolve_project_path
from ..query_images import QueryImageError, authorized_path, prepare_query_images, _snapshot_bytes
from .video_prepare import operation_video_roots, _cache_lock


class ConversationPreparation:
    def __init__(self, items, session_name, profile, limits):
        self.index = ConversationIndex(items)
        self.items = {it["id"]: it for it in items if it.get("session_id")}
        self.root = RUNS_DIR / "conversations" / digest(session_name)[:24]
        self.session_name, self.profile, self.limits = session_name, profile, limits
        self.cache = {}
        self.asset_cache = {}

    def prepare(self, target: dict) -> dict:
        prefix = self.index.prefix(target["session_id"], target["turn_index"])
        findings, turns, assets = [], [], []
        target.update(session_group="compare:" + prefix.session_id, history_turn_count=prefix.target_turn - 1,
                      conversation_adapter_version=VERSION, input_schema_version="2.0",
                      limits_profile_version=self.limits["limits_profile_version"],
                      effective_input_modality="text_image" if any(it.get("query_images") for it in prefix.turns) else "text")
        try:
            effective = effective_profile(self.profile, self.limits)
            for turn in prefix.turns:
                key = turn["id"]
                if key not in self.cache:
                    self.cache[key] = self._prepare_turn(turn, effective)
                prepared, turn_assets, turn_findings = self.cache[key]
                findings.extend(deepcopy(turn_findings))
                assets.extend(deepcopy(turn_assets))
                turns.append(prepared)
                if prepared.get("preparation_error"):
                    raise QueryImageError("conversation_evidence_blocked", prepared["preparation_error"])
                # Check immutable snapshots on every request, including cached prefixes.
                for asset in turn_assets:
                    path = resolve_project_path(asset["original_path"])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != asset["original_sha256"]:
                        raise QueryImageError("conversation_evidence_changed", "冻结原图已变化，不能混用不同版本")
            current = turns[-1]
            for key in ("query_images", "query_image_meta", "query_image_views", "frame_count", "media",
                        *(f"{p}{n}" for p in ("screenshot", "screenshot_meta", "frames") for n in (1, 2, 3))):
                if key in current:
                    target[key] = current[key]
            # Identity is content-based, independent of snapshot paths and future rows.
            identity = []
            for turn in turns:
                identity.append({k: turn.get(k) for k in ("id", "session_id", "turn_index", "query", "context", "product_count",
                    *(f"{p}{n}" for p in ("answer", "context", "screenshot_scope") for n in (1, 2, 3)))})
            target["history_prefix_sha256"] = digest([identity, [{k: a[k] for k in
                ("asset_id", "original_sha256", "source_turn", "image_role", "product_no")} for a in assets]])
            target["input_diagnostic_status"] = "warning" if findings else "passed"
        except Exception as exc:
            target["input_diagnostic_status"] = "blocked"
            if not findings or not any(f["status"] == "blocked" for f in findings):
                findings.append({"finding_id": digest([prefix.session_id, prefix.target_turn, str(exc)]),
                    "code": getattr(exc, "code", "conversation_evidence_missing"), "status": "blocked",
                    "severity": "error", "session_id": prefix.session_id, "source_turn": prefix.target_turn,
                    "message": str(exc), "limit_kind": "unverified" if not self.limits["capability_verified"] else "input_integrity"})
            raise
        finally:
            target["image_findings"] = findings
            target["image_warning_refs"] = list(dict.fromkeys(f["finding_id"] for f in findings))
            target["image_warning_count"] = len(target["image_warning_refs"])
            target["image_blocking_count"] = sum(f["status"] == "blocked" for f in findings)
            target["conversation_asset_manifest"] = [{k: a[k] for k in ("asset_id", "source_turn", "product_no", "image_role", "original_sha256")} for a in assets]
        return {"turns": turns, "target_turn": prefix.target_turn, "session_id": prefix.session_id,
                "limits": self.limits, "profile": effective, "diagnostics": target}

    def _prepare_turn(self, source: dict, effective):
        turn = deepcopy(source)
        assets, findings = [], []
        identity = {"session_id": turn["session_id"], "source_turn": turn["turn_index"]}
        active_identity = identity
        number = turn["turn_index"]
        try:
            # Inspect every original before preparation; retain findings even on failure.
            for n, raw in enumerate(turn.get("query_images") or [], 1):
                active_identity = {**identity, "asset_id": f"T{number:02d}-QI{n:02d}", "image_role": "query_image", "product_no": None}
                path = authorized_path(raw, effective.query_images)
                asset, issues = inspect_asset(path, {**identity, "asset_id": f"T{number:02d}-QI{n:02d}",
                    "image_role": "query_image", "product_no": None}, self.limits)
                assets.append(asset)
                findings.extend(issues)
            count = turn.get("product_count", 2)
            for n in range(1, count + 1):
                active_identity = {**identity, "asset_id": f"P{n}-T{number:02d}-A01", "image_role": "product_answer", "product_no": n}
                path = resolve_project_path(turn[f"screenshot{n}"]).resolve()
                if not any(path.is_relative_to(root) for root in operation_video_roots()):
                    raise ValueError("截图路径不在允许目录中")
                asset, issues = inspect_asset(path, {**identity, "asset_id": f"P{n}-T{number:02d}-A01",
                    "image_role": "product_answer", "product_no": n}, self.limits)
                prior = turn.get(f"screenshot_meta{n}") or {}
                if prior.get("original_sha256") and prior["original_sha256"] != asset["original_sha256"]:
                    raise ValueError("冻结回答截图哈希不一致")
                for part in prior.get("slices", []):
                    if hashlib.sha256(resolve_project_path(part["path"]).read_bytes()).hexdigest() != part["sha256"]:
                        raise ValueError("冻结长截图切片已变化，不能静默重建")
                assets.append(asset)
                findings.extend(issues)
            if any(f["image_role"] == "query_image" for f in findings):
                raise QueryImageError("query_image_limit_exceeded", "题图超过限制，不允许缩放或自动切图")
            prepared_query = prepare_query_images(turn, session_name=self.session_name, cfg=effective.query_images)
            turn.update(prepared_query)
            for n, meta in enumerate(turn["query_image_meta"], 1):
                meta.update(asset_id=f"T{number:02d}-QI{n:02d}", query_image_id=f"T{number:02d}-QI{n:02d}", **identity)
                assets[n - 1]["original_path"] = meta["original_path"]
            turn.update(evidence_mode="long_screenshot", frame_count=0, media=[])
            for asset in [a for a in assets if a["image_role"] == "product_answer"]:
                active_identity = {key: asset[key] for key in ("session_id", "source_turn", "asset_id", "image_role", "product_no")}
                n = asset["product_no"]
                path = resolve_project_path(asset["original_path"])
                directory = self.root / asset["original_sha256"] / self.limits["limits_profile_sha256"][:16]
                directory.mkdir(parents=True, exist_ok=True)
                original = directory / ("original" + path.suffix.lower())
                with _cache_lock(directory):
                    _snapshot_bytes(original, path.read_bytes())
                    if directory not in self.asset_cache:
                        self.asset_cache[directory] = prepare_long_screenshot(original, directory, effective.long_screenshot)
                    meta = deepcopy(self.asset_cache[directory])
                # Recheck every actual slice against both hard and effective limits.
                for part in meta["slices"]:
                    _, issues = inspect_asset(resolve_project_path(part["path"]), {**identity,
                        "asset_id": asset["asset_id"], "image_role": "product_answer", "product_no": n}, self.limits)
                    if issues:
                        raise QueryImageError("image_slice_limit_exceeded", "切片仍超限，无法无损准备完整证据")
                asset.update(original_path=str(original), slices=meta["slices"], preprocess_version=meta["algorithm_version"])
                meta.update(asset_id=asset["asset_id"], **identity)
                turn[f"screenshot{n}"] = str(original)
                turn[f"screenshot_meta{n}"] = meta
                turn[f"frames{n}"] = [p["path"] for p in meta["slices"]]
                turn["media"].append(str(original))
                turn["frame_count"] += meta["split_count"]
                for finding in findings:
                    if finding["asset_id"] == asset["asset_id"]:
                        finding.update(status="resolved_by_lossless_split", action="lossless_split", slice_count=meta["split_count"])
            turn["conversation_assets"] = assets
            self.items[turn["id"]].update({k: v for k, v in turn.items() if k != "source_data"})
        except Exception as exc:
            turn["preparation_error"] = str(exc)
            if not any(f["status"] == "blocked" for f in findings):
                findings.append({**active_identity, "finding_id": digest([active_identity, str(exc), self.limits["limits_profile_sha256"]]),
                    "code": getattr(exc, "code", "conversation_evidence_missing"), "status": "blocked", "severity": "error",
                    "message": str(exc), "limit_kind": "input_integrity"})
        return turn, assets, findings
