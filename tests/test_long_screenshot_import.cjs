// Exercise the actual Vue setup/import/submit functions without a browser/network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function check(kind, count) {
  let app;
  let submitted;
  const item = { id: "case", query: "query", product_count: count };
  for (let n = 1; n <= count; n++) {
    item[`${kind}${n}`] = `data/product${n}.${kind === "screenshot" ? "png" : "mp4"}`;
    item[`answer${n}`] = `answer${n}`;
    item[`context${n}`] = `context${n}`;
  }
  item.evidence_mode = kind === "screenshot" ? "long_screenshot" : "video_frames";
  const sandbox = {
    ref: value => ({ value }),
    computed: getter => ({ get value() { return getter(); } }),
    onMounted() {}, onUnmounted() {}, nextTick: async () => {},
    createApp(options) { app = options.setup(); return { mount() {} }; },
    console: { log() {}, warn() {}, error() {} },
    alert(message) { throw new Error(message); },
    async fetch(url, options) {
      if (url === "/api/parse") return { ok: true, json: async () => ({ items: [item], errors: [] }) };
      assert.equal(url, "/api/eval");
      submitted = JSON.parse(options.body);
      // Stop before task streaming; only input serialization is under test.
      return { ok: false, json: async () => ({ detail: "test complete" }) };
    },
  };
  const source = fs.readFileSync(path.join(__dirname, "../src/auto_eval/web/static/app.js"), "utf8");
  vm.runInNewContext(source.replace(/^import[^\n]+\n/, ""), sandbox);
  app.mode.value = "compare";
  await app.onOpManifestFile({ target: { value: "manifest.jsonl", files: [{ name: "manifest.jsonl", text: async () => JSON.stringify(item) }] } });
  assert.equal(app.canSubmit.value, true);
  await app.submit();
  assert.equal(submitted.items.length, 1);
  const actual = submitted.items[0];
  assert.equal(actual.product_count, count);
  for (let n = 1; n <= count; n++) {
    assert.equal(actual[`${kind}${n}`], item[`${kind}${n}`]);
    assert.equal(actual[`answer${n}`], item[`answer${n}`]);
    assert.equal(actual[`context${n}`], item[`context${n}`]);
    assert.equal(actual[`${kind === "screenshot" ? "video" : "screenshot"}${n}`], undefined);
  }
  if (count === 2) assert.equal(actual[`${kind}3`], undefined);
}

(async () => {
  for (const kind of ["screenshot", "video"]) {
    for (const count of [2, 3]) await check(kind, count);
  }
  console.log("4 JSONL import/submit scenarios passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
