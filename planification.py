# -*- coding: utf-8 -*-
"""Planification B2 : critères ScrapeGraphAI et quatre requêtes par vague."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, Field, ValidationError

import configuration
import recherche
from candidats import (
    CandidateLead,
    is_origin_identity,
    query_contains_identity,
    reference_is_present,
)
from configuration import B2Config
from couverture_criteres import (
    CoverageAssessment,
    evaluer_couverture,
    neutraliser_source_initiale,
    specifications_evidentes,
)
from mission import construire_mission_complement_criteres, construire_mission_criteres
from modeles import (
    Requirement,
    RequirementAddition,
    RequirementSet,
    RequirementSupplement,
)


_GRAPH_OUTPUT_GUARD = RLock()


class PlanningError(ValueError):
    """Le modèle n'a pas respecté le contrat de planification."""


class JsonParsingError(PlanningError):
    """Aucune valeur JSON syntaxiquement valide n'a pu être décodée."""


_REQUIREMENT_PATH = re.compile(
    r"(?:\$|product|origin_brand|criteria(?:\[\d+\])?"
    r"(?:\.(?:id|label|requested_value|critical|evidence_excerpt|page|table|column))?)"
)
_REQUIREMENT_STAGES = frozenset({
    "requirement_initial_extraction",
    "requirement_text_supplement",
    "requirement_visual_supplement",
})
_REQUIREMENT_ISSUES = frozenset({
    "invalid_item",
    "invalid_structure",
    "json_parse_error",
    "unit_label_disagreement",
})
_REQUIREMENT_ACTIONS = frozenset({
    "aborted",
    "discarded",
    "retried",
    "retry_failed",
    "review",
})


def _safe_requirement_path(value: object) -> str:
    path = value if isinstance(value, str) else ""
    return path if _REQUIREMENT_PATH.fullmatch(path) else "$"


def sanitize_requirement_diagnostics(value: object) -> list[dict[str, str]]:
    """Nettoie les diagnostics avant toute frontière de composant."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    sanitized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        stage = item.get("stage")
        issue = item.get("issue")
        action = item.get("action")
        if (
            stage not in _REQUIREMENT_STAGES
            or issue not in _REQUIREMENT_ISSUES
            or action not in _REQUIREMENT_ACTIONS
        ):
            continue
        sanitized.append({
            "stage": str(stage),
            "path": _safe_requirement_path(item.get("path")),
            "issue": str(issue),
            "action": str(action),
        })
    return sanitized


class RequirementExtractionError(PlanningError):
    """Échec d'extraction exposant uniquement un diagnostic allowlisté."""

    def __init__(
        self,
        diagnostic: dict[str, str] | Sequence[dict[str, str]],
    ) -> None:
        items = [diagnostic] if isinstance(diagnostic, dict) else list(diagnostic)
        self.safe_diagnostics = sanitize_requirement_diagnostics(items)
        if not self.safe_diagnostics:
            self.safe_diagnostics = [{
                "stage": "requirement_initial_extraction",
                "path": "$",
                "issue": "invalid_structure",
                "action": "aborted",
            }]
        first = self.safe_diagnostics[0]
        super().__init__(
            f"Échec à l'étage {first['stage']} au chemin {first['path']}."
        )


_REQUIREMENT_ITEM_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "label": {"type": "string", "minLength": 1},
        "requested_value": {"type": "string", "minLength": 1},
        "critical": {"type": "boolean"},
    },
    "required": ["id", "label", "requested_value"],
}
_REQUIREMENT_ADDITION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_REQUIREMENT_ITEM_OUTPUT_SCHEMA["properties"],
        "evidence_excerpt": {"type": "string", "minLength": 1},
        "page": {"type": ["integer", "null"], "minimum": 1},
        "table": {"type": ["integer", "null"], "minimum": 1},
        "column": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["id", "label", "requested_value", "evidence_excerpt"],
}


class RequirementSetEnvelope(BaseModel):
    """Enveloppe initiale permissive ; les critères restent validés un par un."""

    product: object = Field(
        default="",
        json_schema_extra={"type": "string", "minLength": 1},
    )
    origin_brand: object = Field(
        default="",
        json_schema_extra={"type": "string"},
    )
    criteria: list[object] = Field(
        default_factory=list,
        json_schema_extra={
            "items": _REQUIREMENT_ITEM_OUTPUT_SCHEMA,
            "minItems": 1,
        },
    )


class RequirementSupplementEnvelope(BaseModel):
    """Enveloppe permissive : chaque item est validé ensuite séparément."""

    criteria: list[object] = Field(
        default_factory=list,
        json_schema_extra={"items": _REQUIREMENT_ADDITION_OUTPUT_SCHEMA},
    )


@dataclass(frozen=True)
class VisualLocation:
    page: int
    table: int | None
    column: int | None
    table_bbox: tuple[float, float, float, float] | None
    column_bounds: tuple[tuple[float, float], ...]


def _crop_rectangles(
    page_height: float,
    location: VisualLocation,
) -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
    """Convertit la géométrie Camelot en crops PyMuPDF sans colonne sœur."""
    bbox = location.table_bbox
    column = location.column
    bounds = location.column_bounds
    if bbox is None or column is None or not 1 <= column <= len(bounds):
        return ()
    _table_x1, table_y1, _table_x2, table_y2 = bbox
    top = page_height - table_y2
    bottom = page_height - table_y1
    selected = [("libelles", bounds[0])]
    if column != 1:
        selected.append(("produit", bounds[column - 1]))
    return tuple(
        (role, (left, top, right, bottom))
        for role, (left, right) in selected
    )


def _clip_rectangle(
    *,
    page_bounds: tuple[float, float, float, float],
    crop: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Ajoute du contexte vertical sans jamais exposer une colonne voisine."""
    page_x0, page_y0, page_x1, page_y1 = page_bounds
    crop_x0, crop_y0, crop_x1, crop_y1 = crop
    return (
        max(page_x0, crop_x0),
        max(page_y0, crop_y0 - 8),
        min(page_x1, crop_x1),
        min(page_y1, crop_y1 + 8),
    )


class QueryPlan(BaseModel):
    queries: tuple[str, str, str, str]
    strategy: Literal["model", "retry", "fallback"] = "model"


class TargetedQuery(BaseModel):
    query: str
    candidate_key: tuple[str, str]
    angle: str


class TargetedQueryPlan(BaseModel):
    queries: tuple[TargetedQuery, TargetedQuery, TargetedQuery, TargetedQuery]
    strategy: Literal["targeted"] = "targeted"


def _extract_query_list(response: str) -> list[str]:
    """Extrait une liste JSON sans interpréter ni réparer son contenu."""
    text = str(response or "").strip()
    candidates = [text]
    candidates.extend(re.findall(
        r"```(?:json)?\s*(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for position, character in enumerate(candidate):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[position:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                value = value.get("queries")
            if isinstance(value, list):
                return value
    raise PlanningError("Le plan de requêtes ne contient aucune liste JSON valide.")


def _normalize_queries(
    raw: object,
    requirements: RequirementSet,
    target_brand: str | None,
    previous_queries: Set[str],
) -> tuple[str, str, str, str]:
    if not isinstance(raw, list) or len(raw) != 4 or not all(
        isinstance(item, str) for item in raw
    ):
        raise PlanningError("Le plan doit contenir exactement quatre requêtes.")

    queries: list[str] = []
    previous = {" ".join(item.split()).casefold() for item in previous_queries}
    for item in raw:
        query = recherche._nettoyer(item)
        query = recherche.retirer_origine(query, requirements.origin_brand)
        if target_brand and target_brand.strip():
            query = recherche.ajouter_marque(query, target_brand.strip())
        normalized = " ".join(query.split()).casefold()
        current = {" ".join(existing.split()).casefold() for existing in queries}
        if not query or normalized in previous or normalized in current:
            raise PlanningError("Les quatre requêtes doivent être nouvelles et uniques.")
        queries.append(query)
    return tuple(queries)  # type: ignore[return-value]


def _fallback_query_core(
    requirements: RequirementSet,
    missing: Sequence[str],
) -> str:
    missing_keys = {" ".join(item.split()).casefold() for item in missing}
    identity_words = {"reference", "marque", "gamme", "brand", "modele", "model"}

    def usable(item) -> bool:
        identity = unicodedata.normalize(
            "NFKD", f"{item.id} {item.label}".casefold()
        ).encode("ascii", "ignore").decode("ascii")
        return not identity_words.intersection(re.findall(r"[a-z]+", identity))

    eligible = [item for item in requirements.criteria if usable(item)]
    ordered = sorted(
        eligible,
        key=lambda item: (
            " ".join(item.label.split()).casefold() not in missing_keys,
            not item.critical,
        ),
    )
    fragments = [
        f"{item.label} {item.requested_value}"
        for item in ordered[:3]
    ]
    return recherche._nettoyer("produit industriel " + " ".join(fragments))


def _fallback_queries(
    requirements: RequirementSet,
    target_brand: str | None,
    missing: Sequence[str],
    previous_queries: Set[str],
) -> tuple[str, str, str, str]:
    core = _fallback_query_core(requirements, missing)
    angles = (
        "fiche produit",
        "caractéristiques techniques",
        "documentation constructeur pdf",
        "comparaison technique",
    )
    previous = {" ".join(item.split()).casefold() for item in previous_queries}
    queries: list[str] = []
    for index, angle in enumerate(angles, start=1):
        variant = 0
        while True:
            marker = "" if variant == 0 else f" variante{variant}"
            query = recherche._nettoyer(f"{angle}{marker} {core}")
            query = recherche.retirer_origine(query, requirements.origin_brand)
            if target_brand and target_brand.strip():
                query = recherche.ajouter_marque(query, target_brand.strip())
            normalized = " ".join(query.split()).casefold()
            current = {" ".join(item.split()).casefold() for item in queries}
            if query and normalized not in previous and normalized not in current:
                queries.append(query)
                break
            variant += 1
            if variant > 100:
                raise PlanningError(
                    f"Impossible de produire la requête de secours {index}."
                )
    return tuple(queries)  # type: ignore[return-value]


def _default_graph(**kwargs):
    from scrapegraphai.graphs import SmartScraperGraph

    return SmartScraperGraph(**kwargs)


def _default_vision_client(**kwargs):
    from openai import OpenAI

    return OpenAI(**kwargs)


def _largest_table_bbox(page: object) -> tuple[float, float, float, float] | None:
    try:
        tables = getattr(page.find_tables(), "tables", ())
    except Exception:
        return None
    candidates = [
        tuple(getattr(table, "bbox", ()))
        for table in tables
        if len(tuple(getattr(table, "bbox", ()))) == 4
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda bbox: max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]),
    )  # type: ignore[return-value]


def _render_pdf_pages(
    source_path: str,
    locations: Sequence[VisualLocation],
) -> list[tuple[int, int | None, int | None, str, str]]:
    import pymupdf

    document = pymupdf.open(source_path)
    rendered: list[tuple[int, int | None, int | None, str, str]] = []
    try:
        for location in locations:
            page = document[location.page - 1]
            crops = _crop_rectangles(float(page.rect.height), location)
            if not crops and location.table is None:
                crops = (("page", tuple(page.rect)),)
            for role, bbox in crops:
                clip = pymupdf.Rect(_clip_rectangle(
                    page_bounds=tuple(page.rect),
                    crop=bbox,
                ))
                pixmap = page.get_pixmap(dpi=300, alpha=False, clip=clip)
                encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                rendered.append((
                    location.page,
                    location.table,
                    location.column,
                    role,
                    f"data:image/png;base64,{encoded}",
                ))
    finally:
        document.close()
    return rendered


def _extract_json_value(response: str) -> object:
    text = str(response or "").strip()
    candidates = [text]
    candidates.extend(re.findall(
        r"```(?:json)?\s*(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            value, _ = decoder.raw_decode(candidate)
            return value
        except json.JSONDecodeError:
            pass
        for position, character in enumerate(candidate):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[position:])
            except json.JSONDecodeError:
                continue
            return value
    raise JsonParsingError("La réponse ne contient aucune valeur JSON valide.")


def _validation_path(prefix: str, location: Sequence[object]) -> str:
    path = prefix
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _blank_fields(item: object, fields: Sequence[str]) -> tuple[str, ...]:
    if isinstance(item, BaseModel):
        item = item.model_dump()
    if not isinstance(item, Mapping):
        return ()
    return tuple(
        field
        for field in fields
        if isinstance(item.get(field), str) and not item[field].strip()
    )


def _journal_blank_fields(
    *,
    fields: Sequence[str],
    index: int,
    stage: str,
    diagnostics: list[dict[str, str]],
) -> bool:
    for field in fields:
        diagnostics.append({
            "stage": stage,
            "path": f"criteria[{index}].{field}",
            "issue": "invalid_item",
            "action": "discarded",
        })
    return bool(fields)


def _structure_diagnostics(
    error: ValidationError,
    *,
    stage: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for detail in error.errors(include_url=False, include_context=False):
        path = _validation_path("", detail["loc"]).lstrip(".") or "$"
        result.append({
            "stage": stage,
            "path": _safe_requirement_path(path),
            "issue": "invalid_structure",
            "action": "aborted",
        })
    return result


def _validate_supplement_items(
    payload: object,
    *,
    stage: str,
    diagnostics: list[dict[str, str]],
) -> RequirementSupplement:
    """Valide chaque ajout sans perdre ses voisins valides."""
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise RequirementExtractionError({
            "stage": stage,
            "path": "$",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    if not isinstance(payload.get("criteria"), list):
        raise RequirementExtractionError({
            "stage": stage,
            "path": "criteria",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    valid: list[RequirementAddition] = []
    for index, item in enumerate(payload["criteria"]):
        if _journal_blank_fields(
            fields=_blank_fields(
                item, ("id", "label", "requested_value", "evidence_excerpt")
            ),
            index=index,
            stage=stage,
            diagnostics=diagnostics,
        ):
            continue
        try:
            parsed = (
                item
                if isinstance(item, RequirementAddition)
                else RequirementAddition.model_validate(item)
            )
        except ValidationError as error:
            for detail in error.errors(include_url=False, include_context=False):
                diagnostics.append({
                    "stage": stage,
                    "path": _validation_path(f"criteria[{index}]", detail["loc"]),
                    "issue": "invalid_item",
                    "action": "discarded",
                })
            continue
        if _has_unit_label_disagreement(parsed):
            diagnostics.append({
                "stage": stage,
                "path": f"criteria[{index}].requested_value",
                "issue": "unit_label_disagreement",
                "action": "review",
            })
        valid.append(parsed)
    return RequirementSupplement(criteria=valid)


def _validate_requirement_items(
    payload: object,
    *,
    diagnostics: list[dict[str, str]],
) -> RequirementSet:
    """Construit le jeu initial en isolant les critères invalides."""
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise RequirementExtractionError({
            "stage": "requirement_initial_extraction",
            "path": "$",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    if not isinstance(payload.get("criteria"), list):
        raise RequirementExtractionError({
            "stage": "requirement_initial_extraction",
            "path": "criteria",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    valid: list[Requirement] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(payload["criteria"]):
        if _journal_blank_fields(
            fields=_blank_fields(item, ("id", "label", "requested_value")),
            index=index,
            stage="requirement_initial_extraction",
            diagnostics=diagnostics,
        ):
            continue
        try:
            parsed = (
                item
                if isinstance(item, Requirement)
                else Requirement.model_validate(item)
            )
        except ValidationError as error:
            for detail in error.errors(include_url=False, include_context=False):
                diagnostics.append({
                    "stage": "requirement_initial_extraction",
                    "path": _validation_path(f"criteria[{index}]", detail["loc"]),
                    "issue": "invalid_item",
                    "action": "discarded",
                })
            continue
        if parsed.id in seen_ids:
            diagnostics.append({
                "stage": "requirement_initial_extraction",
                "path": f"criteria[{index}].id",
                "issue": "invalid_item",
                "action": "discarded",
            })
            continue
        seen_ids.add(parsed.id)
        if _has_unit_label_disagreement(parsed):
            diagnostics.append({
                "stage": "requirement_initial_extraction",
                "path": f"criteria[{index}].requested_value",
                "issue": "unit_label_disagreement",
                "action": "review",
            })
        valid.append(parsed)
    if not valid:
        invalid_items = [
            item
            for item in diagnostics
            if item.get("stage") == "requirement_initial_extraction"
            and item.get("issue") == "invalid_item"
        ]
        if not invalid_items:
            invalid_items.append({
                "stage": "requirement_initial_extraction",
                "path": "criteria",
                "issue": "invalid_item",
                "action": "discarded",
            })
        raise RequirementExtractionError(invalid_items) from None
    if isinstance(payload.get("product"), str) and not payload["product"].strip():
        raise RequirementExtractionError({
            "stage": "requirement_initial_extraction",
            "path": "product",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    try:
        return RequirementSet.model_validate({**payload, "criteria": valid})
    except ValidationError as error:
        raise RequirementExtractionError(_structure_diagnostics(
            error,
            stage="requirement_initial_extraction",
        )) from None


_LABEL_UNIT_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("frequency", ("frequence", "frequency")),
    ("time", ("intervalle", "periode", "duree", "temps", "lifetime", "vie")),
    ("distance", ("portee", "distance", "range")),
    ("voltage", ("tension", "voltage")),
    ("current", ("courant", "current")),
    ("mass", ("poids", "masse", "weight")),
    ("temperature", ("temperature",)),
    ("power", ("puissance", "power")),
    ("protection", ("protection",)),
)

_VALUE_UNIT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("frequency", r"\b(?:hz|khz|mhz|ghz)\b"),
    ("time", r"\b(?:us|ms|s|sec|seconde?s?|minutes?|min|heures?|h|jours?|ans?|annees?)\b"),
    ("distance", r"\b(?:mm|cm|km|m|metres?|meters?)\b"),
    ("voltage", r"\b(?:mv|v|kv)(?:\s*(?:ac|dc))?\b"),
    ("current", r"\b(?:ma|ka)\b|\b\d+(?:[.,]\d+)?\s*a\b"),
    ("mass", r"\b(?:mg|g|kg)\b"),
    ("temperature", r"°\s*(?:c|f)\b"),
    ("power", r"\b(?:mw|w|kw)\b"),
    ("protection", r"\bip\s*\d{2,3}\b"),
)


def _fold_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value).casefold())
    return folded.encode("ascii", "ignore").decode("ascii")


def _has_unit_label_disagreement(requirement: Requirement) -> bool:
    label = _fold_text(requirement.label)
    value = _fold_text(requirement.requested_value)
    expected = {
        family
        for family, tokens in _LABEL_UNIT_FAMILIES
        if any(re.search(rf"\b{re.escape(token)}\b", label) for token in tokens)
    }
    observed = {
        family
        for family, pattern in _VALUE_UNIT_PATTERNS
        if re.search(pattern, value)
    }
    return bool(expected and observed and observed.difference(expected))


def _append_unit_label_diagnostics(
    criteria: Sequence[Requirement],
    *,
    stage: str,
    diagnostics: list[dict[str, str]],
) -> None:
    for index, item in enumerate(criteria):
        if _has_unit_label_disagreement(item):
            diagnostics.append({
                "stage": stage,
                "path": f"criteria[{index}].requested_value",
                "issue": "unit_label_disagreement",
                "action": "review",
            })


def _recovery_prompt(prompt: str) -> str:
    return (
        prompt
        + "\n\nREPRISE DE PARSING — la réponse précédente n'était pas un JSON "
        "valide. Reprends l'extraction depuis la source fournie et retourne "
        "uniquement l'objet JSON demandé, sans Markdown ni commentaire. "
        "N'ajoute aucune information absente de la source."
    )


def _json_error_path(response: object) -> str:
    """Déduit un chemin JSON sans jamais retourner de contenu du modèle."""
    text = str(response or "")
    start = text.find("{")
    if start < 0:
        return "$"
    candidate = text[start:]
    try:
        json.JSONDecoder().raw_decode(candidate)
        return "$"
    except json.JSONDecodeError as error:
        prefix = candidate[: error.pos + 1]

    key_matches = list(re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', prefix))
    if not key_matches:
        return "$"
    field = key_matches[-1].group(1)
    criteria_matches = list(re.finditer(r'"criteria"\s*:\s*\[', prefix))
    if not criteria_matches:
        return _safe_requirement_path(field)

    segment = prefix[criteria_matches[-1].end():]
    in_string = False
    escaped = False
    array_depth = 1
    object_depth = 0
    item_count = 0
    for character in segment:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            array_depth += 1
        elif character == "]":
            array_depth = max(0, array_depth - 1)
        elif character == "{" and array_depth == 1 and object_depth == 0:
            item_count += 1
            object_depth = 1
        elif character == "{":
            object_depth += 1
        elif character == "}":
            object_depth = max(0, object_depth - 1)
    return _safe_requirement_path(
        f"criteria[{max(0, item_count - 1)}].{field}"
    )


def _run_graph_with_single_json_retry(
    *,
    prompt: str,
    source: str,
    graph_config: dict,
    schema: type[BaseModel],
    graph_factory: Callable[..., object],
    stage: str,
    diagnostics: list[dict[str, str]],
    path_sanitizer: Callable[[object], str] = _safe_requirement_path,
    error_factory: Callable[[dict[str, str]], Exception] = RequirementExtractionError,
) -> object:
    """Même contrat que l'audit : un seul rejeu, réservé au parsing JSON."""
    for attempt, current_prompt in enumerate((prompt, _recovery_prompt(prompt))):
        graph = graph_factory(
            prompt=current_prompt,
            source=source,
            config=graph_config,
            schema=schema,
        )
        try:
            # ScrapeGraphAI peut inclure la réponse brute dans sa sortie standard
            # ou dans un handler pré-lié avant de lever OutputParserException.
            # La coupure de logs est globale : la garde empêche deux extractions
            # concurrentes de restaurer un état intermédiaire ou de laisser fuiter
            # la réponse brute de l'autre appel.
            with _GRAPH_OUTPUT_GUARD:
                previous_logging_disable = logging.root.manager.disable
                logging.disable(10**9)
                try:
                    with contextlib.redirect_stdout(
                        io.StringIO()
                    ), contextlib.redirect_stderr(io.StringIO()):
                        return graph.run()
                finally:
                    logging.disable(previous_logging_disable)
        except OutputParserException as error:
            llm_output = getattr(error, "llm_output", None)
            try:
                return _extract_json_value(str(llm_output or ""))
            except JsonParsingError:
                pass
            path = path_sanitizer(_json_error_path(llm_output))
            if attempt == 0:
                diagnostics.append({
                    "stage": stage,
                    "path": path,
                    "issue": "json_parse_error",
                    "action": "retried",
                })
                continue
            raise error_factory({
                "stage": stage,
                "path": path,
                "issue": "json_parse_error",
                "action": "retry_failed",
            }) from None
    raise AssertionError("boucle de reprise inaccessible")


class Planner:
    def __init__(
        self,
        config: B2Config,
        *,
        call_llm: Callable[[str], str] | None = None,
        graph_factory: Callable[..., object] = _default_graph,
        supplement_extractor: Callable[
            [RequirementSet, CoverageAssessment, str, str | None],
            RequirementSupplement,
        ] | None = None,
        vision_client_factory: Callable[..., object] = _default_vision_client,
        page_renderer: Callable[
            [str, Sequence[VisualLocation]],
            list[tuple[int, int | None, int | None, str, str]],
        ] = _render_pdf_pages,
    ) -> None:
        self.config = config
        self._call_llm_override = call_llm
        self._graph_factory = graph_factory
        self._supplement_extractor = supplement_extractor
        self._vision_client_factory = vision_client_factory
        self._page_renderer = page_renderer
        self.last_coverage: CoverageAssessment | None = None
        self.last_second_pass = False
        self.last_requirement_diagnostics: list[dict[str, str]] = []

    def _call_llm(self, prompt: str) -> str:
        if self._call_llm_override is not None:
            return self._call_llm_override(prompt)
        from openai import OpenAI

        self.config.require_runtime()
        llm = configuration.config_llm(self.config)
        client = OpenAI(
            api_key=llm["api_key"],
            base_url=llm["base_url"],
            timeout=llm["timeout"],
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.config.temperature,
            max_tokens=500,
            extra_body=llm.get("extra_body") or None,
        )
        return response.choices[0].message.content or ""

    def extract_requirements(
        self,
        fiche: str,
        *,
        source_path: str | None = None,
    ) -> RequirementSet:
        self.last_requirement_diagnostics = []
        initial_source = neutraliser_source_initiale(fiche)
        initial_prompt = construire_mission_criteres(
            initial_source,
            source_hint=Path(source_path).name if source_path else None,
        )
        raw = _run_graph_with_single_json_retry(
            prompt=initial_prompt,
            source=initial_source,
            graph_config=configuration.config_graphe(self.config, verbose=False),
            schema=RequirementSetEnvelope,
            graph_factory=self._graph_factory,
            stage="requirement_initial_extraction",
            diagnostics=self.last_requirement_diagnostics,
        )
        requirements = _validate_requirement_items(
            raw,
            diagnostics=self.last_requirement_diagnostics,
        )
        coverage = evaluer_couverture(fiche, requirements)
        self.last_coverage = coverage
        self.last_second_pass = bool(coverage.orphans)
        if not coverage.orphans:
            return requirements

        if self._supplement_extractor is not None:
            supplement_stage = "requirement_text_supplement"
            supplement = self._supplement_extractor(
                requirements, coverage, fiche, source_path
            )
            _append_unit_label_diagnostics(
                supplement.criteria,
                stage=supplement_stage,
                diagnostics=self.last_requirement_diagnostics,
            )
        elif self._can_use_visual_pass(coverage, source_path):
            supplement_stage = "requirement_visual_supplement"
            supplement = self._extract_visual_supplement(
                requirements, coverage, source_path or ""
            )
        else:
            supplement_stage = "requirement_text_supplement"
            orphan_source = self._orphan_source(coverage)
            supplement_raw = _run_graph_with_single_json_retry(
                prompt=construire_mission_complement_criteres(
                    requirements, orphan_source,
                ),
                source=orphan_source,
                graph_config=configuration.config_graphe(self.config, verbose=False),
                schema=RequirementSupplementEnvelope,
                graph_factory=self._graph_factory,
                stage=supplement_stage,
                diagnostics=self.last_requirement_diagnostics,
            )
            supplement = (
                supplement_raw
                if isinstance(supplement_raw, RequirementSupplement)
                else _validate_supplement_items(
                    supplement_raw,
                    stage="requirement_text_supplement",
                    diagnostics=self.last_requirement_diagnostics,
                )
            )
        return self._merge_literal_supplement(
            requirements, supplement, fiche, coverage, source_path
        )

    @staticmethod
    def _can_use_visual_pass(
        coverage: CoverageAssessment,
        source_path: str | None,
    ) -> bool:
        return bool(
            source_path
            and Path(source_path).suffix.casefold() == ".pdf"
            and coverage.orphans
        )

    def _extract_visual_supplement(
        self,
        requirements: RequirementSet,
        coverage: CoverageAssessment,
        source_path: str,
    ) -> RequirementSupplement:
        locations = tuple(sorted({
            VisualLocation(
                page=item.page,
                table=item.table,
                column=item.column,
                table_bbox=item.table_bbox,
                column_bounds=item.column_bounds,
            )
            for item in coverage.orphans
            if item.page is not None
        }, key=lambda item: (item.page, item.table or 0, item.column or 0)))
        rendered = self._page_renderer(source_path, locations)
        checklist = "\n".join(
            f"- page {item.page or '?'} | table {item.table or '?'} "
            f"| colonne {item.column or '?'} | {item.label}"
            + (f" | valeur textuelle : {item.value}" if item.value else "")
            for item in coverage.orphans
        )
        prompt = (
            "TRANSCRIPTION EXHAUSTIVE VISUELLE CIBLÉE\n"
            f"Produit et colonne cibles : {requirements.product}.\n"
            "Les images jointes montrent, dans l'ordre, les pages exactes "
            "indiquées dans les extraits. Lis uniquement la colonne de ce "
            "produit ; la colonne d'une variante voisine est interdite.\n"
            "\nCHECKLIST ORPHELINE À TRAITER LIGNE PAR LIGNE :\n"
            + checklist
            + "\n\n"
            "Transcris chaque ligne technique visible de la colonne cible, y "
            "compris les lignes déjà lisibles dans le texte. Le code retirera "
            "mécaniquement les doublons. Si une cellule contient plusieurs "
            "dimensions physiques, crée un critère séparé pour chacune. Ne "
            "résume pas et ne déduis aucune valeur.\n"
            "Retourne un objet JSON conforme à ce modèle : "
            '{"criteria":[{"id":"...","label":"...",'
            '"requested_value":"...","critical":true,'
            '"evidence_excerpt":"...","page":1,"table":1,"column":2}]}. '
            "Une valeur illisible doit être omise, jamais devinée."
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        for page, table, column, role, data_url in rendered:
            content.append({
                "type": "text",
                "text": (
                    f"page={page} table={table or '?'} "
                    f"colonne={column or '?'} rôle={role}"
                ),
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        client = self._vision_client_factory(
            api_key=self.config.api_key,
            base_url=self.config.api_base,
            timeout=self.config.llm_timeout,
            max_retries=0,
        )
        missing_payload = object()
        payload: object = missing_payload
        for attempt in range(2):
            current_content = list(content)
            if attempt:
                current_content.append({
                    "type": "text",
                    "text": (
                        "REPRISE DE PARSING — retourne uniquement l'objet JSON "
                        "demandé, sans Markdown ni commentaire et sans inventer."
                    ),
                })
            response = client.chat.completions.create(
                model=self.config.vision_model,
                messages=[
                    {"role": "system", "content": "/no_think"},
                    {"role": "user", "content": current_content},
                ],
                temperature=0,
                max_tokens=2000,
            )
            raw = response.choices[0].message.content or ""
            try:
                payload = _extract_json_value(raw)
                break
            except JsonParsingError:
                path = _json_error_path(raw)
                if attempt == 0:
                    self.last_requirement_diagnostics.append({
                        "stage": "requirement_visual_supplement",
                        "path": path,
                        "issue": "json_parse_error",
                        "action": "retried",
                    })
                    continue
                raise RequirementExtractionError({
                    "stage": "requirement_visual_supplement",
                    "path": path,
                    "issue": "json_parse_error",
                    "action": "retry_failed",
                }) from None
        if payload is missing_payload:
            raise AssertionError("boucle de reprise visuelle inaccessible")
        return _validate_supplement_items(
            payload,
            stage="requirement_visual_supplement",
            diagnostics=self.last_requirement_diagnostics,
        )

    @staticmethod
    def _orphan_source(coverage: CoverageAssessment) -> str:
        lines = []
        for item in coverage.orphans:
            location = f"page={item.page}" if item.page is not None else "page=?"
            lines.append(
                f"[ORPHAN reason={item.reason} {location} label={item.label}] "
                f"{item.context}"
            )
        return "\n".join(lines)

    @staticmethod
    def _literal_key(value: str) -> str:
        folded = unicodedata.normalize("NFKD", str(value).casefold())
        ascii_value = folded.encode("ascii", "ignore").decode("ascii")
        return "".join(
            character for character in ascii_value if character.isalnum()
        )

    @classmethod
    def _labels_compatibles(cls, left: str, right: str) -> bool:
        left_key = cls._literal_key(left)
        right_key = cls._literal_key(right)
        if not left_key or not right_key:
            return False
        shorter, longer = sorted((left_key, right_key), key=len)
        return len(shorter) >= 5 and shorter in longer

    @classmethod
    def _merge_literal_supplement(
        cls,
        requirements: RequirementSet,
        supplement: RequirementSupplement,
        fiche: str,
        coverage: CoverageAssessment,
        source_path: str | None,
    ) -> RequirementSet:
        source_key = cls._literal_key(fiche)
        missing_specs = [
            item for item in coverage.orphans
            if item.reason == "missing_spec" and item.value
        ]
        existing_ids = {item.id for item in requirements.criteria}
        existing_values = {
            cls._literal_key(item.requested_value) for item in requirements.criteria
        }
        corrupted_locations = [
            item
            for item in coverage.orphans
            if item.reason == "corrupted_cell"
        ]
        additions: list[Requirement] = []
        for item in supplement.criteria:
            value_key = cls._literal_key(item.requested_value)
            excerpt_key = cls._literal_key(item.evidence_excerpt)
            literal_missing_spec = (
                value_key in source_key
                and value_key in excerpt_key
                and any(
                    value_key == cls._literal_key(orphan.value)
                    and cls._labels_compatibles(item.label, orphan.label)
                    and (
                        source_path is None
                        or (
                            (orphan.page is None or item.page == orphan.page)
                            and (orphan.table is None or item.table == orphan.table)
                            and (orphan.column is None or item.column == orphan.column)
                        )
                    )
                    for orphan in missing_specs
                )
            )
            matching_corrupted = [
                orphan
                for orphan in corrupted_locations
                if orphan.page == item.page
                and orphan.table == item.table
                and orphan.column == item.column
                and (
                    orphan.table is None
                    or (
                        orphan.table_bbox is not None
                        and orphan.column is not None
                        and len(orphan.column_bounds) >= orphan.column
                    )
                )
                and cls._labels_compatibles(item.label, orphan.label)
            ]
            visual_location_matches = bool(source_path and matching_corrupted)
            returned_specs = {
                cls._literal_key(spec)
                for spec in specifications_evidentes(item.requested_value)
            }
            forbidden_specs = {
                cls._literal_key(value)
                for orphan in matching_corrupted
                for value in orphan.forbidden_values
            }
            visual_corrupted_spec = (
                visual_location_matches
                and value_key in excerpt_key
                and returned_specs.isdisjoint(forbidden_specs)
            )
            if (
                not value_key
                or item.id in existing_ids
                or value_key in existing_values
                or not (literal_missing_spec or visual_corrupted_spec)
            ):
                continue

            visual_specs = specifications_evidentes(item.requested_value)
            contains_known_value = any(
                cls._literal_key(spec) in existing_values
                for spec in visual_specs
            )
            if visual_location_matches and contains_known_value:
                new_specs = [
                    spec
                    for spec in visual_specs
                    if cls._literal_key(spec) not in existing_values
                ]
                if new_specs:
                    for index, spec in enumerate(new_specs, start=1):
                        addition_id = f"{item.id}_complement_{index}"
                        if addition_id in existing_ids:
                            continue
                        additions.append(Requirement(
                            id=addition_id,
                            label=(
                                f"{item.label} — température"
                                if "°c" in spec.casefold() or "°f" in spec.casefold()
                                else f"{item.label} — complément"
                            ),
                            requested_value=spec,
                            critical=item.critical,
                        ))
                        existing_ids.add(addition_id)
                        existing_values.add(cls._literal_key(spec))
                    continue
            additions.append(Requirement(
                id=item.id,
                label=item.label,
                requested_value=item.requested_value,
                critical=item.critical,
            ))
            existing_ids.add(item.id)
            existing_values.add(value_key)
        if not additions:
            return requirements
        return requirements.model_copy(
            update={"criteria": [*requirements.criteria, *additions]}
        )

    def plan_targeted_queries(
        self,
        requirements: RequirementSet,
        candidates: Sequence[CandidateLead],
        previous_queries: Set[str],
    ) -> TargetedQueryPlan:
        selected_list: list[CandidateLead] = []
        selected_keys: set[tuple[str, str]] = set()
        for candidate in candidates:
            if is_origin_identity(
                candidate.brand, candidate.reference, requirements
            ) or candidate.key in selected_keys:
                continue
            selected_list.append(candidate)
            selected_keys.add(candidate.key)
            if len(selected_list) == 4:
                break
        selected = tuple(selected_list)
        if not selected:
            raise PlanningError("Aucune piste candidate à cibler.")
        angles = (
            "fiche produit",
            "fiche technique pdf",
            "spécifications techniques",
            "documentation fabricant",
            "manuel technique pdf",
            "catalogue produit pdf",
            "données électriques",
            "caractéristiques mécaniques",
        )
        previous = {" ".join(item.split()).casefold() for item in previous_queries}
        # Les trois premières requêtes d'une vague doivent vérifier trois
        # identités différentes. Les angles supplémentaires ne viennent
        # qu'après ce premier tour de couverture.
        quotas = [1] * len(selected)
        for index in range(4 - len(selected)):
            quotas[index % len(selected)] += 1
        allocated: list[list[TargetedQuery]] = []
        used = set(previous)
        for lead, quota in zip(selected, quotas):
            lead_queries: list[TargetedQuery] = []
            for angle in angles:
                query = recherche._nettoyer(f"{lead.brand} {lead.reference} {angle}")
                normalized = " ".join(query.split()).casefold()
                if normalized in used or not query_contains_identity(query, lead):
                    continue
                lead_queries.append(TargetedQuery(
                    query=query,
                    candidate_key=lead.key,
                    angle=angle,
                ))
                used.add(normalized)
                if len(lead_queries) == quota:
                    break
            if len(lead_queries) != quota:
                raise PlanningError(
                    "Impossible de produire quatre requêtes ciblées uniques."
                )
            allocated.append(lead_queries)

        planned = []
        for position in range(max(quotas)):
            for lead_queries in allocated:
                if position < len(lead_queries):
                    planned.append(lead_queries[position])
        if len(planned) == 4:
            return TargetedQueryPlan(queries=tuple(planned))
        raise PlanningError("Impossible de produire quatre requêtes ciblées uniques.")

    def plan_distributor_queries(
        self,
        requirements: RequirementSet,
        candidates: Sequence[CandidateLead],
        previous_queries: Set[str],
    ) -> tuple[TargetedQuery, ...]:
        """Produit les recherches par reference des distributeurs configurés."""
        previous = {" ".join(item.split()).casefold() for item in previous_queries}
        queries: list[TargetedQuery] = []
        used = set(previous)
        for domain in self.config.distributor_domains:
            for lead in candidates:
                if is_origin_identity(lead.brand, lead.reference, requirements):
                    continue
                query = recherche._nettoyer(f"site:{domain} {lead.reference}")
                normalized = " ".join(query.split()).casefold()
                if (
                    not query
                    or normalized in used
                    or not reference_is_present(lead.reference, query)
                ):
                    continue
                queries.append(TargetedQuery(
                    query=query,
                    candidate_key=lead.key,
                    angle=f"distributor:{domain}",
                ))
                used.add(normalized)
        return tuple(queries)

    def plan_queries(
        self,
        requirements: RequirementSet,
        target_brand: str | None,
        missing: Sequence[str],
        previous_queries: Set[str],
    ) -> QueryPlan:
        target = target_brand.strip() if target_brand and target_brand.strip() else "aucune marque imposée"
        prompt = f"""Produis exactement quatre requêtes Web complémentaires pour trouver une
fiche produit industrielle équivalente chez {target}. Réponds uniquement par
une liste JSON de quatre chaînes. Chaque requête doit viser une fiche produit,
être courte, contenir les caractéristiques discriminantes et ne jamais citer
la marque, la gamme ou la référence du produit d'origine. Les quatre angles
sont : référence/gamme cible, caractéristiques, documentation constructeur et
comparaison technique.

Critères immuables :
{requirements.model_dump_json(indent=2)}

Critères encore non prouvés : {list(missing)}
Requêtes déjà exécutées et interdites : {sorted(previous_queries)}"""

        repair = (
            prompt
            + "\n\nCORRECTION OBLIGATOIRE : la réponse précédente était invalide. "
              "Réponds par un tableau JSON brut contenant exactement quatre "
              "chaînes nouvelles et uniques, sans Markdown ni autre clé."
        )
        for attempt, current_prompt in enumerate((prompt, repair)):
            try:
                response = self._call_llm(current_prompt)
            except Exception:
                continue
            try:
                raw = _extract_query_list(response)
                queries = _normalize_queries(
                    raw, requirements, target_brand, previous_queries
                )
            except PlanningError:
                continue
            return QueryPlan(
                queries=queries,
                strategy="model" if attempt == 0 else "retry",
            )

        return QueryPlan(
            queries=_fallback_queries(
                requirements, target_brand, missing, previous_queries
            ),
            strategy="fallback",
        )
