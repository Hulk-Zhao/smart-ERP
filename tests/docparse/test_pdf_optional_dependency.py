"""未安装 ``[pdf]`` extra 时的降级：必须提示安装方式，不得静默失败。

本模块不 import pypdfium2，未装 extra 的 CI 也能跑。
"""

from __future__ import annotations

import sys

import pytest

from apps.docparse import DocParsePipeline, DocType, ParseStatus
from apps.docparse.errors import PdfSupportUnavailableError
from apps.docparse.extractors import PdfTextExtractor


def _hide_pdfium(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules 里置 None 后，下一次 import 会抛 ImportError，模拟未装 extra
    monkeypatch.setitem(sys.modules, "pypdfium2", None)


def test_extractor_reports_missing_extra(monkeypatch):
    _hide_pdfium(monkeypatch)

    with pytest.raises(PdfSupportUnavailableError) as info:
        PdfTextExtractor().extract(b"%PDF-1.4\n")

    message = str(info.value)
    assert "缺少 PDF 支持" in message
    assert "[pdf]" in message


def test_pipeline_degrades_with_install_hint(tmp_path, monkeypatch):
    _hide_pdfium(monkeypatch)
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% no text layer\n")

    result = DocParsePipeline().parse_file(pdf)

    assert result.status is ParseStatus.UNRECOGNIZED
    assert result.doc_type is DocType.UNKNOWN
    assert "[pdf]" in result.message
