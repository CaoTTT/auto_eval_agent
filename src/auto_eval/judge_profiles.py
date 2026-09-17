"""Resolve approved model choices and freeze task inference settings."""
from __future__ import annotations

from urllib.parse import urlparse

from .config import AppConfig, JudgeConfig, JudgeModelProfile, RateLimitConfig

BAILIAN_MODELS = {
    "qwen3.5-397b-a17b": ("bailian_qwen35_397b", "Qwen3.5-397B-A17B"),
    "qwen3.8-flash": ("bailian_qwen38_flash", "Qwen3.8-Flash"),
}
FROZEN_OPTIONS = {"judges", "judge_model_profile", "enable_thinking"}


def is_bailian(judge: JudgeConfig) -> bool:
    host = (urlparse(judge.base_url or "").hostname or "").lower()
    return host == "dashscope.aliyuncs.com" or (
        host.endswith(".aliyuncs.com") and (host.startswith("dashscope-") or ".maas." in host)
    )


def model_profiles(config: AppConfig) -> list[JudgeModelProfile]:
    profiles = list(config.judge_model_profiles)
    if not profiles:
        for judge in config.judges:
            model = judge.model or judge.name
            if is_bailian(judge) and model.lower() in BAILIAN_MODELS:
                for model_id, (profile_id, display) in BAILIAN_MODELS.items():
                    # Explicit server defaults take precedence over provider defaults.
                    thinking = judge.enable_thinking if judge.enable_thinking is not None else True
                    rate = judge.rate_limit if model_id == model.lower() else None
                    if model_id == "qwen3.8-flash" and rate is None:
                        rate = RateLimitConfig(rpm=60, tpm=100_000, rps=1, max_inflight=4)
                    profiles.append(JudgeModelProfile(
                        id=profile_id if len(config.judges) == 1 else f"{judge.name}_{profile_id}",
                        display=display, judge_name=judge.name, model=model_id,
                        supports_thinking=True, default_enable_thinking=thinking, rate_limit=rate,
                    ))
            else:
                profiles.append(JudgeModelProfile(
                    id=f"configured_{judge.name}", display=model, judge_name=judge.name, model=model,
                    supports_thinking=judge.enable_thinking is not None,
                    default_enable_thinking=judge.enable_thinking, rate_limit=judge.rate_limit,
                ))
    names = {j.name for j in config.judges}
    if len({p.id for p in profiles}) != len(profiles):
        raise ValueError("裁判模型配置 ID 不可重复")
    for profile in profiles:
        if profile.judge_name not in names:
            raise ValueError(f"模型配置引用不存在的裁判：{profile.judge_name}")
        if not profile.supports_thinking and profile.default_enable_thinking is not None:
            raise ValueError(f"模型配置 {profile.id} 不支持思考开关")
    if config.default_judge_model_profile and config.default_judge_model_profile not in {p.id for p in profiles}:
        raise ValueError("默认裁判模型配置不存在")
    return profiles


def default_profile_id(config: AppConfig, profiles=None) -> str | None:
    profiles = model_profiles(config) if profiles is None else profiles
    if config.default_judge_model_profile:
        return config.default_judge_model_profile
    if config.judges:
        first = config.judges[0]
        match = next((p for p in profiles if p.judge_name == first.name and p.model.lower() == (first.model or first.name).lower()), None)
        if match:
            return match.id
    return profiles[0].id if profiles else None


def configured_profile(config: AppConfig, profile: JudgeModelProfile, thinking=None) -> JudgeConfig:
    base = next(j for j in config.judges if j.name == profile.judge_name)
    data = base.model_dump()
    data.update(model=profile.model, enable_thinking=thinking, rate_limit=profile.rate_limit)
    if profile.total_timeout_s is not None:
        data["total_timeout_s"] = profile.total_timeout_s
    return JudgeConfig.model_validate(data)


def resolve_new_runtime(config: AppConfig, options: dict) -> tuple[dict, list[JudgeConfig]]:
    profiles = model_profiles(config)
    selected = options.get("judges")
    if selected is not None and (not isinstance(selected, list) or len(selected) > 1 or any(not isinstance(n, str) for n in selected)):
        raise ValueError("每个任务请选择一个裁判")
    if selected and selected[0] not in {j.name for j in config.judges}:
        raise ValueError("裁判不存在")
    profile_id = options.get("judge_model_profile")
    if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
        raise ValueError("裁判模型配置 ID 必须为非空字符串")
    if profile_id is None and selected:
        base = next(j for j in config.judges if j.name == selected[0])
        candidate = next((p for p in profiles if p.judge_name == base.name and p.id == config.default_judge_model_profile), None)
        candidate = candidate or next((p for p in profiles if p.judge_name == base.name and p.model.lower() == (base.model or base.name).lower()), None)
        profile_id = candidate.id if candidate else None
    profile_id = profile_id or default_profile_id(config, profiles)
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile is None:
        raise ValueError("裁判模型配置不存在")
    if selected and profile.judge_name != selected[0]:
        raise ValueError("裁判与模型配置不匹配")
    thinking = options.get("enable_thinking", profile.default_enable_thinking)
    if "enable_thinking" in options and type(thinking) is not bool:
        raise ValueError("enable_thinking 必须为 true 或 false")
    if not profile.supports_thinking and thinking is not None:
        raise ValueError("所选模型不支持思考开关")
    if profile.supports_thinking and thinking is None:
        raise ValueError("支持思考的模型须配置明确的默认开关")
    judge = configured_profile(config, profile, thinking)
    # Reference the credential slot without writing the secret value.
    snapshot = {"version": 1, "profile_id": profile.id, "judges": [judge.model_dump()]}
    return snapshot, [judge]


def restore_runtime(config: AppConfig, runtime: dict) -> list[JudgeConfig]:
    if runtime.get("version") != 1 or len(runtime.get("judges") or []) != 1:
        raise ValueError("任务裁判配置快照不可用，请新建任务")
    # These records are server-authored snapshots, never request input.
    return [JudgeConfig.model_validate(j) for j in runtime["judges"]]


def check_frozen_options(task, options: dict) -> None:
    """Reject inference overrides before any task/items mutation."""
    runtime = getattr(task, "judge_runtime", {}) or {}
    if not runtime:
        # Never reinterpret a historical task using a newly selected model/toggle.
        if "judge_model_profile" in options or "enable_thinking" in options:
            raise ValueError("旧任务未记录完整裁判配置；切换模型或思考模式请新建任务")
        if "judges" in options and options["judges"] != task.options.get("judges", []):
            raise ValueError("已有任务的裁判不可变；请新建任务")
        return
    judges = runtime["judges"]
    expected = {"judges": [j["name"] for j in judges], "judge_model_profile": runtime["profile_id"],
                "enable_thinking": judges[0].get("enable_thinking")}
    for key in FROZEN_OPTIONS & options.keys():
        if key == "enable_thinking" and type(options[key]) is not bool:
            raise ValueError("enable_thinking 必须为 true 或 false")
        if options[key] != expected[key]:
            raise ValueError("已有任务的模型和思考模式不可变；请复用数据集创建新任务")


def legacy_runtime(config: AppConfig, task) -> dict:
    """Recover recorded inference facts without guessing the historical model."""
    frozen = (task.protocol_manifest or {}).get("judges") or []
    if len(frozen) > 1:
        raise ValueError("旧任务裁判记录不唯一，请复用数据集创建新任务")
    configured = getattr(config, "judges", [])
    names = task.options.get("judges") or ([configured[0].name] if configured else [])
    record = dict(frozen[0]) if frozen else {}
    model = record.get("model")
    if "model" in record and model is None:
        # Old JudgeClient explicitly used cfg.name when cfg.model was absent.
        model = record.get("name")
    known_models = {row.get("judge_model") for row in task.results if row.get("judge_model")}
    if not model and len(known_models) == 1:
        model = next(iter(known_models))
    if not model or any(value != model for value in known_models):
        raise ValueError("旧任务未记录可确认的裁判模型，请复用数据集创建新任务")
    name = record.get("name") or (names[0] if len(names) == 1 else None)
    base = next((j for j in configured if j.name == name), None)
    if base is None:
        raise ValueError("旧任务裁判连接配置不可用，请新建任务")
    data = base.model_dump()
    data.update(record, model=model, enable_thinking=record.get("enable_thinking"))
    if model != base.model:
        data["rate_limit"] = None
    judge = JudgeConfig.model_validate(data)
    return {"version": 1, "profile_id": f"legacy_{name}", "migrated_legacy": True,
            "judges": [judge.model_dump()]}
