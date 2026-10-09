---
name: pdf2epub
description: "Convert scanned or image-only PDFs (books, textbooks, theses) into high-fidelity, EPUBCheck-passing EPUB files. Drives the pdf2epub pipeline: PaddleOCR-VL cloud OCR for recognition, LLM-driven calibration against page images, LLM-authored EPUB chapters, and scripted packaging and validation. Use when the user provides a PDF, especially a scan without a text layer, and wants an EPUB or other reflowable e-book conversion. Requires a Baidu AI Studio token (PADDLE_TOKEN); PDF pages are uploaded to Baidu Cloud OCR for recognition. Optionally uses Java with EPUBCheck for official validation. Long-running: large books pause multiple times with exit code 3 for LLM calibration and composition work."
---

# PDF → EPUB 高保真转换

本仓库根目录即 skill：代码、脚本与操作知识同仓，安装后即可驱动。
分工：**脚本只做搬运、截图、打包、校验；内容判断与转写由 LLM 完成**——
本文件与 `references/` 提供的正是 LLM 侧的操作知识。

## 开始前

1. 安装并自检：

   ```bash
   pip install -e ".[render]"      # render 提供页面渲染，校准必需
   pdf2epub doctor                 # 逐项检查 Token / 渲染 / EPUBCheck
   ```

2. 缺 `PADDLE_TOKEN` 时**停下向用户索要**（在
   [AI Studio](https://aistudio.baidu.com/account/apikey) 创建），写入 `.env`。
   不要编造密钥，不要把密钥回显到对话里。
3. 启动前向用户确认三件事，不要跳过：
   - **隐私**：PDF 内容会上传百度云 OCR 识别；
   - **成本与时长**：按页数估算分片（20 页/片）与校准批次（50~100 页/批），
     大书会多次以退出码 3 暂停等待接力，可能耗时数小时；
   - **版权**：仅对已合法拥有副本的书籍做格式转换。
4. 未装 Java/EPUBCheck 时流水线仍可运行，但收尾必须**显式告知用户"产物未通过
   官方校验"**，不得静默宣称成功。

## 主循环

```bash
pdf2epub init book.pdf
pdf2epub run
```

`run` 不会一次性跑完。按退出码分支处理：

| 退出码 | 含义 | 动作 |
| --- | --- | --- |
| 0 | 成功 | 报告产物路径 `output/<name>.epub`，建议用户在阅读器中实际翻阅 |
| 1 | 失败 | `pdf2epub alerts` 查看原因，修复后重跑 |
| 2 | 有告警 | 逐条查看；若含"已接受不合格产物"，向用户说明接受了什么、为什么 |
| 3 | **等待 LLM** | `pdf2epub show <stage>` 读工单，按下表加载对应参考干活，完成后重跑 `run` |

## 停在关卡时读什么

详细操作在 **`references/`** 下，按需加载对应文件，不要一次全读：

| 停留位置 | 加载 | 干什么 |
| --- | --- | --- |
| calibrate | [references/calibration.md](references/calibration.md) | 接缝核对 → `qc_scan.py` 体检 → 表格对图/换图 → 书签核对 → 编辑 `calibrate/source.md` |
| compose | [references/composition.md](references/composition.md) | 写 `compose-plan.json` 切章 → 撰写 `OEBPS/text/*.xhtml` 与 `book.json` |
| check 报错 | [references/validation.md](references/validation.md) | 修正文层问题（内容、引用、MathML）；包内文件问题归脚本 |
| 遇到反常现象 | [references/pitfalls.md](references/pitfalls.md) | 9 类坑的成因与处理，先查再动手 |

## scripts/ 工具箱

`scripts/` 下的脚本不属于流水线，是固化了实践经验的独立工具，`--help` 查看用法：

| 脚本 | 功能 | 使用时机 |
| --- | --- | --- |
| `split_pdf.py` | 按书签/页数/页段切 PDF，出切分计划与清单 | 校准前摸清章节边界；需要分批作业时 |
| `submit_jobs.py` | 分批提交解析任务：逐个落盘 job_id、被拒退避重试 | 大书 prepare 被限流/拒绝之后（见 pitfalls 教训 8） |
| `fetch_jobs.py` | 按 `jobs.json` 接续取回已提交的解析任务 | 同上 |
| `qc_scan.py` | OCR 体检：页级索引 + 六类全局检测，出 `qc-report.md` | **校准第一步**，先运行再动手 |
| `table_tool.py` | 表格台账 / 结构体检 / 对图核对清单 / 裁图换图 | 校准的表格专项（见 calibration.md） |
| `toc_check.py` | 原书书签 vs 解析产物逐条核对，出 `toc-check.md` | 原书带书签时，校准前运行 |
| `compose_mathml.py` | `source.md` → 章节 XHTML（公式转 MathML） | 小书一键成稿（见 composition.md） |
| `compose_book.py` | 按 `compose-plan.json` 切章组书 | 分章撰写（见 composition.md） |

## 长文档策略

- 解析：脚本自动按 20 页分片（片小并行快，接缝才核对得过来）；prepare 收尾
  自动做交界处校验（页数对账 + 接缝正文检查，结论在 `parsing/boundary.md`，
  有问题发 `BOUNDARY_CHECK` 告警）。**接缝两页是校准必做项。**
- 校准与撰写：按 50~100 页或一章一批，一批完成再做下一批；不要试图一次处理
  整本，也不要在一条回复里输出整本书。
- 章节边界不明时先 `python scripts/split_pdf.py "book.pdf" --list`；切出的
  分片**不要**再喂给 `pdf2epub init`——那会生成几个互不相干的 run，合不成一本书。
- 大书解析任务用 `scripts/submit_jobs.py` 逐批提交并落盘 job_id（一次性全量
  提交会被服务端限流/拒绝，且已提交任务无法接续）。

## 硬性规矩

- **不静默成功**：改不动就 `pdf2epub done --accept --note "原因"` 收尾并留下
  告警；绝不删除内容来让校验通过。
- **只修识别错误**：不重写文案、不调风格；图上确认解析无误的保持原样；
  原书自带笔误不是 OCR 错误，保持原样。
- **改动留痕**：校准阶段的每次修改记录进 `calibrate/notes.md`（改了什么、依据）。

## 收尾检查清单

- [ ] `pdf2epub doctor` 全绿（Token、页面渲染、EPUBCheck）
- [ ] 书厚时确实按页段/章节分批完成，没有一次性处理整本
- [ ] 解析：`parsing/boundary.md` 页数对账无问题，`BOUNDARY_CHECK` 告警已查看
- [ ] 校准：每道缝的两页对照页面图核过，接缝衔接正常
- [ ] 校准：`qc_scan.py` 已运行，可疑处逐条对图确认，`notes.md` 已记录
- [ ] 校准：表格对图核过（`table_tool.py check` + `pages`），读不动的已换图
- [ ] 校准：原书带书签时 `toc_check.py` 已运行，缺失/粘连的标题已处理
- [ ] 撰写：章节切分复核过，标题正确，公式是合法 MathML
- [ ] 校验：`check/report.md` 中 FATAL/ERROR 为 0
- [ ] 产物在真实阅读器中翻阅过：目录可跳转、公式可渲染
- [ ] 包内文件层面的反复问题，回到 `src/pdf2epub/` 修复而不是手工绕过
