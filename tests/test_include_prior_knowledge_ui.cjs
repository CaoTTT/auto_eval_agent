const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const read = (name) =>
  fs.readFileSync(path.join(__dirname, "../src/auto_eval/web/static", name), "utf8");
const strip = (code) =>
  code.replace(/^import[^\n]*\n/gm, "").replace(/^export /gm, "");

async function main() {
  let app;
  let parsed;
  let submitted;
  const sandbox = {
    ref: (value) => ({ value }),
    computed: (fn) => ({ get value() { return fn(); } }),
    onMounted() {},
    onUnmounted() {},
    nextTick() {},
    AbortController,
    FormData,
    console,
    confirm() { return true; },
    alert(message) { throw Error(message); },
    createApp(options) {
      app = options.setup();
      return { mount() {} };
    },
    async fetch(url, options) {
      if (url === "/api/eval") {
        submitted = JSON.parse(options.body);
        return { ok: false, json: async () => ({ detail: "test stop" }) };
      }
      if (url === "/api/parse") {
        return { ok: true, json: async () => parsed };
      }
      if (url === "/api/upload/dataset-source") {
        return { ok: true, json: async () => ({ id: "source" }) };
      }
      throw Error(url);
    },
  };

  vm.runInNewContext(strip(read("compare-cases.js")), sandbox);
  vm.runInNewContext(strip(read("app.js")), sandbox);
  app.mode.value = "compare";
  app.selectedEvaluationProfile.value = "qa_prior";
  parsed = {
    items: [
      {
        id: "prior",
        query: "question",
        product_count: 2,
        evidence_mode: "long_screenshot",
        screenshot1: "one.png",
        screenshot2: "two.png",
        context1: "product one",
        context2: "product two",
        include_prior_knowledge: true,
      },
      {
        id: "no-prior",
        query: "question",
        product_count: 2,
        evidence_mode: "long_screenshot",
        screenshot1: "one.png",
        screenshot2: "two.png",
        context1: "product one",
        context2: "product two",
        include_prior_knowledge: false,
      },
    ],
    errors: [],
  };

  await app.onOpManifestFile({
    target: { files: [{ name: "prior.jsonl", text: async () => "" }], value: "" },
  });
  assert.equal(app.opItems.value[0].includePriorKnowledge, true);
  assert.equal(app.opItems.value[1].includePriorKnowledge, false);

  await app.submit();
  assert.equal(submitted.items[0].include_prior_knowledge, true);
  assert.equal(submitted.items[1].include_prior_knowledge, false);

  console.log("include_prior_knowledge survives JSONL import and evaluation submission");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
