# -*- coding: utf-8 -*-
"""Le repli deterministe doit rester borne sur une page reelle.

Mesure du 2026-08-20, mission `NV1T05BD -> Norel`. Le run s'est fige : ni
reseau, ni navigateur, ni PDF — 1 606 s de CPU et une pile arretee sur
`literal_occurrences`. La page en cause venait de `marchand-a.example/shop/listings`,
1 272 313 caracteres de HTML ramenes par une requete qui avait derive.

Deux defauts se combinaient :

- `deterministic_identity_leads` appelait `literal_occurrences` pour chaque
  jeton alphanumerique trouve, et chaque appel repliait la casse du corpus
  entier. Une page bourree de references produit des milliers de jetons, donc
  des milliers de replis d'un megaoctet.
- Aucun plafond ne bornait le nombre de candidats de repli, alors que ce
  chemin n'est qu'un filet de securite quand le modele n'a rien rendu.

Le repli est une piste de recherche `low_confidence`, jamais une preuve : il
n'a pas a examiner exhaustivement une page hors sujet.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identite import (
    MAX_FALLBACK_IDENTITIES,
    deterministic_identity_leads,
    literal_occurrences,
)


def _document(content: str, *, title: str = "", url: str = "https://ex.example/p"):
    return SimpleNamespace(url=url, title=title, snippets=(), content=content)


def _page_bruyante(jetons: int, remplissage: int = 400) -> str:
    """Une page de listing : beaucoup de references, beaucoup de texte."""
    bourrage = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * remplissage
    return "\n".join(
        f"{bourrage} Modele REF{index:05d}A disponible." for index in range(jetons)
    )


def test_le_repli_reste_borne_en_nombre_de_candidats():
    """Mutation détectée : une page de listing produit des milliers de pistes."""
    trouvees = deterministic_identity_leads(
        _document(_page_bruyante(jetons=MAX_FALLBACK_IDENTITIES * 3, remplissage=2))
    )

    assert len(trouvees) <= MAX_FALLBACK_IDENTITIES
    assert trouvees, "le repli doit rendre des pistes, pas rien"
    assert all(item.low_confidence for item in trouvees)


def test_une_page_volumineuse_ne_bloque_pas_la_mission():
    """Le cas mesuré : 1,2 Mo de contenu figeait le run pendant des dizaines de minutes."""
    page = _page_bruyante(jetons=600, remplissage=60)
    assert len(page) > 1_000_000, f"corpus de test trop petit : {len(page)}"

    depart = time.monotonic()
    deterministic_identity_leads(_document(page))
    ecoule = time.monotonic() - depart

    # Large, mais sans commune mesure avec les minutes observees : la borne
    # attrape la regression quadratique sans dependre de la machine.
    assert ecoule < 5.0, f"repli deterministe trop lent : {ecoule:.1f} s"


def test_une_reference_repetee_ne_compte_que_pour_une_piste():
    """Un catalogue repete la meme reference : elle ne vaut qu'une observation."""
    contenu = " ".join(["Contacteur XZ07-20-10-11 disponible."] * 500)

    trouvees = deterministic_identity_leads(_document(contenu))

    valeurs = [item.raw_value for item in trouvees]
    assert len(valeurs) == len(set(valeurs))


def test_les_occurrences_litterales_restent_exactes():
    """La borne ne doit rien changer au verdict de presence."""
    document = _document(
        "La fiche mentionne XZ07-20-10-11 dans le tableau.",
        title="XZ07-20-10-11 | Norel",
        url="https://ex.example/produits/xz07-20-10-11",
    )

    sources = {item.source for item in literal_occurrences("XZ07-20-10-11", document)}

    assert sources == {"url", "title", "content"}
    assert literal_occurrences("INTROUVABLE-1", document) == ()
