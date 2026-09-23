# -*- coding: utf-8 -*-
"""Extraction PDF B2 : texte rapide puis tableaux ciblés."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction_pdf import PDFExtraction, extract_pdf


class FakePage:
    def __init__(self, text: str, has_table: bool) -> None:
        self.text = text
        self.has_table = has_table
        self.text_calls = 0
        self.table_calls = 0

    def get_text(self, mode: str) -> str:
        assert mode == "text"
        self.text_calls += 1
        return self.text

    def find_tables(self):
        self.table_calls += 1
        return SimpleNamespace(tables=[object()] if self.has_table else [])


class FakeDocument:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        self.metadata = {"title": "Fiche constructeur"}
        self.closed = False

    def __iter__(self):
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    def close(self) -> None:
        self.closed = True


class FakeValues:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    def tolist(self) -> list[list[str]]:
        return self.rows


class FakeCell:
    def __init__(self, x1: float, x2: float, y1: float, y2: float) -> None:
        self.x1 = x1
        self.x2 = x2
        self.y1 = y1
        self.y2 = y2


def test_pymupdf_scans_every_page_and_camelot_runs_once(tmp_path):
    """Mutation détectée : Camelot reparcourt tout le PDF ou une fois par page."""
    pages = [FakePage("Introduction", False), FakePage("Références", True), FakePage("Dimensions", True)]
    document = FakeDocument(pages)
    calls = []

    result = extract_pdf(
        tmp_path / "fiche.pdf",
        document_opener=lambda _: document,
        table_reader=lambda path, **kwargs: calls.append((path, kwargs)) or [],
    )

    assert result.text == (
        "[PAGE page=1]\nIntroduction\n[/PAGE]\n\n"
        "[PAGE page=2]\nRéférences\n[/PAGE]\n\n"
        "[PAGE page=3]\nDimensions\n[/PAGE]"
    )
    assert [page.text_calls for page in pages] == [1, 1, 1]
    assert [page.table_calls for page in pages] == [1, 1, 1]
    assert calls == [(str(tmp_path / "fiche.pdf"), {"pages": "2,3", "flavor": "auto"})]
    assert document.closed


def test_camelot_is_skipped_without_detected_table(tmp_path):
    """Mutation détectée : Camelot est payé même pour un PDF sans tableau."""
    document = FakeDocument([FakePage("Texte courant", False)])

    def must_not_run(*args, **kwargs):
        raise AssertionError("Camelot ne doit pas être appelé")

    assert extract_pdf(
        tmp_path / "texte.pdf",
        document_opener=lambda _: document,
        table_reader=must_not_run,
    ) == PDFExtraction(
        "[PAGE page=1]\nTexte courant\n[/PAGE]",
        "Fiche constructeur",
        (),
    )


def test_large_catalogue_keeps_literal_text_without_running_camelot(tmp_path):
    """Un catalogue ne monopolise pas le run pour enrichir ses centaines de tableaux."""
    pages = [FakePage(f"Page {index}", True) for index in range(1, 34)]
    document = FakeDocument(pages)

    def must_not_run(*args, **kwargs):
        raise AssertionError("Camelot ne doit pas ouvrir un catalogue trop long")

    result = extract_pdf(
        tmp_path / "catalogue.pdf",
        document_opener=lambda _: document,
        table_reader=must_not_run,
    )

    assert "[PAGE page=1]\nPage 1\n[/PAGE]" in result.text
    assert "[PAGE page=33]\nPage 33\n[/PAGE]" in result.text
    assert [page.table_calls for page in pages] == [0] * 33
    assert result.warnings == ("Camelot: skipped_large_document",)


def test_table_cells_keep_page_row_and_column_coordinates(tmp_path):
    """Mutation détectée : les colonnes redeviennent un texte aplati ambigu."""
    document = FakeDocument([FakePage("Table aplatie", True)])
    table = SimpleNamespace(
        page=1,
        _bbox=(10.0, 20.0, 210.0, 120.0),
        cells=[
            [FakeCell(10, 110, 70, 120), FakeCell(110, 210, 70, 120)],
            [FakeCell(10, 110, 20, 70), FakeCell(110, 210, 20, 70)],
        ],
        df=SimpleNamespace(values=FakeValues([
            ["Référence", "Courant nominal"],
            ["K7C41208", "16 A"],
        ])),
    )

    result = extract_pdf(
        tmp_path / "table.pdf",
        document_opener=lambda _: document,
        table_reader=lambda *args, **kwargs: [table],
    )

    assert "[TABLE page=1 table=1]" in result.text
    assert (
        "[GEOMETRY bbox=10,20,210,120 columns=10:110;110:210]"
        in result.text
    )
    assert "row=1 | col=1: Référence | col=2: Courant nominal" in result.text
    assert "row=2 | col=1: K7C41208 | col=2: 16 A" in result.text
    assert "[/TABLE]" in result.text


def test_camelot_table_numbers_are_local_to_each_page(tmp_path):
    document = FakeDocument([FakePage("P1", True), FakePage("P2", True)])
    tables = [
        SimpleNamespace(page=1, df=SimpleNamespace(values=FakeValues([["A"]]))),
        SimpleNamespace(page=2, df=SimpleNamespace(values=FakeValues([["B"]]))),
        SimpleNamespace(page=2, df=SimpleNamespace(values=FakeValues([["C"]]))),
    ]

    result = extract_pdf(
        tmp_path / "multi.pdf",
        document_opener=lambda _: document,
        table_reader=lambda *args, **kwargs: tables,
    )

    assert result.text.count("[TABLE page=1 table=1]") == 1
    assert result.text.count("[TABLE page=2 table=1]") == 1
    assert result.text.count("[TABLE page=2 table=2]") == 1


def test_camelot_failure_keeps_text_and_records_safe_warning(tmp_path):
    """Mutation détectée : une panne Camelot fait perdre le texte PyMuPDF."""
    document = FakeDocument([FakePage("Texte de secours", True)])

    def fail(*args, **kwargs):
        raise RuntimeError("chemin interne sensible")

    result = extract_pdf(
        tmp_path / "fiche.pdf",
        document_opener=lambda _: document,
        table_reader=fail,
    )

    assert result.text == "[PAGE page=1]\nTexte de secours\n[/PAGE]"
    assert result.warnings == ("Camelot: RuntimeError",)
    assert "sensible" not in " ".join(result.warnings)
