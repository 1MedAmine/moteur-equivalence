# -*- coding: utf-8 -*-
"""Extraction PDF B2 : PyMuPDF d'abord, puis un seul lot Camelot ciblé."""

from __future__ import annotations

import gc
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf


@dataclass(frozen=True)
class PDFExtraction:
    text: str
    title: str = ""
    warnings: tuple[str, ...] = ()


def _default_table_reader(path: str, **kwargs: object) -> Iterable[object]:
    import camelot

    return camelot.read_pdf(path, **kwargs)


def _looks_tabular(page: object) -> bool:
    try:
        detection = page.find_tables()
    except Exception:
        return False
    return bool(getattr(detection, "tables", ()))


def _cell_text(value: object) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def _coordinate(value: object) -> str:
    number = float(value)
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _geometry_line(table: object) -> str:
    bbox = tuple(getattr(table, "_bbox", ()) or ())
    cells = getattr(table, "cells", ()) or ()
    if len(bbox) != 4 or not cells:
        return ""
    column_count = max((len(row) for row in cells), default=0)
    columns: list[tuple[float, float]] = []
    for column_index in range(column_count):
        column_cells = [
            row[column_index] for row in cells if column_index < len(row)
        ]
        try:
            columns.append((
                min(float(cell.x1) for cell in column_cells),
                max(float(cell.x2) for cell in column_cells),
            ))
        except (AttributeError, TypeError, ValueError):
            return ""
    if not columns:
        return ""
    rendered_bbox = ",".join(_coordinate(value) for value in bbox)
    rendered_columns = ";".join(
        f"{_coordinate(left)}:{_coordinate(right)}"
        for left, right in columns
    )
    return f"[GEOMETRY bbox={rendered_bbox} columns={rendered_columns}]"


def _render_tables(tables: Iterable[object]) -> str:
    rendered: list[str] = []
    page_counts: dict[str, int] = {}
    for table in tables:
        values = getattr(getattr(table, "df", None), "values", None)
        if values is None or not hasattr(values, "tolist"):
            continue
        rows = values.tolist()
        if not rows:
            continue
        page = _cell_text(getattr(table, "page", "?")) or "?"
        page_counts[page] = page_counts.get(page, 0) + 1
        table_index = page_counts[page]
        lines = [f"[TABLE page={page} table={table_index}]"]
        geometry = _geometry_line(table)
        if geometry:
            lines.append(geometry)
        for row_index, row in enumerate(rows, start=1):
            cells = [
                f"col={column_index}: {_cell_text(cell)}"
                for column_index, cell in enumerate(row, start=1)
            ]
            lines.append(f"row={row_index} | " + " | ".join(cells))
        lines.append("[/TABLE]")
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered)


def extract_pdf(
    path: Path,
    *,
    document_opener: Callable[[Path], Any] = pymupdf.open,
    table_reader: Callable[..., Iterable[object]] = _default_table_reader,
) -> PDFExtraction:
    """Extrait le texte une fois et enrichit seulement les pages tabulaires."""
    document = document_opener(path)
    page_texts: list[str] = []
    candidate_pages: list[int] = []
    try:
        metadata = getattr(document, "metadata", {}) or {}
        title = " ".join(str(metadata.get("title") or "").split())
        for page_number, page in enumerate(document, start=1):
            text = str(page.get_text("text") or "").strip()
            if text:
                page_texts.append(
                    f"[PAGE page={page_number}]\n{text}\n[/PAGE]"
                )
            if _looks_tabular(page):
                candidate_pages.append(page_number)
    finally:
        document.close()

    text = "\n\n".join(page_texts).strip()
    if not candidate_pages:
        return PDFExtraction(text=text, title=title)

    tables: Iterable[object] | None = None
    warnings: tuple[str, ...] = ()
    try:
        tables = table_reader(
            str(path),
            pages=",".join(str(page) for page in candidate_pages),
            flavor="auto",
        )
        table_text = _render_tables(tables)
    except Exception as error:
        table_text = ""
        warnings = (f"Camelot: {type(error).__name__}",)
    finally:
        tables = None
        gc.collect()

    combined = "\n\n".join(part for part in (text, table_text) if part).strip()
    return PDFExtraction(text=combined, title=title, warnings=warnings)
