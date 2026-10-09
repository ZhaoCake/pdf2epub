# 校验（check）

脚本执行自检 + EPUBCheck，结果写入 `check/report.md`，精确到文件、行号、
原文片段。**这里只有报告，没有修复器**——修改是 LLM 的工作，脚本负责把问题
定位得足够准。

## 常见问题归属

| 码 | 含义 | 归谁处理 |
| --- | --- | --- |
| `OPF-014` | 用了 MathML 但 manifest 未声明 | **脚本**（`epubpack` 已自动探测） |
| `RSC-005` | XHTML 解析失败 **或** MathML 结构违规 | LLM（修改 `build/` 下的文件） |
| `RSC-006` | 引用了远程资源 | LLM（EPUB 不允许远程资源） |
| `RSC-007` | 引用的资源不存在 | LLM（可能真丢了图，需看上下文决定补图还是改写正文） |
| `OPF-085` | `identifier` 标了 `urn:uuid:` 但不是合法 UUID | **脚本**（已自动纠正） |
| `OPF-003` | manifest 与目录树不一致 | **脚本**（打包时按目录树重建） |

判断标准：**包内文件层面的问题（OPF/nav/container/zip）归脚本，正文层面的问题
（内容、引用、MathML）归 LLM。** 如果某个包内问题反复出现，那是脚本缺陷，应到
`src/pdf2epub/` 修复，而不是每次手工绕过。

## 处理流程

1. 读 `check/report.md`，按严重度排序：FATAL > ERROR > WARNING。
2. 每条 ERROR 修复后重跑 `pdf2epub run`，直到 FATAL/ERROR 为 0。
3. RSC-005（MathML 结构违规）优先怀疑 `<msup>` 类元素子元素超限，而不是 XML
   语法——XML 能解析不代表结构合法（见 [pitfalls.md](pitfalls.md) 教训 3）。
4. 修复不得删除有效内容来"让校验通过"。

## 确实无法修复的

```bash
pdf2epub done --accept --note "原因"
```

产物保留，同时留下明确告警，**不会静默标记为成功**。接受前向用户说明接受了
什么、为什么；用户不同意就继续修。

## 收尾确认

- `check/report.md` 中 FATAL/ERROR 为 0
- 产物能在真实阅读器中打开：目录可跳转、公式可渲染、换图的表格清晰可读
- 若宿主环境未安装 EPUBCheck（`check.json` 里 `epubcheck_ran: false`），
  **必须显式告知用户"产物未通过官方校验"**
