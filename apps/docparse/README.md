# apps/docparse — S5 文档解析（离线骨架）

当前为**可离线运行、无外网依赖**的规则版实现：输出契约、管线骨架、降级策略与测试已就绪，
真实 OCR / LLM / PDF 抽取尚未接入。所有能力都通过 `Protocol` 暴露，替换实现不需要改后续阶段。

## 管线

```
detect_file_kind → TextExtractor → RuleBasedClassifier → RuleBasedFieldMapper
                 → score_lines（置信度） → validate_consistency（自洽校验） → 状态判定
```

| 模块 | 作用 |
|---|---|
| `models.py` | 输出契约：`ParseResult` / `DocHeader` / `DocLine` / `FieldValue` / `Coordinate` |
| `detection.py` | 按魔数 + 扩展名识别文件类型；白名单外直接拒绝（FR-401） |
| `extractors/` | `TextExtractor` 协议；纯文本/CSV 实现；PDF/图片/XLSX 为桩 |
| `mapping/classifier.py` | 单据分类（对账单/送货单/发票/领料单），置信度 < 0.8 判为未知 |
| `mapping/fields.py` | 标签字典取单据头、表格列头取明细；只取显式值 |
| `scoring.py` | 行内「数量×单价≈金额」校正置信度，低置信字段清单 |
| `validation.py` | 明细合计 vs 小计、小计+税额 vs 价税合计 |
| `pipeline.py` | 编排、降级与状态判定 |

## 哪些是桩（后续要换真模型/真组件）

| 能力 | 当前实现 | 需要替换为 |
|---|---|---|
| PDF 文本层 | `PdfTextExtractor` 抛 `ExtractorNotAvailableError`，结果降级 `unrecognized` | PyMuPDF 文本层抽取（技术架构 §1A 选型） |
| 扫描件 / 图片 | `ImageOcrExtractor` 桩 | PaddleOCR PP-Structure 版面分析 + OCR + 表格结构 |
| XLSX | `SpreadsheetExtractor` 桩 | openpyxl 单元格抽取（保留行列坐标） |
| 复杂表格 / 脏版面 | 规则表格列头映射 | VLM 兜底，实现 `FieldMapper` 协议 |
| 分类 | 标题词 + 关键词打分 | 版面特征模型；接口 `DocClassifier` 不变 |
| 坐标 | 按字符偏移估算的近似 bbox（`CHAR_WIDTH=8`、`LINE_HEIGHT=18`） | PDF/OCR 给出的真实页码 + 包围盒 |
| 字段置信度 | 规则分值（0.98/0.92/0.90/0.88）+ 算术校正 | 模型 logits / 多次采样一致性 |

替换方式：

```python
pipeline = DocParsePipeline(extractors={FileKind.PDF: MyPdfExtractor()})
pipeline = DocParsePipeline(mapper=MyVlmMapper())
```

契约、置信度阈值、降级与测试都不需要改。

## 契约要点

- 每个字段：`value + confidence + coords[] + raw_text + source`；`source` 可区分 `rule/derived/ocr/llm`。
- **缺失 ≠ 零**：未识别字段 `value=None` 且 `confidence=0.0`（模型层强校验）；金额 `0` 仍是合法值。
- 低置信阈值 0.85（FR-401/405），分类阈值 0.80。
- 明细金额只在「数量 + 单价都有、金额为空」时推导，标记 `source=derived` 并输出 `AMOUNT_DERIVED` 警告。
- 一致性不一致时保留原值，输出 `SUBTOTAL_MISMATCH` / `TOTAL_MISMATCH` / `LINE_AMOUNT_MISMATCH`，状态降为 `needs_review`。
- 关键字段缺失或没有明细行 → `status=unrecognized`，`missing_fields` 列出字段路径与中文名。

## 不在本次范围

病毒扫描、主数据 Top3 匹配（FR-403）、重复单据检测（FR-406）、单据草稿写入（FR-405）、
异步任务队列与 `erp_doc` 容器入口。

## 离线夹具

`fixtures/docparse/` 下 4 份虚构样例：清晰发票、缺供应商的送货单、两页对账单、小计不一致的发票。
均为纯文本，不含真实客户数据；页面标记按「第 N 页 / 共 M 页」页脚形式识别。
