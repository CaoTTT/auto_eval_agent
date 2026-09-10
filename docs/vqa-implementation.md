# 图文混合对比评测：首版实现

实现分支：`feat/vqa-evaluation`，基于方案 v1.1。支持非空文字问题和可选一张静态 PNG/JPEG/WebP 提问图，复用 2/3 产品 compare。

## 使用

在统一“垂域视觉对比”入口中导入 JSONL 或编辑题卡。每题可上传提问图、填写授权本地路径、替换或移除图片。产品回答仍提供同题同类的录屏或长截图；不同题的回答证据类型可不同。

```jsonl
{"id":"text-video","query":"介绍这款产品","video1":"data/a.mp4","video2":"data/b.mp4"}
{"id":"image-video","query":"图中有几个红色方块？","query_images":["data/question.png"],"video1":"data/a.mp4","video2":"data/b.mp4"}
{"id":"text-shot","query":"介绍这款产品","screenshot1":"data/a.png","screenshot2":"data/b.png"}
{"id":"image-shot","query":"图中有几个红色方块？","query_images":["data/question.png"],"screenshot1":"data/a.png","screenshot2":"data/b.png"}
```

示例路径需替换为真实文件。`query_images` 缺省或 `[]` 表示文字题；`null`、字符串、空路径、多图及显式题型冲突会报错。题图打不开或超预算是该题技术失败，不产生产品零分，也不阻止同批其他题。

任务级选用 `qa_competitor_compare@0.2-simplified`（1–5 分，bundle `0.2.1`）或 `qa_competitor_compare@0.3`（0–3 分，bundle `0.3.1`）。新任务冻结标准、实现版本、视觉策略和所选裁判的模型/采样参数。评分维度、Gate、准确性不汇总、总分与整体胜负置空等规则沿用原标准。

## 图片与运行语义

- 题图以独立 QI1 角色发送一次；视频帧和截图保持产品归属。题图计入整包图片数、字节及保守 token 预算，不占某产品的抽帧额度。
- 原文件固化在 `runs/query_images/`，方向正常时原字节发送；EXIF 旋转保存独立 PNG 模型视图。原图、模型视图及请求输入均记录 SHA-256。
- `POST /api/upload/query-image` 接收文件。`GET /api/query-images/{image_id}` 只访问已登记图片；`?original=true` 下载原文件。没有任意路径文件预览接口。
- 本地路径默认限项目目录和 runs。外部题图目录配置在 `config/visual_modes/rich_content.yaml` 的 `query_images.allowed_roots`；回答视频/截图继续沿用 `OPERATION_VIDEO_ROOTS`。
- 配置中的数量、尺寸、上下文和请求体预算是本地保守限制，不代表特定厂商的承诺或精确计费；使用真实模型前应按实际网关核实。
- 补跑重新判断，复用固化题图而不缓存评分。同 ID 更新是整条替换；已有图文题必须显式传 `query_images`，移除用 `[]`，并清除旧结果。运行中的图文任务暂不接受更新，避免不同图片版本并发覆盖。
- 旧 bundle `0.2.0/0.3.0` 可继续文字题重跑；向旧任务新增题图须新建任务。多题 compare 不注入前题总结。已有 rich_content 多轮链路保持原行为。
- 题图独立于任务运行内存保留；目前不自动回收无引用图像文件，历史删除不会误删其他引用的图片。

## 回放与导出

当任务包含题图时，XLSX 的“逐题结果”（题目列）、“数据集明细”和“原始长截图”（query 列）均在 Query 后紧邻插入“输入图片原图”列。单元格复用 WPS DISPIMG，嵌入固化原文件的完整字节，保留原始格式、分辨率和 EXIF，不使用模型派生图或缩略图；支持 WPS 的原图查看交互。文字题对应单元格留空；图片缺失或哈希变化则显示原因。CSV 和纯文字任务的列序保持不变。

逐题结果和历史记录提供“输入图片与回答长截图”图片区：QI1 与产品1/2/3分开展示，每张图片均有查看原图和单独下载入口；长截图在卡片内上下滚动。提问图编辑区也可单独下载原图。长截图预览/下载使用 `/api/eval/{task_id}/items/{item_index}/screenshots/{product_no}`（下载加 `?download=true`），按任务、题号和产品取完整原文件；授权目录校验、文件类型及原图哈希检查失败时不展示替代图片。录屏题不显示长截图入口。

补充验证：112 项相关 Python 回归通过，全部 4 个 Node 前端脚本通过；浏览器使用合成题图和两张长截图实测并列预览、滚动与下载入口。HTTP 测试验证下载为完整原文件字节，变更原图返回 409。

页面恢复题图、逐题展示预览，并提供文字/图文筛选和同口径子集汇总。CSV/评分表仅在包含题图时向末尾追加题图信息，旧纯文字评分列不变。XLSX 增加“提问图片清单”和“提问图片”，通过原有 WPS DISPIMG 嵌入原文件；其他阅读器可使用路径/hash 清单及 ZIP。ZIP 中题图放在每题 `query/` 下，原图与模型视图分开，缺失或变更写入清单而不冒充原文件。

## 验证与边界

新增 `tests/test_vqa_input.py` 和 `tests/test_vqa_ui.cjs`：覆盖 16 个基础组合、实际 HTTP 消息组装与 trace、损坏/动画/超限、EXIF、冻结参数、取消、缓存帧下题图准备、同 ID 替换、历史恢复、输入指纹、分组分母、上传、受控预览及 XLSX/ZIP 原图字节。

2026-09-10 验证结果：全量 pytest 226 通过、2 跳过（本地缺少 ffmpeg/ffprobe，真实视频媒体测试未运行）；全部 4 个 Node 前端回归脚本通过。浏览器已实测统一入口、题型切换、合成 PNG 上传与图片预览；`git diff --check` 通过。录屏调度、缓存、角色化消息和既有非媒体回归通过 mock 验证，不能将其描述为本机实际抽帧验证。

```powershell
$env:PYTHONPATH = 'C:\workspace\auto_eval_vqa\src'
& C:\workspace\venv\Scripts\python.exe -m pytest -q
node tests/test_vqa_ui.cjs
```

启动本工作树（需要本地现有模型配置与凭证）：

```powershell
& C:\workspace\venv\Scripts\python.exe -m uvicorn auto_eval.web.server:app --app-dir C:\workspace\auto_eval_vqa\src --host 127.0.0.1 --port 8057
```

首版不支持多图、纯图无文字、text_only 产品回答、动态图片/PDF、任意 URL 抓图，也不新增上游手机采集自动化。验证使用合成图片及 mock 模型；真实模型网关集成和人工一致性校准尚未执行，不能据此宣称裁判准确性已验证。
