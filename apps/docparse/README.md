# apps/docparse — S5 文档解析

**当前状态**：TXT / CSV / XLSX / XML 四类格式已用**标准库**实现真抽取并可跑通；
PDF 文本层已通过可选 `[pdf]` extra（pypdfium2）接入并输出字符级坐标；
扫描件 OCR 仍是桩（结论与许可证评估见下文「PDF 依赖评估」）。
所有能力通过 `Protocol` 暴露，替换实现不需要改后续阶段。

## 管线

```
detect_file_kind → TextExtractor → RuleBasedClassifier → RuleBasedFieldMapper
                 → score_header / score_lines（置信度） → validate_consistency（自洽校验） → 状态判定
```

| 模块 | 作用 |
|---|---|
| `models.py` | 输出契约：`ParseResult` / `DocHeader` / `DocLine` / `FieldValue` / `Coordinate` |
| `detection.py` | 按魔数 + 扩展名识别文件类型；白名单外直接拒绝（FR-401） |
| `extractors/base.py` | `TextExtractor` 协议；`TextLine.cells` 保留表格列位；近似 bbox 估算 |
| `extractors/plaintext.py` | TXT：编码探测、`\f` 与「第 N 页 / 共 M 页」分页 |
| `extractors/csv.py` | CSV/TSV：引号、BOM、分隔符与编码探测 |
| `extractors/xlsx.py` | XLSX：`zipfile` + `ElementTree` 读单元格、保留列位、合并单元格、日期样式 |
| `extractors/xml.py` | XML：标签语义归一、重复行元素还原表格、拒绝 DTD/实体 |
| `mapping/classifier.py` | 单据分类（对账单/送货单/发票/领料单），置信度 < 0.8 判为未知 |
| `mapping/fields.py` | 标签字典取单据头、表格列头取明细；只取显式值；数值/日期归一化 |
| `scoring.py` | 上下文一致性校正置信度（算术、表头合计），输出低置信字段清单 |
| `validation.py` | 明细合计 vs 小计、小计+税额 vs 价税合计 |
| `pipeline.py` | 编排、降级与状态判定 |

## 已真实现的能力与边界

| 格式 | 已做 | 有意不做的（避免编造） |
|---|---|---|
| TXT | 多栏对齐（制表符 / 2 个以上空格切列，单元格内单空格保留）、千分位、货币符号、全角数字、括号负数 `(1,234.50)`、尾随负号 `123.00-`、Unicode 负号 | 不按坐标推断列（纯文本没有坐标），不猜缺失列 |
| CSV/TSV | 引号内换行/逗号/分号、BOM（UTF-8/16/32）、UTF-8 → GB18030 → UTF-16 解码、分隔符按「命中行数」判定 | 不做类型推断，不做多表拼接 |
| XLSX | 按 `<c r="D7">` 还原列位（空单元格不塌陷）、合并单元格只留左上角值不跨列复制、日期序列号按 `styles.xml` 的 numFmt 还原 ISO 日期、一个工作表 = 一页、多工作表按顺序拼接 | 不解析公式（取缓存值）、不解析图表/批注、无表头的工作表直接判为无明细 |
| XML | 命名空间/大小写/下划线归一（`invoiceNo`=`InvoiceNumber`）、重复行元素还原成「表头 + 数据行」、属性与子节点都支持、缺列留空 | 不按 schema 强校验；含 `<!DOCTYPE>`/`<!ENTITY>` 的文档直接拒绝（实体膨胀防护） |

表格类格式（XLSX/CSV/XML 明细行）统一走 `TextLine.cells`，字段映射按**列号**取值，
所以「合并单元格」「缺列」「行尾空单元格」都不会把后面的列顶上来。

## 置信度打分表

| 依据类型 | 场景 | 分值 |
|---|---|---|
| 字段来源 | 表头标签在行首 | 0.98 |
| 字段来源 | 表头标签嵌在其它文本中 | 0.90 |
| 正则命中强度 | 干净数字（纯数字 / 规范千分位 / 货币符号） | 0.92 |
| 正则命中强度 | 需归一化才能读（全角数字、括号负数、尾随负号、Unicode 负号、不规范千分位） | 0.78 |
| 上下文一致性 | 行内 数量 × 单价 ≈ 金额 / 表头 小计 + 税额 ≈ 价税合计 | 各 +0.02（上限 0.99） |
| 上下文一致性 | 上述关系不等 | 金额/合计压到 0.70 |
| 上下文一致性 | 单元格数多于表头列数（错位） | 整行 × 0.80 + `ROW_TOKENS_MISALIGNED` |
| 推导 | 金额由数量 × 单价推导 | 0.88，`source=derived` |

低于 **0.85** 进入 `low_confidence_fields`，状态降为 `needs_review`，不得自动入账（FR-401）。
分档常量与依据同源于 `mapping/fields.py` 的 `SCORING_RUBRIC`，改分档必须同步本表。

## PDF 依赖评估（已接入 pypdfium2，可选 extra）

**结论：标准库做不出可用于对账的 PDF 文本层，必须引一个第三方库；已选 `pypdfium2`
（BSD-3-Clause / Apache-2.0，PDFium 内核）做成 `[pdf]` 可选 extra，核心安装保持零 PDF 依赖。**
`PyMuPDF` 是 AGPL-3.0（或商业授权），闭源交付给制造企业会在客户尽调被卡，本流不采用。

### 四项对比（实测环境：Python 3.12.12，2026-11-11）

| 维度 | `pypdfium2` 5.15.2（推荐） | `pdfminer.six` 20260107（备选） | `PyMuPDF`（否决） |
|---|---|---|---|
| 许可证 | BSD-3-Clause + Apache-2.0；内置 PDFium 为 BSD-3-Clause，无传递依赖 | MIT；传递依赖 charset-normalizer(MIT)、cryptography(Apache-2.0 OR BSD-3)、cffi(MIT)、pycparser(BSD-3) | AGPL-3.0 或商业授权 |
| 中文文本层 | `textpage.get_text_range()` 整页文本，`\r\n` 分行 | `extract_pages()` → `LTTextLine` / `LTChar`，中文正常 | 能力最强，但许可证否决 |
| 字符级 bbox | `get_charbox(i)` 逐字符框（PDF 坐标，原点左下，需转左上） | `LTChar.x0/y0/x1/y1`（同样原点左下） | — |
| 体积 | wheel 3.83 MiB（manylinux_2_17_x86_64）/ 3.99 MiB（win_amd64）；安装 8.14 MiB / 93 文件，自带 PDFium 动态库 | wheel 6.3 MiB + cryptography 3.6 MiB；安装约 19.6 MiB（pdfminer 8.16 + cryptography 9.98 + charset-normalizer 0.59 + cffi 0.59 + pycparser 0.25） | — |
| 离线随镜像预装 | PyPI 提供 win_amd64 与 manylinux_2_17_x86_64 预编译 wheel，构建期 `pip download` 进 wheelhouse 即可，运行期不联网 | 纯 Python wheel + cryptography 二进制 wheel，同样可离线，但多 4 个传递依赖 | — |

选 `pypdfium2` 的理由：许可证干净、wheel 自带 PDFium 不需要系统库、无传递依赖、
字符框来自实际渲染度量；`pdfminer.six` 作为纯 Python 兜底（目标架构没有 pypdfium2
预编译 wheel 时），二者都满足「不引 AGPL」。

### 实测证据（自造中文发票 PDF）

样本：Type0 / Identity-H + CIDFontType2 + ToUnicode CMap，内容流里是 CID 而非汉字，
共 7 行（标题 / 发票号码 / 开票日期 / 销售方 / 价税合计 / 表头 / 明细）。
脚本在仓库外一次性运行，**未安装进项目 `.venv`，未改 `pyproject.toml` / `uv.lock`**。

| 库 | 抽出的文本 | 字符级坐标（`增` / 发票号码首位 `8`） |
|---|---|---|
| `pypdfium2` | `增值税专用发票\r\n发票号码: 88231007\r\n开票日期: 2026-04-01\r\n销售方名称: 蓝票科技有限公司\r\n价税合计: 113.00\r\n…` | `增` = (50.77, 788.33, 66.52, 804.90)；`8` = (122.52, 750.10, 127.29, 758.41) |
| `pdfminer.six` | 同上，逐行 `增值税专用发票` / `发票号码: 88231007` / … | `增` = (50.00, 787.84, 68.00, 805.84)；`8` = (122.00, 748.56, 134.00, 760.56) |
| 标准库（对照） | 同一内容流只能拿到 `<00010002>` 字形码，还原不出「蓝票」 | 无法定位到字符 |

说明：样本未嵌入字体，PDFium 按替代字体度量给出字宽（12pt 字宽约 15.75pt），
`pdfminer.six` 严格按 `/W` 声明值（12pt 步进）；真实发票嵌入了子集字体时两者都以
嵌入度量为准。两者都能稳定给出「字段值 → 字符框」的映射，满足点选回链；
字段级准确率仍需真实脱敏发票回归（本轮不做）。

### 接入与降级（已实现）

- 安装：`pip install -e ".[pdf]"`；对应 `pyproject.toml` 的
  `[project.optional-dependencies].pdf = ["pypdfium2>=5"]`。核心安装与 `.[dev]`
  都不含 PDF 依赖，doc 镜像按需安装。
- 抽取：`extractors/pdf.py` 逐页 `get_text_range()` 取文本、`get_charbox()` 取逐字符框，
  把 PDF 左下原点换算成契约的左上原点，写入 `TextLine.char_boxes`；
  `line_bbox()` 优先用真实字符框，字段坐标因此是字符级真实坐标，不是按字宽估算。
- 未装 extra：抛 `PdfSupportUnavailableError`，管线降级为 `unrecognized`，消息为
  「缺少 PDF 支持：未安装 pypdfium2，请安装 `[pdf]` extra（pip install -e ".[pdf]"）」。
- 无文本层（扫描件）：抛 `PdfTextLayerMissingError`，消息明确「需 OCR；当前未接入
  OCR 能力（PaddleOCR PP-Structure）」，不返回空文本冒充解析成功。
- 扫描件 OCR 接入后，替换 `ImageOcrExtractor` 或在构造管线时注入 OCR 抽取器即可，
  分类 / 映射 / 置信度 / 校验阶段不变。
- `docs/product/tech/01-技术架构.md` 第 518 行仍写 PyMuPDF，属共享文档，需架构流同步。

## 契约要点

- 每个字段：`value + confidence + coords[] + raw_text + source`；`source` 可区分 `rule/derived/ocr/llm`。
- **缺失 ≠ 零**：未识别字段 `value=None` 且 `confidence=0.0`（模型层强校验）；金额 `0` 仍是合法值。
- 低置信阈值 0.85（FR-401/405），分类阈值 0.80。
- 明细金额只在「数量 + 单价都有、金额为空」时推导，标记 `source=derived` 并输出 `AMOUNT_DERIVED` 警告。
- 一致性不一致时保留原值，输出 `SUBTOTAL_MISMATCH` / `TOTAL_MISMATCH` / `LINE_AMOUNT_MISMATCH`，状态降为 `needs_review`。
- 关键字段缺失或没有明细行 → `status=unrecognized`，`missing_fields` 列出字段路径与中文名。
- 表格行错位时保留原值并降置信，不重排列、不猜列。

## 不在本次范围

病毒扫描、主数据 Top3 匹配（FR-403）、重复单据检测（FR-406）、单据草稿写入（FR-405）、
异步任务队列与 `erp_doc` 容器入口。

## 离线夹具

`fixtures/docparse/` 全部为虚构样例，不含真实客户数据：

| 文件 | 覆盖的脏形态 |
|---|---|
| `clear_invoice.txt` | 清晰发票（`\|` 分隔，含税额） |
| `missing_fields_delivery_note.txt` | 缺供应商的送货单 → 缺失字段列清单 |
| `multipage_statement.txt` | 两页对账单（页脚页码） |
| `amount_mismatch_invoice.txt` | 小计不一致 → `SUBTOTAL_MISMATCH` |
| `aligned_statement.txt` | 多栏空格对齐、千分位、单元格内单空格 |
| `dirty_fullwidth_invoice.txt` | 全角数字 `１，０００` → 低置信 → `needs_review` |
| `misaligned_rows.txt` | 明细行多一列（错位）→ `ROW_TOKENS_MISALIGNED` |
| `quoted_statement.csv` | BOM + 分号分隔 + 引号内含 `;` 与 `,` |
| `invoice.xml` | 命名空间、重复 `<Item>`、表头 + 明细 + 合计 |
| `messy_invoice.xlsx` | 合并标题/表头、纵向合并、日期序列号、两个工作表拼接 |
| `missing_header.xlsx` | 只有数据没有表头 → `unrecognized` |
| `invoice_zh.pdf` | 中文发票 PDF（Type0/Identity-H + ToUnicode），验证字段与字符级坐标 |
| `scanned_no_text.pdf` | 无文本层 PDF（模拟扫描件）→ 明确降级「需 OCR」 |

模糊扫描件与真实客户 PDF 不在离线夹具内（前者需要 OCR，后者涉及脱敏）。
