import hashlib
import io
import json
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image, PngImagePlugin

from auto_eval.web import history, server
from auto_eval.web.tasks import _task_from_snapshot


NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "etc": "http://www.wps.cn/officeDocument/2017/etCustomData",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def make_image(path, *, fmt="PNG", mode="RGB", size=(96, 800), color=None, metadata=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new(mode, size, color=color) as image:
        image.save(path, format=fmt, **({"pnginfo": metadata} if metadata else {}))
    return path.read_bytes()


def screenshot_item(paths, *, index=0, prepared=True):
    item = {"id": "重复题号", "query": f"Query {index}", "product_count": len(paths)}
    for n, path in enumerate(paths, 1):
        item[f"screenshot{n}"] = str(path)
        if prepared:
            item["evidence_mode"] = "long_screenshot"
            item[f"screenshot_meta{n}"] = {
                "original_path": str(path), "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "split_status": "risky", "split_count": 3,
                "slices": [{"path": "must-not-export-model-slice.png"}],
            }
            item[f"frames{n}"] = ["must-not-export-model-slice.png"]
    return item


def snapshot(items):
    return {
        "mode": "compare", "items": items,
        # 结果完成顺序不应影响原图按输入顺序排列，失败题也要保留图片。
        "results": [{"index": i, "error": "provider failed"} for i in reversed(range(len(items)))],
    }


def workbook(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    sheets = {}
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
    for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
        target = targets[sheet.attrib[f"{{{NS['r']}}}id"]]
        sheets[sheet.attrib["name"]] = ET.fromstring(archive.read(f"xl/{target}"))
    return archive, sheets


def cell_text(cell):
    return "".join(cell.itertext())


def headers(sheet):
    return [cell_text(c) for c in sheet.findall("s:sheetData/s:row", NS)[0]]


def embedded_cells(archive, sheet):
    """从工作表公式沿图片 ID、关系文件解析到二进制，验证整条 XLSX 引用链。"""
    rels = ET.fromstring(archive.read("xl/_rels/cellimages.xml.rels"))
    targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
    images = {}
    for pic in ET.fromstring(archive.read("xl/cellimages.xml")).findall("etc:cellImage/xdr:pic", NS):
        name = pic.find("xdr:nvPicPr/xdr:cNvPr", NS).attrib["name"]
        rel_id = pic.find("xdr:blipFill/a:blip", NS).attrib[f"{{{NS['r']}}}embed"]
        images[name] = archive.read(f"xl/{targets[rel_id]}")
    cells = {}
    for cell in sheet.findall("s:sheetData/s:row/s:c", NS):
        formula = cell.find("s:f", NS)
        if formula is not None:
            match = re.fullmatch(r'_xlfn.DISPIMG\("(ID_[A-F0-9]{32})",1\)', formula.text)
            assert match and cell.attrib["t"] == "str"
            assert cell.find("s:v", NS).text == "=" + formula.text.removeprefix("_xlfn.")
            cells[cell.attrib["r"]] = images[match[1]]
    return cells


def assert_valid_package(archive):
    assert archive.testzip() is None
    names = archive.namelist()
    assert len(names) == len(set(names))
    types = ET.fromstring(archive.read("[Content_Types].xml"))
    extensions = {n.attrib["Extension"] for n in types.findall("ct:Default", NS)}
    overrides = {n.attrib["PartName"] for n in types.findall("ct:Override", NS)}
    for name in names:
        if name.endswith((".xml", ".rels")):
            ET.fromstring(archive.read(name))
        if name != "[Content_Types].xml":
            assert name.rsplit(".", 1)[-1] in extensions or f"/{name}" in overrides
        if name.endswith(".rels"):
            rels = ET.fromstring(archive.read(name))
            ids = [r.attrib["Id"] for r in rels]
            assert len(ids) == len(set(ids))
            directory = "" if name == "_rels/.rels" else name.split("/_rels/")[0]
            for rel in rels:
                assert rel.attrib.get("TargetMode") != "External"
                target = posixpath.normpath(posixpath.join(directory, rel.attrib["Target"]))
                assert target in names


def test_question_original_immediately_after_query_in_result_sheets(tmp_path, monkeypatch):
    original = tmp_path / "question.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (120, 80), "red").save(original, exif=exif)
    raw = original.read_bytes()
    model_view = tmp_path / "model-view.png"
    make_image(model_view, size=(80, 120), color="blue")
    answer = tmp_path / "answer.png"
    make_image(answer, color="green")
    items = [screenshot_item([answer, answer], index=i) for i in range(3)]
    items[1].update(query_images=[str(original)], query_image_meta=[{
        "original_path": str(original), "original_sha256": hashlib.sha256(raw).hexdigest(),
        "path": str(model_view), "query_image_id": "QI1",
    }])
    items[2].update(query_images=["missing.png"], query_image_meta=[{"original_path": str(tmp_path / "missing.png")}])
    before = snapshot(items)
    unchanged = json.dumps(before)
    for method in ("save", "resize", "convert", "crop"):
        monkeypatch.setattr(Image.Image, method, lambda *a, **k: pytest.fail("必须嵌入原文件，不能重编码"))
    archive, sheets = workbook(history.build_xlsx(before))
    for name, query_header in (("数据集明细", "query"), ("逐题结果", "题目"), ("原始长截图", "query")):
        sheet = sheets[name]
        labels = headers(sheet)
        column = labels.index(query_header) + 1
        assert labels[column] == "输入图片原图"
        rows = sheet.findall("s:sheetData/s:row", NS)
        assert len(rows) == 4
        assert cell_text(rows[1][column]) == ""  # Text-only first row does not move later images.
        assert "原图文件缺失" in cell_text(rows[3][column])
        assert embedded_cells(archive, sheet)[f"{history._col(column + 1)}3"] == raw
        if name != "原始长截图":
            assert rows[2].attrib["ht"] == "96"
            assert "ht" not in rows[1].attrib
    media = [archive.read(name) for name in archive.namelist() if name.startswith("xl/media/")]
    assert media.count(raw) == 1  # All sheets share one byte-identical embedded original.
    assert model_view.read_bytes() not in media
    assert json.dumps(before) == unchanged
    assert all("输入图片原图" not in row for row in history.export_rows(before)["逐题结果"])
    assert_valid_package(archive)


@pytest.mark.parametrize("count", [2, 3])
def test_one_query_per_row_product_columns_and_originals_only(tmp_path, monkeypatch, count):
    items, expected = [], {}
    for i in range(2):
        paths = []
        for n in range(count):
            # 故意重名文件和题号，不能按文件名或题号错误去重。
            path = tmp_path / str(i) / str(n) / "原图.png"
            raw = make_image(path, color=(i * 80, n * 80, 30))
            paths.append(path)
            expected[f"{chr(68 + n)}{i + 2}"] = raw
        items.append(screenshot_item(paths, index=i))
    original_snapshot = snapshot(items)
    before = json.dumps(original_snapshot)

    def forbidden(*args, **kwargs):
        pytest.fail("导出不得缩放、重编码或切图")

    for method in ("save", "resize", "convert", "crop"):
        monkeypatch.setattr(Image.Image, method, forbidden)
    archive, sheets = workbook(history.build_xlsx(original_snapshot))
    sheet = sheets["原始长截图"]
    assert headers(sheet) == ["数据集序号", "id", "query"] + [f"产品{n}原图" for n in range(1, count + 1)]
    rows = sheet.findall("s:sheetData/s:row", NS)
    assert len(rows) == 3
    assert [cell_text(row[2]) for row in rows[1:]] == ["Query 0", "Query 1"]
    assert all(float(row.attrib["ht"]) <= 409 for row in rows[1:])
    assert embedded_cells(archive, sheet) == expected
    assert len([n for n in archive.namelist() if n.startswith("xl/media/")]) == len(expected)
    assert sheet.find("s:mergeCells", NS) is None
    assert sheet.find("s:sheetViews/s:sheetView/s:pane", NS).attrib["topLeftCell"] == "D2"
    if count == 2:
        assert not any("产品3" in h for h in headers(sheets["逐题结果"]))
    assert json.dumps(original_snapshot) == before
    assert_valid_package(archive)


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("JPEG", "CMYK"), ("WEBP", "RGB")])
def test_original_formats_bytes_and_dimensions_preserved(tmp_path, fmt, mode):
    # 实际 MIME 由文件内容识别，不使用可能错误的扩展名。
    path = tmp_path / "misnamed.png"
    raw = make_image(path, fmt=fmt, mode=mode)
    archive, sheets = workbook(history.build_xlsx(snapshot([screenshot_item([path, path])])))
    assert embedded_cells(archive, sheets["原始长截图"]) == {"D2": raw, "E2": raw}
    assert len([n for n in archive.namelist() if n.startswith("xl/media/")]) == 1
    extent = ET.fromstring(archive.read("xl/cellimages.xml")).find(".//a:ext", NS)
    assert extent.attrib == {"cx": str(96 * 9525), "cy": str(800 * 9525)}
    types = ET.fromstring(archive.read("[Content_Types].xml"))
    expected_mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    assert any(n.attrib.get("ContentType") == expected_mime for n in types)
    assert_valid_package(archive)


@pytest.mark.parametrize("limit", ["pixels", "bytes"])
def test_excel_uses_complete_original_even_above_model_limits(tmp_path, limit):
    info = PngImagePlugin.PngInfo()
    if limit == "bytes":
        info.add_text("original_metadata", "x" * (10 * 1024 * 1024))
    size = (1080, 20000) if limit == "pixels" else (96, 800)
    path = tmp_path / "long.png"
    raw = make_image(path, size=size, metadata=info)
    archive, sheets = workbook(history.build_xlsx(snapshot([screenshot_item([path, path])])))
    assert embedded_cells(archive, sheets["原始长截图"])["D2"] == raw
    assert len(sheets["原始长截图"].findall("s:sheetData/s:row", NS)) == 2
    with Image.open(io.BytesIO(archive.read("xl/media/original_image1.png"))) as image:
        assert image.size == size


@pytest.mark.parametrize("problem,message", [
    ("missing", "原图文件缺失"), ("changed", "原图已变更，未嵌入"),
    ("corrupt", "原图损坏或无法识别"), ("unsupported", "原图不是静态 PNG/JPEG/WebP"),
])
def test_unavailable_original_never_replaced_by_model_slice(tmp_path, problem, message):
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    make_image(first)
    raw = make_image(second, color="red")
    item = screenshot_item([first, second])
    if problem == "missing":
        first.unlink()
    elif problem == "changed":
        make_image(first, color="blue")
    elif problem == "corrupt":
        first.write_bytes(b"bad png")
        item["screenshot_meta1"].pop("original_sha256")
    else:
        make_image(first, fmt="BMP")
        item["screenshot_meta1"].pop("original_sha256")
    # 即使切片文件存在，也不能代替缺失或变化的原图。
    item["frames1"] = [str(second)]
    archive, sheets = workbook(history.build_xlsx(snapshot([item])))
    assert embedded_cells(archive, sheets["原始长截图"]) == {"E2": raw}
    assert message in cell_text(sheets["原始长截图"])
    assert "逐题结果" in sheets
    assert_valid_package(archive)


def test_missing_all_originals_still_keeps_rows_without_dangling_relationships(tmp_path):
    item = screenshot_item([tmp_path / "a.png", tmp_path / "b.png"], prepared=False)
    archive, sheets = workbook(history.build_xlsx(snapshot([item])))
    assert "原始长截图" in sheets
    assert not any("cellimages" in name or "/media/" in name for name in archive.namelist())
    assert "原图文件缺失" in cell_text(sheets["原始长截图"])
    assert_valid_package(archive)


def test_history_source_paths_relative_to_project_and_mixed_product_counts(tmp_path, monkeypatch):
    paths = [tmp_path / "data" / f"{n}.png" for n in range(3)]
    originals = [make_image(p, color=(n * 80, 30, 30)) for n, p in enumerate(paths)]
    source = {f"screenshot{n + 1}": p.relative_to(tmp_path).as_posix() for n, p in enumerate(paths)}
    items = [
        {"id": "old", "query": "historical", "source_data": source},
        {"id": "video", "query": "video", "video1": "a.mp4", "video2": "b.mp4"},
        screenshot_item(paths[:2], index=2, prepared=False),
    ]
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    archive, sheets = workbook(history.build_xlsx(snapshot(items)))
    cells = embedded_cells(archive, sheets["原始长截图"])
    assert cells == {"D2": originals[0], "E2": originals[1], "F2": originals[2], "D4": originals[0], "E4": originals[1]}
    assert len(sheets["原始长截图"].findall("s:sheetData/s:row", NS)) == 4
    assert "录屏模式，无原始长截图" in cell_text(sheets["原始长截图"])
    assert_valid_package(archive)


def test_input_text_cannot_become_dispimg_or_other_formula(tmp_path):
    path = tmp_path / "a.png"
    make_image(path)
    item = screenshot_item([path, path])
    item["query"] = '=DISPIMG("UNTRUSTED",1) & <Query>'
    archive, sheets = workbook(history.build_xlsx(snapshot([item])))
    cell = sheets["原始长截图"].find('s:sheetData/s:row/s:c[@r="C2"]', NS)
    assert cell.attrib["t"] == "inlineStr"
    assert cell_text(cell) == item["query"]
    assert cell.find("s:f", NS) is None
    assert embedded_cells(archive, sheets["原始长截图"])


@pytest.mark.parametrize("mode", ["compare", "rich_content", "operation"])
def test_old_video_exports_do_not_add_image_parts(mode):
    data = {"mode": mode, "items": [{"id": "v", "query": "q", "video1": "a.mp4", "video2": "b.mp4"}], "results": []}
    archive, sheets = workbook(history.build_xlsx(data))
    assert "原始长截图" not in sheets
    assert not any("cellimages" in name or "/media/" in name for name in archive.namelist())
    assert len(ET.fromstring(archive.read("xl/styles.xml")).find("s:cellXfs", NS)) == 2
    assert_valid_package(archive)


@pytest.mark.asyncio
async def test_existing_export_endpoint_includes_original_images(tmp_path, monkeypatch):
    from urllib.parse import unquote

    path = tmp_path / "a.png"
    raw = make_image(path)
    data = snapshot([screenshot_item([path, path])])
    data["dataset_name"] = "测试数据.jsonl"
    async def peek(_id):
        return _task_from_snapshot(data, "task")
    monkeypatch.setattr(server, "peek_task_async", peek)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path)
    response = await server.api_export("task", "xlsx")
    assert response.status_code == 200
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert unquote(response.headers["content-disposition"]).endswith("测试数据_模型测评结果.xlsx")
    archive, sheets = workbook(Path(response.path).read_bytes())
    assert embedded_cells(archive, sheets["原始长截图"]) == {"D2": raw, "E2": raw}
    await response.background()
    assert not Path(response.path).exists()
