#!/usr/bin/env node
/**
 * Expert-review workbook authoring with the Codex primary runtime artifact tool.
 * Run a copy of this script from a conversation-specific temporary directory
 * whose node_modules points to CODEX_PRIMARY_RUNTIME_NODE_MODULES.
 * No model calls or source-workbook writes are performed by this exporter.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';
import { generationInfo, generationSummary, queryOf } from './review_data.mjs';

const HEADERS = [
  '样本ID', 'Query', '审核状态', '事实确认', '条件确认', '审核意见', '审核人', '审核日期',
  '原垂域', '原答案类型', '原答案摘要', '判定方式', '题意与适用条件', '最终答案',
  '核心准确性规则', '允许变体', '参考覆盖范围', '按需检查规则', '任务完成要求',
  '时效类型', '时效范围', '核验依据', '证据对应日期', '模型待确认问题',
];
const STATES = ['待审核', '通过', '修改后通过', '待补充', '剔除'];
const MODES = ['确定答案核对', '开放规则核验', '条件澄清'];
const COVERAGE = ['核心答案穷尽', '不穷尽', '待确认'];
const TEMPORAL = ['稳定', '动态', '待确认'];
const COLORS = { ink: '#243247', navy: '#263C59', muted: '#66778B', amber: '#FFF2CF', gray: '#F0F3F7', line: '#D9E1EA', red: '#FCE5E3' };
const FONT = 'Arial';
const SOURCE_KEYS = ['垂域', 'query来源', 'query类型', 'query等级', '单轮/多轮', 'query', '是否强时效性', '答案类型', '答案内容'];

function argsFrom(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--help') { out.help = true; continue; }
    if (!argv[i].startsWith('--') || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error(`参数不完整: ${argv[i]}`);
    out[argv[i].slice(2)] = argv[++i];
  }
  return out;
}
function asText(value) { return value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value); }
function literal(value) { const v = asText(value); return v.startsWith('=') ? `'${v}` : v; }
function excelCol(index) {
  let n = index + 1, out = '';
  while (n > 0) { n--; out = String.fromCharCode(65 + n % 26) + out; n = Math.floor(n / 26); }
  return out;
}
async function loadRecords(file) {
  const txt = (await fs.readFile(file, 'utf8')).replace(/^\uFEFF/, '').trim();
  if (!txt) throw new Error(`数据为空: ${file}`);
  const result = txt.startsWith('[') ? JSON.parse(txt) : txt.split(/\r?\n/).filter(Boolean).map(x => JSON.parse(x));
  if (!Array.isArray(result)) throw new Error('输入必须为 JSON 数组或 JSONL');
  return result;
}
/** Split on exact characters, keeping every newline and all original text. */
function answerChunks(value, maxChars = 700, maxLines = 18) {
  const raw = asText(value), chunks = [];
  let current = '', characters = 0, newlineCount = 0;
  for (const ch of raw) {
    if (characters >= maxChars || (ch === '\n' && newlineCount >= maxLines - 1)) {
      chunks.push(current); current = ''; characters = 0; newlineCount = 0;
    }
    current += ch; characters++; if (ch === '\n') newlineCount++;
  }
  if (current || !chunks.length) chunks.push(current);
  if (chunks.join('') !== raw) throw new Error('原始答案分段未通过无损校验');
  return chunks;
}
function answerPreview(value) {
  const raw = asText(value);
  // Remove repeated empty preview lines only. The original is preserved intact.
  const lines = raw.replace(/\n+/g, '\n').split('\n');
  const text = [...lines.slice(0, 14).join('\n')].slice(0, 420).join('');
  return text === raw ? raw : `${text}\n（摘要；完整原答见“原始输入”）`;
}
function wrapLines(value, widthChars) {
  const width = Math.max(5, widthChars - 3);
  return asText(value).split('\n').reduce((sum, line) => {
    const units = [...line].reduce((a, c) => a + (c.charCodeAt(0) > 255 ? 1.8 : 1), 0);
    return sum + Math.max(1, Math.ceil(units / width));
  }, 0);
}
function formatSheet(sheet, colCount, rowCount) {
  sheet.showGridLines = false;
  const all = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
  all.format.font = { name: FONT, size: 11, color: COLORS.ink };
  all.format.wrapText = true;
  all.format.verticalAlignment = 'top';
  all.format.horizontalAlignment = 'left';
}
function widths(sheet, values, rowCount) {
  values.forEach((width, i) => { sheet.getRangeByIndexes(0, i, rowCount, 1).format.columnWidth = width; });
}
function headers(sheet, row, values) {
  const range = sheet.getRangeByIndexes(row - 1, 0, 1, values.length);
  range.values = [values];
  range.format.fill = COLORS.navy;
  range.format.font = { name: FONT, size: 11, bold: true, color: '#FFFFFF' };
  range.format.horizontalAlignment = 'center';
  range.format.verticalAlignment = 'center';
  range.format.borders = { insideVertical: { style: 'thin', color: '#FFFFFF' }, bottom: { style: 'thin', color: COLORS.line } };
  range.format.rowHeight = 38;
}
function title(sheet, text, note = '') {
  sheet.getRange('B2').values = [[text]];
  sheet.getRange('B2').format.font = { name: FONT, size: 16, bold: true, color: COLORS.navy };
  sheet.getRange('A2').format.rowHeight = 30;
  if (note) sheet.getRange('B3').values = [[note]];
  sheet.getRange('A3').format.rowHeight = 26;
  sheet.getRange('B3').format.font = { name: FONT, size: 11, color: COLORS.muted };
}
function sizedRows(sheet, startRow, rows, colWidths, minimum = 50) {
  rows.forEach((row, i) => {
    const maxLines = Math.max(...row.map((v, j) => wrapLines(v, colWidths[j])));
    const needed = Math.max(minimum, 22 + maxLines * 17);
    if (needed > 409) throw new Error(`文字超过 Excel 单行可显示高度: ${sheet.name} 第${startRow + i}行，预计${needed}pt。请增大对应列宽或拆分内容。`);
    sheet.getRangeByIndexes(startRow - 1 + i, 0, 1, row.length).format.rowHeight = needed;
  });
}
function table(sheet, lastCol, lastRow, name) {
  const t = sheet.tables.add(`A4:${lastCol}${lastRow}`, true, name);
  t.showFilterButton = true;
  t.showBandedColumns = false;
  return t;
}
function dropdown(sheet, col, last, values) {
  sheet.getRange(`${col}5:${col}${last}`).dataValidation = { rule: { type: 'list', values } };
}

async function build(args) {
  const samples = await loadRecords(args.samples);
  const drafts = await loadRecords(args.drafts);
  if (!samples.length || samples.length !== drafts.length) throw new Error('原始样本与草稿条数必须一致且非空');
  const draftById = new Map();
  for (const d of drafts) {
    if (draftById.has(d.sample_id)) throw new Error(`重复草稿ID: ${d.sample_id}`);
    draftById.set(d.sample_id, d);
  }
  const seenSamples = new Set();
  for (const s of samples) {
    if (!s._sample_id || seenSamples.has(s._sample_id)) throw new Error(`原始样本ID缺失或重复: ${s._sample_id}`);
    seenSamples.add(s._sample_id);
    if (!draftById.has(s._sample_id)) throw new Error(`草稿缺失: ${s._sample_id}`);
    const d = draftById.get(s._sample_id);
    if (!MODES.includes(d.judge_mode) || !COVERAGE.includes(d.reference_coverage) || !TEMPORAL.includes(d.temporal_type)) throw new Error(`草稿枚举值不合法: ${s._sample_id}`);
    if (!Array.isArray(d.issues)) throw new Error(`issues必须为数组: ${s._sample_id}`);
  }

  const wb = Workbook.create();
  const main = wb.worksheets.add('专家审核');
  const model = wb.worksheets.add('模型草稿');
  const source = wb.worksheets.add('原始输入');
  const guide = wb.worksheets.add('填写说明');
  const last = samples.length + 4;
  const mainWidths = [13, 44, 15, 13, 13, 40, 18, 17, 17, 17, 70, 22, 55, 58, 76, 68, 22, 74, 48, 15, 64, 60, 19, 68];
  const rows = samples.map(s => {
    const d = draftById.get(s._sample_id);
    return [s._sample_id, queryOf(s), '待审核', '未确认', '未确认', '', '', '', s['垂域'], s['答案类型'], answerPreview(s['答案内容']),
      d.judge_mode, `${d.question_intent}\n适用条件：${d.applicable_conditions}`, d.suggested_answer, d.accuracy_rules,
      d.accepted_variants, d.reference_coverage, d.conditional_rules, d.task_completion, d.temporal_type, d.temporal_scope,
      '', '', d.issues.length ? d.issues.map((x, i) => `${i + 1}. ${x}`).join('\n') : '模型未列出问题；仍需确认事实、条件与依据。'];
  });
  formatSheet(main, 24, last);
  widths(main, mainWidths, last);
  title(main, `判定卡专家审核（${samples.length}条）`, '黄色区域可改，灰色区域只读。');
  main.tabColor = COLORS.navy;
  headers(main, 4, HEADERS);
  main.getRange(`A5:X${last}`).values = rows.map(r => r.map(literal));
  table(main, 'X', last, 'ExpertReview');
  // These editable cells start unapproved; source and model proposals are separate.
  for (const block of [`A5:B${last}`, `I5:K${last}`, `X5:X${last}`]) main.getRange(block).format.fill = COLORS.gray;
  for (const block of [`C5:H${last}`, `L5:W${last}`]) main.getRange(block).format.fill = COLORS.amber;
  sizedRows(main, 5, rows, mainWidths, 90);
  main.freezePanes.freezeRows(4);
  main.freezePanes.freezeColumns(2);
  dropdown(main, 'C', last, STATES);
  dropdown(main, 'D', last, ['未确认', '已确认']);
  dropdown(main, 'E', last, ['未确认', '已确认']);
  dropdown(main, 'L', last, MODES);
  dropdown(main, 'Q', last, COVERAGE);
  dropdown(main, 'T', last, TEMPORAL);
  main.getRange(`H5:H${last}`).setNumberFormat('yyyy-mm-dd');
  main.getRange(`W5:W${last}`).setNumberFormat('yyyy-mm-dd');
  main.getRange('I2:J2').values = [['待审核', null]];
  main.getRange('J2').formulas = [[`=COUNTIFS(C5:C${last},"待审核")`]];
  main.getRange('L2:M2').values = [['已批准', null]];
  main.getRange('M2').formulas = [[`=COUNTIFS(C5:C${last},"通过")+COUNTIFS(C5:C${last},"修改后通过")`]];
  main.getRange('O2:P2').values = [['待补充', null]];
  main.getRange('P2').formulas = [[`=COUNTIFS(C5:C${last},"待补充")`]];
  main.getRange('R2:S2').values = [['剔除', null]];
  main.getRange('S2').formulas = [[`=COUNTIFS(C5:C${last},"剔除")`]];
  for (const cell of ['J2', 'M2', 'P2', 'S2']) main.getRange(cell).setNumberFormat('0');
  main.getRange(`C5:C${last}`).conditionalFormats.add('containsText', { text: '待补充', format: { fill: COLORS.red, font: { color: '#963529' } } });

  const modelHeaders = ['样本ID', 'Query', '判定方式', '题意', '适用条件', '建议答案', '核心准确性规则', '允许变体', '参考覆盖范围', '按需检查规则', '任务完成要求', '时效类型', '时效范围', '建议核验依据', '待确认问题', '生成来源', '模型', '运行方式', '生成元数据JSON'];
  const modelWidths = [13, 44, 22, 50, 60, 58, 76, 68, 22, 74, 48, 15, 64, 74, 68, 27, 30, 24, 75];
  const modelRows = samples.map(s => {
    const d = draftById.get(s._sample_id), meta = d._generation_meta || {};
    const info = generationInfo(d);
    return [s._sample_id, queryOf(s), d.judge_mode, d.question_intent, d.applicable_conditions, d.suggested_answer, d.accuracy_rules,
      d.accepted_variants, d.reference_coverage, d.conditional_rules, d.task_completion, d.temporal_type, d.temporal_scope,
      d.evidence_to_verify, d.issues.join('\n'), info.source, info.model,
      info.run, JSON.stringify(meta)];
  });
  formatSheet(model, 19, last);
  widths(model, modelWidths, last);
  title(model, '模型原始草稿', '保留初始建议，在“专家审核”中修改。');
  headers(model, 4, modelHeaders);
  model.getRange(`A5:S${last}`).values = modelRows.map(r => r.map(literal));
  table(model, 'S', last, 'ModelDrafts');
  model.getRange(`A5:S${last}`).format.fill = COLORS.gray;
  sizedRows(model, 5, modelRows, modelWidths, 90);
  model.freezePanes.freezeRows(4); model.freezePanes.freezeColumns(2);

  const rawRows = [];
  for (const s of samples) {
    answerChunks(s['答案内容']).forEach((chunk, index) => {
      rawRows.push([s._sample_id, s._source_row, index + 1, ...SOURCE_KEYS.slice(0, -1).map(key => key === 'query' ? queryOf(s) : s[key]), chunk]);
    });
  }
  const rawLast = rawRows.length + 4;
  const rawHeaders = ['样本ID', '原始行号', '答案分段序号', '垂域', 'query来源', 'query类型', 'query等级', '单轮/多轮', 'query', '是否强时效性', '答案类型', '答案内容（分段）'];
  const rawWidths = [13, 15, 18, 20, 15, 15, 15, 18, 44, 20, 17, 116];
  formatSheet(source, 12, rawLast); widths(source, rawWidths, rawLast);
  title(source, '');
  // Put the source heading in the existing wide query column; no table merges.
  source.getRange('I2').values = [['原始输入与完整答案']];
  source.getRange('I2').format.font = { name: FONT, size: 16, bold: true, color: COLORS.navy };
  source.getRange('I3').values = [[`来源：${args['source-name'] || '导入数据（文件名未记录）'}`]];
  source.getRange('I3').format.font = { name: FONT, size: 11, color: COLORS.muted };
  source.getRange('I3').format.wrapText = false;
  headers(source, 4, rawHeaders);
  source.getRange(`A5:L${rawLast}`).values = rawRows.map(row => row.map((v, i) => [1, 2].includes(i) ? Number(v) : literal(v)));
  table(source, 'L', rawLast, 'OriginalInputs');
  source.getRange(`A5:L${rawLast}`).format.fill = COLORS.gray;
  source.getRange(`B5:C${rawLast}`).setNumberFormat('0');
  sizedRows(source, 5, rawRows, rawWidths, 44);
  source.freezePanes.freezeRows(4); source.freezePanes.freezeColumns(2);

  const guideRows = [
    ['开始审核', '每行对应一个样本。在“专家审核”修改黄色区域。灰色区域为来源和模型问题提示，供对照。'],
    ['完整原答案', '“原答案摘要”仅作预览。完整答案保存在“原始输入”，长答案拆为多行。筛选样本ID后，按答案分段序号阅读。'],
    ['修改草稿', 'L至U列已填写模型建议，尚未确认。直接将错误答案、过严规则、缺失条件等修改成专家定稿。“模型草稿”保留原建议。'],
    ['事实确认', '确认最终答案及核心事实正确后，D列选择“已确认”。模型生成内容和原标准答案都需要复核。'],
    ['条件确认', '确认日期、地区、机型、版本、用户条件、前文等已明确且不由答案反推后，E列选择“已确认”。无额外条件也需要确认。'],
    ['核验依据', 'V列由专家填写。可记录可信来源与关键支持内容、可复算公式或专家确认的事实依据与适用范围。模型建议的依据只在“模型草稿”展示。'],
    ['动态题', 'T列为“动态”时，U列写清事实适用日期、地区或版本，W列填写证据对应日期（YYYY-MM-DD）。缺少时间或快照时选“待补充”。'],
    ['通过', '专家确认事实、条件及依据充分，且L至U模型草稿未改动时选“通过”。填写审核人、审核日期和核验依据。'],
    ['修改后通过', '修改了L至U任一草稿内容，并确认事实、条件及依据充分后选“修改后通过”。在审核意见概述修改点，填写审核人和审核日期。'],
    ['待补充', '仍缺参考、上下文、条件、依据或存在未解决问题时选“待补充”，在审核意见写明需要补什么。暂不进入批准卡库。'],
    ['剔除', '样本不适合准确性评测或无法修复时选“剔除”，在审核意见记录原因。暂不进入批准卡库。'],
    ['允许变体', '确定答案接受等价表述和合理精度。开放题接受参考之外的合法词、有效方案或事实，不能将参考作为封闭白名单。'],
    ['按需检查规则', '产品主动提到的额外事实才检查，不强加为必答内容。新事实未被卡片覆盖时，应转独立核验。'],
    ['任务完成要求', '只保存题目要求的信息范围或数量。少答和事实错误分开记录，不用格式、篇幅或参考中的可选示例增加准确性要求。'],
    ['保留结构', '可以筛选、排序及修改黄色字段。保留样本ID、第4行表头、列顺序和工作表名，不合并审核数据行。'],
    ['提交审核', '审核日期填YYYY-MM-DD。全部样本初始为“待审核”，事实与条件初始为“未确认”。确认后导入时会再次检查必要字段。'],
    ['生成记录', generationSummary(drafts)],
  ];
  const guideLast = guideRows.length + 4;
  formatSheet(guide, 2, guideLast); widths(guide, [24, 115], guideLast);
  title(guide, '专家填写说明'); headers(guide, 4, ['项目', '填写要求']);
  guide.getRange(`A5:B${guideLast}`).values = guideRows;
  guide.getRange(`A5:A${guideLast}`).format.fill = COLORS.gray;
  guide.getRange(`A5:A${guideLast}`).format.font = { name: FONT, size: 11, bold: true, color: COLORS.navy };
  sizedRows(guide, 5, guideRows, [24, 115], 44);
  guide.tabColor = '#7B8796';

  wb.recalculate();
  const qaDir = args['qa-dir'] ? path.resolve(args['qa-dir']) : await fs.mkdtemp(path.join(os.tmpdir(), 'judge-review-qa-'));
  await fs.mkdir(qaDir, { recursive: true });
  const inspections = [];
  inspections.push((await wb.inspect({ kind: 'table', range: '专家审核!I2:S2', include: 'values,formulas', tableMaxRows: 1, tableMaxCols: 11, maxChars: 2500 })).ndjson);
  inspections.push((await wb.inspect({ kind: 'region', sheetId: '专家审核', range: 'A4:H6', maxChars: 2000 })).ndjson);
  inspections.push((await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options: { useRegex: true, maxResults: 100 }, summary: 'final formula error scan', maxChars: 3000 })).ndjson);
  await fs.writeFile(path.join(qaDir, 'inspect.ndjson'), inspections.join('\n'), 'utf8');
  const longIdx = samples.reduce((best, s, i) => asText(s['答案内容']).length > asText(samples[best]['答案内容']).length ? i : best, 0);
  const dynamicIdx = samples.findIndex(s => draftById.get(s._sample_id).temporal_type === '动态');
  const longRow = longIdx + 5, dynamicRow = Math.max(0, dynamicIdx) + 5;
  const sourceLong = rawRows.findIndex(r => r[0] === samples[longIdx]._sample_id) + 5;
  const renders = [
    ['专家审核', 'A1:H6', 'review_overview'],
    ['专家审核', 'I1:S3', 'review_counts'],
    ['专家审核', 'I4:L6', 'review_source_mode'],
    ['专家审核', 'M4:O6', 'review_core_rules'],
    ['专家审核', 'P4:R6', 'review_variants'],
    ['专家审核', 'S4:X6', 'review_temporal_basis'],
    ['专家审核', `K${longRow}:O${longRow}`, 'review_long_answer'],
    ['专家审核', `S${dynamicRow}:X${dynamicRow}`, 'review_dynamic_row'],
    ['专家审核', `P${Math.min(last, 9)}:S${Math.min(last, 9)}`, 'review_capacity_units'],
    ['专家审核', `S${Math.min(last, 53)}:U${Math.min(last, 53)}`, 'review_task_scope'],
    ['模型草稿', 'A1:F6', 'model_draft'],
    ['模型草稿', 'M4:S6', 'model_provenance'],
    ['原始输入', 'A1:K6', 'source_metadata'],
    ['原始输入', `I${sourceLong}:L${Math.min(sourceLong + 1, rawLast)}`, 'source_long_answer'],
    ['填写说明', `A1:B${guideLast}`, 'instructions'],
  ];
  const qaSheets = args['qa-sheets'] ? new Set(args['qa-sheets'].split(',')) : null;
  const qaRenders = args['qa-renders'] ? new Set(args['qa-renders'].split(',')) : null;
  for (const [sheetName, range, file] of renders) {
    if (qaSheets && !qaSheets.has(sheetName)) continue;
    if (qaRenders && !qaRenders.has(file)) continue;
    const blob = await wb.render({ sheetName, range, scale: 1, format: 'png' });
    await fs.writeFile(path.join(qaDir, `${file}.png`), new Uint8Array(await blob.arrayBuffer()));
  }
  // Values used for round-trip checks are retained in a small QA report.
  const sourceChecks = samples.map(s => {
    const reconstructed = rawRows.filter(r => r[0] === s._sample_id).sort((a, b) => a[2] - b[2]).map(r => r[11]).join('');
    return { sample_id: s._sample_id, answer_segments: rawRows.filter(r => r[0] === s._sample_id).length, lossless_answer: reconstructed === asText(s['答案内容']) };
  });
  if (sourceChecks.some(x => !x.lossless_answer)) throw new Error('完整原答无损复原检查失败');
  await fs.writeFile(path.join(qaDir, 'source_checks.json'), JSON.stringify(sourceChecks, null, 2), 'utf8');
  const output = path.resolve(args.out);
  await fs.mkdir(path.dirname(output), { recursive: true });
  await (await SpreadsheetFile.exportXlsx(wb)).save(output);
  console.log(JSON.stringify({ output, sample_count: samples.length, raw_answer_segment_rows: rawRows.length, qa_dir: qaDir, review_status: '全部待审核', review_columns: HEADERS.length }));
}

const args = argsFrom(process.argv.slice(2));
if (args.help || !args.samples || !args.drafts || !args.out) {
  console.log('Usage: export_review.mjs --samples raw_samples.json --drafts drafts.jsonl --out review.xlsx [--source-name 原文件名.xlsx] [--qa-dir qa目录] [--qa-sheets 仅重查的工作表名] [--qa-renders 仅重查的预览名]');
  if (!args.help) process.exitCode = 2;
} else {
  await build(args);
}
