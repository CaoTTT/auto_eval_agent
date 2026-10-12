/** Pure data helpers; no workbook runtime or network dependency. */
export function queryOf(sample) {
  // Match pipeline.query_of's alias order and presence check, including an
  // empty first alias. XLSX imports preserve these values as strings.
  for (const key of ['query', 'Query', '问题']) {
    if (Object.hasOwn(sample, key)) {
      const value = sample[key];
      if (value === null) return 'None';
      if (typeof value === 'boolean') return value ? 'True' : 'False';
      return String(value);
    }
  }
  throw new Error(`样本 ${sample._sample_id ?? '?'} 缺少 query 字段`);
}

export function generationInfo(draft) {
  const meta = draft._generation_meta ?? {};
  if (meta.origin === 'configured_api') {
    const model = meta.model || meta.config?.model || '未记录的API模型';
    const endpoint = meta.config?.base_url || '未记录endpoint';
    return { category: 'api', source: '配置的模型API生成', model,
      run: '外部API运行', configuration: `${model}；${endpoint}` };
  }
  const currentSession = meta.origin === 'chatgpt_in_session'
    || meta.origin === 'chatgpt_current_session'
    || meta.provider === 'chatgpt_in_session';
  if (currentSession && meta.external_api_called !== true) {
    return { category: 'chatgpt', source: 'ChatGPT 当前会话生成',
      model: meta.model || 'ChatGPT（当前会话）', run: '非外部API运行', configuration: '' };
  }
  return { category: 'unknown', source: '生成来源待确认',
    model: meta.model || meta.config?.model || '未记录模型',
    run: '运行方式待确认', configuration: '' };
}

export function generationSummary(drafts) {
  const infos = drafts.map(generationInfo);
  const counts = { chatgpt: 0, api: 0, unknown: 0 };
  const configurations = new Set();
  for (const info of infos) {
    counts[info.category]++;
    if (info.category === 'api') configurations.add(info.configuration);
  }
  const groups = [];
  if (counts.chatgpt) groups.push(`ChatGPT当前会话生成${counts.chatgpt}条（非外部API运行）`);
  if (counts.api) {
    const configs = [...configurations];
    const preview = configs.slice(0, 3).join(' / ');
    const suffix = configs.length > 3 ? '；其余配置见模型草稿原始元数据' : '';
    groups.push(`配置API生成${counts.api}条，${configs.length}种配置：${preview}${suffix}`);
  }
  if (counts.unknown) groups.push(`生成来源或运行方式待确认${counts.unknown}条`);
  const prefix = groups.length > 1 ? '本批草稿来源混合：' : '本批草稿：';
  return `${prefix}${groups.join('；') || '无草稿'}。各条完整生成元数据保存在“模型草稿”。专家确认信息保持空白，等待实际审核。`;
}
