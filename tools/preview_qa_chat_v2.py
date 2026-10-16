"""Render V2 UI/export examples using synthetic rows, without a judge call."""
from __future__ import annotations

import json
import runpy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

from auto_eval.judges.compare_protocols import list_compare_protocols
from auto_eval.web.history import build_xlsx
from auto_eval.web.human_baseline_import import build_human_template
from auto_eval.web.runner import _summarize
from auto_eval.web.tasks import Task


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "docs" / "examples" / "qa-chat-v2"
    output.mkdir(parents=True, exist_ok=True)
    helpers = runpy.run_path(str(root / "tests" / "test_qa_chat_v2_runtime.py"))
    snapshot = helpers["v2_snapshot"](3)
    row = snapshot["results"][0]
    row.update(query=snapshot["items"][0]["query"], needs_human_review=True,
               review_reasons=["合成样例：响应Gate无法判断、部分核验，其他有效分仍参与统计"])
    row.update(answer2_personalization_applicable=False,
               answer2_personalization_verification_status="not_required",
               answer2_personalization_score_status="not_applicable", answer2_personalization_score=None)
    row["personalization_reason"] = "产品1：使用了明确的用户偏好，5分；产品2：不适用，没有相关个性化需求且未使用个人信息；产品3：按已知条件回应，3分。"
    row["personalization_rank_groups"] = []
    row.update(answer3_understanding_verification_status="unverifiable",
               answer3_understanding_score_status="unverifiable", answer3_understanding_score=None)
    row["understanding_reason"] = "产品1：T01第1块上部准确承接了用户想聊天的需求，5分；产品2：T01第1块回应基本贴合，4分；产品3：无法核验，缺少关键指代的历史。"
    row["understanding_rank_groups"] = []
    snapshot.update(status="done", created_at=1792112400, dataset_name="V2合成样例 · 共享理由与独立状态")
    task = Task(snapshot["task_id"], "compare", snapshot["items"], {},
                evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"],
                results=snapshot["results"])
    snapshot["summary"] = _summarize(task)
    (output / "synthetic-results.xlsx").write_bytes(build_xlsx(snapshot, include_images=False))
    (output / "blank-human-template.xlsx").write_bytes(build_human_template(snapshot, "qa_chat_compare@1.1"))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root / "src" / "auto_eval" / "web"), **kwargs)

        def do_GET(self):
            if self.path == "/":
                self.path = "/static/index.html"
            if self.path.startswith("/api/"):
                payload = ({"evaluation_profiles": [p.public_metadata() for p in list_compare_protocols()],
                            "judges": [], "judge_model_profiles": []} if self.path.startswith("/api/config") else
                           snapshot if self.path.startswith(("/api/history/v2-export", "/api/datasets/v2-export")) else
                           {"items": [], "total": 0, "entries": [], "running": None})
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            page.wait_for_function("document.querySelector('#app').__vue_app__")
            page.evaluate("document.querySelector('#app').__vue_app__._instance.proxy.loadHistoryTask('v2-export')")
            page.wait_for_function("document.querySelector('.results')?.innerText.includes('个性化适配')")
            page.locator(".result-table-wrap").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "three-products.png"))
            # The expanded text also verifies that the shared container is visibly
            # labeled rather than passed off as one product's private evidence.
            score_cell = page.locator(".result-table-wrap td").filter(has=page.locator(".reason-toggle")).nth(2)
            score_cell.locator(".reason-toggle").click()
            score_cell.screenshot(path=str(output / "shared-reason.png"))
            page.get_by_role("button", name="与人工评分对比", exact=True).click()
            page.get_by_label("模板人工标准").wait_for()
            page.locator(".human-panel").screenshot(path=str(output / "human-template.png"))
            page.get_by_role("button", name="收起人工评分对比", exact=True).click()
            page.evaluate("""() => {
                const app = document.querySelector('#app').__vue_app__._instance.proxy;
                app.results[0].product_count = 2;
                app.items[0].product_count = 2;
            }""")
            page.screenshot(path=str(output / "two-products.png"))
            browser.close()
            if errors:
                raise RuntimeError("UI errors: " + "; ".join(errors))
    finally:
        server.shutdown()
        server.server_close()
    print(output)


if __name__ == "__main__":
    main()
