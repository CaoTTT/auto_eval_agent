"""Reason-before-score revision, retaining the complete frozen 1.0.0 rubric."""
import re

from jinja2 import Template

from .visual_compare_prompt_dialogue_experience_r100 import (
    SYSTEM_TEMPLATE_SOURCE as LEGACY_SYSTEM_TEMPLATE_SOURCE,
    USER_TEMPLATE_SOURCE as LEGACY_USER_TEMPLATE_SOURCE,
)


# Only the model's output instructions and example order change. The scoring
# rubric, evidence rules, four fields and parser remain unchanged.
_rubric, _output = LEGACY_SYSTEM_TEMPLATE_SOURCE.split("四、输出要求\n", 1)
_output = _output.replace("score / reason / suggestion / domain", "reason / score / suggestion / domain")
_output = _output.replace(
    "score为1—5整数。reason使用以下固定前缀：",
    "每个产品必须按生成顺序先写reason，再写score，最后写suggestion和domain。"
    "reason先根据实际证据概述表现及影响、说明分档依据，再据此确定score；"
    "不要先确定分数再倒推理由。reason保持原要求的简短、可核查说明，不扩写长篇推理。\n"
    "score为1—5整数。reason使用以下固定前缀：",
)
_output = re.sub(
    r'\{"score": (\d+), "reason": ("(?:[^"\\]|\\.)*"),',
    r'{"reason": \2, "score": \1,',
    _output,
)
SYSTEM_TEMPLATE_SOURCE = _rubric + "四、输出要求\n" + _output
USER_TEMPLATE_SOURCE = LEGACY_USER_TEMPLATE_SOURCE.replace(
    "只输出各产品四字段JSON，总分必须为1—5整数。",
    "只输出各产品四字段JSON；每个产品先生成reason，再生成score，最后生成suggestion和domain。"
    "reason按证据、实际表现及影响、分档依据简短说明；score必须为1—5整数。",
)

VISUAL_COMPARE_SYSTEM = Template(SYSTEM_TEMPLATE_SOURCE)
VISUAL_COMPARE_USER = Template(USER_TEMPLATE_SOURCE)
