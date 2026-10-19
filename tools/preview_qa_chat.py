"""Render synthetic UI and Excel evidence; never connect to a judge or real runs.

Run from the repository root with the workspace interpreter. Playwright and its
Chromium browser are needed for screenshots; no application settings are changed.
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from playwright.sync_api import sync_playwright

from auto_eval.judges.compare_protocols import list_compare_protocols
from auto_eval.web.history import build_xlsx
from auto_eval.web.human_baseline_import import build_human_template
from auto_eval.web.runner import _summarize
from auto_eval.web.tasks import Task


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "docs" / "examples" / "qa-chat"
    output.mkdir(parents=True, exist_ok=True)
    helpers = runpy.run_path(str(root / "tests" / "test_qa_chat_runtime.py"))
    snapshot = helpers["snapshot"]([helpers["sample_result"](scores=(5, 3, None))])
    row = snapshot["results"][0]
    row.update(item_id="one", query=snapshot["items"][0]["query"], rationale="合成界面样例：各产品与维度独立保存。")
    row["answer2_personalization_score"] = None
    row["answer2_personalization_score_status"] = "not_applicable"
    row["answer2_personalization_applicable"] = False
    row["answer2_personalization_verification_status"] = "not_required"
    row["answer2_personalization_reason"] = "当前产品没有相关个性化需要。"
    row["answer3_understanding_reason"] = "产品3缺少当前轮决定性证据，无法核验；产品1/2继续计分。"
    snapshot.update(status="done", created_at=1792022400, dataset_name="合成样例 · 六维独立评分")
    task = Task("chat-synthetic", "compare", snapshot["items"], {},
                evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"],
                results=snapshot["results"])
    snapshot["summary"] = _summarize(task)
    (output / "synthetic-results.xlsx").write_bytes(build_xlsx(snapshot, include_images=False))
    (output / "blank-human-template.xlsx").write_bytes(build_human_template(snapshot, "qa_chat_compare@1.0"))
    (output / "synthetic-input.json").write_text(json.dumps(snapshot["items"], ensure_ascii=False, indent=2), encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root / "src" / "auto_eval" / "web"), **kwargs)

        def do_GET(self):
            if self.path == "/":
                self.path = "/static/index.html"
            if self.path.startswith("/api/"):
                payload = ({"evaluation_profiles": [p.public_metadata() for p in list_compare_protocols()],
                            "judges": [], "judge_model_profiles": []} if self.path.startswith("/api/config") else
                           snapshot if self.path.startswith(("/api/history/chat-synthetic", "/api/datasets/chat-synthetic")) else
                           {"items": [], "total": 0, "entries": [], "running": None})
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
            page = browser.new_page(viewport={"width": 3000, "height": 1100}, device_scale_factor=1)
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            page.wait_for_function("document.querySelector('#app').__vue_app__")
            page.evaluate("document.querySelector('#app').__vue_app__._instance.proxy.loadHistoryTask('chat-synthetic')")
            page.wait_for_function("document.querySelector('.results')?.innerText.includes('个性化适配')")
            page.locator(".result-table-wrap").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "three-products.png"), full_page=False)
            page.get_by_role("button", name="与人工评分对比", exact=True).click()
            page.get_by_label("模板人工标准").wait_for()
            page.locator(".human-panel").screenshot(path=str(output / "human-template.png"))
            page.get_by_role("button", name="收起人工评分对比", exact=True).click()
            page.evaluate("""() => {
                const app=document.querySelector('#app').__vue_app__._instance.proxy;
                app.results[0].product_count=2;
                app.items[0].product_count=2;
            }""")
            page.screenshot(path=str(output / "two-products.png"), full_page=False)
            page.evaluate("""() => {
                const app=document.querySelector('#app').__vue_app__._instance.proxy;
                app.setTaskProtocol({evaluation_profile:'qa_competitor_compare@0.3'});
                app.results[0]={index:0,item_id:'legacy',product_count:2,query:'旧模式合成结果',
                  understanding_applicable:true,answer1_understanding_score:0,answer2_understanding_score:3};
            }""")
            page.screenshot(path=str(output / "legacy-mode.png"), full_page=False)
            browser.close()
            if failures:
                raise RuntimeError("UI errors: " + "; ".join(failures))
    finally:
        server.shutdown()
        server.server_close()
    print(output)


if __name__ == "__main__":
    main()
