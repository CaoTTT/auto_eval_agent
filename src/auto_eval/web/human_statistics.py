"""Pure paired human/model statistics. Every aggregate includes its sample set."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations, permutations
from statistics import mean

from .compare_statistics import SCORE_DIMENSIONS

METRIC_VERSION = "human-statistics-1.0"


def ratio(a, b):
    return a / b if b else None


def average(values):
    return mean(values) if values else None


def _single(rows, eligible):
    human = [r for r in rows if r["human_score"] is not None]
    paired = [r for r in human if r["comparable"] and r["case_key"] in eligible]
    differences = [r["model_score"] - r["human_score"] for r in paired]
    n = len(paired)
    paired_ids = {r["match_id"] for r in paired}
    return dict(human_n=len(human), n=n, coverage=ratio(n, len(human)),
                exact_n=differences.count(0), exact=ratio(differences.count(0), n),
                within_one_n=sum(abs(d) <= 1 for d in differences), within_one=ratio(sum(abs(d) <= 1 for d in differences), n),
                mae=average([abs(d) for d in differences]), bias=average(differences),
                higher=ratio(sum(d > 0 for d in differences), n), lower=ratio(sum(d < 0 for d in differences), n),
                severe=ratio(sum(abs(d) >= 2 for d in differences), n),
                human_mean=average([r["human_score"] for r in paired]), model_mean=average([r["model_score"] for r in paired]),
                difference_counts=dict(Counter(str(int(d)) for d in differences)),
                human_distribution=dict(Counter(str(int(r["human_score"])) for r in paired)),
                model_distribution=dict(Counter(str(int(r["model_score"])) for r in paired)),
                confusion=dict(Counter(f'{int(r["human_score"])},{int(r["model_score"])}' for r in paired)),
                sample_ids=[r["match_id"] for r in paired],
                excluded=dict(Counter(r["exclusion"] or "未进入共同样本" for r in human if r["match_id"] not in paired_ids)),
                score_states=dict(Counter(("人工数字" if r["human_score"] is not None else "人工无数字") + "/" +
                                         ("模型数字" if r["model_score"] is not None else "模型无数字") for r in rows)))


def _pair(a, b, keys):
    h_a, h_b = [a[k]["human_score"] for k in keys], [b[k]["human_score"] for k in keys]
    m_a, m_b = [a[k]["model_score"] for k in keys], [b[k]["model_score"] for k in keys]
    hs = [(x > y) - (x < y) for x, y in zip(h_a, h_b)]
    ms = [(x > y) - (x < y) for x, y in zip(m_a, m_b)]
    n = len(keys)
    rh = ratio(sum(h_a), sum(h_b))
    rm = ratio(sum(m_a), sum(m_b))
    h_gsb, m_gsb = [hs.count(k) for k in (1, 0, -1)], [ms.count(k) for k in (1, 0, -1)]
    return dict(n=n, human_ratio=rh, model_ratio=rm,
                ratio_bias_pp=(rm-rh)*100 if rh is not None and rm is not None else None,
                human_a_mean=average(h_a), human_b_mean=average(h_b), model_a_mean=average(m_a), model_b_mean=average(m_b),
                human_gap=average([x-y for x,y in zip(h_a,h_b)]), model_gap=average([x-y for x,y in zip(m_a,m_b)]),
                human_gsb=h_gsb, model_gsb=m_gsb,
                human_gsb_rates=[ratio(v,n) for v in h_gsb], model_gsb_rates=[ratio(v,n) for v in m_gsb],
                human_net_win=ratio(h_gsb[0]-h_gsb[2],n), model_net_win=ratio(m_gsb[0]-m_gsb[2],n),
                outcome_agreement=ratio(sum(x==y for x,y in zip(hs,ms)),n),
                reversal=ratio(sum(x*y==-1 for x,y in zip(hs,ms)),n),
                confusion=dict(Counter(f"{x},{y}" for x,y in zip(hs,ms))),
                case_keys=sorted(keys), sample_ids=[a[k]["match_id"] for k in keys]+[b[k]["match_id"] for k in keys])


def build_human_statistics(rows: list[dict], task_ids: list[str], products: list[str], dimensions=None) -> dict:
    dimensions = [d for d in (dimensions or SCORE_DIMENSIONS) if d in SCORE_DIMENSIONS]
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["task_id"], row["product_id"], row["dimension_id"]].append(row)
    valid = {key: {r["case_key"]: r for r in group if r["comparable"]} for key, group in grouped.items()}
    scores, pairs, ranks, states = [], [], [], []
    scopes = ("own", "common") if len(task_ids) > 1 else ("own",)
    for dim in dimensions:
        for product in products:
            common = set.intersection(*(set(valid.get((t,product,dim), {})) for t in task_ids))
            for task in task_ids:
                group = grouped[task,product,dim]
                for scope in scopes:
                    eligible = common if scope == "common" else set(valid.get((task,product,dim), {}))
                    scores.append(dict(task_id=task, product_id=product, dimension_id=dim, scope=scope, **_single(group,eligible)))
        for a, b in permutations(products, 2):
            all_pairs = {t: set(valid.get((t,a,dim), {})) & set(valid.get((t,b,dim), {})) for t in task_ids}
            common = set.intersection(*all_pairs.values())
            for task in task_ids:
                human_a = {r["case_key"] for r in grouped[task,a,dim] if r["human_score"] is not None}
                human_b = {r["case_key"] for r in grouped[task,b,dim] if r["human_score"] is not None}
                hn = len(human_a & human_b)
                for scope in scopes:
                    keys = sorted(common if scope == "common" else all_pairs[task])
                    pairs.append(dict(task_id=task, product_a=a, product_b=b, dimension_id=dim, scope=scope,
                                      human_n=hn, coverage=ratio(len(keys),hn),
                                      **_pair(valid.get((task,a,dim),{}),valid.get((task,b,dim),{}),keys)))
        if len(products) == 3:
            all_three = {t: set.intersection(*(set(valid.get((t,p,dim), {})) for p in products)) for t in task_ids}
            common = set.intersection(*all_three.values())
            for task in task_ids:
                for scope in scopes:
                    keys = sorted(common if scope == "common" else all_three[task])
                    agree = 0
                    for k in keys:
                        values = [valid[task,p,dim][k] for p in products]
                        agree += all(((a["human_score"] > b["human_score"]) - (a["human_score"] < b["human_score"])) ==
                                     ((a["model_score"] > b["model_score"]) - (a["model_score"] < b["model_score"])) for a,b in combinations(values,2))
                    ranks.append(dict(task_id=task, dimension_id=dim, scope=scope, n=len(keys), exact_n=agree,
                                      agreement=ratio(agree,len(keys)), case_keys=keys))
    # State metrics require explicit labels and valid identity. Unknown is never a pass.
    for task in task_ids:
        for dim in dimensions:
            group = [r for r in rows if r["task_id"]==task and r["dimension_id"]==dim]
            applicable = {r["case_key"]: r for r in group if r["identity_accepted"] and
                          r.get("human_applicable") is not None and r.get("model_applicable") is not None}
            values = list(applicable.values())
            states.append(dict(task_id=task, dimension_id=dim, kind="applicability", product_id="", n=len(values),
                               agreement=ratio(sum(r["human_applicable"]==r["model_applicable"] for r in values),len(values))))
        for product in products:
            for gate in ("response", "safety"):
                group = {r["case_key"]: r for r in rows if r["task_id"]==task and r["product_id"]==product and
                         r["identity_accepted"] and r.get("human_gates",{}).get(gate) in ("pass","fail","unclear") and
                         r.get("model_gates",{}).get(gate) in ("pass","fail","unclear") and r.get("model_input_status") in ("complete","partial")}
                values = list(group.values())
                states.append(dict(task_id=task, dimension_id="", kind=gate+"_gate", product_id=product, n=len(values),
                                   agreement=ratio(sum(r["human_gates"][gate]==r["model_gates"][gate] for r in values),len(values))))
    for row in scores:
        if row["scope"] == "common":
            base = next(r for r in scores if r["task_id"]==task_ids[0] and r["scope"]=="common" and
                        r["dimension_id"]==row["dimension_id"] and r["product_id"]==row["product_id"])
            row["vs_first_task"] = {key: row[key]-base[key] if row[key] is not None and base[key] is not None else None
                                    for key in ("exact", "mae", "bias")}
    overview = {}
    for task in task_ids:
        group = [r for r in rows if r["task_id"]==task]
        cases = {r["case_id"] for r in group}
        matched = {r["case_id"] for r in group if r["task_item_index"] is not None and not r["case_key"].startswith("out:")}
        overview[task] = dict(label_rows=len(group),case_count=len(cases),matched_case_count=len(matched),
                              case_match_rate=ratio(len(matched),len(cases)),
                              matches=dict(Counter(r["match_status"] for r in group)),
                              bindings=dict(Counter(r["result_input_binding"] for r in group)))
    return dict(metric_version=METRIC_VERSION, scores=scores, pairs=pairs, ranks=ranks, states=states,overview=overview)
