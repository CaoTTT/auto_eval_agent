const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');

async function main() {
  let app;
  const requests = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
    ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
    onMounted() {}, onUnmounted() {}, nextTick() {}, console,
    window: {}, document: {},
    createApp(options) {app = options.setup(); return {mount() {}};},
    async fetch(url, options) {
      requests.push({url, body: JSON.parse(options.body)});
      // The rejected start lets the test inspect submission without opening SSE.
      return {ok: false, json: async () => ({detail: 'test submission'})};
    },
  });
  app.mode.value = 'rich_content';
  app.opItems.value = [{id: 'case-one', query: 'test', videoPath: 'recording.mp4'}];
  assert.equal(app.evalTimeout.value, 900);
  await app.submit();
  assert.equal(requests[0].url, '/api/eval');
  assert.equal(requests[0].body.options.eval_timeout_s, 900, 'new task sends the 900-second default');
  app.evalTimeout.value = 420;
  await app.submit();
  assert.equal(requests[1].body.options.eval_timeout_s, 420, 'explicit user timeout remains configurable');
  console.log('Evaluation timeout UI: 900-second default and explicit override submissions passed');
}

main().catch(error => {console.error(error); process.exitCode = 1;});
