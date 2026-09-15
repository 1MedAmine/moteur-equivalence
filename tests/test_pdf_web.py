# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction_pdf import PDFExtraction
from pdf_web import WebPDFScraper


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(
            content=b"%PDF-1.7 synthetic",
            headers={"content-type": "application/pdf"},
            raise_for_status=lambda: None,
        )


def test_web_pdf_uses_shared_extractor_and_removes_temp_file():
    """Mutation détectée : le PDF Web contourne PyMuPDF/Camelot ou fuit son fichier."""
    session = Session()
    observed_paths = []

    def extractor(path):
        observed_paths.append(Path(path))
        assert Path(path).read_bytes() == b"%PDF-1.7 synthetic"
        return PDFExtraction("Référence REF-PDF 16 A", "Catalogue", ("Camelot: RuntimeError",))

    page = WebPDFScraper(session=session, timeout=25.0, extractor=extractor).scrape(
        "https://maker.example/catalogue.pdf"
    )

    assert page.mode == "pdf"
    assert page.content == "Référence REF-PDF 16 A"
    assert page.title == "Catalogue"
    assert page.warnings == ("Camelot: RuntimeError",)
    assert session.calls[0][1]["timeout"] == 25.0
    assert observed_paths and not observed_paths[0].exists()
