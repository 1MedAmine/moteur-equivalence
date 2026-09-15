# -*- coding: utf-8 -*-
"""Téléchargement sûr d'un PDF Web puis extraction B2 partagée."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import requests

from extraction_pdf import PDFExtraction, extract_pdf
from scraping import PageContent


class WebPDFScraper:
    def __init__(
        self,
        *,
        session=None,
        timeout: float = 25.0,
        extractor: Callable[[Path], PDFExtraction] = extract_pdf,
    ) -> None:
        self.session = session or requests
        self.timeout = timeout
        self.extractor = extractor

    def scrape(self, url: str) -> PageContent:
        response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        response.raise_for_status()
        with tempfile.TemporaryDirectory(prefix="b2-pdf-") as folder:
            path = Path(folder).resolve() / "document.pdf"
            path.write_bytes(response.content)
            extraction = self.extractor(path)
        return PageContent(
            url=url,
            content=extraction.text,
            title=extraction.title,
            mode="pdf",
            warnings=extraction.warnings,
        )
