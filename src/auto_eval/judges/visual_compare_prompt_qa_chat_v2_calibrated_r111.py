"""Demand and score calibration for chat V2, with the original I/O contract."""
from __future__ import annotations

from jinja2 import Template

from .visual_compare_prompt_qa_chat_v2 import (
    RULES,
    SOURCE_ANCHORS,
    VISUAL_COMPARE_USER,
    output_example,
)


CALIBRATION_RULES = r"""
【执行补充：先确定当前需求，再按相关子项分档】
以下仅细化理解需求、情感表达恰当、风格恰当、场景化满足、引导推荐五维的执行顺序。
原表评分锚点、Gate、适用性、核验、空分及个性化适配规则保持不变。

一、当前需求与相关子项
1. 先根据Query及对应产品的有效上下文，确定当前主要目的、明确要求，以及有依据且
   影响当前回应的隐含需求。区分分享、倾诉、寻求安慰、分析、方法、话术和轻松闲聊；
   多种目的并存时判断主次，不把无依据推测的状态或目标设为必须满足的条件。
   关键歧义确实影响回应方向时，简洁、必要的澄清本身不构成理解浅或回应不足；
   已有信息足够时仍应直接回应，不能用澄清回避当前可回答的核心需求。
2. 再根据当前目的选择本维相关子项。不将未触发的正向要求设为补齐义务：
   简单交流、分享或情感陪伴不必额外提供分析框架、方案、步骤或话术；
   明确求方法或话术时，不因出现情绪词就强制要求先安慰。
   用户已有依据的情绪需求和其他相关要求仍须评价，不能借任务类型忽略它们。
3. 回答实际引入的偏题、越界或其他缺陷，仍须按原标准评价，不能以用户未提出该需求为由跳过。
   子项未触发不等于该维不适用，继续执行原有维度适用性与核验规则。
   按当前任务的必要回应和交流节奏判断充分程度，不以内容更多、篇幅更长或延伸更多证明更优秀。

二、五维逐产品分档自检
1. 分别将可定位证据、实际表现及其影响与原表相关档位核对，不设置默认3分或默认5分，
   不按问题数量固定扣分、不增加分数上限，也不为了拉开产品差距调整尺度。
2. 给3分或4分时，理由须指出符合原档位的具体不足、它对应的当前需求或相关子项，
   以及从现有证据可判断的影响。不得仅以“简单、较通用、单薄、承接浅、推进有限”扣分，
   不得为了证明未达高分而追加用户未要求且无依据的目标、情绪或后续任务。
3. 给5分时，说明当前情境相关高分要求如何得到满足；简单任务可以达到5分，
   但完成核心需求或未发现缺点也不自动等于满分，仍须核对本维相关高分锚点。
4. 理由中的证据、表现及原档位依据必须支持最终分数。发现不一致时重新核对证据和分档，
   不为保留原分补造缺陷、影响或优点，不把未经验证的用户感受和实际效果写成事实。
   缺少决定性证据时沿用原有核验规则，不用中间分代替无法判断。

上述步骤只用于内部核对。将依据简要写入已有的五维reason，每产品通常1—3句；
不输出需求分类、子项清单、自检过程或新字段，原有六维JSON键、类型及顺序保持不变。
"""


VISUAL_COMPARE_SYSTEM = Template(
    RULES
    + "\n" + CALIBRATION_RULES
    + "\n【完整原表锚点与案例】\n"
    + SOURCE_ANCHORS
    + "\n【本次完整输出模板】\n"
      "以下示例只展示字段、类型和顺序。pass和3仅为形状示例，不能照抄；"
      "必须按本题证据独立选择Gate、分数或null。\n"
      "{{ output_example(product_count) }}"
)
VISUAL_COMPARE_SYSTEM.globals["output_example"] = output_example


def render_system_prompt(
    *, product_count: int = 2, evidence_mode: str = "long_screenshot", persona: str = ""
) -> str:
    """Render only the new variant; existing chat V2 templates stay untouched."""
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("product_count must be 2 or 3")
    if evidence_mode not in ("long_screenshot", "video_frames"):
        raise ValueError("unknown evidence_mode")
    return VISUAL_COMPARE_SYSTEM.render(
        persona=persona, product_count=product_count, evidence_mode=evidence_mode
    )
