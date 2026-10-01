# pdf2epub

把 PDF（含扫描版）转成 EPUB。

这个项目的前提很简单：**脚本不该去猜内容**。

MinerU 解析得不错，但总有些段落它自己也没把握（形近字、串列、公式符号）。判断这些
地方对不对，需要看原页面、理解上下文——这是 LLM 的活。把内容转写成 EPUB 的章节结构
同样是 LLM 的活。脚本该干的是搬运、截图、打包、校验这些机械事。

所以这里**没有** Markdown→XHTML 转换器，没有章节切分启发式，没有元数据推断，
没有确定性修复引擎。写那些代码的回报远不如把话语权交给 LLM。

---

## 四个阶段

| 阶段 | 谁干活 | 产出 |
| --- | --- | --- |
| `prepare` | 脚本 | 解析 PDF → 调 MinerU → 解压产物（原样保存，不加工） |
| `calibrate` | **LLM** | 对着页面图校准低分段落，直接编辑 `calibrate/source.md` |
| `compose` | **LLM** | 直接写 `build/book.json` 与 `build/OEBPS/text/*.xhtml` |
| `check` | 脚本 | 生成 OPF/nav/container → 打包 → 自检 + EPUBCheck → 出报告 |

脚本只做搬运、截图、打包、报告。**判断和转写全部交给 LLM。**

> 要真正跑一轮、或者你就是那个被停下来干活的 Agent，
> 看 [`docs/agent-guide.md`](docs/agent-guide.md)：三个关卡上具体该怎么干，
> 以及已经踩过的坑。

---

## 使用

```bash
pip install -e ".[render]"     # render 提供页面渲染，强烈建议装

export MINERU_TOKEN=...        # 或写进 pdf2epub.toml

pdf2epub doctor                 # 环境自检
pdf2epub init book.pdf
pdf2epub run
```

`run` 会在需要 LLM 时停下，并以**退出码 3** 结束：

```
$ pdf2epub run
...
轮到你了：校准
------------------------------------------------
  工单：    .pdf2epub/runs/<id>/calibrate/report.md
  要改的：  .pdf2epub/runs/<id>/calibrate/source.md
  页面图：  .pdf2epub/runs/<id>/calibrate/pages
```

于是循环变成：

```bash
pdf2epub run                    # → 3，停在校准
pdf2epub show calibrate         # 读工单
#  对着 pages/page-0007.png 把 source.md 里认错的地方改对
pdf2epub run                    # → 3，停在撰写
pdf2epub show compose           # 读撰写说明
#  写 build/book.json 与 build/OEBPS/text/*.xhtml
pdf2epub run                    # → 0，打包 + 校验通过，出书
```

每一步都可以反复跑：已完成的阶段不会重来，做得不对的阶段会重新派工单。

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功，产物通过校验 |
| 1 | 失败，看 `pdf2epub alerts` |
| 2 | 跑完了但有告警（含"已接受不合格产物"） |
| 3 | **轮到 LLM 了**，按工单干活后重跑 `run` |

退出码只看**这一次运行**产生的告警，不会因为历史告警把一次干净的收尾变成 2。

### 命令

| 命令 | 用途 |
| --- | --- |
| `init <pdf>` | 登记一个 PDF，创建运行 |
| `run` | 跑流水线；需要 LLM 时停下并给出工单 |
| `done [calibrate\|compose]` | 表态"这一步干完了"（校准阶段也可用它跳过） |
| `show <calibrate\|compose\|check>` | 直接打印工单，省得去翻文件路径 |
| `status` | 现在停在哪、下一步干嘛 |
| `list` | 有哪些运行 |
| `alerts` | 告警历史 |
| `doctor` | 环境自检 |
| `clean` | 删运行记录 |

加 `--json` 拿到机器可读输出；加 `--accept` 接受一份不合格的产物收尾（会留下告警）。

---

## 校准阶段：脚本给 LLM 看什么

脚本从 MinerU 的产物里把带置信度的段落捞出来（`content_list` 优先，缺分数就用
`model.json` 的版面框按 IoU 配对；schema 完全不认识时递归找"有分数又有文本"的节点），
低于 `calibrate.score_threshold` 的进工单：

```
.calibrate/
├── report.md          ← 工单：哪些段落、哪一页、原文是什么、上下文长什么样
├── segments.json      ← 机器可读的同一份清单
├── source.md          ← ★ LLM 直接编辑这个
├── pages/page-0007.png ← 相关页截图（含前后各一页的上下文）
└── images/            ← MinerU 抽出的图，供对照
```

报告里写死了几条规矩：只改识别错的地方、不要重写文案、看图确认没认错的就保持原样、
实在认不出的写进 `notes.md` 而不是硬猜。

没有低分段落时不会拿工单去烦 LLM，直接进入下一阶段。

---

## 撰写阶段：LLM 直接写 EPUB

脚本准备好材料，剩下全是 LLM 的事：

```
build/
├── BRIEF.md                  ← 撰写说明
├── book.json                 ← ★ 书目 + 阅读顺序
└── OEBPS/
    ├── text/*.xhtml          ← ★ 章节正文
    ├── images/               ← 脚本已搬好的图
    └── style/base.css        ← 样式
```

章节 XHTML 只有三条硬要求：良构 XML、根元素带 XHTML 命名空间、`<head><title>` 与
`<body>` 齐全。**`content.opf` / `nav.xhtml` / `META-INF/container.xml` 不用 LLM 管**——
打包时脚本从 `book.json` 与目录扫描生成。zip 结构、mimetype、media-type 同理。

`book.json` 里不确定的字段留空即可，脚本不因为空值报错。

---

## 校验阶段：只报告，不修复

脚本跑自检 + EPUBCheck，把结果写成 `check/report.md`：

```markdown
### `OEBPS/text/ch003.xhtml`

**RSC-007** (ERROR) — `OEBPS/text/ch003.xhtml:42`

引用的资源不存在：../images/fig-12.png

```text
    39 |     <p>如下图：</p>
    40 |     <figure>
>>  42 |       <img src="../images/fig-12.png" alt="图 12"/>
    43 |       <figcaption>图 12　系统架构</figcaption>
```

改完再跑一次 `pdf2epub run` 即可。

**这里只有报告，没有修复器。** 改文本是 LLM 的事——脚本定位得准就够了。
包内文件层面的问题（`nav` 书签指向非 spine 项、`identifier` 不是合法 UUID）由脚本
自己保证，不该让 LLM 操心。

确实改不动的，用 `pdf2epub done --accept` 收尾：产物照样保留，同时留下明确告警，
**绝不静默当成功**。

---

## 运行目录

```
.pdf2epub/runs/<run_id>/
├── run.json               # 状态机，断点续跑的真相源
├── input.pdf              # 输入副本
├── logs/pipeline.jsonl
├── alerts.json            # 告警历史（同一条告警只累计次数，不刷屏）
├── mineru/
│   ├── zips/              # 官方 API 返回的原始 zip
│   └── extracted/         # 解压后的产物，原样保留
├── calibrate/             # 校准工单 + 可编辑工作稿 + 页面图
├── build/                 # LLM 写的 EPUB 内容 + 脚本生成的包内文件
├── check/                 # 校验报告
└── output/<name>.epub
```

---

## 配置

优先级（后者覆盖前者）：

```
内置默认值 < .env < pdf2epub.toml < 真实环境变量 < 命令行覆盖
```

`.env` 放在当前工作目录（项目根），专门用来放密钥，**会被自动加载**：

```ini
MINERU_TOKEN=sk-...
```

它只补空缺：已经有同名环境变量时不覆盖，所以临时
`$env:MINERU_TOKEN=...` 依然优先。值按字面使用，不做 `%TEMP%` 之类的变量展开。

其余配置写在 `pdf2epub.toml`，也能用 `PDF2EPUB__SECTION__KEY` 形式的环境变量覆盖
（如 `PDF2EPUB__CALIBRATE__SCORE_THRESHOLD=0.8`）。完整清单见仓库里的
[`pdf2epub.toml`](pdf2epub.toml)，常用的几个：

```toml
[mineru]
token_env = "MINERU_TOKEN"     # Token 从哪个环境变量读
model_version = "vlm"          # pipeline | vlm | MinerU-HTML

[calibrate]
score_threshold = 0.75         # 低于此置信度的段落进校准清单
render_dpi = 140               # 页面图清晰度

[validate]
fail_on_severity = "ERROR"     # FATAL | ERROR | WARNING
epubcheck_mode = "auto"        # auto | require | off
```

---

## 环境要求

- Python 3.10+
- MinerU Token：<https://mineru.net/apiManage/token>，写进 `.env`
- **页面渲染**（`pypdfium2` + `Pillow`）：装了才能做多模态校准；不装会退化成
  纯文本校准并告警
- **EPUBCheck**（需要 Java）：<https://github.com/w3c/epubcheck/releases>
- **`latex2mathml`**（可选，撰写阶段用）：`pip install latex2mathml`。
  只有在用 [`scripts/compose_mathml.py`](scripts/compose_mathml.py) 把公式转
  MathML 时才需要，流水线本身不依赖它

EPUBCheck 不需要配环境变量——解压到 `tools/` 下就会被自动发现：

```
tools/epubcheck-5.1.0/epubcheck.jar
```

也可以放别处用 `EPUBCHECK_JAR` 指路。没装也能跑，只剩自检这一道，并且会明确告警
（`check.json` 里 `epubcheck_ran: false`），不会把"只跑了一道"说成"跑过了"。

`pdf2epub doctor` 会把这几项逐条查一遍。

---

## 开发

```bash
pip install -e ".[render,dev]"
pytest
```

测试用伪造的 MinerU 产物跑通整条链路，不需要联网、不需要 Token。

### 代码地图

```
src/pdf2epub/
├── cli.py           命令行
├── pipeline.py      四阶段调度与"谁来干活"的边界
├── mineru_client.py MinerU 官方 API 客户端
├── ingest.py        PDF 剖析与超限切分
├── bundle.py        产物解压与定位（不做任何加工）
├── segments.py      从产物里抽出带置信度的段落
├── tasks.py         给 LLM 的两个工单
├── pageimage.py     PDF 页 -> 图片
├── epubpack.py      book.json + 目录树 -> OPF/nav/container -> zip
├── formatcheck.py   自检 + EPUBCheck + 校验报告
├── selflint.py      EPUB 结构自检
├── epubcheck.py     外部 EPUBCheck 调用
├── runstate.py      运行状态与断点续跑
└── alerts.py        告警出口

scripts/
└── compose_mathml.py  示例：把 source.md 写成章节（公式转 MathML）

docs/
└── agent-guide.md     三个关卡上具体怎么干活 + 踩过的坑
```

`scripts/` 里的东西**不是流水线的一部分**，是顺手工具。流水线坚持"撰写是 LLM
的活"，所以那里没有 Markdown→XHTML 转换器；但把机械部分固化下来能省掉每次手敲
几万字，也避免重新踩 `\binom` 那个坑。
