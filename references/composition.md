# 撰写（compose）

目标：把校准后的 `source.md` 转写成 EPUB 章节。**切章决策由 LLM 做，机械转换
交给脚本**；长书必须分章撰写、逐章打包校验。

## 交付物

```
<run>/build/
├── BRIEF.md          ← 撰写说明
├── book.json         ← 书目 + 阅读顺序（模板已就位）
└── OEBPS/
    ├── text/         ← ★ 章节正文写在这里
    ├── images/       ← 脚本搬好的图
    └── style/base.css
```

## 撰写脚本

小书可以用一条命令成稿：

```bash
python scripts/compose_mathml.py --run <run 目录> \
    --title "书名" --author "作者"
```

它完成机械部分：标题分级、段落/列表、`$$…$$` 与 `$…$` 转 MathML、公式编号
右对齐、转义、`book.json`。**需要 `pip install latex2mathml`。**
注意它会清空 `text/` 一次性重写——长书按章分批手写更稳。

按 `#` 分章、有前置页（书名页/版权/前言/印刷目录）的书，使用
[`scripts/compose_book.py`](../scripts/compose_book.py)：它不猜测边界，读取
`build/compose-plan.json`（撰写者写好的行区间、标题、目录层级），其余机械
工作照做（表格、插图、代码清单、印刷目录、多级导航锚点）：

```bash
python scripts/compose_book.py --run .pdf2epub/runs/<id> --audit   # 先看审计，不写文件
python scripts/compose_book.py --run .pdf2epub/runs/<id>           # 写章节 + book.json
```

它是示例脚本而非流水线的一步——章节怎么切、标题叫什么，由撰写者决定并复核。
脚本执行完**必须检查**：

- 切出的章数是否正确（标题层级启发式按特定后端产物调整过；换后端先复核层级）
- 标题是否被截断或粘连
- 印刷目录的页码是否仍与原书对应

## 章节 XHTML 的硬要求

只有三条：良构 XML、根元素带 XHTML 命名空间、有 `<head><title>` 与 `<body>`。
`content.opf` / `nav.xhtml` / `META-INF/container.xml` **无需撰写者处理**，
打包时自动生成。

**公式必须是合法 MathML。** 仅仅"XML 良构"不够——`<msup>` 这类元素只允许
固定个数的子元素，超出后 XML 仍可解析，但 EPUBCheck 会判 RSC-005。
`scripts/compose_mathml.py` 的 `bad_structure()` 就是拦截这类问题的。
已知陷阱（`\binom` 非法结构、HTML 实体产生裸 `&`、假标签、`\limits` 退化、
寄存器 `$` 与公式 `$…$` 冲突）的成因与处理见
[pitfalls.md](pitfalls.md) 教训 3 与教训 6。

## 撰写规矩

- **一章一个 xhtml**，写完一章即 `pdf2epub run` 打包校验一次，通过后再写
  下一章；不要在一条回复里输出整本书。
- 校准阶段换图的表格：按 `calibrate/table-images.json` 把对应 `<table>`
  替换为 `<img>`，**表题保留为正文**（可搜索）。
- 单元格内的字面 `\n` 转成 `<br/>`。
- 校准 `notes.md` 里的存疑项（认不出、原书笔误）保持原样，不要"顺手修正"。
