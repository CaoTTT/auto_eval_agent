"""Freeze the actual 13 existing protocols from upstream 8dbb0502.

This adds a regression guard without modifying older prompts or their older
independent snapshot tests. Hashes include request identity and 8 renders.
"""
import hashlib
import json

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol

BASELINE = {'chitchat_evidence_first_flat@1.1': '6f89b2abde7f830b1fa297273fbdcbefaa69e30d60507ab3e628b1db182d1c1d', 'knowledge_qa_compare@1.0': '9d4491b81040b59c142d9b656bf6b9945bdb6c14a843fd6cc14fc0097deff114', 'qa_chat_compare@1.0': '7b3a2d69523f57e484d5de43a397e6bd526cdae460b88c6e87889f103b204aa6', 'qa_chat_compare@1.1': 'd3e90afd9135309437738569f84cce312236b8c6bed0662e07fc40d25cf8d209', 'qa_chat_compare@1.1-demand-calibrated': 'f92a9b472b1f0ea8db2223f365f0c88c1da7a8e33bbb76b8c002465a5d374a2b', 'qa_competitor_compare@0.2-simplified': 'e02b22e3576544c1064fd16e6051d7821861c30002627ddea1cbf68399dfdb78', 'qa_competitor_compare@0.2-simplified-1022': 'fe5cf3796d796c1355abd328bc5e7c47302a0899a355bbe699ac941328149a41', 'qa_competitor_compare@0.2-simplified-calibrated': '4714a4267581c60d7abab1c98299f83076c9fe99f08fe9674585c151e9235c92', 'qa_competitor_compare@0.2-simplified-plus': 'f9810a5d3cf125e42770b854dc91f687aa58aa28e1d15c133ae1e45e543f95b4', 'qa_competitor_compare@0.2-simplified-prior-knowledge': '75e35eb16593764a3d50ede539b0496b768f89dd47aa2d439c5f926fe214b80d', 'qa_competitor_compare@0.2-simplified-thinking-exposure': '373545e1d3fcf965b5b7f00acb12cbaec13532966789b0f2f755c0c2a9f5e295', 'qa_competitor_compare@0.3': 'f6254cb55a4486feaf04dc61e34cc0038cf1fc9ee98f4d42e12ec7739df2c23c', 'timeliness_compare@1.0': 'a9e9cd97834cc7810e628021dcb4870a11dce19d5824ddee1860eb4a0ec995e5'}


@pytest.mark.parametrize("profile,expected", BASELINE.items())
def test_existing_protocol_identity_and_renders_are_unchanged(profile, expected):
    protocol = resolve_compare_protocol(profile)
    renders = []
    for count in (2, 3):
        for mode in ("video_frames", "long_screenshot"):
            for prior in (False, True):
                args = dict(persona="frozen-check", product_count=count, evidence_mode=mode,
                            include_prior_knowledge=prior, evaluation_datetime="2026-11-02T12:00:00+08:00",
                            question="原题", context1="背景1", context2="背景2", context3="背景3",
                            answer1="回答1", answer2="回答2", answer3="回答3",
                            frame_count1=1, frame_count2=1, frame_count3=1,
                            image_count1=1, image_count2=1, image_count3=1,
                            split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
                system, user = protocol.prompt_templates(count)
                renders.append(hashlib.sha256((system.render(**args) + "\0" + user.render(**args)).encode()).hexdigest())
    value = {"metadata": protocol.request_metadata(), "renders": renders}
    assert hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() == expected
