# -*- coding: utf-8 -*-
"""Pistes candidates vérifiables, sans produire de preuve de compatibilité."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import unicodedata
from typing import Literal, Sequence
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

from compatibilite import canonical_url
from modeles import CandidateAudit, PageAudit, RequirementSet


FieldName = Literal["title", "snippet", "url", "content"]


@dataclass(frozen=True)
class DiscoveryLimits:
    max_active: int = 4
    title_chars: int = 300
    snippet_chars: int = 1_000
    content_chars: int = 4_000
    total_chars: int = 48_000


class CandidateProposal(BaseModel):
    brand: str = Field(min_length=1)
    reference: str = Field(min_length=1)


_PAGE_AUDIT_CANDIDATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "reference": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "requested_value": {"type": "string"},
                    "observed_value": {"type": "string"},
                    "status": {"type": "string"},
                    "proofs": {"type": "array"},
                },
            },
        },
        "deviations": {"type": "array"},
        "limitations": {"type": "array"},
    },
}


class PageAuditEnvelope(BaseModel):
    """Enveloppe permissive ; les candidats et critères sont validés ensuite."""

    candidates: list[object] = Field(
        default_factory=list,
        json_schema_extra={"items": _PAGE_AUDIT_CANDIDATE_OUTPUT_SCHEMA},
    )


_DECLARED_CANDIDATE_CRITERIA_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_index": {"type": "integer"},
        "criteria": _PAGE_AUDIT_CANDIDATE_OUTPUT_SCHEMA["properties"]["criteria"],
    },
}


class DeclaredCandidateCriteriaEnvelope(BaseModel):
    """Jugements techniques pour des identités déjà déclarées par le code."""

    candidate_criteria: list[object] = Field(
        default_factory=list,
        json_schema_extra={"items": _DECLARED_CANDIDATE_CRITERIA_OUTPUT_SCHEMA},
    )


class DiscoveryEnvelope(BaseModel):
    leads: list[object] = Field(default_factory=list)
    audit: PageAuditEnvelope | None = None


@dataclass(frozen=True)
class CandidateOccurrence:
    url: str
    field: FieldName
    fragment: str
    rank: int


@dataclass(frozen=True)
class CandidateLead:
    brand: str
    reference: str
    canonical_brand: str
    canonical_reference: str
    occurrences: tuple[CandidateOccurrence, ...]
    rank: int = 0
    low_confidence: bool = False
    discovery_relevance: int = 0
    identity_strength: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.canonical_brand, self.canonical_reference


@dataclass(frozen=True)
class RejectedCandidate:
    brand: str
    reference: str
    reason: str


@dataclass(frozen=True)
class CandidateIngestResult:
    accepted: tuple[CandidateLead, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True)
class CandidateParseResult:
    proposals: tuple[CandidateProposal, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True)
class DiscoveryDocument:
    url: str
    title: str
    snippets: tuple[str, ...]
    content: str
    prompt_source: str
    rank: int
    truncated_fields: tuple[str, ...]
    bounded_fields: tuple[tuple[FieldName, str], ...] = ()


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFKC", unquote(value)).casefold()


def _canonical(value: str) -> str:
    return "".join(character for character in _normalise(value) if character.isalnum())


def canonical_candidate_key(brand: str, reference: str) -> tuple[str, str]:
    """Retourne une identité insensible aux encodages, casse et séparateurs."""
    return _canonical(brand), _canonical(reference)


def _segments(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", _normalise(value), flags=re.UNICODE))


def _literal_matches(value: str, source: str) -> tuple[str, ...]:
    """Trouve une identité dont chaque segment garde des bornes non alphanumériques."""
    segments = _segments(value)
    if not segments:
        return ()
    separator = r"[\W_]*"
    pattern = r"(?<![^\W_])" + separator.join(re.escape(part) for part in segments) + r"(?![^\W_])"
    return tuple(match.group(0) for match in re.finditer(pattern, _normalise(source), flags=re.UNICODE))


def identity_present(value: str, source: str) -> bool:
    """Présence littérale d'une identité, compatible avec les variantes de séparateurs."""
    return bool(_literal_matches(value, source))


def brand_is_present(brand: str, source: str) -> bool:
    return identity_present(brand, source)


def reference_is_present(reference: str, source: str) -> bool:
    return identity_present(reference, source)


def confirmed_candidate_leads(
    leads: Sequence[CandidateLead],
    *,
    page_url: str,
    title: str,
    content: str,
    document: DiscoveryDocument | None = None,
) -> tuple[CandidateLead, ...]:
    """Confirme marque et référence sur la page avant tout audit technique."""
    metadata = [title, content]
    if document is not None:
        metadata.extend((document.title, *document.snippets))
    confirmed: list[CandidateLead] = []
    for lead in leads:
        segments = _segments(lead.reference)
        if not segments:
            continue
        separator = r"[\W_]*"
        reference_pattern = re.compile(
            r"(?<![^\W_])"
            + separator.join(re.escape(part) for part in segments)
            + r"(?![^\W_])",
            re.UNICODE,
        )
        attributed = False
        for raw_source in metadata:
            source = _normalise(raw_source)
            for match in reference_pattern.finditer(source):
                segment = _fallback_attribution_segment(source, *match.span())
                if (
                    brand_is_present(lead.brand, segment)
                    and _brand_reference_attributed(
                        lead.brand, lead.reference, segment
                    )
                ):
                    attributed = True
                    break
            if attributed:
                break
        if attributed:
            confirmed.append(lead)
    return tuple(confirmed)


def query_contains_identity(query: str, lead: CandidateLead) -> bool:
    return brand_is_present(lead.brand, query) and reference_is_present(
        lead.reference, query
    )


def reference_is_literal(
    reference: str,
    document: DiscoveryDocument,
    *,
    bounded: bool = False,
) -> bool:
    return bool(find_occurrences(reference, document, bounded=bounded))


def brand_is_literal(
    brand: str,
    document: DiscoveryDocument,
    *,
    bounded: bool = False,
) -> bool:
    return any(
        identity_present(brand, source)
        for _, source in _document_fields(document, bounded=bounded)
    )


_RECOMMENDATION_SECTION = re.compile(
    r"(?im)^\s*(?:"
    r"customers?\s+also\s+(?:bought|viewed)|"
    r"you\s+may\s+also\s+like|"
    r"related\s+products?|recommended\s+products?|"
    r"les\s+clients\s+ont\s+aussi\s+achet[eé]|"
    r"produits?\s+similaires?|vous\s+aimerez\s+aussi|"
    r"(?:\d+\s+)?autres?\s+produits?\s+s[eé]lectionn[eé]s?\s+pour\s+vous|"
    r"alternatives?\s+comparables?|produits?\s+de\s+substitution|"
    r"equivalents?\s+(?:produits?|compatibles?)"
    r")\s*$"
)


def primary_product_content(content: str) -> str:
    """Isole l'identité du produit principal avant les rayons de recommandation.

    Le corps complet reste disponible à l'audit technique. Cette vue ne sert
    qu'à déclarer ou confirmer l'identité : une carte « aussi acheté » ne doit
    pas devenir un candidat ni confirmer la fiche de son voisin.
    """
    match = _RECOMMENDATION_SECTION.search(content or "")
    return (content or "")[:match.start()] if match else (content or "")


def _document_fields(
    document: DiscoveryDocument,
    *,
    bounded: bool = False,
) -> tuple[tuple[FieldName, str], ...]:
    if bounded:
        if document.bounded_fields:
            return tuple(
                (field, primary_product_content(value) if field == "content" else value)
                for field, value in document.bounded_fields
            )
        legacy_limit = DiscoveryLimits().total_chars
        return (("content", primary_product_content(
            document.prompt_source[:legacy_limit]
        )),)
    return (
        ("title", document.title),
        *(("snippet", snippet) for snippet in document.snippets),
        ("url", unquote(document.url)),
        ("content", primary_product_content(document.content)),
    )


def find_occurrences(
    reference: str,
    document: DiscoveryDocument,
    *,
    bounded: bool = False,
) -> tuple[CandidateOccurrence, ...]:
    occurrences: list[CandidateOccurrence] = []
    for field, source in _document_fields(document, bounded=bounded):
        for fragment in _literal_matches(reference, source):
            occurrence = CandidateOccurrence(document.url, field, fragment, document.rank)
            if occurrence not in occurrences:
                occurrences.append(occurrence)
    return tuple(occurrences)


def _brands_match(left: str, right: str) -> bool:
    left_key, right_key = _canonical(left), _canonical(right)
    return bool(left_key) and left_key == right_key


@dataclass(frozen=True)
class OriginIdentities:
    """Identites d'origine normalisees extraites du contrat de besoin."""

    brand_groups: tuple[frozenset[str], ...]
    ranges: frozenset[str]
    references: frozenset[str]


_ORIGIN_LABELS = {
    "brand": "brand",
    "manufacturer": "brand",
    "fabricant": "brand",
    "marque": "brand",
    "range": "range",
    "series": "range",
    "serie": "range",
    "family": "range",
    "famille": "range",
    "gamme": "range",
    "reference": "reference",
    "ref": "reference",
    "model": "reference",
    "modele": "reference",
    "partnumber": "reference",
}
_GENERIC_BRAND_WORDS = frozenset({
    "corp", "corporation", "electric", "electrical", "gmbh", "group",
    "groupe", "inc", "industrial", "industrie", "ltd", "sa", "sas",
})
_ORIGIN_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_/-]*[A-Za-z])(?=[A-Za-z0-9_/-]*\d)"
    r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*(?![A-Za-z0-9])"
)
_ORIGIN_CRITERION_HINT = re.compile(
    r"reference|référence|ref\b|model|modèle|part\s*number|sku|"
    r"origin|origine|source|identity|identité",
    re.IGNORECASE,
)


def _ascii_words(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", unquote(value)).casefold()
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return tuple(re.findall(r"[a-z0-9]+", ascii_value))


def _brand_group(value: str) -> frozenset[str]:
    words = frozenset(
        word for word in _ascii_words(value)
        if word not in _GENERIC_BRAND_WORDS and len(word) > 1
    )
    return words or frozenset(_ascii_words(value))


def origin_identities(requirements: RequirementSet) -> OriginIdentities:
    """Decoupe fabricant, gamme et reference sans connaitre de catalogue."""
    brands: list[frozenset[str]] = []
    ranges: set[str] = set()
    references: set[str] = set()
    unlabeled_index = 0
    chunks = [
        item.strip()
        for item in re.split(r"[;|\n]+", requirements.origin_brand or "")
        if item.strip()
    ]
    for chunk in chunks:
        match = re.match(r"^\s*([^:=]+?)\s*[:=]\s*(.+?)\s*$", chunk)
        kind: str | None = None
        value = chunk
        if match:
            label_key = "".join(_ascii_words(match.group(1)))
            kind = _ORIGIN_LABELS.get(label_key)
            value = match.group(2).strip()
        if kind is None:
            kind = "brand" if unlabeled_index == 0 else "range"
            unlabeled_index += 1
        if kind == "brand":
            group = _brand_group(value)
            if group and group not in brands:
                brands.append(group)
        elif kind == "range":
            key = _canonical(value)
            if key:
                ranges.add(key)
        else:
            key = _canonical(value)
            if key:
                references.add(key)

    # Une valeur technique compacte (par exemple une capacité ou une classe)
    # peut avoir la même forme alphanumérique qu'une référence. Si elle est
    # déjà portée par un critère non identitaire, elle doit rester auditable et
    # ne peut pas devenir une identité source par sa seule présence dans le
    # libellé produit.
    technical_values = {
        _canonical(item.requested_value)
        for item in requirements.criteria
        if not _ORIGIN_CRITERION_HINT.search(f"{item.id} {item.label}")
    }
    for match in _ORIGIN_REFERENCE.finditer(unquote(requirements.product)):
        key = _canonical(match.group(0))
        if key and key not in technical_values:
            references.add(key)
    return OriginIdentities(tuple(brands), frozenset(ranges), frozenset(references))


def is_origin_identity(
    brand: str,
    reference: str,
    requirements: RequirementSet,
) -> bool:
    identities = origin_identities(requirements)
    reference_key = _canonical(reference)
    if reference_key and (
        reference_key in identities.references | identities.ranges
        or reference_is_present(reference, requirements.product)
    ):
        return True
    candidate_brand = _brand_group(brand)
    return bool(candidate_brand) and any(
        candidate_brand.issubset(group) or group.issubset(candidate_brand)
        for group in identities.brand_groups
    )


def _is_origin_identity(proposal: CandidateProposal, requirements: RequirementSet) -> bool:
    return is_origin_identity(proposal.brand, proposal.reference, requirements)


_UNIT_VALUE = re.compile(
    r"\d+\s*(?:v(?:dc|ac)?|a(?:dc|ac)?|ma|ka|w|kw|hz|khz|mhz|ohm|ω|mm|cm|bar|pa|n|nm|p)\b",
    flags=re.IGNORECASE,
)
_IDENTITY_LABEL = re.compile(r"reference|référence|model|modèle|product|produit|identity|identit", re.IGNORECASE)


def _is_isolated_requirement_value(reference: str, requirements: RequirementSet) -> bool:
    key = _canonical(reference)
    for criterion in requirements.criteria:
        if key != _canonical(criterion.requested_value):
            continue
        identity_criterion = _IDENTITY_LABEL.search(f"{criterion.id} {criterion.label}")
        if identity_criterion or _UNIT_VALUE.search(_normalise(criterion.requested_value)):
            return True
    return False


#: Unites du SI et de l'electrotechnique, prefixes compris. Vocabulaire de
#: mesure, jamais un catalogue : aucune reference, aucun fabricant ici.
_UNITES: frozenset[str] = frozenset({
    "v", "kv", "mv", "vac", "vdc",
    "a", "ka", "ma", "aac", "adc", "ah", "mah",
    "w", "kw", "mw", "va", "kva", "wh", "kwh",
    "hz", "khz", "mhz", "ghz",
    "ohm", "f", "uf", "nf", "pf", "mh", "uh",
    "mm", "cm", "dm", "m", "km", "um",
    "g", "kg", "mg", "t",
    "s", "ms", "us", "ns", "min",
    "bar", "pa", "kpa", "mpa", "nm", "rpm",
    "j", "kj", "c", "k",
})

#: Un nombre, une unite, et rien d'autre — avec un suffixe AC/DC facultatif.
_MOTIF_GRANDEUR = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*([a-zµ°%]{1,4})(?:\s*(?:ac|dc|c\.a\.|c\.c\.))?\s*$",
    re.IGNORECASE,
)


def est_grandeur_physique(reference: str) -> bool:
    """Vrai si la valeur mesure quelque chose au lieu de designer un produit.

    Mesure du 2026-08-20 : `24V` et `100Hz` etaient retenus comme candidats
    Norel, et le budget de requetes partait chercher des produits nommes d'apres
    une tension. Une grandeur ne designe jamais un article.
    """
    correspondance = _MOTIF_GRANDEUR.match(str(reference or ""))
    return bool(correspondance) and correspondance.group(1).casefold() in _UNITES


_DIMENSION_SIGNATURE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:mm\s*)?[x×]"
    r"\s*\d+(?:[.,]\d+)?\s*(?:mm)?"
    r"(?:\s*[x×]\s*\d+(?:[.,]\d+)?\s*(?:mm)?)?\s*$",
    re.IGNORECASE,
)


def reference_is_dimension_signature(reference: str) -> bool:
    """Refuse une cote ``d x D x B`` présentée comme référence article."""
    return _DIMENSION_SIGNATURE.fullmatch(str(reference or "")) is not None


def _without_redundant_brand_suffix(
    proposal: CandidateProposal,
) -> CandidateProposal:
    """Retire le nom de marque ajouté à la fin d'un code distributeur.

    `ALT-205-MAKER` reste une observation littérale de `ALT-205` : seule la
    répétition terminale de la marque est retirée, avec un séparateur exigé.
    """
    brand_parts = _segments(proposal.brand)
    if not brand_parts:
        return proposal
    suffix = r"[\W_]+" + r"[\W_]*".join(
        re.escape(part) for part in brand_parts
    ) + r"\s*$"
    reference = re.sub(suffix, "", proposal.reference, flags=re.IGNORECASE).rstrip()
    plausible, _ = _plausible_fallback_reference(reference)
    if not reference or reference == proposal.reference or not plausible:
        return proposal
    return proposal.model_copy(update={"reference": reference})


_EXPLICIT_REFERENCE_LABEL = r"(?:reference|r[eé]f[eé]rence|ref|model|mod[eè]le|sku|mpn)"
_EXPLICIT_BRAND_LABEL = r"(?:de\s+la\s+marque|brand|manufacturer|fabricant|marque)"


def _identity_pattern(value: str) -> str:
    segments = _segments(value)
    if not segments:
        return r"(?!)"
    return r"(?<![^\W_])" + r"[\W_]*".join(
        re.escape(segment) for segment in segments
    ) + r"(?![^\W_])"


def _brand_reference_attributed(
    brand: str,
    reference: str,
    source: str,
    requirements: RequirementSet | None = None,
) -> bool:
    """Exige une attribution locale, ou des libellés d'identité explicites."""
    normalized = _normalise(source)
    brand_pattern = _identity_pattern(brand)
    reference_pattern = _identity_pattern(reference)
    explicit_patterns = (
        rf"{_EXPLICIT_REFERENCE_LABEL}\s*[:#=-]?\s*{reference_pattern}"
        rf".{{0,100}}?{_EXPLICIT_BRAND_LABEL}\s*[:#=-]?\s*{brand_pattern}",
        rf"{_EXPLICIT_BRAND_LABEL}\s*[:#=-]?\s*{brand_pattern}"
        rf".{{0,100}}?{_EXPLICIT_REFERENCE_LABEL}\s*[:#=-]?\s*{reference_pattern}",
    )
    if any(re.search(pattern, normalized, re.DOTALL) for pattern in explicit_patterns):
        return True

    connector = re.compile(
        r"^(?:[ \t|:,/\-–—()]*|[ \t|:,/\-–—()]*(?:(?:by|par|de|from|maker|brand|marque|"
        r"manufacturer|fabricant|model|mod[eè]le|reference|r[eé]f[eé]rence|"
        r"ref|product|produit)[ \t|:,/\-–—()]*){1,4})$",
        re.IGNORECASE,
    )
    brand_matches = tuple(re.finditer(brand_pattern, normalized, re.UNICODE))
    reference_matches = tuple(re.finditer(reference_pattern, normalized, re.UNICODE))
    for brand_match in brand_matches:
        for reference_match in reference_matches:
            if brand_match.end() <= reference_match.start():
                between = normalized[brand_match.end():reference_match.start()]
            elif reference_match.end() <= brand_match.start():
                between = normalized[reference_match.end():brand_match.start()]
            else:
                return True
            if len(between) <= 80 and connector.fullmatch(between):
                return True
            if (
                requirements is not None
                and len(between) <= 80
                and not re.search(r"[\n\r;.!?]", between)
                and _fallback_product_vocabulary(between, requirements)
            ):
                return True
    return False


def _document_attributes_identity(
    proposal: CandidateProposal,
    document: DiscoveryDocument,
    requirements: RequirementSet,
    *,
    bounded: bool,
) -> bool:
    fields = _document_fields(document, bounded=bounded)
    if any(
        field != "url"
        and _brand_reference_attributed(
            proposal.brand, proposal.reference, source, requirements
        )
        for field, source in fields
    ):
        return True
    return False


def reference_est_purement_numerique(reference: str) -> bool:
    """Vrai si la valeur ne porte aucune lettre.

    Un fabricant peut publier une reference entierement numerique — Dorval
    le fait — mais c'est aussi la forme des numeros d'article de
    distributeurs, invisibles pour un moteur de recherche. On ne tranche
    donc pas : on retrograde, et une reference alphanumerique passera devant.
    """
    canonique = _canonical(reference)
    return bool(canonique) and canonique.isdigit()


def _reference_is_embedded_fragment(
    reference: str,
    document: DiscoveryDocument,
    *,
    bounded: bool,
) -> bool:
    """Refuse un suffixe court qui n'est jamais publié comme référence autonome."""
    if len(_canonical(reference)) >= _MIN_NESTED_REFERENCE_LENGTH:
        return False
    seen = False
    for field, raw_source in _document_fields(document, bounded=bounded):
        if not reference_is_present(reference, raw_source):
            continue
        seen = True
        if not reference_is_only_embedded(reference, raw_source, field=field):
            return False
    return seen


def reference_is_only_embedded(
    reference: str,
    source: str,
    *,
    field: FieldName = "content",
) -> bool:
    """Vrai si chaque occurrence courte appartient à une désignation plus longue."""
    if len(_canonical(reference)) >= _MIN_NESTED_REFERENCE_LENGTH:
        return False
    normalized = _normalise(source)
    pattern = re.compile(_identity_pattern(reference), re.IGNORECASE | re.UNICODE)
    seen = False
    for match in pattern.finditer(normalized):
        seen = True
        separators = r"\s_\-–—" if field == "url" else r"\s/_\-–—"
        left = re.search(
            rf"([a-z0-9]+)[{separators}]*$", normalized[:match.start()]
        )
        right = re.match(
            rf"[{separators}]*([a-z0-9]+)", normalized[match.end():]
        )
        neighbours = [
            neighbour.group(1)
            for neighbour in (left, right)
            if neighbour is not None
        ]
        embedded = any(
            len(_canonical(neighbour)) >= 2
            and any(character.isdigit() for character in neighbour)
            for neighbour in neighbours
        )
        if not embedded:
            return False
    return seen


def _reference_is_site_identity(
    reference: str,
    document: DiscoveryDocument,
) -> bool:
    """Écarte le nom de site lorsqu'une vraie référence produit est explicite."""
    reference_key = _canonical(reference)
    hostname = (urlsplit(document.url).hostname or "").casefold()
    hostname_keys = {
        _canonical(label) for label in hostname.split(".") if _canonical(label)
    }
    if not reference_key or reference_key not in hostname_keys:
        return False

    explicit_references = [
        value
        for _, label, value in _discovery_metadata_fields(document)
        if _EXPLICIT_REFERENCE_METADATA.search(label)
    ]
    if any(
        _canonical(value) and _canonical(value) != reference_key
        for value in explicit_references
    ):
        return True

    path = unquote(urlsplit(document.url).path)
    for token in re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+", path):
        plausible, strong = _plausible_fallback_reference(token)
        if plausible and strong and _canonical(token) != reference_key:
            return True
    return False


def _ambiguous_reference(reference: str) -> bool:
    return (
        len(_canonical(reference)) <= 3
        or reference_est_purement_numerique(reference)
    )


def rejection_reason(
    proposal: CandidateProposal,
    document: DiscoveryDocument,
    requirements: RequirementSet,
    target_brand: str | None,
    *,
    bounded: bool = False,
) -> str | None:
    """Applique l'ordre de diagnostic stable du contrat de découverte."""
    if _canonical(proposal.brand) == _canonical(proposal.reference):
        return "brand_is_reference"
    if _brand_is_product_noun(proposal.brand):
        return "brand_is_product_noun"
    if _is_origin_identity(proposal, requirements):
        return "origin_identity"
    if target_brand and target_brand.strip() and not _brands_match(proposal.brand, target_brand):
        return "target_brand_mismatch"
    if reference_is_dimension_signature(proposal.reference):
        return "reference_is_dimension_signature"
    if not reference_is_literal(proposal.reference, document, bounded=bounded):
        return "reference_not_literal"
    if _reference_is_embedded_fragment(
        proposal.reference, document, bounded=bounded,
    ):
        return "reference_embedded_fragment"
    if _reference_is_site_identity(proposal.reference, document):
        return "reference_is_site_identity"
    target_graph_proposal = bool(target_brand and target_brand.strip() and _brands_match(proposal.brand, target_brand))
    if (
        not brand_is_literal(proposal.brand, document, bounded=bounded)
        and not target_graph_proposal
    ):
        return "brand_not_literal"
    if _is_isolated_requirement_value(proposal.reference, requirements):
        return "isolated_requirement_value"
    # `isolated_requirement_value` ne rejette une grandeur que si elle egale
    # exactement une valeur demandee. Mesure du 2026-08-20 : le besoin disait
    # `24 V DC` et le candidat valait `24V` — pas egal, donc accepte ; et
    # `100Hz` n'etait demande nulle part. Cette garde-ci est intrinseque : une
    # grandeur ne designe aucun produit, qu'elle figure ou non au besoin.
    if est_grandeur_physique(proposal.reference):
        return "reference_is_a_quantity"
    if (
        not (target_brand and target_brand.strip())
        and _explicit_metadata_brand_conflict(proposal.brand, document)
    ):
        return "brand_conflicts_explicit_metadata"
    if (
        not (target_brand and target_brand.strip())
        and not _document_attributes_identity(
            proposal, document, requirements, bounded=bounded
        )
    ):
        return "brand_reference_not_attributed"
    return None


def _low_confidence(
    proposal: CandidateProposal,
    document: DiscoveryDocument,
    target_brand: str | None,
    *,
    bounded: bool = False,
) -> bool:
    graph_target = bool(target_brand and target_brand.strip() and _brands_match(proposal.brand, target_brand))
    return _ambiguous_reference(proposal.reference) or (
        graph_target and not brand_is_literal(proposal.brand, document, bounded=bounded)
    )


def _orthographe_retenue(
    occurrences: Sequence[CandidateOccurrence],
    proposees: Sequence[str],
) -> str:
    """Elit l'orthographe vue dans le plus de sources distinctes.

    `canonical_candidate_key` efface les separateurs : `XZ07-20-10-11` et
    `XZ07201011` sont la meme piste, mais pas la meme chaine a chercher.
    Mission du 2026-08-20 : la forme arrivee en premier etait conservee, B2 a
    donc interroge douze fois `Norel XZ07201011`, que le web n'emploie pas, et
    n'a jamais atteint la fiche Norel qui repond a `XZ07-20-10-11`.

    On compte les sources, pas les occurrences : une page qui repete la
    reference dans son titre, son URL et son corps reste une seule
    observation, sinon elle pesait plus lourd qu'une page supplementaire.

    Le choix se fait uniquement parmi les formes rencontrees. Reinserer des
    separateurs dans une reference qui n'en a jamais montre fabriquerait une
    reference, ce qui est interdit.
    """
    sources_par_forme: dict[str, set[str]] = {}
    for occurrence in occurrences:
        sources_par_forme.setdefault(occurrence.fragment, set()).add(occurrence.url)

    # Une seule orthographe observee : il n'y a pas de concurrence a arbitrer,
    # et la forme proposee reste la reference. `fragment` sort de `_normalise`,
    # donc replie ; l'elire ici afficherait la trace du filtre a la place du
    # texte de la source.
    if len(sources_par_forme) < 2:
        return proposees[0]

    # `max` rend le premier maximum : a egalite, la forme deja retenue gagne,
    # car les occurrences existantes precedent les nouvelles.
    elue = max(sources_par_forme, key=lambda forme: len(sources_par_forme[forme]))

    for proposee in proposees:
        if _normalise(proposee) == elue:
            return proposee
    return proposees[0]


def merge_lead(
    existing: CandidateLead | None,
    proposal: CandidateProposal,
    occurrences: Sequence[CandidateOccurrence],
    *,
    low_confidence: bool = False,
    discovery_relevance: int = 0,
    identity_strength: int = 0,
) -> CandidateLead:
    key = canonical_candidate_key(proposal.brand, proposal.reference)
    if existing is None:
        return CandidateLead(
            brand=proposal.brand,
            reference=_orthographe_retenue(occurrences, (proposal.reference,)),
            canonical_brand=key[0],
            canonical_reference=key[1],
            occurrences=tuple(occurrences),
            low_confidence=low_confidence,
            discovery_relevance=discovery_relevance,
            identity_strength=identity_strength,
        )
    merged = list(existing.occurrences)
    for occurrence in occurrences:
        if occurrence not in merged:
            merged.append(occurrence)
    return replace(
        existing,
        reference=_orthographe_retenue(
            merged, (existing.reference, proposal.reference)
        ),
        occurrences=tuple(merged),
        low_confidence=existing.low_confidence and low_confidence,
        discovery_relevance=max(
            existing.discovery_relevance, discovery_relevance,
        ),
        identity_strength=max(existing.identity_strength, identity_strength),
    )


_MIN_NESTED_REFERENCE_LENGTH = 8


def _nested_reference(left: str, right: str) -> bool:
    """Deux references emboitees partagent l'identite la plus courte."""
    if left == right:
        return False
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= _MIN_NESTED_REFERENCE_LENGTH and shorter in longer


def _merge_nested_leads(
    leads: Sequence[CandidateLead],
    proposal: CandidateProposal,
    occurrences: Sequence[CandidateOccurrence],
    *,
    low_confidence: bool,
    discovery_relevance: int,
    identity_strength: int,
) -> CandidateLead:
    """Fusionne des identites emboitees et conserve la forme la plus courte."""
    proposal_key = canonical_candidate_key(proposal.brand, proposal.reference)
    all_occurrences: list[CandidateOccurrence] = []
    for lead in leads:
        for occurrence in lead.occurrences:
            if occurrence not in all_occurrences:
                all_occurrences.append(occurrence)
    for occurrence in occurrences:
        if occurrence not in all_occurrences:
            all_occurrences.append(occurrence)

    shortest_lead = min(leads, key=lambda lead: len(lead.canonical_reference))
    if len(proposal_key[1]) < len(shortest_lead.canonical_reference):
        reference = proposal.reference
        canonical_reference = proposal_key[1]
    else:
        reference = shortest_lead.reference
        canonical_reference = shortest_lead.canonical_reference
    return CandidateLead(
        brand=shortest_lead.brand,
        reference=reference,
        canonical_brand=proposal_key[0],
        canonical_reference=canonical_reference,
        occurrences=tuple(all_occurrences),
        low_confidence=(
            low_confidence and all(lead.low_confidence for lead in leads)
        ),
        discovery_relevance=max(
            discovery_relevance,
            *(lead.discovery_relevance for lead in leads),
        ),
        identity_strength=max(
            identity_strength,
            *(lead.identity_strength for lead in leads),
        ),
    )


_FIELD_QUALITY: dict[FieldName, int] = {
    "content": 3,
    "title": 3,
    "snippet": 2,
    "url": 1,
}


def rank_leads(leads: Sequence[CandidateLead], target_brand: str | None) -> tuple[CandidateLead, ...]:
    """Classe les pistes avec des départages explicites puis expose des rangs 1..n."""
    target = target_brand.strip() if target_brand and target_brand.strip() else None

    def sort_key(lead: CandidateLead) -> tuple[object, ...]:
        best_quality = max((_FIELD_QUALITY[item.field] for item in lead.occurrences), default=0)
        best_rank = min((item.rank for item in lead.occurrences), default=10**9)
        return (
            0 if target and _brands_match(lead.brand, target) else 1,
            -lead.identity_strength,
            -lead.discovery_relevance,
            0 if not lead.low_confidence else 1,
            -len({item.url for item in lead.occurrences}),
            # A pertinence egale, un code de commande complet doit gagner sur
            # le simple prefixe de gamme lu dans le titre (4KBL103001R8110
            # plutot que 4KBL10). Ce departage reste limite a la decouverte.
            -len(lead.canonical_reference),
            -best_quality,
            best_rank,
            lead.canonical_brand,
            lead.canonical_reference,
        )

    return tuple(replace(lead, rank=index) for index, lead in enumerate(sorted(leads, key=sort_key), start=1))


_FALLBACK_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9\-–—]*[A-Za-z])(?=[A-Za-z0-9\-–—]*\d)"
    r"[A-Za-z0-9]+(?:[\-–—][A-Za-z0-9]+)*(?![A-Za-z0-9])"
)

_FALLBACK_QUANTIFIER = re.compile(
    r"^\d+(?:pcs?|pieces?|packs?|lots?|units?|poles?|stel|stuck)$",
    re.IGNORECASE,
)
_FALLBACK_COMPACT_SPEC = re.compile(
    r"(?:"
    r"(?:ac|dc)\d+\d+p"
    r"|\d+(?:no|nc|nf)\d+(?:(?:vac|vdc|ac|dc))?"
    r"|\d+p\d+(?:no|nc|nf)(?:\d+(?:vac|vdc))?"
    r")",
    re.IGNORECASE,
)
_FALLBACK_PRODUCT_WORDS = frozenset({
    "article", "bearing", "bearings", "catalog", "catalogue", "contactor", "contacteur",
    "datasheet", "disjoncteur", "item", "model", "modele", "part",
    "product", "produit", "reference", "relais", "relay", "roulement", "roulements", "serie",
    "series", "switch", "type",
})
_FALLBACK_COMPARE_VERBS = frozenset({
    "compare", "comparer", "comparaison", "comparison", "comparatif",
})
#: Ponctuation qui separe deux propositions. Le point d'une abreviation n'en
#: est pas une : il se reconnait a la lettre unique qui le precede (`a.c.`,
#: `d.c.`, `p.ex.`, une initiale), la ou une vraie fin de phrase suit un mot
#: d'au moins deux lettres.
#:
#: Mesure du 2026-08-27 : dans le titre `Contacteur Norel 3 Poles 9A 24-60 V
#: a.c./d.c. XZ07201011`, le point de `d.c.` coupait la clause et laissait
#: `XZ07201011` seule, sans sa marque ni son vocabulaire produit. La page
#: prouvait pourtant les six criteres cherches, et sa reference etait perdue
#: pour toute la mission.
_FALLBACK_CLAUSE_BOUNDARY = re.compile(
    r"[\n\r;!?]"
    r"|(?<=[^\W\d_][^\W\d_])\."
    r"|(?<=[\W\d_])\."
)
_FALLBACK_SIDE_SEPARATOR = re.compile(r"\b(?:vs|versus)\b", re.IGNORECASE)
_FALLBACK_COMPARE_SEPARATOR = re.compile(
    r"\b(?:avec|to|with)\b", re.IGNORECASE
)
_FALLBACK_RANGE_QUANTITY = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*[-\u2013\u2014]\s*"
    r"\d+(?:[.,]\d+)?\s*([a-z]{1,6})\s*$",
    re.IGNORECASE,
)
_FALLBACK_MAX_CANONICAL_LENGTH = 40

_DISCOVERY_METADATA_LINE = re.compile(
    r"^\s*(title|og:title|twitter:title|meta\.(?:title|name)|"
    r"jsonld\.(?:name|brand(?:\.name)?|manufacturer(?:\.name)?|model|sku|mpn|productid)|"
    r"brand|manufacturer|fabricant|marque|model|modele|mod[eè]le|"
    r"sku|mpn|reference|r[eé]f[eé]rence)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_DISCOVERY_METADATA_PAIR_LABEL = re.compile(
    r"^\s*(brand|manufacturer|fabricant|marque|model|modele|mod[eè]le|"
    r"sku|mpn|reference|r[eé]f[eé]rence)\s*:\s*$",
    re.IGNORECASE,
)
_EXPLICIT_REFERENCE_METADATA = re.compile(
    r"(?:model|modele|mod[eè]le|sku|mpn|productid|reference|r[eé]f[eé]rence)$",
    re.IGNORECASE,
)
_EXPLICIT_BRAND_METADATA = re.compile(
    r"(?:brand(?:\.name)?|manufacturer(?:\.name)?|fabricant|marque)$",
    re.IGNORECASE,
)
_METADATA_BRAND_STOPWORDS = frozenset({
    "accueil", "ble", "catalog", "catalogue", "home", "product",
    "produit", "shop",
})


def _brand_is_product_noun(brand: str) -> bool:
    """Un type d'article ou un libellé de marque absente n'est pas un fabricant."""
    normalized = "".join(
        character for character in unicodedata.normalize("NFKD", _canonical(brand))
        if not unicodedata.combining(character)
    )
    return normalized in {
        *(_canonical(word) for word in _FALLBACK_PRODUCT_WORDS),
        *(_canonical(word) for word in _METADATA_BRAND_STOPWORDS),
        "generic", "generique",
    }
_INLINE_BRAND_VALUE = re.compile(
    rf"{_EXPLICIT_BRAND_LABEL}\s*[:#=-]?\s*([^,;|.\n]{{1,60}})",
    re.IGNORECASE,
)


def _discovery_metadata_fields(
    document: DiscoveryDocument,
) -> tuple[tuple[FieldName, str, str], ...]:
    """Isole titres et metadonnees structurees, sans en faire des preuves."""
    fields: list[tuple[FieldName, str, str]] = []
    if document.title.strip():
        fields.append(("title", "title", document.title.strip()))
    fields.extend(
        ("snippet", "snippet", snippet.strip())
        for snippet in document.snippets
        if snippet.strip()
    )
    bounded_content = next((
        value for field, value in _document_fields(document, bounded=True)
        if field == "content"
    ), "")
    lines = bounded_content.splitlines()
    for index, line in enumerate(lines):
        match = _DISCOVERY_METADATA_LINE.match(line)
        if match:
            fields.append(("content", match.group(1).casefold(), match.group(2).strip()))
            continue
        pair_label = _DISCOVERY_METADATA_PAIR_LABEL.match(line)
        if pair_label and index + 1 < len(lines):
            value = lines[index + 1].strip()
            if value and len(value) <= 80 and ":" not in value:
                fields.append(("content", pair_label.group(1).casefold(), value))
    return tuple(fields)


def _explicit_metadata_brand_conflict(
    brand: str,
    document: DiscoveryDocument,
) -> bool:
    explicit = [
        value for _, label, value in _discovery_metadata_fields(document)
        if _EXPLICIT_BRAND_METADATA.search(label)
    ]
    return bool(explicit) and not any(_brands_match(brand, value) for value in explicit)


def _identity_strength(
    proposal: CandidateProposal,
    document: DiscoveryDocument,
) -> int:
    """Priorité de piste, sans produire de preuve technique."""
    fields = _discovery_metadata_fields(document)
    if not any(
        _EXPLICIT_BRAND_METADATA.search(label) and _brands_match(proposal.brand, value)
        for _, label, value in fields
    ):
        return 0
    reference_key = _canonical(proposal.reference)
    brand_key = _canonical(proposal.brand)
    for _, label, value in fields:
        if not _EXPLICIT_REFERENCE_METADATA.search(label):
            continue
        value_key = _canonical(value)
        if value_key == reference_key or value_key == reference_key + brand_key:
            return 3 if re.search(r"(?:model|mod[eè]le|reference|r[eé]f[eé]rence)$", label) else 2
    if reference_is_present(proposal.reference, document.title):
        return 1
    return 0


def _metadata_brand_candidates(
    document: DiscoveryDocument,
    metadata_fields: Sequence[tuple[FieldName, str, str]],
    target_brand: str | None,
    requirements: RequirementSet,
) -> tuple[str, ...]:
    metadata_text = "\n".join(value for _, _, value in metadata_fields)
    if target_brand and target_brand.strip():
        brand = target_brand.strip()
        return (brand,) if brand_is_present(brand, metadata_text) else ()

    hostname = (urlsplit(document.url).hostname or "").casefold()
    canonical_hostname = _canonical(hostname)
    candidates: list[str] = []
    explicit_candidates: list[str] = []
    bounded_content = next((
        value for field, value in _document_fields(document, bounded=True)
        if field == "content"
    ), "")
    inline_values = [
        match.group(1).strip()
        for match in _INLINE_BRAND_VALUE.finditer(bounded_content)
    ]
    for _, label, value in (
        *metadata_fields,
        *(("content", "inline_brand", value) for value in inline_values),
    ):
        fragments = [value]
        if label in {"title", "og:title", "twitter:title", "meta.title"}:
            fragments.extend(
                part.strip()
                for part in re.split(r"\s(?:[-|\u2013\u2014])\s", value)
                if part.strip()
            )
        for fragment in fragments:
            candidate = " ".join(fragment.split()).strip(" -|()")
            canonical = _canonical(candidate)
            words = _segments(candidate)
            if (
                not canonical
                or any(character.isdigit() for character in canonical)
                or len(words) > 4
                or canonical in _METADATA_BRAND_STOPWORDS
            ):
                continue
            explicit = bool(
                _EXPLICIT_BRAND_METADATA.search(label) or label == "inline_brand"
            )
            domain_confirmed = len(canonical) >= 3 and canonical in canonical_hostname
            if not explicit and not domain_confirmed:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
            if explicit and candidate not in explicit_candidates:
                explicit_candidates.append(candidate)
    inferred_candidates: list[str] = []
    product_roots = {
        word.rstrip("s") for word in _segments(requirements.product)
        if len(word) >= 4
    }
    generic_roots = {word.rstrip("s") for word in _FALLBACK_PRODUCT_WORDS}
    for _, label, value in metadata_fields:
        if label not in {
            "title", "snippet", "og:title", "twitter:title", "meta.title",
        }:
            continue
        for match in _FALLBACK_IDENTIFIER.finditer(value):
            reference = match.group(0)
            plausible, strong = _plausible_fallback_reference(reference)
            if (
                not plausible
                or est_grandeur_physique(reference)
                # Les références compactes publiées sans séparateur sont
                # courantes. Six caractères mixtes, immédiatement voisins
                # d'une marque dans un titre, sont assez précis pour créer
                # une piste — jamais une preuve de compatibilité.
                or (not strong and len(_canonical(reference)) < 6)
                or reference_is_dimension_signature(reference)
            ):
                continue
            prefix = value[:match.start()]
            tokens = re.findall(r"[^\W\d_]+|\d+[A-Za-z]+", prefix, re.UNICODE)
            retained = [
                token for token in tokens
                if token.casefold().rstrip("s") not in product_roots | generic_roots
                and not _FALLBACK_QUANTIFIER.fullmatch(token.casefold())
            ]
            candidate = " ".join(retained[-4:]).strip()
            if candidate and not any(char.isdigit() for char in candidate):
                if candidate not in inferred_candidates:
                    inferred_candidates.append(candidate)
            # Certains distributeurs écrivent d'abord la référence, puis la
            # marque (`6205EE NTN SNR`). Deux mots suffisent ici : au-delà, on
            # commencerait à absorber la description du produit. La validation
            # d'attribution ci-dessous exige ensuite que ce groupe soit bien
            # adjacent à la référence dans la même métadonnée.
            suffix = value[match.end():]
            suffix_tokens = re.findall(
                r"[^\W\d_]+|\d+[A-Za-z]+", suffix, re.UNICODE
            )
            suffix_retained = [
                token for token in suffix_tokens
                if token.casefold().rstrip("s") not in product_roots | generic_roots
                and not _FALLBACK_QUANTIFIER.fullmatch(token.casefold())
            ]
            candidate = " ".join(suffix_retained[:2]).strip()
            if candidate and not any(char.isdigit() for char in candidate):
                if candidate not in inferred_candidates:
                    inferred_candidates.append(candidate)
    return tuple(explicit_candidates or inferred_candidates or candidates)


def _metadata_reference_score(
    reference: str,
    value: str,
    label: str,
    brand: str,
    requirements: RequirementSet,
) -> int:
    plausible, strong = _plausible_fallback_reference(reference)
    if not plausible or est_grandeur_physique(reference):
        return 0
    if _EXPLICIT_REFERENCE_METADATA.search(label):
        return 4

    normalized_value = _normalise(value)
    reference_matches = _literal_matches(reference, value)
    brand_matches = _literal_matches(brand, value)
    if reference_matches and brand_matches:
        brand_end = normalized_value.find(brand_matches[0]) + len(brand_matches[0])
        brand_start = normalized_value.find(brand_matches[0])
        reference_start = normalized_value.find(reference_matches[0])
        reference_end = reference_start + len(reference_matches[0])
        between = normalized_value[brand_end:reference_start]
        if reference_start >= brand_end and re.fullmatch(r"[\s:|\-\u2013\u2014]*", between):
            return 3
        between = normalized_value[reference_end:brand_start]
        if brand_start >= reference_end and re.fullmatch(r"[\s:|\-\u2013\u2014]*", between):
            return 3
    if strong:
        return 2
    if _fallback_product_vocabulary(value, requirements):
        return 1
    return 0


def _url_reference_candidates(
    document: DiscoveryDocument,
    brand: str,
) -> tuple[str, ...]:
    """Relit les codes atomiques d'une URL attribuee a la marque cible."""
    decoded_url = unquote(document.url)
    if not identity_present(brand, decoded_url):
        return ()
    canonical_brand = _canonical(brand)
    found: list[str] = []
    for token in re.split(r"[/+_?&=#.-]+", decoded_url):
        plausible, strong = _plausible_fallback_reference(token)
        canonical = _canonical(token)
        if (
            not plausible
            or not strong
            or est_grandeur_physique(token)
            or canonical_brand in canonical
        ):
            continue
        if token not in found:
            found.append(token)
    return tuple(found)


def deterministic_metadata_proposals(
    document: DiscoveryDocument,
    requirements: RequirementSet,
    target_brand: str | None,
) -> tuple[CandidateProposal, ...]:
    """Extrait les identites explicites des titres/metadonnees pour chercher.

    Ce chemin ne cree que des pistes. Il n'alimente jamais les preuves de
    compatibilite, qui restent soumises aux extraits techniques du corps de
    page dans ``compatibilite.py``.
    """
    metadata_fields = _discovery_metadata_fields(document)
    brands = _metadata_brand_candidates(
        document, metadata_fields, target_brand, requirements
    )
    # Avec une marque imposee, une caracteristique compacte repetee dans un
    # titre (`AC3-3P`, `1NF-24VDC`) ne doit pas prendre la place d'une vraie
    # reference. On exige alors soit un libelle d'identite explicite, soit une
    # reference directement voisine de la marque. Sans marque cible, la
    # corroboration par les metadonnees et le domaine reste necessaire pour
    # identifier Blue TAG ou Sinwa.
    minimum_score = 3 if target_brand and target_brand.strip() else 1
    found: list[CandidateProposal] = []
    best_brand_score_by_reference: dict[str, int] = {}
    for brand in brands:
        scored: list[tuple[int, str]] = []
        scored.extend(
            (3, reference)
            for reference in _url_reference_candidates(document, brand)
        )
        for _, label, value in metadata_fields:
            if (
                _EXPLICIT_REFERENCE_METADATA.search(label)
                and 2 <= len(_segments(value)) <= 4
                and re.fullmatch(r"[\w./\-\s]+", value, re.UNICODE)
                and _plausible_fallback_reference(value)[0]
            ):
                # Un SKU explicite peut contenir plusieurs groupes séparés
                # d'espaces ; l'extraction atomique perdrait son préfixe.
                scored.append((5, value))
            for match in _FALLBACK_IDENTIFIER.finditer(value):
                reference = match.group(0)
                if reference_is_dimension_signature(reference):
                    continue
                score = _metadata_reference_score(
                    reference, value, label, brand, requirements,
                )
                if score >= minimum_score:
                    scored.append((score, reference))
        bounded_content = next((
            value for field, value in _document_fields(document, bounded=True)
            if field == "content"
        ), "")
        for match in _FALLBACK_IDENTIFIER.finditer(bounded_content):
            reference = match.group(0)
            plausible, _ = _plausible_fallback_reference(reference)
            if (
                plausible
                and not reference_is_dimension_signature(reference)
                and _brand_reference_attributed(
                    brand,
                    reference,
                    bounded_content,
                    requirements if not (target_brand and target_brand.strip()) else None,
                )
            ):
                scored.append((4, reference))
        if not scored:
            continue
        # Une page produit peut publier a la fois une designation commerciale
        # et un code de commande. Ce sont deux alias du meme produit, pas deux
        # pistes qui doivent consommer la moitie du registre. Le libelle le
        # plus explicite gagne sur la simple proximite dans le titre.
        best_score = max(score for score, _ in scored)
        for score, reference in scored:
            if score != best_score:
                continue
            proposal = _without_redundant_brand_suffix(
                CandidateProposal(brand=brand, reference=reference)
            )
            if (
                _is_origin_identity(proposal, requirements)
                or _is_isolated_requirement_value(reference, requirements)
            ):
                continue
            reference_key = _canonical(proposal.reference)
            previous_score = best_brand_score_by_reference.get(reference_key, -1)
            if score < previous_score:
                continue
            if score > previous_score:
                found = [
                    item for item in found
                    if _canonical(item.reference) != reference_key
                ]
                best_brand_score_by_reference[reference_key] = score
            if proposal not in found:
                found.append(proposal)
    return tuple(found)


def _discovery_metadata_relevance(
    document: DiscoveryDocument,
    requirements: RequirementSet,
) -> int:
    """Compte les valeurs demandees relues dans les metadonnees de recherche.

    Ce compteur sert uniquement a ordonner les pistes avant audit. Il ne
    produit aucun statut `proven` et n'entre jamais dans le score technique.
    """
    metadata = [unquote(document.url)]
    metadata.extend(value for _, _, value in _discovery_metadata_fields(document))
    canonical_metadata = _canonical("\n".join(metadata))
    return sum(
        bool(value) and value in canonical_metadata
        for criterion in requirements.criteria
        if len(value := _canonical(criterion.requested_value)) >= 2
    )


def _fallback_clause_bounds(
    source: str, start: int, end: int
) -> tuple[int, int]:
    """Bornes de la clause locale qui porte l'occurrence courante."""
    lower_bound = max(0, start - 220)
    upper_bound = min(len(source), end + 220)
    # Les bornes passent par `pos`/`endpos` plutot que par un decoupage : une
    # tranche masquerait le caractere qui precede, or le motif de frontiere le
    # regarde pour distinguer le point d'une abreviation de celui d'une fin de
    # phrase. Sur une tranche, ce lookbehind echouerait toujours en tete.
    previous = list(
        _FALLBACK_CLAUSE_BOUNDARY.finditer(source, lower_bound, start)
    )
    local_start = previous[-1].end() if previous else lower_bound
    following = _FALLBACK_CLAUSE_BOUNDARY.search(source, end, upper_bound)
    local_end = following.start() if following else upper_bound
    return local_start, local_end


def _fallback_product_vocabulary(
    clause: str, requirements: RequirementSet
) -> bool:
    words = {word.rstrip("s") for word in _segments(clause)}
    requirement_words = {
        word.rstrip("s") for word in _segments(requirements.product)
        if len(word) >= 4
    }
    generic_words = {word.rstrip("s") for word in _FALLBACK_PRODUCT_WORDS}
    return bool(words & (generic_words | requirement_words))


def _fallback_attribution_segment(
    source: str, reference_start: int, reference_end: int
) -> str:
    """Isole le cote de comparaison qui contient la reference courante."""
    clause_start, clause_end = _fallback_clause_bounds(
        source, reference_start, reference_end
    )
    clause = source[clause_start:clause_end]
    local_reference_start = reference_start - clause_start
    local_reference_end = reference_end - clause_start
    separators = list(_FALLBACK_SIDE_SEPARATOR.finditer(clause))
    if set(_segments(clause)).intersection(_FALLBACK_COMPARE_VERBS):
        separators.extend(_FALLBACK_COMPARE_SEPARATOR.finditer(clause))
    start, end = 0, len(clause)
    for separator in sorted(separators, key=lambda item: item.start()):
        if separator.end() <= local_reference_start:
            start = separator.end()
        elif separator.start() >= local_reference_end:
            end = separator.start()
            break
    return clause[start:end]


def _fallback_is_comparative(source: str) -> bool:
    words = set(_segments(source))
    return bool(
        words.intersection(_FALLBACK_COMPARE_VERBS)
        or _FALLBACK_SIDE_SEPARATOR.search(source)
    )


def _fallback_url_has_brand_attribution(
    bounded_fields: Sequence[tuple[FieldName, str]],
    target_brand: str,
    reference: str,
) -> bool:
    """Une reference d'URL exige des metadonnees qui l'attribuent a la marque."""
    metadata = [
        (field, _normalise(raw_source))
        for field, raw_source in bounded_fields
        if field != "url"
    ]
    reference_segments = _segments(reference)
    separator = r"[\W_]*"
    reference_pattern = re.compile(
        r"(?<![^\W_])"
        + separator.join(re.escape(part) for part in reference_segments)
        + r"(?![^\W_])",
        re.UNICODE,
    )
    explicit_reference_seen = False
    for _, source in metadata:
        for reference_match in reference_pattern.finditer(source):
            explicit_reference_seen = True
            segment = _fallback_attribution_segment(
                source, *reference_match.span()
            )
            if identity_present(target_brand, segment):
                return True
    if explicit_reference_seen:
        return False
    for field, source in metadata:
        if (
            field in {"title", "snippet"}
            and identity_present(target_brand, source)
            and not _fallback_is_comparative(source)
        ):
            return True
    return False


def _plausible_fallback_reference(reference: str) -> tuple[bool, bool]:
    """Rend (forme minimale, forme forte) sans connaitre un catalogue."""
    canonical = _canonical(reference)
    range_quantity = _FALLBACK_RANGE_QUANTITY.fullmatch(reference)
    if (
        len(canonical) < 4
        or len(canonical) > _FALLBACK_MAX_CANONICAL_LENGTH
        or not any(character.isalpha() for character in canonical)
        or not any(character.isdigit() for character in canonical)
        or _FALLBACK_QUANTIFIER.fullmatch(canonical)
        # Une configuration compacte trouvee dans un titre (par exemple
        # ``AC3-3P`` ou ``1NF-24VDC``) est utile pour orienter la recherche,
        # mais ce n'est jamais une reference produit autonome.
        or _FALLBACK_COMPACT_SPEC.fullmatch(canonical)
        or (
            range_quantity is not None
            and range_quantity.group(1).casefold() in _UNITES
        )
        or (
            len(canonical) >= 20
            and re.fullmatch(r"[0-9a-f]+", canonical) is not None
        )
    ):
        return False, False
    transitions = sum(
        left.isdigit() != right.isdigit()
        for left, right in zip(canonical, canonical[1:])
    )
    separated = bool(re.search(r"[-\u2013\u2014]", reference))
    strong = (
        (separated and len(canonical) >= 5)
        or (len(canonical) >= 8 and transitions >= 2)
    )
    return True, strong


def deterministic_proposals(
    document: DiscoveryDocument,
    requirements: RequirementSet,
    target_brand: str | None,
) -> tuple[CandidateProposal, ...]:
    """Extrait seulement des références mixtes présentes près d'une marque cible confirmée."""
    bounded_fields = _document_fields(document, bounded=True)
    brand_in_metadata_or_content = bool(target_brand and target_brand.strip()) and any(
        field != "url" and identity_present(target_brand, source)
        for field, source in bounded_fields
    )
    if not brand_in_metadata_or_content:
        return ()
    found: list[CandidateProposal] = []
    # Le repli parcourt uniquement la projection bornee. Une reference peut
    # venir du chemin URL, mais la marque a deja ete exigee dans les metadonnees
    # lisibles ou le contenu borne ci-dessus.
    for field, raw_source in bounded_fields:
        source = unquote(raw_source)
        for match in _FALLBACK_IDENTIFIER.finditer(source):
            reference = match.group(0)
            plausible, strong = _plausible_fallback_reference(reference)
            if not plausible:
                continue
            if field == "url":
                attributed = strong and _fallback_url_has_brand_attribution(
                    bounded_fields,
                    target_brand,
                    reference,
                )
            else:
                attribution_segment = _fallback_attribution_segment(
                    source, *match.span()
                )
                attributed = (
                    identity_present(target_brand, attribution_segment)
                    and (
                        strong
                        or _fallback_product_vocabulary(
                            attribution_segment, requirements
                        )
                    )
                )
            if not attributed:
                continue
            proposal = CandidateProposal(
                brand=target_brand.strip(), reference=reference
            )
            if _is_origin_identity(proposal, requirements) or _is_isolated_requirement_value(proposal.reference, requirements):
                continue
            if proposal not in found:
                found.append(proposal)
    return tuple(found)


def parse_candidate_proposals(items: Sequence[object]) -> CandidateParseResult:
    proposals: list[CandidateProposal] = []
    rejected: list[RejectedCandidate] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("brand"), str) and isinstance(item.get("reference"), str):
            brand = item["brand"].strip()
            reference = item["reference"].strip()
            if brand and reference:
                proposals.append(CandidateProposal(brand=brand, reference=reference))
                continue
        rejected.append(RejectedCandidate("", "", "malformed_proposal"))
    return CandidateParseResult(tuple(proposals), tuple(rejected))


def audit_proposals(audit: PageAudit) -> list[CandidateProposal]:
    return [CandidateProposal(brand=item.brand, reference=item.reference) for item in audit.candidates]


def filter_page_audit(
    audit: PageAudit,
    *,
    page_url: str,
    title: str,
    content: str,
    document: DiscoveryDocument | None = None,
    authorized_candidates: Sequence[CandidateLead] = (),
    targeted: bool = False,
) -> PageAudit:
    allowed = {lead.key: lead for lead in authorized_candidates}
    identity_document = document or build_discovery_document(
        url=page_url, title=title, snippets=(), content=content, rank=1,
    )
    page_identity_text = f"{title}\n{content}"
    reference_sources = [page_identity_text]
    if document is not None:
        reference_sources.extend((
            unquote(document.url),
            document.title,
            *document.snippets,
        ))
    kept: list[CandidateAudit] = []
    for candidate in audit.candidates:
        key = canonical_candidate_key(candidate.brand, candidate.reference)
        if key[0] == key[1] or _brand_is_product_noun(candidate.brand) or _explicit_metadata_brand_conflict(
            candidate.brand, identity_document
        ):
            continue
        if targeted and key not in allowed:
            continue
        lead = allowed.get(key)
        reference_seen = any(
            reference_is_present(candidate.reference, source)
            for source in reference_sources
        )
        if lead is not None:
            reference_seen = reference_seen or any(
                canonical_url(item.url) == canonical_url(page_url)
                for item in lead.occurrences
            )
        if reference_seen and brand_is_present(candidate.brand, page_identity_text):
            kept.append(candidate)
    return PageAudit(page_url=page_url, candidates=kept)


def build_discovery_document(
    *,
    url: str,
    title: str,
    snippets: Sequence[str],
    content: str,
    rank: int,
    limits: DiscoveryLimits = DiscoveryLimits(),
) -> DiscoveryDocument:
    """Conserve la page entière et fabrique séparément le corpus borné du prompt."""
    truncated: list[str] = []

    def limited(value: str, maximum: int, field: str) -> str:
        if len(value) > maximum:
            truncated.append(field)
            return value[:maximum]
        return value

    prompt_title = limited(title, limits.title_chars, "title")
    prompt_snippets = limited("\n".join(snippets), limits.snippet_chars, "snippet")
    prompt_content = limited(content, limits.content_chars, "content")
    prompt_parts = (
        ("url", f"URL: {unquote(url)}", unquote(url)),
        ("title", f"TITLE: {prompt_title}", prompt_title),
        ("snippet", f"SNIPPETS: {prompt_snippets}", prompt_snippets),
        ("content", f"CONTENT: {prompt_content}", prompt_content),
    )
    remaining = max(limits.total_chars, 0)
    bounded_fields: list[tuple[FieldName, str]] = []
    for field, _, value in prompt_parts:
        if remaining <= 0:
            break
        bounded_value = value[:remaining]
        if bounded_value:
            bounded_fields.append((field, bounded_value))  # type: ignore[arg-type]
            remaining -= len(bounded_value)
    prompt = "\n".join(part for _, part, _ in prompt_parts)
    if len(prompt) > limits.total_chars:
        offset = 0
        for field, part, value in prompt_parts:
            offset += len(part)
            if value and limits.total_chars < offset and field not in truncated:
                truncated.append(field)
            offset += 1
        truncated.append("total")
        prompt = prompt[:limits.total_chars]
    return DiscoveryDocument(
        url=url,
        title=title,
        snippets=tuple(snippets),
        content=content,
        prompt_source=prompt,
        rank=rank,
        truncated_fields=tuple(truncated),
        bounded_fields=tuple(bounded_fields),
    )


class CandidateRegistry:
    def __init__(self, limits: DiscoveryLimits = DiscoveryLimits()) -> None:
        self._limits = limits
        self._leads: dict[tuple[str, str], CandidateLead] = {}
        self._target_brand: str | None = None

    def ingest(
        self,
        proposals: Sequence[CandidateProposal],
        document: DiscoveryDocument,
        requirements: RequirementSet,
        target_brand: str | None,
        *,
        allow_deterministic_fallback: bool = True,
    ) -> CandidateIngestResult:
        if target_brand and target_brand.strip():
            self._target_brand = target_brand.strip()
        accepted_keys: set[tuple[str, str]] = set()
        rejected: list[RejectedCandidate] = []

        def consider(proposal: CandidateProposal, *, fallback: bool = False) -> None:
            proposal = _without_redundant_brand_suffix(proposal)
            reason = rejection_reason(
                proposal,
                document,
                requirements,
                target_brand,
                bounded=fallback,
            )
            if reason:
                rejected.append(RejectedCandidate(proposal.brand, proposal.reference, reason))
                return
            key = canonical_candidate_key(proposal.brand, proposal.reference)
            occurrences = find_occurrences(
                proposal.reference, document, bounded=fallback
            )
            low_confidence = fallback or _low_confidence(
                proposal, document, target_brand, bounded=fallback
            )
            discovery_relevance = _discovery_metadata_relevance(
                document, requirements,
            )
            identity_strength = _identity_strength(proposal, document)
            nested_keys = [
                existing_key
                for existing_key in self._leads
                if existing_key[0] == key[0]
                and _nested_reference(existing_key[1], key[1])
            ]
            if nested_keys:
                nested_leads = [self._leads.pop(item) for item in nested_keys]
                lead = _merge_nested_leads(
                    nested_leads,
                    proposal,
                    occurrences,
                    low_confidence=low_confidence,
                    discovery_relevance=discovery_relevance,
                    identity_strength=identity_strength,
                )
                accepted_keys.difference_update(nested_keys)
            else:
                lead = merge_lead(
                    self._leads.get(key),
                    proposal,
                    occurrences,
                    low_confidence=low_confidence,
                    discovery_relevance=discovery_relevance,
                    identity_strength=identity_strength,
                )
            self._leads[lead.key] = lead
            accepted_keys.add(lead.key)

        for proposal in proposals:
            consider(proposal)
        # Un titre ou une metadonnee produit porte une identite exploitable
        # pour la recherche, meme si le graphe de decouverte ne l'a pas rendue.
        # Ces pistes sont ingerees comme des identites litterales normales :
        # elles ne deviennent une preuve qu'apres un audit technique distinct.
        if allow_deterministic_fallback:
            for proposal in deterministic_metadata_proposals(
                document, requirements, target_brand
            ):
                consider(proposal)
        if allow_deterministic_fallback and not accepted_keys:
            for proposal in deterministic_proposals(document, requirements, target_brand):
                consider(proposal, fallback=True)
        ranked = rank_leads(tuple(self._leads.values()), target_brand)
        self._leads = {lead.key: lead for lead in ranked[: self._limits.max_active]}
        return CandidateIngestResult(
            tuple(lead for lead in self.active() if lead.key in accepted_keys),
            tuple(rejected),
        )

    def active(self) -> tuple[CandidateLead, ...]:
        return rank_leads(tuple(self._leads.values()), self._target_brand)
