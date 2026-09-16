import copy

import pytest

from auto_eval.web.human_compare import align_baseline_to_run
from auto_eval.web.human_statistics import build_human_statistics
from test_human_compare import example


def test_hand_calculated_metrics_and_common_samples():
    _,snapshot,baseline,mapping=example([[4,3],[2,5]])
    rows=align_baseline_to_run(baseline,snapshot,mapping,["case-0","case-1"],["understanding"],"same_standard")
    stats=build_human_statistics(rows,["test"],["p1","p2"],["understanding"])
    a,b=stats["scores"]
    assert (a["n"],a["coverage"],a["exact"],a["mae"],a["bias"])==(2,1,0,1,0)
    assert (b["n"],b["exact"],b["mae"],b["bias"])==(2,.5,1,1)
    pair=stats["pairs"][0]
    assert pair["human_ratio"]==1 and pair["model_ratio"]==.75
    assert pair["ratio_bias_pp"]==-25 and pair["human_gsb"]==[0,2,0] and pair["model_gsb"]==[1,0,1]
    assert pair["outcome_agreement"]==0 and pair["reversal"]==0
    other=copy.deepcopy(rows)
    for r in other:r["task_id"]="second"
    other[0].update(comparable=False,model_score=None,exclusion="模型失败")
    stats=build_human_statistics(rows+other,["test","second"],["p1","p2"],["understanding"])
    scores={(r["task_id"],r["product_id"],r["scope"]):r for r in stats["scores"]}
    assert scores["second","p1","own"]["coverage"]==.5
    assert scores["test","p1","common"]["n"]==1
    assert scores["test","p1","common"]["bias"]==-1
    assert scores["second","p1","common"]["vs_first_task"]["mae"]==0


def test_third_product_failure_preserves_ab_and_zero_denominator():
    _,snapshot,baseline,mapping=example([[3,2,1],[1,1,0]],standard="0.3")
    snapshot["results"][1]["answer3_input_status"]="failed"
    rows=align_baseline_to_run(baseline,snapshot,mapping,["case-0","case-1"],["understanding"],"same_standard")
    stats=build_human_statistics(rows,["test"],["p1","p2","p3"],["understanding"])
    pairs={(r["product_a"],r["product_b"]):r for r in stats["pairs"]}
    assert len(pairs)==6 and pairs["p1","p2"]["n"]==2 and pairs["p1","p3"]["n"]==1
    assert stats["ranks"][0]["n"]==1 and stats["ranks"][0]["agreement"]==0
    assert all(s["agreement"] is None for s in stats["states"])
    for r in rows:r.update(comparable=False,exclusion="missing")
    empty=build_human_statistics(rows,["test"],["p1","p2","p3"],["understanding"])
    assert empty["scores"][0]["mae"] is None and empty["scores"][0]["coverage"]==0
    assert empty["pairs"][0]["human_ratio"] is None
