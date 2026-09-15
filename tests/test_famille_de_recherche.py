# -*- coding: utf-8 -*-
"""L'ancre d'une recherche ciblee doit pouvoir designer des variantes.

Mission reelle `NV1T05BD -> Norel` du 2026-08-20. Le candidat trouve etait
`HPL1211001R0101`, prouve incompatible : 6 A la ou il en fallait 9. La bonne
reaction est de chercher la variante 9 A de la meme gamme. La requete partie
etait :

    HPL1211001R0101 Courant nominal "9 A (AC-3)"

Cette chaine ne peut ramener qu'un seul produit — celui qu'on sait deja
mauvais. Chercher un code de commande exact accompagne d'un critere qu'il ne
remplit pas, c'est chercher ce qui n'existe pas.

`_famille_observee` coupe au premier separateur : `XZ07-20-10-13` donne
`XZ07`, ce qui marche. Mais `HPL1211001R0101` n'en a aucun, donc la famille
valait la reference entiere.

Or une autre ecriture du meme produit etait lisible dans l'URL meme de la
page auditee :

    new.norel.example/products/fr/HPL1211001R0101/b6-30-10-01

`b6-30-10-01` est une forme reellement observee. La part qu'elle partage avec
ses variantes est `b6-30-10`. Rien n'est fabrique : la designation est lue
telle quelle, l'ancre en est un prefixe, et c'est le moteur qui decouvrira
quelles variantes existent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recherche_adaptative import _famille_observee, famille_de_recherche


URL_Norel = "https://new.norel.example/products/fr/HPL1211001R0101/b6-30-10-01"


# --------------------------------------------------------------------------
# Le cas mesure


def test_un_code_de_commande_prend_pour_ancre_la_designation_observee():
    """Mutation détectée : la recherche ciblée ne peut trouver que le mauvais produit."""
    assert famille_de_recherche("HPL1211001R0101", (URL_Norel,)) == "b6-30-10"


def test_l_ancre_est_un_prefixe_d_une_forme_reellement_presente():
    """Garde-fou : aucune reference ne doit sortir d'ici sans avoir ete lue."""
    ancre = famille_de_recherche("HPL1211001R0101", (URL_Norel,))

    assert ancre in URL_Norel.casefold()


# --------------------------------------------------------------------------
# Ce qui ne doit pas changer


def test_une_reference_deja_decoupable_garde_sa_famille():
    """`XZ07-20-10-13` marchait deja : les URL ne sont meme pas consultees."""
    urls = ("https://new.norel.example/products/fr/4KBL137001R1310/xz07-20-10-13",)

    assert famille_de_recherche("XZ07-20-10-13", urls) == _famille_observee(
        "XZ07-20-10-13"
    )
    assert famille_de_recherche("XZ07-20-10-13", urls) == "XZ07"


def test_sans_designation_exploitable_la_reference_reste_l_ancre():
    urls = ("https://revendeur.example/panier", "https://revendeur.example/p/48812")

    assert famille_de_recherche("HPL1211001R0101", urls) == "HPL1211001R0101"


def test_aucune_url_laisse_le_comportement_inchange():
    assert famille_de_recherche("HPL1211001R0101", ()) == "HPL1211001R0101"


def test_la_reference_elle_meme_ecrite_avec_separateurs_n_est_pas_une_variante():
    """Une autre ponctuation du meme code ne dit rien de plus : elle est ignoree."""
    urls = ("https://ex.example/p/HPL-1211001-R0101",)

    assert famille_de_recherche("HPL1211001R0101", urls) == "HPL1211001R0101"


@pytest.mark.parametrize("url", [
    "https://ex.example/p/a-1",
    "https://ex.example/fr-be/x-2",
])
def test_une_ancre_trop_courte_est_refusee(url):
    """`fr-be` ou `a-1` designent une locale ou rien : chercher dessus est du bruit."""
    assert famille_de_recherche("HPL1211001R0101", (url,)) == "HPL1211001R0101"


def test_une_designation_sans_chiffre_n_est_pas_une_designation_produit():
    urls = ("https://ex.example/produits/basse-tension/contacteurs-et-relais",)

    assert famille_de_recherche("HPL1211001R0101", urls) == "HPL1211001R0101"


def test_une_reference_vide_ne_casse_rien():
    assert famille_de_recherche("", (URL_Norel,)) == ""
