<div align="center">

<img src="docs/assets/logo.png" width="140" alt="pdf2epub"/>

# pdf2epub

将 PDF（含扫描版）转换为 EPUB。
**脚本负责机械性工作，内容判断与转写交给 LLM。**

解析引擎：[PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR)（云API令牌在百度 [AI Studio](https://aistudio.baidu.com/account/apikey) 获取，每日两万页免费）。

LLM模型：只是基本的文本核对工作，使用DeepSeek V4.1 Flash已经足够。

文本翻译：目前已经支持文本翻译功能，可以输入英文pdf，输出中文epub了！

</div>

## 背景

PDF 的版式为纸面印刷而设计，在小屏幕电纸书（6~7 英寸墨水屏）上阅读体验较差：
字号偏小、版面拥挤、无法随阅读器设置重排；大屏设备虽可阅读，但便携性不足。
EPUB 作为重排格式，更适合小屏阅读设备。因此一条自然的路径是：
**使用 OCR 模型识别 PDF，由 LLM 校准识别结果，再转换为 EPUB。**

本项目是这条链路的完整实现。其中"识别 → 校准 → 转写 → 校验"的流程不绑定目标格式，
其他"将 PDF 转换为其他格式"的需求同样适用。

> 需要说明的是，对于大量表格、公式的情况，虽然大部分都不会出问题，但是与原文不完全一致是难免的，毕竟不是人工核对，LLM覆盖率有限，我们确实可以设计一个覆盖率机制，但那或许会消耗大量token，并且不一定生效。不过准确性是更重要的，我们会在之后的更新中尝试添加这样的覆盖率机制。

## 实际效果

以《计算机体系结构：量化研究方法（第五版）》612 页扫描版为例：原书为整页扫描图像、
无文本层，包含大量跨页表格与公式。前两图为原书扫描页，下图为同一内容转换后在
阅读器中的效果（全书分 31 片解析，成品通过 EPUBCheck 校验，0 错误）：

<p align="center">
  <img src="docs/assets/lhyjff_pdf1.png" width="30%" alt="原书扫描页：表 3-4 与 3.5.1 节"/>
  &nbsp;
  <img src="docs/assets/lhyjff_pdf2.png" width="30%" alt="原书扫描页：表 3-5"/>
  <br/>
  <img src="docs/assets/lhyjff_epub.png" width="66%" alt="转换后在阅读器中的效果"/>
</p>

说明：EPUB 中**部分表格以图片形式呈现**，这是有意的设计——对于难以可靠识别的表格
（列数多、结构错位、单元格含公式），要求 LLM 不强行转换为 HTML，而是直接从原书
截取图像插入，以保证内容不失真；其余表格仍保留为可搜索、可重排的普通表格。

## 更新记录

- **2026-10-14** 接入百度翻译大模型文本翻译 API：`translate_blocks.py` 按行翻译
  校准稿——代码清单、表格、公式与目录页码不进请求，行内代码/公式包成 XML 标签
  由服务端原样保留，行级缓存重跑不重复计费；同一份校准稿可出双语 EPUB
- **2026-10-14** 代码密集书组稿：`compose_codebook.py` 行级判定代码块，没有围栏的
  终端会话、Verilog 清单能正确排成代码；印刷目录与索引单独成条目页；导航标签按
  书语言自适应；`table_row_check.py` 抓"结构完好但说明串位"的表格
- **2026-10-13** 校准工具链固化：`textlayer_diff.py`（原书有文本层时逐页对差）、
  `calibrate_fix.py`（机械修复收敛到单一入口）、`qc_scan.py`（OCR 体检）；
  `ocr_images.py` 支持输入是图片而非 PDF 的场景
- **2026-10-09** 文档按关卡拆分：根目录 `SKILL.md` 驱动，`references/` 下
  calibration / composition / validation / pitfalls 四份手册供人与 agent 按需加载
- **2026-10-08** 表格、书签、OCR 体检脚本固化：`table_tool.py`（表格专项：结构体检、
  对图核对、裁图换图）、`toc_check.py`（书签核对）；实测高频问题清单沉淀进
  `references/calibration.md`
- **2026-10-03** 组稿脚本化：`compose_book.py` 按撰写计划（`compose-plan.json`）
  切章组书，不再让脚本猜章节边界
- **2026-10-02** 解析后端换成 PaddleOCR-VL：20 页分片并行解析 + 交界处自动校验
  （页数对账、接缝正文检查，结论落 `parsing/boundary.md`）
- **2026-10-01** 项目成型：脚本只做搬运、截图、打包、校验；内容判断与转写交给
  LLM，不引入 Markdown→XHTML 转换器与确定性修复引擎；随后修好 `.env` 不生效、
  EPUBCheck 配置语义与 bbox 坐标系识别

## 快速开始

两种用法等价：

- **作为 Agent Skill 安装**（Claude Code、CodeBuddy 等支持 Agent Skills 的
  环境；需先将本仓库推送到 GitHub）：

  ```bash
  npx skills add zhaocake/pdf2epub
  ```

  也可手动 clone 到 agent 的 skills 目录（如 `~/.claude/skills/pdf2epub`）。
- **直接把仓库提供给 AI Agent**：agent 读根目录 [SKILL.md](SKILL.md) 按流程
  驱动；详细操作知识在 [references/](references/)（按关卡拆分的四份手册），
  供人阅读。

以下是基本流程：

```bash
pip install -e ".[render]"       # render 提供页面渲染，建议安装
export PADDLE_TOKEN=...          # 或写入 .env

pdf2epub doctor                  # 环境自检
pdf2epub init book.pdf
pdf2epub run
```

`run` 不会一次性执行完毕：流程推进到需要内容判断的环节时会**暂停并输出工单**
（退出码 3），按照工单完成相应工作后重新执行 `run`，直至退出码为 0：

```text
pdf2epub run    # → 3  停在校准阶段：对照 pages/*.png 修正解析错误
pdf2epub run    # → 3  停在撰写阶段：编写 build/book.json 与 OEBPS/text/*.xhtml
pdf2epub run    # → 0  打包并通过 EPUBCheck，产物输出至 output/<name>.epub
```

已完成的阶段不会重复执行；未通过的阶段会重新生成工单。

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功，产物通过校验 |
| 1 | 失败，详见 `pdf2epub alerts` |
| 2 | 执行完毕但存在告警（包括"已接受不合格产物"） |
| 3 | **等待 LLM 处理**，按工单完成后重新执行 `run` |

## 工作方式

| 阶段 | 执行者 | 产出 |
| --- | --- | --- |
| `prepare` | 脚本 | 切分 PDF → PaddleOCR-VL 解析 → 产物归一化 + 交界处校验 |
| `calibrate` | **LLM** | 对照页面图校准解析结果，直接编辑 `calibrate/source.md` |
| `compose` | **LLM** | 直接编写 `build/book.json` 与 `build/OEBPS/text/*.xhtml` |
| `check` | 脚本 | 生成 OPF/nav/container → 打包 → 自检 + EPUBCheck → 报告 |

设计动机：无论解析后端能力多强，总存在没有把握的段落（形近字、串列、公式符号）。
判断这些内容是否正确需要查看原始页面、理解上下文，这属于 LLM 的工作。
因此本项目**没有**引入 Markdown→XHTML 转换器、章节切分启发式、元数据推断或
确定性修复引擎；脚本只保证三件事：搬运准确、定位精确、**不将不合格产物静默标记为成功**。

各关卡的具体操作方式与已知问题：**[references/](references/)**
（calibration / composition / validation / pitfalls，按关卡加载，必读）。

## 配置

配置优先级：`内置默认 < .env < pdf2epub.toml < 环境变量 < 命令行`。

- 解析服务 Token 写入 `.env`：`PADDLE_TOKEN=...`
- 出译本需要百度翻译凭据，见[「获取百度翻译密钥」](#获取百度翻译密钥)
- 其余选项见 [`pdf2epub.toml`](pdf2epub.toml) 内注释，可通过 `PDF2EPUB__SECTION__KEY`
  形式的环境变量覆盖

### 获取百度翻译密钥

出译本（`scripts/translate_blocks.py`）调用百度翻译的**大模型文本翻译 API**，
需要三样凭据，都在[百度翻译开放平台](https://fanyi-api.baidu.com)的控制台里：

1. 用百度账号登录并注册为开发者；建议完成**个人实名认证**——未认证账号
   只支持 `zh↔en` 且免费额度很小。
2. **管理控制台 → 开发者信息**：拿到 `APPID`（一串数字）与 `密钥`（32 位串）。
3. 控制台的 **API Key 管理**页：创建一个 **API Key**（大模型文本翻译 API
   推荐用它鉴权，与上一步的"密钥"不是同一个东西）。
4. 控制台**开发者服务**：开通"大模型文本翻译 API"（不开通调用会被拒）。
5. 写进项目根目录的 `.env`：

   ```dotenv
   BAIDU_TRANS_APPID=...      # 开发者信息页的 APPID
   BAIDU_TRANS_SECRET=...     # 同页的"密钥"（sign 鉴权用）
   BAIDU_TRANS_API_KEY=...    # API Key 管理页创建的 Key（Bearer 鉴权用）
   ```

免费额度与 QPS 以控制台显示为准。翻译按行缓存（`*.cache.json`），同一行
重跑不重复计费；代码清单、表格、公式与印刷目录的页码不进请求，不会被机翻。

## 环境要求

- Python 3.10+；PaddleOCR Token（在 AI Studio 创建）
- 出译本另需百度翻译凭据（见[「获取百度翻译密钥」](#获取百度翻译密钥)；不出译本不用配）
- 页面渲染依赖 `pypdfium2` + `Pillow`（即 `[render]` extra；未安装时将退化为
  纯文本校准并产生告警）
- EPUBCheck（依赖 Java）：将 jar 解压至 `tools/` 目录即可被自动发现，或通过
  `EPUBCHECK_JAR` 指定路径；未安装时仍可运行，但仅保留自检并明确告警

`pdf2epub doctor` 可逐项检查上述依赖。

## 开发

```bash
pip install -e ".[render,dev]"
pytest        # 基于伪造解析产物跑通全链路，无需联网与 Token
```

常用命令：`init / run / done / show / status / list / alerts / doctor / clean`，
加 `--json` 获取机器可读输出。
