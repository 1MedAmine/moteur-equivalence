# -*- coding: utf-8 -*-
"""Contrôle mécanique des spécifications oubliées dans un RequirementSet."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from modeles import RequirementSet


_TABLE_RE = re.compile(
    r"\[TABLE page=(?P<page>\d+) table=(?P<table>\d+)\]\s*"
    r"(?P<body>.*?)\s*\[/TABLE\]",
    flags=re.DOTALL,
)
_PAGE_RE = re.compile(
    r"\[PAGE page=(?P<page>\d+)\]\s*(?P<body>.*?)\s*\[/PAGE\]",
    flags=re.DOTALL,
)
_GEOMETRY_RE = re.compile(
    r"\[GEOMETRY bbox=(?P<bbox>[-+0-9.,]+) "
    r"columns=(?P<columns>[-+0-9.:;]+)\]"
)
_CELL_RE = re.compile(r"col=(\d+):\s*(.*?)(?=\s*\|\s*col=\d+:|$)")
_SPEC_RE = re.compile(
    r"(?<![\w])(?:"
    r"IP\s*\d{2,3}"
    r"|[-+]?\d+(?:[.,]\d+)?(?:\s*(?:-|–|—|à|to|\.{2,3})\s*[-+]?\d+(?:[.,]\d+)?)?"
    r"\s*(?:ans?|years?|ms|s(?:ec(?:onde)?s?)?|g|kg|mg|µg|ug|"
    r"V|mV|kV|A|mA|kA|Hz|kHz|MHz|GHz|W|kW|MW|dBm|"
    r"°C|°F|%|mm|cm|m|km|Pa|kPa|MPa|bar|rpm)"
    r")(?![\w])",
    flags=re.IGNORECASE,
)
_NAMED_SPEC_RE = re.compile(
    r"^\s*(?P<label>[^:\n]{1,100}?)\s*:\s*(?P<value>[^:\n]{1,200}?)\s*$"
)
_CATEGORICAL_DISCRIMINANT_LABELS = frozenset({
    "etancheite",
    "jeuinterne",
    "jeuinterneradial",
    "radialinternalclearance",
    "sealing",
})
_BOTH_SIDES_SEALING_RE = re.compile(
    r"\b(?:"
    r"(?:with\s+)?seals?\s+on\s+both\s+sides"
    r"|sealed\s+(?:on\s+)?both\s+sides"
    r")\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class CoverageOrphan:
    value: str
    label: str
    context: str
    reason: str
    page: int | None = None
    table: int | None = None
    column: int | None = None
    table_bbox: tuple[float, float, float, float] | None = None
    column_bounds: tuple[tuple[float, float], ...] = ()
    forbidden_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageAssessment:
    detected_specs: int
    covered_specs: int
    orphans: tuple[CoverageOrphan, ...]


def _normaliser(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value).casefold())
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    return "".join(character for character in ascii_value if character.isalnum())


def _cellule_corrompue(value: str) -> bool:
    return any(ord(character) < 32 and character not in "\r\n\t" for character in value)


def _lignes_table(body: str) -> list[dict[int, str]]:
    rows: list[dict[int, str]] = []
    for line in body.splitlines():
        if not line.lstrip().startswith("row="):
            continue
        cells = {int(column): value.strip() for column, value in _CELL_RE.findall(line)}
        if cells:
            rows.append(cells)
    return rows


def _geometrie_table(
    body: str,
) -> tuple[
    tuple[float, float, float, float] | None,
    tuple[tuple[float, float], ...],
]:
    match = _GEOMETRY_RE.search(body)
    if match is None:
        return None, ()
    try:
        bbox_values = tuple(float(value) for value in match.group("bbox").split(","))
        columns = tuple(
            tuple(float(value) for value in bounds.split(":"))
            for bounds in match.group("columns").split(";")
        )
    except ValueError:
        return None, ()
    if len(bbox_values) != 4 or any(len(bounds) != 2 for bounds in columns):
        return None, ()
    return bbox_values, columns  # type: ignore[return-value]


def _colonne_produit(rows: list[dict[int, str]], product: str) -> int | None:
    product_key = _normaliser(product)
    for row in rows[:3]:
        for column, value in row.items():
            value_key = _normaliser(value)
            if len(value_key) >= 3 and value_key in product_key:
                return column
    return None


def _label_contexte(context: str) -> str:
    left = context.split(":", 1)[0].strip()
    return left if left and len(left) <= 80 else "Spécification"


def _specs(value: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for segment in re.split(
        r"(?<=;)\s+|(?<!\.)\.(?!\.)\s+",
        value.strip(),
    ):
        for match in _SPEC_RE.finditer(segment):
            found.append((match.group(0).strip(), segment.strip()))
    return found


def _categorical_spec(line: str) -> tuple[str, str, str] | None:
    match = _NAMED_SPEC_RE.fullmatch(line.strip())
    if match is None:
        return None
    label = match.group("label").strip()
    value = match.group("value").strip()
    if _normaliser(label) not in _CATEGORICAL_DISCRIMINANT_LABELS:
        return None
    if not value or _cellule_corrompue(value):
        return None
    return label, value, f"{label}: {value}"


def _categorical_specs(line: str) -> tuple[tuple[str, str, str], ...]:
    """Repère aussi une étanchéité explicite écrite dans une désignation.

    Les fiches courtes mettent fréquemment « with seals on both sides » dans
    la description, sans champ `Sealing`. C'est une propriété discriminante
    littérale : elle doit déclencher la seconde passe si le modèle initial la
    néglige. La valeur reste le groupe exact extrait de la fiche.
    """
    found: list[tuple[str, str, str]] = []
    named = _categorical_spec(line)
    if named is not None:
        found.append(named)
    for match in _BOTH_SIDES_SEALING_RE.finditer(line):
        value = match.group(0).strip()
        found.append(("Sealing", value, f"Sealing: {value}"))
    return tuple(found)


def neutraliser_source_initiale(fiche: str) -> str:
    """Masque les glyphes illisibles avant le premier appel structuré.

    Le texte original n'est pas modifié et reste l'autorité du compteur. Cette
    projection empêche seulement le modèle de recopier des contrôles invalides
    dans sa réponse JSON.
    """
    lines: list[str] = []
    for line in fiche.splitlines():
        if line.lstrip().startswith("row="):
            def replace_cell(match: re.Match[str]) -> str:
                column, value = match.groups()
                replacement = (
                    "[VALEUR ILLISIBLE]" if _cellule_corrompue(value) else value
                )
                return f"col={column}: {replacement}"

            lines.append(_CELL_RE.sub(replace_cell, line))
        elif _cellule_corrompue(line):
            lines.append("[LIGNE ILLISIBLE]")
        else:
            lines.append(line)
    return "\n".join(lines)


def specifications_evidentes(value: str) -> tuple[str, ...]:
    """Rend les nombres+unités et indices IP dans leur ordre littéral."""
    found: list[str] = []
    seen: set[str] = set()
    for spec, _context in _specs(value):
        key = _normaliser(spec)
        if key and key not in seen:
            seen.add(key)
            found.append(spec)
    return tuple(found)


def evaluer_couverture(
    fiche: str,
    requirements: RequirementSet,
) -> CoverageAssessment:
    """Compare les valeurs factuelles du texte aux critères déjà extraits.

    Les tableaux sont liés à la colonne du produit demandé. Une valeur présente
    seulement dans une colonne sœur ne peut donc pas devenir un orphelin.
    """
    requirement_text = "\n".join(
        f"{item.label} {item.requested_value}" for item in requirements.criteria
    )
    requirement_specs = {
        _normaliser(spec) for spec in specifications_evidentes(requirement_text)
    }
    requirement_categorical = {
        (_normaliser(item.label), _normaliser(item.requested_value))
        for item in requirements.criteria
        if _normaliser(item.label) in _CATEGORICAL_DISCRIMINANT_LABELS
    }
    table_matches = list(_TABLE_RE.finditer(fiche))
    table_geometry = {
        (int(match.group("page")), int(match.group("table"))):
        _geometrie_table(match.group("body"))
        for match in table_matches
    }
    allowed_table_values: set[str] = set()
    sibling_table_values: set[str] = set()
    corrupted: list[CoverageOrphan] = []

    for table_match in table_matches:
        page = int(table_match.group("page"))
        table = int(table_match.group("table"))
        table_bbox, column_bounds = table_geometry[(page, table)]
        rows = _lignes_table(table_match.group("body"))
        target_column = _colonne_produit(rows, requirements.product)
        if target_column is None:
            continue
        current_label = "Spécification"
        for row in rows:
            target = row.get(target_column, "")
            raw_label = row.get(1, "").strip()
            if raw_label:
                current_label = raw_label
            label = current_label
            target_corrupted = _cellule_corrompue(target)
            sibling_corrupted = any(
                _cellule_corrompue(cell)
                for column, cell in row.items()
                if column not in {1, target_column}
            )
            if target_corrupted or (not target.strip() and sibling_corrupted):
                forbidden_values = tuple(
                    spec
                    for column, cell in row.items()
                    if column not in {1, target_column}
                    for spec, _ in _specs(cell)
                )
                corrupted.append(CoverageOrphan(
                    value="",
                    label=label,
                    context=(
                        f"{label} : "
                        + " | ".join(row[column] for column in sorted(row) if column != 1)
                    ),
                    reason="corrupted_cell",
                    page=page,
                    table=table,
                    column=target_column,
                    table_bbox=table_bbox,
                    column_bounds=column_bounds,
                    forbidden_values=forbidden_values,
                ))
            for spec, _ in _specs(target):
                allowed_table_values.add(_normaliser(spec))
            for column, cell in row.items():
                if column == target_column:
                    continue
                for spec, _ in _specs(cell):
                    sibling_table_values.add(_normaliser(spec))

    plain_parts: list[str] = []
    cursor = 0
    for match in table_matches:
        plain_parts.append(fiche[cursor:match.start()])
        cursor = match.end()
    plain_parts.append(fiche[cursor:])

    found: list[tuple[str, str, int | None, int | None, int | None]] = []
    for table_match in table_matches:
        page = int(table_match.group("page"))
        table = int(table_match.group("table"))
        rows = _lignes_table(table_match.group("body"))
        target_column = _colonne_produit(rows, requirements.product)
        if target_column is None:
            continue
        for row in rows:
            target = row.get(target_column, "")
            label = row.get(1, "Spécification").strip() or "Spécification"
            found.extend(
                (spec, f"{label} : {target}", page, table, target_column)
                for spec, _ in _specs(target)
            )
    plain_text = "\n".join(plain_parts)
    page_matches = list(_PAGE_RE.finditer(plain_text))
    for page_match in page_matches:
        page = int(page_match.group("page"))
        for line in page_match.group("body").splitlines():
            for spec, context in _specs(line):
                key = _normaliser(spec)
                if key in sibling_table_values and key not in allowed_table_values:
                    continue
                found.append((spec, context, page, None, None))

    categorical_found: list[tuple[str, str, str, int | None]] = []
    for page_match in page_matches:
        page = int(page_match.group("page"))
        for line in page_match.group("body").splitlines():
            for label, value, context in _categorical_specs(line):
                categorical_found.append((label, value, context, page))

    outside_pages: list[str] = []
    cursor = 0
    for page_match in page_matches:
        outside_pages.append(plain_text[cursor:page_match.start()])
        cursor = page_match.end()
    outside_pages.append(plain_text[cursor:])
    for line in "\n".join(outside_pages).splitlines():
        for spec, context in _specs(line):
            key = _normaliser(spec)
            if key in sibling_table_values and key not in allowed_table_values:
                continue
            found.append((spec, context, None, None, None))
        for label, value, context in _categorical_specs(line):
            categorical_found.append((label, value, context, None))

    unique_found: list[
        tuple[str, str, str, int | None, int | None, int | None]
    ] = []
    seen_specs: set[str] = set()
    for spec, context, page, table, column in found:
        key = _normaliser(spec)
        if not key or key in seen_specs:
            continue
        seen_specs.add(key)
        unique_found.append((spec, context, key, page, table, column))

    orphans: list[CoverageOrphan] = []
    covered = 0
    for spec, context, key, page, table, column in unique_found:
        if key in requirement_specs:
            covered += 1
            continue
        table_bbox, column_bounds = table_geometry.get(
            (page, table),
            (None, ()),
        )
        orphans.append(CoverageOrphan(
            value=spec,
            label=_label_contexte(context),
            context=context,
            reason="missing_spec",
            page=page,
            table=table,
            column=column,
            table_bbox=table_bbox,
            column_bounds=column_bounds,
        ))

    seen_corrupted: set[
        tuple[str, int | None, int | None, int | None]
    ] = set()
    for item in corrupted:
        identity = (_normaliser(item.label), item.page, item.table, item.column)
        if identity in seen_corrupted:
            continue
        seen_corrupted.add(identity)
        orphans.append(item)

    seen_categorical: set[tuple[str, str]] = set()
    covered_categorical = 0
    for label, value, context, page in categorical_found:
        identity = (_normaliser(label), _normaliser(value))
        if not all(identity) or identity in seen_categorical:
            continue
        seen_categorical.add(identity)
        if identity in requirement_categorical:
            covered_categorical += 1
            continue
        orphans.append(CoverageOrphan(
            value=value,
            label=label,
            context=context,
            reason="missing_categorical_spec",
            page=page,
        ))

    return CoverageAssessment(
        detected_specs=(
            len(unique_found) + len(seen_corrupted) + len(seen_categorical)
        ),
        covered_specs=covered + covered_categorical,
        orphans=tuple(orphans),
    )
