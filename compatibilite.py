# -*- coding: utf-8 -*-
"""Validation des preuves et calcul déterministe de compatibilité B2."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, deque
from functools import lru_cache
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from tld import get_fld

from marques_connues import alias_canoniques, domaines_officiels
from modeles import (
    CandidateAudit,
    CandidateEvaluation,
    CompatibilitySummary,
    CriterionAudit,
    PageAudit,
    Requirement,
    RequirementSet,
    SourceProof,
)


class CompatibilityContractError(ValueError):
    """L'audit ne couvre pas le jeu immuable des critères."""


class EvidenceContractError(ValueError):
    """Une preuve ne correspond pas à une page réellement récupérée."""


def _est_page_de_liste(url: str) -> bool:
    """Import differe pour garder `recherche` comme source unique sans cycle."""
    from recherche import est_page_de_liste

    return est_page_de_liste(url)


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    query = urlencode([
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"gclid", "fbclid", "srsltid"}
    ])
    return urlunsplit((
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        parsed.path or "/",
        query,
        "",
    ))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


#: Apostrophes typographiques ramenées à l'apostrophe simple. `’` (U+2019) est
#: la forme la plus courante en typographie française ; `‘` et `ʼ` couvrent
#: les variantes plausibles d'un texte scrapé.
_APOSTROPHE_VARIANTS = "’‘ʼ"

#: Tirets ramenés au trait d'union simple. Sous-ensemble de
#: `recherche_catalogue/comparaison.py:_REFERENCE_JOINERS`, sans `-` (déjà la
#: cible) ni `/` (pas un tiret).
_DASH_VARIANTS = "‐‑‒–—―−﹘﹣－"

#: Trait d'union conditionnel : invisible hors coupure de ligne, supprimé
#: plutôt que remplacé.
_SOFT_HYPHEN = "­"

_PROOF_TRANSLATION = str.maketrans(
    {
        **{char: "'" for char in _APOSTROPHE_VARIANTS},
        **{char: "-" for char in _DASH_VARIANTS},
        _SOFT_HYPHEN: None,
    }
)


def _normalized_proof(value: str) -> str:
    """Normalise un extrait ou un contenu de page pour la comparaison de preuve.

    NFKD seul, tel que la generation precedente l'appliquait, ne
    couvre que la composition unicode (accents, `µ`/`μ`) : vérifié, il laisse
    apostrophes typographiques, tirets et trait d'union conditionnel
    strictement inchangés. Dédiée à `:84-85` — `_normalized` reste inchangée
    ailleurs (comparaison de `requested_value`, `_manufacturer_domain`).
    """
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    decomposed = "".join(c for c in decomposed if not unicodedata.combining(c))
    translated = decomposed.translate(_PROOF_TRANSLATION)
    return " ".join(translated.casefold().split())


_DOMAIN_BRAND_DESCRIPTORS = frozenset({
    "ag", "co", "company", "corp", "corporation", "electric", "electrical",
    "electronics", "france", "gmbh", "group", "inc", "industries",
    "industrial", "international", "limited", "llc", "ltd", "plc", "sa",
    "sarl", "sas",
})
#: Domaines officiels reconnus, par marque. Aucune marque n'est nommée dans
#: ce module : la table vient de `marques_connues`, qui lit un fichier de
#: configuration (`B2_MARQUES_CONNUES`, à défaut `marques.exemple.json`).
#: Table vide = aucune exception, la règle générale du domaine enregistrable
#: s'applique seule.
_KNOWN_OFFICIAL_DOMAINS: dict[frozenset[str], frozenset[str]] = domaines_officiels()
def _manufacturer_domain(url: str, brand: str) -> bool:
    """Valide le domaine enregistrable, jamais une sous-chaîne du hostname."""
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    labels = hostname.split(".")
    if len(labels) < 2:
        return False
    brand_tokens = set(re.findall(r"[a-z0-9]+", _normalized(brand)))
    identity = brand_tokens - _DOMAIN_BRAND_DESCRIPTORS
    if not identity:
        return False

    known_domains = _KNOWN_OFFICIAL_DOMAINS.get(frozenset(identity))
    if known_domains is not None:
        return any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in known_domains
        )

    # `tld` fournit une copie locale de la Public Suffix List : aucun appel
    # réseau et aucune table maison incomplète pour les suffixes comme
    # `.co.uk`, `.com.vn` ou `.co.id`.
    registrable_domain = get_fld(url, fail_silently=True)
    if not registrable_domain:
        return False
    organization = re.sub(
        r"[^a-z0-9]", "", _normalized(registrable_domain.split(".", 1)[0])
    )
    if not organization:
        return False
    components = sorted(
        identity | _DOMAIN_BRAND_DESCRIPTORS,
        key=lambda component: (-len(component), component),
    )
    found: set[str] = set()
    remaining = organization
    while remaining:
        component = next(
            (item for item in components if remaining.startswith(item)),
            "",
        )
        if not component:
            return False
        if component in identity:
            found.add(component)
        remaining = remaining[len(component):]
    return identity <= found


def _source_domain(url: str) -> str:
    """Identifie l'organisation source, sans recompter ses sous-domaines."""
    registrable = get_fld(url, fail_silently=True)
    if registrable:
        return registrable.casefold().rstrip(".")
    return (urlsplit(url).hostname or "").casefold().rstrip(".")


_ORIGIN_IDENTITY_LABEL_HINTS: tuple[str, ...] = (
    "fabricant",
    "manufacturer",
    "marque",
    "brand",
    "reference",
    "mpn",
    "part number",
)


def _est_identite_origine(
    requirement: Requirement,
    requirements: RequirementSet,
) -> bool:
    """Isole l'identité de départ par libellé ou valeur, sans classer la fonction.

    L'import reste local parce que ``candidats`` réutilise ``canonical_url`` de
    ce module. Les identités de référence sont bornées au registre extrait :
    tester toute sous-chaîne de ``product`` classerait aussi sa description
    fonctionnelle comme identité d'origine.
    """
    normalized = _normalized_proof(requirement.label)
    words = set(re.findall(r"[a-z0-9]+", normalized))
    for hint in _ORIGIN_IDENTITY_LABEL_HINTS:
        normalized_hint = _normalized_proof(hint)
        if " " in normalized_hint:
            if normalized_hint in normalized:
                return True
        elif normalized_hint in words:
            return True
    from candidats import (
        canonical_candidate_key,
        is_origin_identity,
        origin_identities,
    )

    value = requirement.requested_value
    if is_origin_identity(value, "", requirements):
        return True
    reference_key = canonical_candidate_key("", value)[1]
    identities = origin_identities(requirements)
    return bool(
        reference_key
        and reference_key in identities.references | identities.ranges
        and is_origin_identity("", value, requirements)
    )


_NUMBER = r"\d+(?:[.,]\d+)?"
_UNIT = r"(?:vdc|vac|adc|aac|kva|kwh|khz|mhz|ghz|kv|mv|ka|ma|kw|mw|hz|bar|kpa|mpa|pa|rpm|nm|mm|cm|km|um|v|a|w|c|k)"
_MODE = r"(?:ac\s*/\s*dc|dc\s*/\s*ac|ca\s*/\s*cc|cc\s*/\s*ca|ac|dc|ca|cc)"
_POINT_PATTERN = re.compile(
    rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})(?:\s*(?P<mode>{_MODE}))?(?!\w)",
    re.IGNORECASE,
)
_RANGE_PATTERN = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:-|\.{{2,}}|\ba\b|\bto\b)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>{_UNIT})(?:\s*(?P<mode>{_MODE}))?(?!\w)",
    re.IGNORECASE,
)
_PREFIX_MODE_PATTERN = re.compile(
    rf"\b(?P<mode>{_MODE})\b\s*[:=]?\s*$",
    re.IGNORECASE,
)


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _quantity_signature(unit: str, mode: str | None) -> tuple[str, frozenset[str]]:
    normalized_unit = unit.casefold()
    normalized_mode = (mode or "").casefold().replace(" ", "")
    modes: set[str] = set()
    if normalized_unit.endswith("dc"):
        normalized_unit = normalized_unit[:-2]
        modes.add("dc")
    elif normalized_unit.endswith("ac"):
        normalized_unit = normalized_unit[:-2]
        modes.add("ac")
    for item in normalized_mode.split("/"):
        if item in {"dc", "cc"}:
            modes.add("dc")
        elif item in {"ac", "ca"}:
            modes.add("ac")
    return normalized_unit, frozenset(modes)


def _quantity_signature_at(
    text: str,
    match: re.Match[str],
) -> tuple[str, frozenset[str]]:
    """Lit aussi un mode placé juste avant la grandeur (`DC: 24 V`)."""
    mode = match.group("mode")
    if not mode:
        prefix = text[max(0, match.start() - 24):match.start()]
        prefixed = _PREFIX_MODE_PATTERN.search(prefix)
        if prefixed:
            mode = prefixed.group("mode")
    return _quantity_signature(match.group("unit"), mode)


def _range_contains_requested(requested_value: str, observed_value: str) -> bool | None:
    """Compare une valeur ponctuelle à une plage de même unité et même mode.

    `None` signifie que le parseur ne sait pas conclure. L'appelant rétrograde
    alors l'incompatibilité en `not_proven` au lieu de créer un bloqueur.
    """
    requested_text = _normalized_proof(requested_value).replace("…", "...")
    observed_text = _normalized_proof(observed_value).replace("…", "...")
    point = _POINT_PATTERN.search(requested_text)
    if point is None:
        return None
    requested_number = _decimal(point.group("value"))
    if requested_number is None:
        return None
    requested_unit, requested_modes = _quantity_signature_at(requested_text, point)

    ranges = list(_RANGE_PATTERN.finditer(observed_text))
    unknown_mode = False
    excluded = False
    comparable = False
    for item in ranges:
        low = _decimal(item.group("low"))
        high = _decimal(item.group("high"))
        if low is None or high is None:
            continue
        observed_unit, observed_modes = _quantity_signature_at(observed_text, item)
        if observed_unit != requested_unit:
            continue
        comparable = True
        lower, upper = min(low, high), max(low, high)
        if not lower <= requested_number <= upper:
            excluded = True
            continue
        if requested_modes and not observed_modes:
            unknown_mode = True
            continue
        if requested_modes and requested_modes.isdisjoint(observed_modes):
            excluded = True
            continue
        return True

    range_spans = [item.span() for item in ranges]
    range_index = 0
    for item in _POINT_PATTERN.finditer(observed_text):
        while (
            range_index < len(range_spans)
            and range_spans[range_index][1] <= item.start()
        ):
            range_index += 1
        if (
            range_index < len(range_spans)
            and range_spans[range_index][0] <= item.start()
            and item.end() <= range_spans[range_index][1]
        ):
            continue
        observed_number = _decimal(item.group("value"))
        if observed_number is None:
            continue
        observed_unit, observed_modes = _quantity_signature_at(observed_text, item)
        if observed_unit != requested_unit:
            continue
        comparable = True
        if observed_number != requested_number:
            excluded = True
            continue
        if requested_modes and not observed_modes:
            unknown_mode = True
            continue
        if requested_modes and requested_modes.isdisjoint(observed_modes):
            excluded = True
            continue
        return True
    if unknown_mode:
        return None
    return False if comparable and excluded else None


def _effective_status(requirement: Requirement, criterion: CriterionAudit) -> str:
    requested_is_quantity = (
        _POINT_PATTERN.search(_normalized_proof(requirement.requested_value)) is not None
    )
    if criterion.status == "proven" and requested_is_quantity:
        comparison = _range_contains_requested(
            requirement.requested_value,
            criterion.observed_value,
        )
        if comparison is True:
            return "proven"
        if comparison is False:
            return "incompatible"
        return "not_proven"
    if criterion.status != "incompatible":
        return criterion.status
    if not criterion.observed_value.strip():
        return "not_proven"
    if not requested_is_quantity:
        return "incompatible"
    comparison = _range_contains_requested(
        requirement.requested_value,
        criterion.observed_value,
    )
    if comparison is True:
        return "proven"
    if comparison is False:
        return "incompatible"
    return "not_proven"


_PROOF_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_WINDOWED_PROOF_MAX_CHARS = 300
_IDENTITY_LISTING_MAX_CHARS = 180
_TECHNICAL_LISTING_VALUE = re.compile(
    rf"(?:{_NUMBER}\s*{_UNIT}(?:\s*{_MODE})?(?!\w)|"
    r"\d+\s*(?:p|poles?|no|nc|nf)(?!\w)|ip\s*\d+(?!\w))",
    re.IGNORECASE,
)
_TECHNICAL_CONTEXT_BEFORE_VALUE = re.compile(
    r"\b(?:has|have|with|is|are|rated|nominal|operational|"
    r"est|avec|dispose)\b",
    re.IGNORECASE,
)

# Une preuve secondaire mise en cache n'est relue que pres de l'identite
# produit confirmee. La fenetre couvre les tableaux compacts des distributeurs
# sans transformer une page catalogue entiere en preuve du candidat.
_CACHED_EVIDENCE_BEFORE = 350
_CACHED_EVIDENCE_AFTER = 550
_CACHED_IDENTITY_MAX_ALIASES = 16
_CACHED_IDENTITY_MAX_MATCHES = 24
_CACHED_IDENTITY_MAX_WINDOWS = 12
_CACHED_IDENTITY_MAX_MERGED_WINDOW = 1_800
_CACHED_OFFICIAL_EVIDENCE_MAX_CHARS = 12_000
_CACHED_DESIGNATION = re.compile(
    r"[a-z0-9]+(?:[-_][a-z0-9]+)+",
    re.IGNORECASE,
)
_CACHED_USAGE_LABELS = ("usage", "utilisation", "application")
_CACHED_CURRENT_LABELS = (
    "courant nominal", "rated operational current", "operational current",
    "rated current",
)
_COMPARATIVE_IDENTITY_MARKER = re.compile(
    r"\b(?:alternative|equivalen\w*|replacement|replace\w*|"
    r"compar\w*|versus|vs)\b",
    re.IGNORECASE,
)
_NEXT_PRODUCT_BOUNDARY = re.compile(
    r"\b(?:related|other)\s+(?:product|model|reference|ref|"
    r"produit|mod[eè]le|r[eé]f[eé]rence)\b",
    re.IGNORECASE,
)
_PLAUSIBLE_PRODUCT_IDENTIFIER = re.compile(
    r"\b(?:ref[-_][a-z0-9]+|(?=[a-z0-9_-]{3,}\b)"
    r"(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)"
    r"[a-z0-9]+(?:[-_][a-z0-9]+)*)\b",
    re.IGNORECASE,
)
_UNIT_LIKE_IDENTIFIER = re.compile(
    r"^(?:\d+(?:[.,]\d+)?(?:v|a|ma|ka)(?:ac|dc)?|"
    r"(?:ac|dc)\d*(?:[-_][a-z0-9]+)*|ip\d*|"
    r"[a-z0-9]+[-_]\d+(?:v|a)(?:ac|dc))$",
    re.IGNORECASE,
)
_TECHNICAL_IDENTIFIER = re.compile(
    r"^(?:\d+[-_](?:pole|phase|stack)|\d+(?:no|nc|nf)|"
    r"\d+hz|ec\d+)$",
    re.IGNORECASE,
)
_STANDARD_IDENTIFIER = re.compile(
    # Une forme lettres+chiffres ne suffit pas : BSL07, XZ07 et KB6 sont des
    # familles Norel réelles. Seules les familles de normes explicites restent
    # exemptées lorsqu'une nouvelle marque précède le code.
    r"^(?:iec|en|ul|vde|gb|csa|nf|iso|din|ansi|nema|rohs|reach|ce)"
    r"\d+(?:[-_]\d+)*$",
    re.IGNORECASE,
)
_AC3_PATTERN = re.compile(r"\bac\s*[- ]?\s*3\b", re.IGNORECASE)
_CURRENT_AC3_HEADING = re.compile(
    r"(?:courant\s+nominal|rated\s+operational\s+current|"
    r"operational\s+current|rated\s+current)[^\n]{0,120}?"
    r"\bac\s*[- ]?\s*3\b",
    re.IGNORECASE,
)
_CACHED_CURRENT_SECTION_MAX_CHARS = 1_200
_CACHED_STOP_WORDS = frozenset({
    "a", "au", "aux", "de", "des", "du", "et", "la", "le", "les",
    "of", "the", "to",
})


def _windowed_excerpt_matches(excerpt: str, content: str) -> bool:
    """Accepte tous les jetons, avec leur multiplicité, dans 300 caractères."""
    tokens = _PROOF_TOKEN_PATTERN.findall(excerpt)
    if not tokens or not any(
        len(token) >= 6 or any(character.isdigit() for character in token)
        for token in tokens
    ):
        return False
    minimum_span = sum(len(token) for token in tokens) + len(tokens) - 1
    if minimum_span > _WINDOWED_PROOF_MAX_CHARS:
        return False

    needed = Counter(tokens)
    present: Counter[str] = Counter()
    satisfied = 0
    window: deque[tuple[int, int, str]] = deque()
    for match in _PROOF_TOKEN_PATTERN.finditer(content):
        token = match.group(0)
        if token not in needed:
            continue
        start, end = match.span()
        window.append((start, end, token))
        present[token] += 1
        if present[token] == needed[token]:
            satisfied += 1
        while window and end - window[0][0] > _WINDOWED_PROOF_MAX_CHARS:
            _, _, old_token = window.popleft()
            if present[old_token] == needed[old_token]:
                satisfied -= 1
            present[old_token] -= 1
        if satisfied == len(needed):
            return True
    return False


#: Une designation produit lisible telle quelle : des segments alphanumeriques
#: ponctues, portant a la fois lettres et chiffres. `200-K09ZL01M` en est une ;
#: `24V DC`, `AC-3` et `3P` sont des valeurs techniques, pas des identites.
_PAGE_DESIGNATION = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9]{2,}(?:[-_.][A-Za-z0-9]+)+(?![A-Za-z0-9])"
)
_PAGE_DESIGNATION_MIN_CHARS = 8

#: Au-dela, la page enumere un rayon plutot qu'elle ne decrit un produit.
#: Mesure du 2026-08-27 sur deux corpus de rejeu : les pages de categorie
#: portaient 49 et 83 identites distinctes, tandis qu'aucune fiche produit n'en
#: portait plus de treize. Le seuil laisse donc une marge des deux cotes.
_MULTI_PRODUCT_PAGE_MIN_IDENTITIES = 20


#: Au-dela, on ne lit qu'une tete de page. Distinguer un rayon d'une fiche
#: produit ne demande pas de parcourir un manuel PDF d'un million de
#: caracteres : la densite d'identites se lit des les premieres pages, et
#: balayer le reste coute de la memoire sans rien apprendre.
_PAGE_IDENTITY_SCAN_MAX_CHARS = 200_000


@lru_cache(maxsize=8)
def _page_identity_count(content: str) -> int:
    """Compte les identites produit distinctes lisibles dans une page.

    Memoise : la meme page est reexaminee pour chaque preuve de chaque critere.
    Le cache reste court, car il retient des contenus de page entiers.
    """
    identities: set[str] = set()
    for match in _PAGE_DESIGNATION.finditer(
        content[:_PAGE_IDENTITY_SCAN_MAX_CHARS]
    ):
        canonical = "".join(
            character for character in match.group(0).casefold()
            if character.isalnum()
        )
        if (
            len(canonical) >= _PAGE_DESIGNATION_MIN_CHARS
            and any(character.isalpha() for character in canonical)
            and any(character.isdigit() for character in canonical)
        ):
            identities.add(canonical)
    return len(identities)


def _excerpt_borrows_a_neighbour_identity(
    candidate: CandidateAudit,
    excerpt: str,
    content: str,
) -> bool:
    """Sur un rayon, un extrait doit porter l'identite qu'il pretend prouver.

    Mesure du 2026-08-27 : l'extrait `Ministykac 200-K09ZL01M 24V DC`, lu sur
    une page de categorie, prouvait « 24 V DC » pour `HPL1211001R0101` — un
    contacteur dont la bobine est en 24 V AC. L'extrait decrivait la ligne
    voisine de la liste, pas le candidat.

    La regle ne s'applique qu'aux pages qui enumerent. Sur une fiche produit,
    c'est la page qui etablit l'identite : un extrait contextuel comme
    « 3-polovy stykac » y reste une preuve valide, et le doit.

    `est_page_de_liste` ne suffit pas : il lit l'URL, et la page en cause
    (`revendeur-c.example/stykace-c663/`) n'en porte aucun marqueur. Le compte se
    fait donc sur le contenu, ce qui vaut aussi hors des nomenclatures d'URL
    francaises et anglaises.
    """
    if _page_identity_count(content) < _MULTI_PRODUCT_PAGE_MIN_IDENTITIES:
        return False
    return _raw_literal_match(candidate.reference, excerpt) is None


def _excerpt_is_only_an_identity(
    candidate: CandidateAudit,
    excerpt: str,
) -> bool:
    """Un extrait reduit a nommer le produit ne prouve aucune valeur.

    Mesure du 2026-08-27 sur un run reel : la page officielle
    `new.norel.example/products/fr/HPL1213001R0101/kb6-20-10-01` ne rend que sa
    navigation — menus de pays, de langues, « Loading documents ». Ses
    caracteristiques sont chargees dynamiquement et n'etaient pas dans le texte
    recupere. Le modele a cite `KB6-20-10-01` comme preuve des six criteres, et
    la mission a rendu `complete` a 100 % : une page sans aucune donnee
    technique presentee comme une preuve complete.

    Le controle litteral ne pouvait pas le voir : `KB6-20-10-01` figure bel et
    bien dans la page. Ce qui manque, c'est la valeur. Une designation produit
    n'enonce ni courant nominal, ni tension de bobine, ni nombre de poles — quoi
    qu'elle designe.
    """
    reste = _PAGE_DESIGNATION.sub(" ", excerpt)
    for identite in (candidate.brand, candidate.reference):
        if identite and identite.strip():
            reste = re.sub(re.escape(identite.strip()), " ", reste, flags=re.IGNORECASE)
    # Aucun filtre de longueur : `9` et `A` sont toute la substance d'un courant
    # nominal. Ce qui compte est qu'il reste quelque chose une fois l'identite
    # retiree, pas que ce reste soit un mot.
    reste = [
        jeton for jeton in re.split(r"[^\w]+", reste, flags=re.UNICODE)
        if jeton
    ]
    return not reste


def _is_compact_identity_value_listing(
    candidate: CandidateAudit,
    excerpt: str,
) -> bool:
    """Refuse un titre compact qui juxtapose identite et valeurs sans contexte."""
    text = _normalized_proof(excerpt)
    if not text or len(text) > _IDENTITY_LISTING_MAX_CHARS:
        return False
    reference = _raw_literal_match(candidate.reference, text)
    if reference is None or reference.start() > 60:
        return False
    identity_prefix = text[:reference.start()]
    if _raw_literal_match(candidate.brand, identity_prefix) is None:
        return False
    trailing = text[reference.end():]
    values = list(_TECHNICAL_LISTING_VALUE.finditer(trailing))
    if len(values) < 2:
        return False
    context_before_first_value = trailing[:values[0].start()]
    return _TECHNICAL_CONTEXT_BEFORE_VALUE.search(context_before_first_value) is None


def _raw_literal_pattern(value: str) -> str:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value), re.UNICODE)
    if not tokens:
        return ""
    return r"(?<![^\W_])" + r"[\W_]*".join(
        re.escape(token) for token in tokens
    ) + r"(?![^\W_])"


def _raw_literal_match(value: str, source: str) -> re.Match[str] | None:
    """Retrouve dans le texte brut la variante de separateurs d'une valeur."""
    pattern = _raw_literal_pattern(value)
    if not pattern:
        return None
    return re.search(pattern, source, flags=re.IGNORECASE | re.UNICODE)


def _is_family_requirement(requirement: Requirement | None) -> bool:
    if requirement is None:
        return False
    identity = _normalized_proof(f"{requirement.id} {requirement.label}")
    tokens = set(identity.split())
    return bool(
        tokens & {"famille", "family", "serie", "series"}
        or "product range" in identity
        or "gamme produit" in identity
    )


def _is_product_family_alias(value: str) -> bool:
    normalized = _normalized_proof(value)
    if _POINT_PATTERN.search(normalized) or _RANGE_PATTERN.search(normalized):
        return False
    compact = "".join(character for character in normalized if character.isalnum())
    return bool(
        len(compact) >= 4
        and any(character.isalpha() for character in compact)
        and any(character.isdigit() for character in compact)
    )


def _candidate_identity_aliases(
    candidate: CandidateAudit,
    visited_pages: Mapping[str, str],
    requirements: RequirementSet,
) -> tuple[str, ...]:
    """Identites litterales ancrees dans une page fabricant recuperee."""
    values = [candidate.reference]
    canonical_pages = {
        canonical_url(url): content for url, content in visited_pages.items()
    }
    expected = {item.id: item for item in requirements.criteria}
    for criterion in candidate.criteria:
        for proof in criterion.proofs:
            # Une tension, un courant ou un nombre de poles n'est jamais une
            # identite produit. Les alias supplementaires proviennent
            # uniquement d'une URL fabricant deja rattachee au candidat.
            if (
                proof.type != "web_officiel"
                or not _manufacturer_domain(proof.url, candidate.brand)
                or _est_page_de_liste(proof.url)
            ):
                continue
            content = canonical_pages.get(canonical_url(proof.url), "")
            # Le collecteur doit avoir recupere la page et la reference du
            # candidat doit y etre litterale. Une URL seulement emise par le
            # modele n'a aucune autorite pour creer un alias.
            if not content or _raw_literal_match(candidate.reference, content) is None:
                continue
            if (
                _is_family_requirement(expected.get(criterion.requirement_id))
                and criterion.observed_value
                and _is_product_family_alias(criterion.observed_value)
                and _raw_literal_match(criterion.observed_value, content) is not None
            ):
                values.append(criterion.observed_value)
            for designation in _CACHED_DESIGNATION.findall(unquote(proof.url)):
                if _raw_literal_match(designation, content) is None:
                    continue
                values.extend((designation, re.split(r"[-_]", designation, maxsplit=1)[0]))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = "".join(character for character in value.casefold() if character.isalnum())
        if (
            len(compact) < 5
            or not any(character.isalpha() for character in compact)
            or not any(character.isdigit() for character in compact)
            or compact in seen
        ):
            continue
        seen.add(compact)
        result.append(value)
    return tuple(result)


def _identity_compact(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _bounded_identity_end(
    content: str,
    anchor_end: int,
    maximum_end: int,
    *,
    aliases: Sequence[str] = (),
    brand: str = "",
) -> int:
    """Coupe une fenêtre avant l'identité plausible d'un autre produit.

    Une page catalogue juxtapose volontiers plusieurs références sans titre
    explicite. On accepte les désignations déjà connues du candidat, mais une
    nouvelle référence plausible après un libellé produit ou une marque
    redémarrée borne la preuve. Une ligne seule ne suffit pas : les normes
    industrielles ont la même forme qu'une référence. Les unités et normes
    techniques ne sont jamais traitées comme des références.
    """
    boundary = _NEXT_PRODUCT_BOUNDARY.search(content, anchor_end, maximum_end)
    end = boundary.start() if boundary is not None else maximum_end
    known = {_identity_compact(value) for value in aliases}
    brand_pattern = _raw_literal_pattern(brand) if brand else None
    for match in _PLAUSIBLE_PRODUCT_IDENTIFIER.finditer(content, anchor_end, end):
        token = match.group(0)
        if (
            _identity_compact(token) in known
            or _UNIT_LIKE_IDENTIFIER.fullmatch(token)
            or _TECHNICAL_IDENTIFIER.fullmatch(token)
            or _STANDARD_IDENTIFIER.fullmatch(token)
        ):
            continue
        gap = content[anchor_end:match.start()]
        # Deux codes contigus dans un même titre désignent souvent le même
        # produit (code commande + désignation). Une norme comme IEC60947 ou
        # VDE0660 ne doit donc jamais couper la fiche. Une occurrence nouvelle
        # de la marque avec une référence plausible est, elle, un changement
        # de produit fiable.
        if brand_pattern is not None and re.search(
            brand_pattern, gap, flags=re.IGNORECASE | re.UNICODE,
        ) is not None:
            return match.start()
    return end


def _identity_windows(
    content: str,
    aliases: tuple[str, ...],
    *,
    candidate_brand: str = "",
    known_aliases: Sequence[str] = (),
) -> tuple[str, ...]:
    spans: list[tuple[int, int]] = []
    for alias in aliases[:_CACHED_IDENTITY_MAX_ALIASES]:
        pattern = _raw_literal_pattern(alias)
        if not pattern:
            continue
        for match in re.finditer(pattern, content, flags=re.IGNORECASE | re.UNICODE):
            spans.append((
                max(0, match.start() - _CACHED_EVIDENCE_BEFORE),
                _bounded_identity_end(
                    content,
                    match.end(),
                    min(len(content), match.end() + _CACHED_EVIDENCE_AFTER),
                    aliases=known_aliases or aliases,
                    brand=candidate_brand,
                ),
            ))
            if len(spans) >= _CACHED_IDENTITY_MAX_MATCHES:
                break
        if len(spans) >= _CACHED_IDENTITY_MAX_MATCHES:
            break

    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if (
            merged
            and start <= merged[-1][1]
            and end - merged[-1][0] <= _CACHED_IDENTITY_MAX_MERGED_WINDOW
        ):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
        if len(merged) >= _CACHED_IDENTITY_MAX_WINDOWS:
            break
    return tuple(content[start:end] for start, end in merged)


def _official_identity_windows(
    content: str,
    reference: str,
    *,
    aliases: Sequence[str] = (),
    candidate_brand: str = "",
) -> tuple[str, ...]:
    """Fenêtres étendues, mais toujours ancrées à la référence exacte.

    Les fiches officielles peuvent éloigner le tableau de caractéristiques de
    quelques milliers de caractères. Nous élargissons donc seulement *après*
    chaque occurrence de la référence du candidat : un préfixe de catalogue
    portant les données d'un autre produit ne peut pas servir de preuve.
    """
    pattern = _raw_literal_pattern(reference)
    if not pattern:
        return ()
    windows: list[str] = []
    for match in re.finditer(pattern, content, flags=re.IGNORECASE | re.UNICODE):
        start = max(0, match.start() - _CACHED_EVIDENCE_BEFORE)
        end = _bounded_identity_end(
            content,
            match.end(),
            min(
                len(content), match.end() + _CACHED_OFFICIAL_EVIDENCE_MAX_CHARS,
            ),
            aliases=aliases,
            brand=candidate_brand,
        )
        window = content[start:end]
        if window not in windows:
            windows.append(window)
        if len(windows) >= _CACHED_IDENTITY_MAX_WINDOWS:
            break
    return tuple(windows)


def _contact_no_evidence(requirement: Requirement, window: str) -> str:
    normalized_label = _normalized_proof(requirement.label)
    normalized_value = _normalized_proof(requirement.requested_value)
    if "contact" not in normalized_label or not re.search(r"\bno\b", normalized_value):
        return ""
    count = re.search(r"\d+", normalized_value)
    if count is None:
        return ""
    number = re.escape(count.group(0))
    gap = r"[^.!?;\r\n]"
    main_contacts = (
        r"(?:contacts?\s+(?:principaux|principales|main)|main\s+contacts?)"
    )
    normally_open = r"(?:fermeture|normally\s+open|\bno\b)"
    patterns = (
        # Libelle ETIM employe par Norel/Distributeur B : le nombre peut etre sur la
        # ligne suivante, mais aucun contact auxiliaire n'entre dans le motif.
        rf"(?:nombre\s+de\s+)?contacts?\s+[aà]\s+fermeture\s+en\s+tant\s+que\s+"
        rf"contacts?\s+principaux\s*:\s*\b{number}\b",
        rf"{main_contacts}{gap}{{0,80}}\b{number}\b{gap}{{0,50}}{normally_open}",
        rf"{main_contacts}{gap}{{0,100}}{normally_open}{gap}{{0,80}}\b{number}\b",
        rf"\b{number}\b{gap}{{0,50}}{normally_open}{gap}{{0,80}}{main_contacts}",
        rf"\b{number}\b{gap}{{0,80}}{main_contacts}{gap}{{0,50}}{normally_open}",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, window, flags=re.IGNORECASE | re.DOTALL):
            if not re.search(
                r"\b(?:auxiliary|auxiliaires?)\b",
                match.group(0),
                flags=re.IGNORECASE,
            ):
                return match.group(0)
    return ""


def _usage_evidence(requirement: Requirement, window: str) -> str:
    label = _normalized_proof(requirement.label)
    if not any(marker in label for marker in _CACHED_USAGE_LABELS):
        return ""
    for branch in re.split(r"\s*(?:/|;|\bou\b|\bor\b)\s*", requirement.requested_value, flags=re.IGNORECASE):
        tokens = [
            token for token in _PROOF_TOKEN_PATTERN.findall(_normalized_proof(branch))
            if token not in _CACHED_STOP_WORDS and len(token) >= 4
        ]
        if len(tokens) < 2:
            continue
        pattern = r"(?<![^\W_])" + r"\w*.{0,100}?".join(
            re.escape(token.rstrip("s")) for token in tokens
        ) + r"\w*(?![^\W_])"
        match = re.search(pattern, window, flags=re.IGNORECASE | re.DOTALL | re.UNICODE)
        if match is not None:
            return match.group(0)
    return ""


def _nominal_current_evidence(
    requirement: Requirement,
    window: str,
    *,
    candidate_reference: str = "",
) -> str:
    """Relie `AC-3` et l'intensité demandée dans une même zone produit."""
    label = _normalized_proof(requirement.label)
    requested = _normalized_proof(requirement.requested_value)
    if (
        not any(marker in label for marker in _CACHED_CURRENT_LABELS)
        or _AC3_PATTERN.search(requested) is None
    ):
        return ""
    requested_point = _POINT_PATTERN.search(requested)
    if requested_point is None or requested_point.group("unit").casefold() not in {"a", "ma", "ka"}:
        return ""
    quantity_pattern = _raw_literal_pattern(
        f"{requested_point.group('value')} {requested_point.group('unit')}"
    )
    if not quantity_pattern:
        return ""
    headings = tuple(_CURRENT_AC3_HEADING.finditer(window))
    selected_indices: tuple[int, ...] = tuple(range(len(headings)))
    reference_pattern = _raw_literal_pattern(candidate_reference)
    if reference_pattern and headings:
        # Une fenêtre officielle peut contenir plusieurs variantes. Pour chaque
        # occurrence de la référence candidate, seul le heading AC-3 le plus
        # proche est autorisé; le tableau de la variante suivante est hors
        # périmètre, même lorsqu'il porte la valeur demandée.
        nearest: set[int] = set()
        for reference in re.finditer(
            reference_pattern, window, flags=re.IGNORECASE | re.UNICODE,
        ):
            nearest.add(min(
                range(len(headings)),
                key=lambda index: min(
                    abs(reference.start() - headings[index].end()),
                    abs(headings[index].start() - reference.end()),
                ),
            ))
        if nearest:
            selected_indices = tuple(sorted(nearest))
    for index in selected_indices:
        heading = headings[index]
        section_end = min(
            len(window), heading.end() + _CACHED_CURRENT_SECTION_MAX_CHARS,
        )
        if index + 1 < len(headings):
            # Une même page catalogue peut juxtaposer plusieurs variantes. La
            # valeur de la variante suivante ne doit jamais prouver celle qui
            # précède, même si elle se trouve dans le budget de 1 200 signes.
            section_end = min(section_end, headings[index + 1].start())
        quantity = re.search(
            quantity_pattern,
            window[heading.end():section_end],
            flags=re.IGNORECASE | re.UNICODE,
        )
        if quantity is not None:
            return window[heading.start():heading.end() + quantity.end()]
    return ""


def _cached_requirement_evidence(
    requirement: Requirement,
    window: str,
    *,
    candidate_reference: str = "",
) -> str:
    direct = _raw_literal_match(requirement.requested_value, window)
    if direct is not None:
        return direct.group(0)
    return (
        _contact_no_evidence(requirement, window)
        or _nominal_current_evidence(
            requirement, window, candidate_reference=candidate_reference,
        )
        or _usage_evidence(requirement, window)
    )


def _official_page_confirms_identity(
    candidate: CandidateAudit,
    visited_pages: Mapping[str, str],
) -> bool:
    """Une page fabricant récupérée suffit à confirmer l'identité.

    La confirmation ne dépend volontairement pas d'une `SourceProof` déjà
    émise par le modèle : cette preuve est précisément ce que le ré-audit va
    construire depuis le contenu réellement récupéré.
    """
    return any(
        _is_official_identity_page(candidate, url, content)
        for url, content in visited_pages.items()
    )


def _page_has_literal_candidate_identity(
    candidate: CandidateAudit,
    content: str,
    *,
    aliases: Sequence[str] = (),
) -> bool:
    """Exige marque et référence dans chaque page qui sert de preuve.

    Les alias de famille servent uniquement à trouver une fenêtre pertinente
    *après* cette garde. Ils ne doivent jamais permettre à la fiche d'un autre
    fabricant qui cite l'alternative de prouver ses caractéristiques.
    L'import différé évite le cycle ``candidats -> compatibilite`` au chargement.
    """
    from candidats import brand_is_present, reference_is_present

    if not brand_is_present(candidate.brand, content):
        return False
    # Une page cache peut exposer le code commande (p. ex. BSL07-20-10-81)
    # plutôt que la référence fabricant 4KBL. Les alias ne sont acceptés que
    # s'ils ont déjà été ancrés dans une fiche officielle visitée.
    identifiers = (candidate.reference, *(
        alias for alias in aliases
        if len(_identity_compact(alias)) >= 8
    ))
    for identifier in dict.fromkeys(identifiers):
        pattern = _raw_literal_pattern(identifier)
        if not pattern:
            continue
        for match in re.finditer(pattern, content, flags=re.IGNORECASE | re.UNICODE):
            context = content[max(0, match.start() - 120):match.end() + 120]
            # Une page concurrente qui mentionne seulement « alternative à Norel
            # REF-X » ne possède pas l'identité Norel : elle reste une piste,
            # jamais une preuve technique. La présence de la marque est déjà
            # exigée sur la page entière : les fiches Norel la placent parfois
            # loin du bloc de référence, donc ne la rendons pas locale.
            if not _COMPARATIVE_IDENTITY_MARKER.search(context):
                return True
    return False


def _is_official_identity_page(
    candidate: CandidateAudit,
    url: str,
    content: str,
) -> bool:
    return bool(
        content
        and not _est_page_de_liste(url)
        and _manufacturer_domain(url, candidate.brand)
        and _raw_literal_match(candidate.reference, content) is not None
    )


def _verified_identity_proof_urls(
    candidate: CandidateAudit,
    proof_urls: Sequence[str],
    visited_pages: Mapping[str, str],
) -> set[str]:
    """Retient les sources de preuve qui portent aussi l'identité produit.

    Une source secondaire est admissible seulement si la page produit visitée
    contient littéralement la marque et la référence, hors citation
    comparative. Les pages fabricant conservent leur règle historique : le
    domaine porte déjà la marque, mais la référence doit rester littérale.
    """
    canonical_pages = {
        canonical_url(url): content for url, content in visited_pages.items()
    }
    confirmed: set[str] = set()
    for value in proof_urls:
        url = canonical_url(value)
        content = canonical_pages.get(url, "")
        if not content or _est_page_de_liste(url):
            continue
        if (
            _is_official_identity_page(candidate, url, content)
            or _page_has_literal_candidate_identity(candidate, content)
        ):
            confirmed.add(url)
    return confirmed


def _criterion_has_official_identity_proof(
    candidate: CandidateAudit,
    criterion: CriterionAudit,
    visited_pages: Mapping[str, str],
) -> bool:
    return any(
        _proof_is_official_identity(candidate, proof, visited_pages)
        for proof in criterion.proofs
    )


def _proof_is_official_identity(
    candidate: CandidateAudit,
    proof: SourceProof,
    visited_pages: Mapping[str, str],
) -> bool:
    canonical_pages = {
        canonical_url(url): content for url, content in visited_pages.items()
    }
    return _is_official_identity_page(
        candidate,
        proof.url,
        canonical_pages.get(canonical_url(proof.url), ""),
    )


def _with_verified_proof_types(
    candidate: CandidateAudit,
    visited_pages: Mapping[str, str],
) -> tuple[CandidateAudit, bool]:
    """Ignore l'étiquette de source fournie par le modèle pour toute preuve."""
    criteria: list[CriterionAudit] = []
    changed = False
    for criterion in candidate.criteria:
        proofs = [
            proof.model_copy(update={
                "type": (
                    "web_officiel"
                    if _proof_is_official_identity(candidate, proof, visited_pages)
                    else "web_secondaire"
                ),
            })
            for proof in criterion.proofs
        ]
        if tuple(proofs) != tuple(criterion.proofs):
            changed = True
            criteria.append(criterion.model_copy(update={"proofs": proofs}))
        else:
            criteria.append(criterion)
    if not changed:
        return candidate, False
    return candidate.model_copy(update={"criteria": criteria}), True


def _minimum_page_evidence(
    page_evidence: list[tuple[str, dict[str, str]]],
) -> list[tuple[str, dict[str, str]]]:
    """Retient un nombre minimal de pages couvrant toutes les preuves trouvees.

    Le nombre de criteres d'une fiche est petit en pratique. La programmation
    dynamique reste toutefois bornee : au-dela de 65 536 etats, on conserve
    le couvert glouton puis on elimine toute page devenue redondante.
    """
    if not page_evidence:
        return []
    universe = frozenset().union(*(frozenset(values) for _, values in page_evidence))
    states: dict[frozenset[str], tuple[int, ...]] = {frozenset(): ()}
    overflow = False
    for index, (_, values) in enumerate(page_evidence):
        covered_by_page = frozenset(values)
        for covered, selected in tuple(states.items()):
            merged = covered | covered_by_page
            proposal = (*selected, index)
            current = states.get(merged)
            if current is None or len(proposal) < len(current):
                states[merged] = proposal
        if len(states) > 65_536:
            overflow = True
            break
    if not overflow and universe in states:
        return [page_evidence[index] for index in states[universe]]

    # Secours borne : couvert glouton, puis suppression des pages dont les
    # criteres restent tous couverts par les autres pages retenues.
    selected: list[tuple[str, dict[str, str]]] = []
    covered: set[str] = set()
    remaining = list(page_evidence)
    while covered != set(universe):
        best = max(
            remaining,
            key=lambda item: len(set(item[1]) - covered),
            default=None,
        )
        if best is None or not (set(best[1]) - covered):
            break
        selected.append(best)
        covered.update(best[1])
        remaining.remove(best)
    for item in tuple(selected):
        others = [other for other in selected if other is not item]
        if set().union(*(set(other[1]) for other in others)) >= set(universe):
            selected.remove(item)
    return selected


def reaudit_cached_product_evidence(
    requirements: RequirementSet,
    audit: PageAudit,
    visited_pages: Mapping[str, str],
) -> PageAudit:
    """Complète un audit avec les pages déjà payées et liées au produit.

    Aucune URL n'est ouverte et aucun modele n'est appele. Une page secondaire
    contribue seulement si le contenu recupere porte litteralement la reference
    ou une designation/famille observee sur la source officielle. Les pages de
    liste restent exclues. Une incompatibilite secondaire peut être annulée
    uniquement par une preuve officielle de la valeur demandée.
    """
    expected = {item.id: item for item in requirements.criteria}
    enriched_candidates: list[CandidateAudit] = []
    changed = False
    for candidate in audit.candidates:
        candidate, proof_types_changed = _with_verified_proof_types(
            candidate, visited_pages,
        )
        changed = changed or proof_types_changed
        if not _official_page_confirms_identity(candidate, visited_pages):
            enriched_candidates.append(candidate)
            continue
        downgradable = {
            criterion.requirement_id
            for criterion in candidate.criteria
            if (
                (requirement := expected.get(criterion.requirement_id)) is not None
                and not _est_identite_origine(requirement, requirements)
                and criterion.status == "incompatible"
                and criterion.proofs
                and all(
                    not _proof_is_official_identity(
                        candidate, proof, visited_pages,
                    )
                    for proof in criterion.proofs
                )
            )
        }
        identity_aliases = _candidate_identity_aliases(
            candidate, visited_pages, requirements,
        )
        evidence_aliases = tuple(dict.fromkeys((
            candidate.reference,
            *(
                alias for alias in identity_aliases
                if len(_identity_compact(alias)) >= 8
            ),
        )))
        page_evidence: list[tuple[str, dict[str, str]]] = []
        for url, content in visited_pages.items():
            if (
                not content
                or _est_page_de_liste(url)
                or not _page_has_literal_candidate_identity(
                    candidate, content, aliases=evidence_aliases,
                )
            ):
                continue
            official_identity_page = _is_official_identity_page(
                candidate, url, content,
            )
            windows = _identity_windows(
                content,
                evidence_aliases,
                candidate_brand=candidate.brand,
                known_aliases=identity_aliases,
            )
            if official_identity_page:
                for official_window in _official_identity_windows(
                    content,
                    candidate.reference,
                    aliases=identity_aliases,
                    candidate_brand=candidate.brand,
                ):
                    if official_window not in windows:
                        windows = (*windows, official_window)
            if not windows:
                continue
            found_on_page: dict[str, str] = {}
            for criterion in candidate.criteria:
                requirement = expected.get(criterion.requirement_id)
                if (
                    requirement is None
                    or _est_identite_origine(requirement, requirements)
                ):
                    continue
                if criterion.status == "incompatible":
                    if criterion.requirement_id not in downgradable:
                        continue
                    # Une source secondaire en conflit ne peut être remplacée
                    # que par la donnée publiée par le fabricant.
                    if not official_identity_page:
                        continue
                elif criterion.status == "proven":
                    # Une preuve déjà positive est enrichie seulement par une
                    # page officielle : une redondance secondaire n'ajoute
                    # aucune autorité au dossier.
                    if (
                        _criterion_has_official_identity_proof(
                            candidate, criterion, visited_pages,
                        )
                        or not official_identity_page
                    ):
                        continue
                elif criterion.status != "not_proven":
                    continue
                for window in windows:
                    excerpt = _cached_requirement_evidence(
                        requirement,
                        window,
                        candidate_reference=candidate.reference,
                    )
                    if excerpt:
                        found_on_page[criterion.requirement_id] = excerpt
                        break
            if found_on_page:
                page_evidence.append((url, found_on_page))

        # Une page produit qui couvre quatre criteres vaut mieux que quatre
        # occurrences eparses. Le tri est stable : a couverture egale, l'ordre
        # de recuperation est conserve. Une autre page n'est retenue que pour
        # un critere encore absent.
        page_evidence.sort(key=lambda item: (
            -len(item[1]),
            not _is_official_identity_page(candidate, item[0], visited_pages[item[0]]),
        ))
        page_evidence = _minimum_page_evidence(page_evidence)
        evidence_by_id: dict[str, list[tuple[str, str]]] = {}
        for url, found_on_page in page_evidence:
            for requirement_id, excerpt in found_on_page.items():
                if requirement_id not in evidence_by_id:
                    evidence_by_id[requirement_id] = [(url, excerpt)]

        criteria: list[CriterionAudit] = []
        for criterion in candidate.criteria:
            reclassified_proofs = tuple(
                proof.model_copy(update={
                    "type": (
                        "web_officiel"
                        if _proof_is_official_identity(candidate, proof, visited_pages)
                        else "web_secondaire"
                    ),
                })
                for proof in criterion.proofs
            )
            found = evidence_by_id.get(criterion.requirement_id, [])
            if not found:
                if reclassified_proofs != tuple(criterion.proofs):
                    changed = True
                    criteria.append(criterion.model_copy(update={
                        "proofs": list(reclassified_proofs),
                    }))
                else:
                    criteria.append(criterion)
                continue
            if criterion.requirement_id in downgradable:
                # Une donnée fabricant contredit l'incompatibilité issue d'une
                # source secondaire, mais elle ne peut pas l'établir positive
                # dans le même passage. Le tour suivant, désormais
                # ``not_proven``, applique la voie normale de ré-audit.
                changed = True
                criteria.append(criterion.model_copy(update={
                    "status": "not_proven",
                    "observed_value": "",
                    "proofs": [],
                }))
                continue
            changed = True
            proofs: list[SourceProof] = (
                list(reclassified_proofs)
                if criterion.status == "proven" else []
            )
            seen_proofs: set[tuple[str, str]] = set()
            for proof in proofs:
                seen_proofs.add((canonical_url(proof.url), proof.excerpt.casefold()))
            for url, excerpt in found:
                key = (canonical_url(url), excerpt.casefold())
                if key in seen_proofs:
                    continue
                seen_proofs.add(key)
                proofs.append(SourceProof(
                    url=url,
                    excerpt=excerpt,
                    type=(
                        "web_officiel"
                        if _is_official_identity_page(candidate, url, visited_pages[url])
                        else "web_secondaire"
                    ),
                ))
            criteria.append(CriterionAudit(
                requirement_id=criterion.requirement_id,
                requested_value=criterion.requested_value,
                observed_value=found[0][1],
                status="proven",
                proofs=proofs,
            ))
        enriched_candidates.append(candidate.model_copy(update={"criteria": criteria}))
    if not changed:
        return audit
    return audit.model_copy(update={"candidates": enriched_candidates})


def cached_reaudit_downgraded_criteria(
    requirements: RequirementSet,
    before: PageAudit,
    after: PageAudit,
) -> tuple[str, ...]:
    """Projette les incompatibilités secondaires levées par le ré-audit.

    Cette transition ne peut être créée que par `reaudit_cached_product_evidence`
    après confirmation d'identité sur une page fabricant visitée. Les libellés
    de RequirementSet sont les seules données envoyées au diagnostic.
    """
    before_by_candidate = {
        (_normalized(candidate.brand), _normalized(candidate.reference)): candidate
        for candidate in before.candidates
    }
    after_by_candidate = {
        (_normalized(candidate.brand), _normalized(candidate.reference)): candidate
        for candidate in after.candidates
    }
    labels: list[str] = []
    for key, before_candidate in before_by_candidate.items():
        after_candidate = after_by_candidate.get(key)
        if after_candidate is None:
            continue
        before_by_id = {
            criterion.requirement_id: criterion
            for criterion in before_candidate.criteria
        }
        after_by_id = {
            criterion.requirement_id: criterion
            for criterion in after_candidate.criteria
        }
        for requirement in requirements.criteria:
            if _est_identite_origine(requirement, requirements):
                continue
            prior = before_by_id.get(requirement.id)
            current = after_by_id.get(requirement.id)
            if (
                prior is not None
                and current is not None
                and prior.status == "incompatible"
                and current.status != "incompatible"
                and requirement.label not in labels
            ):
                labels.append(requirement.label)
    return tuple(labels)


def _validate_candidate(
    requirements: RequirementSet,
    candidate: CandidateAudit,
    visited_pages: Mapping[str, str],
    *,
    strict: bool = True,
) -> tuple[
    set[str], int, set[str], set[str], dict[str, str], CandidateAudit,
]:
    """Vérifie que chaque preuve renvoie à une page réellement récupérée.

    En mode `strict` (défaut, comportement historique), une preuve invérifiable
    lève `EvidenceContractError` et le candidat entier est perdu. Sinon le
    critère concerné est rendu séparément des identifiants absents : l'appelant
    les réunit pour préserver la rétrogradation en `not_proven`, tout en
    exposant l'origine exacte de la lacune (spec CAP-4).

    Un audit malformé — identifiants divergents, valeur demandée réécrite —
    lève `CompatibilityContractError` dans les deux modes : ce n'est pas une
    preuve fragile, c'est un audit qui ne répond pas à la question posée.
    """
    expected = {item.id: item for item in requirements.criteria}
    ids = [item.requirement_id for item in candidate.criteria]
    audit_ids = set(ids)
    if len(ids) != len(audit_ids) or audit_ids - set(expected):
        raise CompatibilityContractError(
            "Les identifiants de l'audit diffèrent des critères demandés."
        )
    missing_ids = set(expected) - audit_ids

    canonical_pages = {canonical_url(url): content for url, content in visited_pages.items()}
    proof_urls: set[str] = set()
    official_count = 0
    rejected_proof_ids: set[str] = {
        item.requirement_id
        for item in candidate.criteria
        if item.evidence_rejected
    }
    proof_grades: dict[str, str] = {}
    sanitized_criteria: list[CriterionAudit] = []

    def _rejeter(message: str, requirement_id: str) -> None:
        if strict:
            raise EvidenceContractError(message)
        rejected_proof_ids.add(requirement_id)

    for criterion in candidate.criteria:
        requirement = expected[criterion.requirement_id]
        if _normalized_proof(criterion.requested_value) != _normalized_proof(
            requirement.requested_value
        ):
            raise CompatibilityContractError(
                "La valeur demandée a été modifiée dans l'audit."
            )
        effective_status = _effective_status(requirement, criterion)
        if effective_status in {"proven", "incompatible"} and not criterion.proofs:
            _rejeter(
                f"Le critère {criterion.requirement_id} n'a aucune preuve.",
                criterion.requirement_id,
            )
            sanitized_criteria.append(criterion.model_copy(update={
                "status": "not_proven",
                "proofs": [],
                "evidence_rejected": True,
            }))
            continue
        if effective_status == "not_proven":
            sanitized_criteria.append(criterion.model_copy(update={
                "status": "not_proven",
                "proofs": [],
            }))
            continue
        admissible_proofs = [
            proof for proof in criterion.proofs
            if not _est_page_de_liste(proof.url)
        ]
        if not admissible_proofs:
            _rejeter(
                f"Le critere {criterion.requirement_id} ne cite qu'une page de liste.",
                criterion.requirement_id,
            )
            sanitized_criteria.append(criterion.model_copy(update={
                "status": "not_proven",
                "proofs": [],
                "evidence_rejected": True,
            }))
            continue
        accepted_proofs: list[SourceProof] = []
        accepted_grades: list[str] = []
        for proof in admissible_proofs:
            url = canonical_url(proof.url)
            if url not in canonical_pages:
                if strict:
                    _rejeter(
                        f"Preuve issue d'une URL non visitée : {proof.url}",
                        criterion.requirement_id,
                    )
                continue
            content = _normalized_proof(canonical_pages[url])
            excerpt = _normalized_proof(proof.excerpt)
            contiguous = bool(excerpt and excerpt in content)
            windowed = not contiguous and _windowed_excerpt_matches(excerpt, content)
            if not contiguous and not windowed:
                if strict:
                    _rejeter(
                        f"Extrait absent de la page visitée : {proof.url}",
                        criterion.requirement_id,
                    )
                continue
            if _is_compact_identity_value_listing(candidate, proof.excerpt):
                if strict:
                    _rejeter(
                        "Un titre compact d'identite ne prouve pas une caracteristique.",
                        criterion.requirement_id,
                    )
                continue
            if _excerpt_borrows_a_neighbour_identity(
                candidate, proof.excerpt, canonical_pages[url],
            ):
                if strict:
                    _rejeter(
                        "Un extrait de rayon doit porter l'identite qu'il prouve.",
                        criterion.requirement_id,
                    )
                continue
            if _excerpt_is_only_an_identity(candidate, proof.excerpt):
                if strict:
                    _rejeter(
                        "Une designation produit seule n'enonce aucune valeur.",
                        criterion.requirement_id,
                    )
                continue
            grade = "windowed" if windowed else "contiguous"
            accepted_proofs.append(proof)
            accepted_grades.append(grade)
            proof_urls.add(url)
            if proof.type == "web_officiel" and _manufacturer_domain(proof.url, candidate.brand):
                official_count += 1
        if not accepted_proofs:
            _rejeter(
                f"Le critere {criterion.requirement_id} n'a aucune preuve admissible.",
                criterion.requirement_id,
            )
            sanitized_criteria.append(criterion.model_copy(update={
                "status": "not_proven",
                "proofs": [],
                "evidence_rejected": True,
            }))
            continue
        proof_grades[criterion.requirement_id] = (
            "windowed" if "windowed" in accepted_grades else "contiguous"
        )
        sanitized_criteria.append(criterion.model_copy(update={
            "status": effective_status,
            "proofs": accepted_proofs,
            "evidence_rejected": False,
        }))
    sanitized = candidate.model_copy(update={"criteria": sanitized_criteria})
    return (
        proof_urls,
        official_count,
        missing_ids,
        rejected_proof_ids,
        proof_grades,
        sanitized,
    )


def sanitize_candidate_evidence(
    requirements: RequirementSet,
    candidate: CandidateAudit,
    visited_pages: Mapping[str, str],
    *,
    strict: bool = False,
) -> CandidateAudit:
    """Retourne uniquement les statuts et preuves qui passent l'anti-invention."""
    return _validate_candidate(
        requirements,
        candidate,
        visited_pages,
        strict=strict,
    )[-1]


#: Alias courts explicitement documentés : un alias d'au plus deux caractères
#: ne désigne QUE la marque inscrite ici, jamais une sous-chaîne d'un autre
#: fabricant ni un suffixe juridique. La table est de la connaissance de
#: marque : elle vit dans le fichier de configuration lu par
#: `marques_connues`, pas dans le moteur. L'y étendre reste une décision
#: humaine (voir spec CAP-2).
_CANONICAL_BRAND_ALIASES: dict[frozenset[str], frozenset[str]] = alias_canoniques()


def _brand_tokens(value: object) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    decomposed = "".join(c for c in decomposed if not unicodedata.combining(c))
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", decomposed) if token}


def _canonical_brand_tokens(tokens: set[str]) -> frozenset[str]:
    frozen = frozenset(tokens)
    return _CANONICAL_BRAND_ALIASES.get(frozen, frozen)


def _brand_matches(fabricant: object, cible: object) -> bool:
    """Le fabricant porte-t-il la marque cible, sous une forme plus longue ?

    Porté depuis la generation precedente du moteur.
    Une abréviation courte et non répertoriée n'est jamais élargie : `cible`
    à un seul token de 2 caractères ou moins, absent de la table d'alias,
    exige une égalité canonique exacte plutôt qu'une inclusion.
    """
    fabricant_tokens = _brand_tokens(fabricant)
    cible_tokens = _brand_tokens(cible)
    if not fabricant_tokens or not cible_tokens:
        return False
    fabricant_canonique = _canonical_brand_tokens(fabricant_tokens)
    cible_canonique = _canonical_brand_tokens(cible_tokens)
    cible_figee = frozenset(cible_tokens)
    if (
        len(cible_tokens) == 1
        and len(next(iter(cible_tokens))) <= 2
        and cible_figee not in _CANONICAL_BRAND_ALIASES
    ):
        return fabricant_canonique == cible_canonique
    return cible_canonique.issubset(fabricant_canonique)


def criteres_notables(requirements: RequirementSet) -> RequirementSet:
    """Rend le jeu prive de ses criteres d'identite d'origine.

    Le fabricant, la reference exacte et la gamme du produit demande ne peuvent
    pas s'appliquer a une alternative d'une autre marque : `evaluate_candidates`
    les classe deja `non_applicable` et les exclut du score. Les envoyer malgre
    tout au modele revient a lui demander de prouver qu'un Norel est un Kerion,
    au milieu des consignes qui comptent vraiment.

    Mesure du 2026-08-27 sur trois runs reels : les audits revenaient avec des
    criteres techniques entierement absents de la reponse — le modele rendait un
    audit partiel ou vide sur des pages qui portaient pourtant la fiche produit.

    Si le jeu ne contient que des criteres d'origine, il est rendu tel quel :
    c'est alors `evaluate_candidates` qui refuse le contrat, avec son message.
    """
    notables = [
        item for item in requirements.criteria
        if not _est_identite_origine(item, requirements)
    ]
    if not notables:
        return requirements
    return requirements.model_copy(update={"criteria": notables})


def evaluate_candidates(
    requirements: RequirementSet,
    audits: Sequence[PageAudit],
    target_brand: str | None,
    threshold: int,
    visited_pages: Mapping[str, str],
    *,
    strict_evidence: bool = True,
) -> list[CandidateEvaluation]:
    """Note chaque candidat audité contre le jeu de critères demandé.

    `strict_evidence=False` active la rétrogradation CAP-4 : un critère dont la
    preuve est invérifiable devient `not_proven` et le candidat reste dans la
    liste, avec ses critères invérifiés nommés dans le résumé. Le défaut reste
    strict — la décision de basculer appartient à l'humain tant que le critère
    de classement des candidats plausibles n'est pas défini (spec CAP-4).
    """
    if not 1 <= threshold <= 100:
        raise ValueError("Le seuil doit être compris entre 1 et 100.")
    expected = {item.id: item for item in requirements.criteria}
    non_applicable = [
        item for item in requirements.criteria
        if _est_identite_origine(item, requirements)
    ]
    if len(non_applicable) * 2 > len(requirements.criteria):
        raise CompatibilityContractError(
            "Plus de la moitié des critères sont non applicables ; "
            "le jeu ne contient pas assez de critères notables."
        )
    notable = [item for item in requirements.criteria if item not in non_applicable]
    if not notable:
        raise CompatibilityContractError(
            "Le jeu de critères ne contient aucun critère notable."
        )
    notable_ids = {item.id for item in notable}
    non_applicable_ids = [item.id for item in non_applicable]
    evaluations: list[CandidateEvaluation] = []
    for audit in audits:
        for candidate in audit.candidates:
            # Defense de rejeu : les corpus anciens contiennent les audits tels
            # qu'ils existaient avant ContentGate. Une identite issue seulement
            # d'une liste ne doit donc pas reapparaitre comme evaluation.
            proof_urls = [
                proof.url
                for criterion in candidate.criteria
                for proof in criterion.proofs
            ]
            if _est_page_de_liste(audit.page_url) and not any(
                not _est_page_de_liste(url) for url in proof_urls
            ):
                continue
            (
                proof_urls,
                official_count,
                missing_ids,
                rejected_proof_ids,
                proof_grades,
                candidate,
            ) = _validate_candidate(
                requirements, candidate, visited_pages, strict=strict_evidence
            )
            unverified = missing_ids | rejected_proof_ids
            by_id = {item.requirement_id: item for item in candidate.criteria}
            effective_statuses = {
                key: _effective_status(expected[key], item)
                for key, item in by_id.items()
            }
            # Un critère dont la preuve n'a pas résisté à la vérification ne
            # compte ni comme prouvé ni comme bloquant : il est invérifié.
            proven = [
                key for key, item in by_id.items()
                if key in notable_ids
                and effective_statuses[key] == "proven"
                and key not in unverified
            ]
            not_proven = [
                key for key, item in by_id.items()
                if key in notable_ids
                and effective_statuses[key] == "not_proven"
                and key not in unverified
            ]
            incompatible = [
                key for key, item in by_id.items()
                if key in notable_ids
                and effective_statuses[key] == "incompatible"
                and key not in unverified
            ]
            blockers = [
                expected[key].label for key in incompatible if expected[key].critical
            ]
            score = (100 * len(proven)) // len(notable)
            brand_matches = not (target_brand or "").strip() or _brand_matches(
                candidate.brand, target_brand
            )
            identity_proof_urls = _verified_identity_proof_urls(
                candidate, proof_urls, visited_pages,
            )
            identity_confirmed = bool(identity_proof_urls)
            technical_proofs = [
                proof
                for requirement_id in proven
                for proof in by_id[requirement_id].proofs
            ]
            technical_proof_urls = {
                canonical_url(proof.url) for proof in technical_proofs
            }
            technical_official_count = sum(
                1
                for proof in technical_proofs
                if proof.type == "web_officiel"
                and _manufacturer_domain(proof.url, candidate.brand)
            )
            technical_source_domains = {
                domain
                for proof in technical_proofs
                if (domain := _source_domain(proof.url))
            }
            completion_proof = (
                technical_official_count > 0
                or len(technical_source_domains) >= 3
            )
            complete = (
                identity_confirmed
                and brand_matches
                and score == 100
                and not incompatible
                and completion_proof
            )
            # Une source produit vérifiée peut être secondaire, mais le score
            # minimal reste le seuil de proposition. Une incompatibilité
            # technique établie, même non critique, exclut toujours le
            # candidat : elle est à expliquer, jamais à proposer.
            eligible = (
                identity_confirmed
                and brand_matches
                and bool(proven)
                and score >= threshold
                and not incompatible
            )
            evaluations.append(CandidateEvaluation(
                candidate=candidate,
                summary=CompatibilitySummary(
                    score=score,
                    brand=candidate.brand,
                    reference=candidate.reference,
                    proven_criteria=[expected[key].label for key in proven],
                    not_proven_criteria=[expected[key].label for key in not_proven],
                    incompatible_criteria=[expected[key].label for key in incompatible],
                    non_applicable_criteria=[
                        expected[key].label for key in non_applicable_ids
                    ],
                    critical_blockers=blockers,
                    unverified_criteria=[
                        expected[key].label
                        for key in sorted(unverified & notable_ids)
                    ],
                    missing_audit_criteria=[
                        expected[key].label
                        for key in sorted(missing_ids & notable_ids)
                    ],
                    rejected_proof_criteria=[
                        expected[key].label
                        for key in sorted(rejected_proof_ids & notable_ids)
                    ],
                    proof_grades={
                        expected[key].label: grade
                        for key, grade in proof_grades.items()
                        if key in notable_ids and key not in unverified
                    },
                ),
                eligible=eligible,
                complete=complete,
                official_proof_count=technical_official_count,
                proof_url_count=len(technical_proof_urls),
            ))
    return evaluations


def select_best_candidate(
    evaluations: Sequence[CandidateEvaluation],
) -> CandidateEvaluation | None:
    eligible = [item for item in evaluations if item.eligible]
    if not eligible:
        return None
    return sorted(eligible, key=lambda item: (
        -item.summary.score,
        -item.official_proof_count,
        -item.proof_url_count,
        _normalized(item.summary.brand),
        _normalized(item.summary.reference),
    ))[0]
