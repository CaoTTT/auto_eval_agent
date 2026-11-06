const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  let app;
  let submitted;
  const raw = ["first", "rejected", "third"].map((id, index) => ({
    id,
    query: `question ${index}`,
    product_count: 2,
    screenshot1: `${id}-1.png`,
    screenshot2: `${id}-2.png`,
    context1: "context 1",
    context2: "context 2",
  }));
  class FakeStream {
    addEventListener() {}
    close() {}
  }
  const sandbox = {
    ref: value => ({ value }),
    computed: getter => ({ get value() { return getter(); } }),
    onMounted() {},
    onUnmounted() {},
    nextTick: async () => {},
    AbortController,
    FormData,
    console,
    alert(message) { throw new Error(message); },
    createApp(options) {
      app = options.setup();
      return { mount() {} };
    },
    EventSource: FakeStream,
    async fetch(url, options) {
      if (url === "/api/eval") {
        submitted = JSON.parse(options.body);
        return {
          ok: true,
          json: async () => ({
            task_id: "partial-task",
            status: "queued",
            queue_position: 1,
            queue_position_note: "",
            accepted_indexes: [0, 2],
            accepted_count: 2,
            rejected_count: 1,
            rejected_items: [{
              index: 1,
              position: 2,
              id: "rejected",
              error: "screenshot1 and video1 are missing",
            }],
            evaluation_profile: "current-standard",
            protocol_manifest: {},
            judge_runtime: {},
            judge_product_order_note: "original order",
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    },
  };

  const strip = code => code.replace(/^import[^\n]*\n/gm, "").replace(/^export /gm, "");
  vm.runInNewContext(
    strip(fs.readFileSync(path.join(__dirname, "../src/auto_eval/web/static/compare-cases.js"), "utf8")),
    sandbox,
  );
  vm.runInNewContext(
    strip(fs.readFileSync(path.join(__dirname, "../src/auto_eval/web/static/app.js"), "utf8")),
    sandbox,
  );
  app.mode.value = "compare";
  app.selectedEvaluationProfile.value = "current-standard";
  app.useComparisonDataset({ task_id: "source", dataset_name: "mixed.jsonl", items: raw });
  await app.submit();

  assert.equal(submitted.items.length, 3);
  assert.deepEqual(app.items.value.map(item => item.id), ["first", "third"]);
  assert.equal(app.total.value, 2);
  assert.deepEqual(Object.keys(app.itemProgress.value), ["0", "1"]);
  assert.equal(app.errors.value.length, 1);
  assert.match(app.errors.value[0], /Case rejected/);
  assert.match(app.queueNotice.value, /已跳过 1 条/);
  console.log("Partial eval UI filtering and rejection notice passed");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
