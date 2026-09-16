"""Human reports use the existing OOXML table writer and frozen metrics only."""
from __future__ import annotations

from io import BytesIO
import json
import re
from xml.sax.saxutils import escape
import zipfile

from .compare_statistics import DIMENSION_NAMES, StatisticsTable
from .compare_statistics_xlsx import statistics_sheet_xml
from .history import _col, _sheet_xml, _styles_xml, _xlsx_text


def tables_workbook(sheets: dict[str,list[StatisticsTable]], *, plain: bool=False) -> bytes:
    output=BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        overrides=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+overrides+'</Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names=''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,name in enumerate(sheets,1))
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+names+'</sheets></workbook>')
        relationships=''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+relationships+f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml",_styles_xml(statistics=True).replace('</styleSheet>',
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'))
        for i,(name,tables) in enumerate(sheets.items(),1):
            if plain:
                table=tables[0]
                xml=_sheet_xml([dict(zip(table.headers,row)) for row in table.rows] or [dict.fromkeys(table.headers)])
                # Empty annotation cells are truly blank, not zero or an empty-string formula.
                xml=re.sub(r'<c r="[A-Z]+\d+" t="inlineStr"[^>]*><is><t xml:space="preserve"></t></is></c>', '', xml)
            elif name in ("分歧明细","匹配与排除"):
                table=tables[0]
                xml=_sheet_xml([dict(zip(table.headers,row)) for row in table.rows] or [dict.fromkeys(table.headers)])
                if table.rows:
                    xml=xml.replace('</worksheet>',f'<autoFilter ref="A1:{_col(len(table.headers))}{len(table.rows)+1}"/></worksheet>')
            else:
                xml=statistics_sheet_xml(tables,style_start=2,escape_text=_xlsx_text,column_name=_col)
            archive.writestr(f"xl/worksheets/sheet{i}.xml",xml)
    return output.getvalue()


def export_report(report: dict) -> bytes:
    baseline=report["baseline"]
    names={p["product_id"]:p["display_name"] for p in baseline["products"]}
    def product(pid):return names.get(pid,pid)
    def dim(d):return DIMENSION_NAMES.get(d,d)
    def scope(s):return "共同样本" if s=="common" else "各自样本"
    summary=StatisticsTable("人工基准与冻结任务",("项目","内容"),rows=[
        ["报告",report["report_id"]],["人工基准",f'{baseline["name"]} v{baseline["version"]}'],
        ["人工标准",baseline["human_standard_version"]],["人工口径",baseline["human_policy_note"]],
        ["兼容模式",report["compatibility_mode"]],["用途",baseline["purpose"]],
        ["模型协议",json.dumps(report.get("model_protocols",[]),ensure_ascii=False)],
        ["内容准确性人工数字标签（仅留档）",report.get("accuracy_label_count",0)],
        ["任务与快照",json.dumps(report["frozen_at"],ensure_ascii=False)],["信息缺失", "；".join(report["warnings"])],
        ["范围外人工题号",json.dumps(report["out_of_scope"],ensure_ascii=False)]])
    coverage=StatisticsTable("题号匹配覆盖",("任务","范围题数","匹配题数","题号匹配率"),
        ("text","count","count","percent"),
        [[t,r["case_count"],r["matched_case_count"],r["case_match_rate"]] for t,r in report["metrics"]["overview"].items()])
    score=StatisticsTable("人机评分统计",("任务","产品","维度","样本口径","人工有效数","可比数","覆盖率","人工均分","模型均分","完全一致率","差≤1分","MAE","平均偏差","偏高比例","偏低比例","严重分歧"),
        ("text",)*4+("count",)*2+("percent","decimal","decimal","percent","percent","decimal","decimal","percent","percent","percent"))
    confusion=StatisticsTable("混淆矩阵与分差分布",("任务","产品","维度","样本口径","混淆矩阵 人工,模型","分差分布","样本数"))
    for r in report["metrics"]["scores"]:
        score.rows.append([r["task_id"],product(r["product_id"]),dim(r["dimension_id"]),scope(r["scope"]),*[r[k] for k in ("human_n","n","coverage","human_mean","model_mean","exact","within_one","mae","bias","higher","lower","severe")]])
        confusion.rows.append([r["task_id"],product(r["product_id"]),dim(r["dimension_id"]),scope(r["scope"]),json.dumps(r["confusion"],ensure_ascii=False),json.dumps(r["difference_counts"]),r["n"]])
    pair=StatisticsTable("产品差距对比",("任务","方向 A/B","维度","样本口径","人工配对数","可比数","覆盖率","人工分位值","模型分位值","偏差（百分点）","人工均分差","模型均分差","人工 G/S/B","模型 G/S/B","人工净胜率","模型净胜率","胜平负一致率","胜负反转率"),
        ("text",)*4+("count",)*2+("percent",)*3+("decimal",)*3+("text",)*2+("percent",)*4)
    for r in report["metrics"]["pairs"]:
        pair.rows.append([r["task_id"],product(r["product_a"])+" / "+product(r["product_b"]),dim(r["dimension_id"]),scope(r["scope"]),
                          *[r[k] for k in ("human_n","n","coverage","human_ratio","model_ratio","ratio_bias_pp","human_gap","model_gap")],
                          "/".join(map(str,r["human_gsb"])),"/".join(map(str,r["model_gsb"])),
                          *[r[k] for k in ("human_net_win","model_net_win","outcome_agreement","reversal")]])
    ranking=StatisticsTable("三产品单维排名（保留平局）",("任务","维度","口径","样本数","一致数","一致率"),
                            ("text","text","text","count","count","percent"),
                            [[r["task_id"],dim(r["dimension_id"]),scope(r["scope"]),r["n"],r["exact_n"],r["agreement"]] for r in report["metrics"]["ranks"]])
    states=StatisticsTable("人工明确状态一致性",("任务","产品","维度","指标","有效数","一致率"),
                           ("text","text","text","text","count","percent"),
                           [[r["task_id"],product(r["product_id"]),dim(r["dimension_id"]),r["kind"],r["n"],r["agreement"]] for r in report["metrics"]["states"]])
    detail=StatisticsTable("分歧明细",("任务","题号","产品","维度","问题","人工分","模型分","分差","人工状态","模型状态","人工来源","人工批注","模型理由","身份状态","结果绑定","排除原因","证据引用"))
    matches=StatisticsTable("匹配与排除",("任务","题号","产品","维度","匹配ID","身份状态","核验依据","结果绑定","绑定依据","排除原因"))
    for r in report["rows"]:
        prefix=[r["task_id"],r["case_id"],product(r["product_id"]),dim(r["dimension_id"])]
        detail.rows.append(prefix+[r["query"],r["human_score"],r["model_score"],r["difference"],r["human_status"],r["model_status"],json.dumps(r["human_source"],ensure_ascii=False),r["human_reason"],r["model_reason"],r["match_status"],r["result_input_binding"],r["exclusion"],r["evidence"]])
        matches.rows.append(prefix+[r["match_id"],r["match_status"],"；".join(r["match_reasons"]),r["result_input_binding"],"；".join(r["binding_reasons"]),r["exclusion"]])
    policy=StatisticsTable("统计口径",("项目","定义"),rows=[
        ["指标版本",report["metrics"]["metric_version"]],["覆盖率", "可比标签数 / 范围内人工有效数字标签数；模型失败不补0"],
        ["一致率 / MAE / 偏差","同一有效集合上的 M=H 比例 / mean(abs(M-H)) / mean(M-H)"],
        ["产品配对","人工 A/B 与模型 A/B 四分均有效的同一题集；分位值=mean(A)/mean(B)"],
        ["共同样本","所选任务每产品维度/产品对的有效题集交集；空集合不排名"],
        ["空值","零分母为 —；NA 与未标注不计数字一致率；内容准确性仅留档"],
        ["范围与映射",json.dumps(report["config"],ensure_ascii=False)],
        ["身份确认",json.dumps(report["confirmation"],ensure_ascii=False)],
        ["人工标签摘要",baseline["labels_sha256"]]])
    return tables_workbook({"对比概览":[summary,coverage,states],"人机评分统计":[score,confusion],"产品差距对比":[pair,ranking],
                            "分歧明细":[detail],"匹配与排除":[matches],"统计口径":[policy]})
