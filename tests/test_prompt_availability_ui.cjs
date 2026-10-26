const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');

async function main() {
  let app, history, submitted;
  const requests = [];
  const context = {
    ref: value => ({ value }), computed: getter => ({ get value() { return getter(); } }),
    onMounted() {}, onUnmounted() {}, nextTick() {}, AbortController, FormData, console,
    confirm() { return true; }, alert(message) { throw Error(message); },
    createApp(options) { app = options.setup(); return { mount() {} }; },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url === '/api/eval') {
        submitted = JSON.parse(options.body);
        return { ok: false, json: async () => ({ detail: 'test stop' }) };
      }
      if (url.startsWith('/api/history/')) return { ok: true, json: async () => history };
      if (url.endsWith('/retries')) return { ok: false, json: async () => ({ detail: 'test stop' }) };
      throw Error(url);
    },
  };
  vm.runInNewContext(strip(read('compare-cases.js')), context);
  vm.runInNewContext(strip(read('app.js')), context);

  const ids = [
    'qa_competitor_compare@0.2-simplified',
    'qa_competitor_compare@0.2-simplified-plus',
    'qa_competitor_compare@0.3',
    'qa_competitor_compare@0.2-simplified-calibrated',
    'qa_competitor_compare@0.2-simplified-thinking-exposure',
    'qa_chat_compare@1.0',
    'qa_chat_compare@1.1',
    'timeliness_compare@1.0',
    'qa_competitor_compare@0.2-simplified-1022',
  ];
  const disabled = new Set(ids.slice(2, 6));
  const profiles = ids.map((id, index) => ({
    id, display: id, modes: ['compare'], selectable: !disabled.has(id),
    status: index < 2 ? 'stable' : 'experimental', score_range: [1, 5],
  }));
  app.evaluationProfiles.value = profiles;
  const enabledIds = ids.filter(id => !disabled.has(id));
  assert.deepEqual(Array.from(app.compareProfiles.value, profile => profile.id),
    [...enabledIds, ...ids.filter(id => disabled.has(id))],
    'all nine remain visible, enabled first, with stable order inside both groups');
  assert.deepEqual(profiles.map(profile => profile.id), ids, 'display ordering does not mutate config order');
  profiles[4].display = 'V0.2 简化版·思考暴露优化（可用）';
  assert.equal(app.evaluationProfileOptionLabel(profiles[4]), 'V0.2 简化版·思考暴露优化');
  assert.equal(app.evaluationProfileLabel(profiles[4].id), profiles[4].display,
    'the unavailable option label does not rewrite historical protocol labels');
  const html = read('index.html');
  assert.match(html, /<option\b[^>]*v-for="profile in compareProfiles"[^>]*:disabled="profile\.selectable === false"/);
  assert.match(html, /profile\.selectable === false \? '（暂不可选）'/);
  assert.match(read('style.css'), /select option:disabled\s*\{[^}]*color:/);

  app.switchMode('compare');
  assert.equal(app.selectedEvaluationProfile.value, ids[0], 'existing default is retained');
  profiles[0].selectable = false;
  app.switchMode('compare');
  assert.equal(app.selectedEvaluationProfile.value, ids[1], 'a disabled stable profile cannot become the default');
  profiles[1].selectable = false;
  app.switchMode('compare');
  assert.equal(app.selectedEvaluationProfile.value, enabledIds[2], 'fallback chooses the first selectable experimental profile');
  profiles[0].selectable = profiles[1].selectable = true;

  const item = { id: 'case', query: '问题', product_count: 2,
    screenshot1: 'a.png', screenshot2: 'b.png', context1: '产品1背景', context2: '产品2背景' };
  app.useComparisonDataset({ task_id: 'source', items: [item] });
  for (const id of disabled) {
    app.selectedEvaluationProfile.value = id;
    assert.equal(app.canSubmit.value, false, `${id} blocks new-task submission`);
    const before = requests.length;
    await app.submit();
    assert.equal(requests.length, before, 'disabled selection never reaches preflight or evaluation');
    assert.match(app.runError.value, /暂不可选/);
    app.useComparisonDataset({ task_id: 'source', items: [item] });
    assert.equal(app.selectedEvaluationProfile.value, ids[0], 'reusing data replaces a disabled new-task selection');
  }
  for (const id of enabledIds) {
    app.selectedEvaluationProfile.value = id;
    assert.equal(app.canSubmit.value, true, `${id} remains usable`);
    await app.submit();
    assert.equal(submitted.evaluation_profile, id);
    app.useComparisonDataset({ task_id: 'source', items: [item] });
    assert.equal(app.selectedEvaluationProfile.value, id, 'reusing data preserves a valid chosen standard');
  }

  for (const id of disabled) {
    history = { task_id: 'historical', mode: 'compare', status: 'done',
      evaluation_profile: id, protocol_manifest: { id, bundle_revision: 'frozen-revision' },
      items: [item], results: [] };
    app.runError.value = '';
    await app.loadHistoryTask('historical');
    assert.equal(app.runError.value, '');
    assert.equal(app.taskEvaluationProfile.value, id, 'frozen result identity remains the original disabled standard');
    assert.equal(app.taskProtocolManifest.value.bundle_revision, 'frozen-revision');
    assert.equal(app.selectedEvaluationProfile.value, ids[0], 'new-task selector falls back when loading disabled history');
    assert.equal(app.canSubmit.value, true, 'historical data can be reevaluated under an available standard');
    app.selectedEvaluationProfile.value = id;
    await app.retryFailedCases([0]);
    const retry = requests.at(-1);
    assert.equal(retry.url, '/api/eval/historical/retries', 'disabled new-task selection does not block frozen-task retries');
    assert.equal(JSON.parse(retry.options.body).evaluation_profile, undefined, 'retry does not replace the frozen protocol');
  }
  console.log('Prompt availability UI: visible disabled options, stable grouping, selectable defaults, blocked new submissions and frozen-history retries passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
