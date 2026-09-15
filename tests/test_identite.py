# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identite import (
    CORROBORATING_SOURCES,
    IdentityExtractor,
    ProductIdentity,
    deterministic_identity_leads,
    is_valid_ean,
    normalize_identity,
)


@dataclass(frozen=True)
class Doc:
    url: str = "https://maker.test/p"
    title: str = ""
    snippets: tuple[str, ...] = ()
    content: str = ""


def document(content="", title="", url="https://maker.test/p", snippets=()):
    return Doc(url=url, title=title, snippets=tuple(snippets), content=content)


def test_extractor_rejects_value_absent_from_document_content():
    """Mutation détectée : le modèle invente une référence et elle devient identité."""
    identity = IdentityExtractor().extract(
        document(content="Norel XZ07-20-10-11, bobine 24 V DC"),
        [{"kind": "mpn", "value": "XZ12-20"}],
    )

    assert identity.values == ()


def test_extractor_normalizes_only_after_literal_presence():
    """Mutation détectée : la normalisation précède la preuve littérale."""
    identity = IdentityExtractor().extract(
        document(content="Marque : Kerion Electric"),
        [{"kind": "brand", "value": "Kerion Electric"}],
    )

    assert identity.values[0].raw_value == "Kerion Electric"
    assert identity.values[0].normalized_value == "kerion electric"


def test_normalization_never_makes_an_absent_reference_match():
    """Le piège inverse : une valeur qui ne se normalise pas comme la page."""
    identity = IdentityExtractor().extract(
        document(content="Reference XZ07201011 en stock"),
        [{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    assert identity.values == ()


@pytest.mark.parametrize("ean, accepted", [
    ("4006381333931", True),
    ("4006381333932", False),
    ("96385074", True),
    ("012345678905", True),
    ("abcdefgh", False),
    ("400638133393", False),
])
def test_ean_checksum(ean, accepted):
    assert is_valid_ean(ean) is accepted


def test_ean_failing_its_checksum_is_not_kept_as_an_ean():
    identity = IdentityExtractor().extract(
        document(content="Code 4006381333932 imprime sur l'etiquette"),
        [{"kind": "ean", "value": "4006381333932"}],
    )

    assert identity.values == ()


def test_occurrences_record_every_field_carrying_the_raw_value():
    identity = IdentityExtractor().extract(
        document(
            url="https://maker.test/p/xz07-20-10-11",
            title="XZ07-20-10-11 | Norel",
            content="Le contacteur XZ07-20-10-11 est disponible.",
        ),
        [{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    sources = {item.source for item in identity.values[0].occurrences}
    assert sources == {"url", "title", "content"}


def test_origin_product_identity_is_excluded():
    """Mutation détectée : la référence d'origine oriente la recherche d'alternative."""
    identity = IdentityExtractor().extract(
        document(content="Equivalent du NV1T05BD : XZ07-20-10-11"),
        [
            {"kind": "mpn", "value": "NV1T05BD"},
            {"kind": "mpn", "value": "XZ07-20-10-11"},
        ],
        excluded_raw_values=("NV1T05BD",),
    )

    assert [value.raw_value for value in identity.values] == ["XZ07-20-10-11"]


def test_unknown_kinds_and_malformed_entries_are_ignored():
    identity = IdentityExtractor().extract(
        document(content="XZ07-20-10-11 couleur bleu"),
        [
            {"kind": "couleur", "value": "bleu"},
            {"value": "XZ07-20-10-11"},
            "XZ07-20-10-11",
            {"kind": "mpn", "value": "XZ07-20-10-11"},
        ],
    )

    assert [value.kind for value in identity.values] == ["mpn"]


def test_duplicate_values_are_merged_once_normalized():
    identity = IdentityExtractor().extract(
        document(content="XZ07-20-10-11 et xz07-20-10-11"),
        [
            {"kind": "mpn", "value": "XZ07-20-10-11"},
            {"kind": "mpn", "value": "xz07-20-10-11"},
        ],
    )

    assert len(identity.values) == 1
    assert identity.values[0].raw_value == "XZ07-20-10-11"


def test_raw_identifiers_feed_the_gate_with_product_kinds_only():
    """Mutation détectée : la marque sert de critère et qualifie toute page du site."""
    identity = IdentityExtractor().extract(
        document(content="Norel XZ07-20-10-11, code 4KBL137001R1110"),
        [
            {"kind": "brand", "value": "Norel"},
            {"kind": "mpn", "value": "XZ07-20-10-11"},
            {"kind": "model", "value": "4KBL137001R1110"},
        ],
    )

    assert identity.raw_identifiers() == ("XZ07-20-10-11", "4KBL137001R1110")
    assert identity.raw_identifiers(("brand", "manufacturer")) == ("Norel",)


def test_reference_present_only_in_the_url_never_becomes_a_gate_criterion():
    """Mutation détectée : le chemin de l'URL suffit à qualifier une page vide.

    Cas réel Norel : `/products/fr-lu/4KBL137001R1110/xz07-20-10-11` porte les
    deux références, alors que la page rendue n'a aucun fait technique.
    """
    identity = IdentityExtractor().extract(
        document(
            url="https://new.norel.example/products/fr-lu/4KBL137001R1110/xz07-20-10-11",
            content="Select Country Argentina Bahamas Barbados Belgium",
        ),
        [{"kind": "mpn", "value": "xz07-20-10-11"}],
    )

    # L'identité est bien observée — mais pas par une source qui prouve.
    assert identity.values[0].occurrences == (
        identity.values[0].occurrences[0],
    )
    assert {item.source for item in identity.values[0].occurrences} == {"url"}
    # Donc elle n'ouvre pas PRODUCT_EVIDENCE.
    assert identity.raw_identifiers() == ()


def test_title_corroborates_identity_but_never_qualifies_product_evidence():
    """Mutation détectée : le titre annoncé ouvre la porte d'audit.

    Cas réel Norel : titre `XZ07-20-10-11 | Norel`, contenu sans aucun fait.
    """
    identity = IdentityExtractor().extract(
        document(
            title="XZ07-20-10-11 | Norel",
            content="Select Country Argentina Bahamas Barbados Belgium",
        ),
        [{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    # Corroborée : SourceDiscovery et la traçabilité y ont accès.
    assert identity.raw_identifiers(sources=CORROBORATING_SOURCES) == ("XZ07-20-10-11",)
    # Mais elle ne qualifie pas : la preuve se lit dans le contenu récupéré.
    assert identity.raw_identifiers() == ()


def test_reference_in_content_qualifies():
    identity = IdentityExtractor().extract(
        document(content="Le contacteur XZ07-20-10-11 est disponible."),
        [{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    assert identity.raw_identifiers() == ("XZ07-20-10-11",)


def test_fallback_finds_a_literal_mpn_but_marks_it_low_confidence():
    """Mutation détectée : le repli produit une identité forte, voire une marque."""
    leads = deterministic_identity_leads(
        document(title="XZ07-20-10-11 | Norel", content="Contacteur XZ07-20-10-11.")
    )

    assert leads
    assert all(lead.low_confidence for lead in leads)
    assert all(lead.kind == "mpn" for lead in leads)
    assert "XZ07-20-10-11" in {lead.raw_value for lead in leads}


def test_low_confidence_leads_never_become_gate_criteria():
    """Une piste faible cherche ; elle ne qualifie pas une page."""
    identity = ProductIdentity(values=deterministic_identity_leads(
        document(content="Contacteur XZ07-20-10-11.")
    ))

    assert identity.raw_identifiers() == ()
    assert identity.raw_identifiers(include_low_confidence=True) == ("XZ07-20-10-11",)


def test_fallback_never_guesses_a_brand():
    leads = deterministic_identity_leads(
        document(content="Kerion Electric propose le NV1T05BD.")
    )

    assert all(lead.kind == "mpn" for lead in leads)


def test_normalize_identity_unifies_unicode_and_separators():
    assert normalize_identity("XZ07-20-10-11") == "xz07 20 10 11"
    assert normalize_identity("Kerion  Electric") == "kerion electric"
