# -*- coding: utf-8 -*-
"""Tests de B2. Aucun n'a besoin de reseau, de cle ni de SearXNG.

    packages\\equivalence\\.venv\\Scripts\\python -m pytest packages\\equivalence\\tests -q

Les points couverts sont ceux annonces par la specification d'origine : lecture de la
fiche, construction de la mission, sortie complete avec preuves, cas sans
alternative fiable, et ecriture JSON/Markdown.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyse
import fiche as module_fiche
import rapport
import recherche
import robustesse
from mission import Alternative, Source, construire_mission, construire_prompt_requete


# --------------------------------------------------------------------------
# Lecture de la fiche
# --------------------------------------------------------------------------

def test_fiche_absente(tmp_path):
    with pytest.raises(module_fiche.FicheInvalide, match="introuvable"):
        module_fiche.lire(tmp_path / "rien.txt")


def test_fiche_vide(tmp_path):
    chemin = tmp_path / "vide.txt"
    chemin.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(module_fiche.FicheInvalide, match="vide"):
        module_fiche.lire(chemin)


def test_fiche_trop_courte(tmp_path):
    chemin = tmp_path / "court.txt"
    chemin.write_text("K7C48208", encoding="utf-8")
    with pytest.raises(module_fiche.FicheInvalide, match="trop courte"):
        module_fiche.lire(chemin)


def test_fiche_lue_telle_quelle(tmp_path):
    contenu = "Marque : Kerion\nReference : K7C48208\nCalibre : 16 A courbe C"
    chemin = tmp_path / "fiche.txt"
    chemin.write_text(contenu, encoding="utf-8")
    assert module_fiche.lire(chemin) == contenu


def test_fiche_encodage_windows(tmp_path):
    """Une fiche exportee depuis un outil Windows reste lisible."""
    chemin = tmp_path / "fiche.txt"
    chemin.write_bytes("Disjoncteur protégé 16 A courbe C".encode("cp1252"))
    assert "protégé" in module_fiche.lire(chemin)


# --------------------------------------------------------------------------
# Mission et requete
# --------------------------------------------------------------------------

def test_mission_cite_la_fiche_et_la_marque():
    mission = construire_mission("Disjoncteur 2P 16A courbe C", marque="Dorval")
    assert "Disjoncteur 2P 16A courbe C" in mission
    assert "Dorval" in mission


def test_mission_sans_marque_reste_libre():
    mission = construire_mission("Disjoncteur 2P 16A courbe C")
    assert "n'importe quel fabricant" in mission


def test_mission_refuse_une_fiche_vide():
    with pytest.raises(ValueError):
        construire_mission("   ")


def test_mission_porte_les_regles_anti_invention():
    """Sans ces regles, la bibliotheque se contente de « mets NA »."""
    mission = construire_mission("Disjoncteur 2P 16A")
    assert "copiée telle quelle" in mission
    assert "not_resolved" in mission
    # Un critere seulement plausible doit finir en limite, pas en preuve.
    assert "n'est PAS vérifié" in mission


def test_prompt_requete_exclut_tout_le_produit_dorigine():
    """Marque, gamme et reference d'origine ramenent le produit d'origine.

    Constat du 2026-08-14 : en n'excluant que la reference, le modele a ecrit
    « ... Sercia equivalent » et la recherche a rendu des pages Kerion.
    """
    prompt = construire_prompt_requete("Sercia SC60N 2P 16A", marque="Dorval")
    assert "NI la marque" in prompt
    assert "NI la gamme" in prompt
    assert "NI la reference du produit" in prompt
    assert "gamme equivalente de Dorval" in prompt


def test_prompt_requete_sans_marque_interdit_toute_marque():
    """Sans cible, le modele comblait le vide avec la gamme d'origine.

    Essai du 2026-08-14 : une fiche « Kerion, disjoncteur 2P 16A » avait
    produit « ... SC60N » et ramene le produit de depart.
    """
    prompt = construire_prompt_requete("Kerion, disjoncteur 2P 16A")
    assert "ne nommer AUCUNE marque" in prompt


@pytest.mark.parametrize("url", [
    "https://www.administration.example/files/BPU21102025-def.ods",
    "https://exemple.fr/notice.odt",
])
def test_formats_opendocument_ecartes(url):
    """Cas reel : un .ods de administration.example dans une recherche de disjoncteur."""
    assert not robustesse.url_lisible(url)


def test_requete_nettoyee_de_la_phrase_dintroduction():
    """Le modele glisse parfois une introduction malgre la consigne."""
    reponse = "Voici la requête :\ndisjoncteur Dorval DX3 2P 16A courbe C"
    requete = recherche.construire_requete("fiche", "Dorval", lambda _: reponse)
    assert requete == "disjoncteur Dorval DX3 2P 16A courbe C"


def test_requete_tronquee_si_le_modele_redige():
    requete = recherche.construire_requete("fiche", None, lambda _: "mot " * 40)
    assert len(requete.split()) == recherche.MOTS_MAXIMUM


def test_requete_vide_refusee():
    with pytest.raises(recherche.RechercheIndisponible):
        recherche.construire_requete("fiche", None, lambda _: "  ")


def test_gamme_dorigine_retiree_de_la_requete():
    """Cas reel : « Contacteur Tersa D 9A 24V DC 3P Norel » ramenait du Kerion."""
    nettoyee = recherche.retirer_origine(
        "Contacteur Tersa D 9A 24V DC 3P Norel", "Kerion Electric; Tersa D"
    )
    assert "Tersa" not in nettoyee
    assert "Norel" in nettoyee and "9A" in nettoyee
    # « Tersa D » retire laisse un « D » seul, qui avait ramene l'article
    # Wikipedia sur la lettre D.
    assert " D " not in f" {nettoyee} "


def test_lettre_qui_qualifie_un_critere_est_conservee():
    """« courbe C » est un critere ; seule une lettre ORPHELINE doit sauter."""
    nettoyee = recherche.retirer_origine(
        "disjoncteur 2P 16A courbe C Sercia", "Kerion; Sercia"
    )
    assert nettoyee.endswith("courbe C")


def test_mission_distingue_ecart_constate_et_point_a_confirmer():
    """L'outil avait range un ecart reel dans les limites, et dit 'complete'."""
    mission = construire_mission("Contacteur 3P 9A bobine 24 V DC")
    assert "ÉCART CONSTATÉ" in mission
    assert "avec_adaptation" in mission
    # Un ecart bloquant doit faire chuter la pertinence, pas etre absous.
    assert "descendre sous 50" in mission


def test_mots_generiques_conserves():
    """Retirer « Electric » ne doit pas emporter « contacteur »."""
    nettoyee = recherche.retirer_origine(
        "contacteur 3P 9A 24V DC", "Kerion Electric"
    )
    assert nettoyee == "contacteur 3P 9A 24V DC"


def test_nettoyage_qui_viderait_la_requete_est_abandonne():
    """Mieux vaut une requete imparfaite qu'un fragment inutilisable."""
    requete = "Sercia SC60N"
    assert recherche.retirer_origine(requete, "Kerion; Sercia SC60N") == requete


def test_marque_visee_ajoutee_si_absente():
    """Sans le nom de la marque, la requete ramene le produit d'origine."""
    appels = iter(["contacteur 3P 9A 24V DC", "Kerion Electric; Tersa D"])
    requete = recherche.construire_requete("fiche", "Norel", lambda _: next(appels))
    assert requete.startswith("Norel ")


def test_marque_visee_non_dupliquee():
    appels = iter(["contacteur Norel 3P 9A 24V DC", "Kerion; Tersa"])
    requete = recherche.construire_requete("fiche", "Norel", lambda _: next(appels))
    assert requete.lower().count("norel") == 1


def test_marque_visee_survit_au_nettoyage():
    """Si le modele confond origine et cible, la cible doit rester."""
    appels = iter(["contacteur 3P 9A 24V DC Norel", "Norel; Tersa D"])
    requete = recherche.construire_requete("fiche", "Norel", lambda _: next(appels))
    assert "Norel" in requete
    assert "Tersa" not in requete


# --------------------------------------------------------------------------
# Filtrage des ressources illisibles
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://assets.dorval.example/general/cession/autre/3d_lg-080054.stp",
    "https://exemple.fr/plan.dwg?v=2",
    "https://exemple.fr/fiche.pdf",
    "https://exemple.fr/archive.zip#bloc",
])
def test_ressources_illisibles_ecartees(url):
    """Le .stp est le cas reel qui avait fait tomber un run entier."""
    assert not robustesse.url_lisible(url)


@pytest.mark.parametrize("url", [
    "https://www.dorval.example/produits/disjoncteur-dx3-16a",
    "https://kr.example/product/K7C48208/",
])
def test_pages_conservees(url):
    assert robustesse.url_lisible(url)


# --------------------------------------------------------------------------
# Selection du meilleur resultat
# --------------------------------------------------------------------------

def _resultat(alternative="Dorval DX3 507800", statut="complete",
              types=("web_officiel",), limites=(), ecarts=(), pertinence=100):
    return Alternative(
        produit_demande="Kerion K7C48208",
        statut=statut,
        alternative=alternative,
        justification="Mêmes critères essentiels.",
        pertinence=pertinence,
        ecarts=list(ecarts),
        limites=list(limites),
        sources=[Source(url=f"https://exemple.fr/{i}", extrait="extrait", type=t)
                 for i, t in enumerate(types)],
    )


# --------------------------------------------------------------------------
# Equivalent moyennant adaptation
# --------------------------------------------------------------------------

def test_ecart_rattrapable_est_propose_et_non_tu():
    """Cas Norel XZ07-20-10-11 : 1 NO au lieu de 1 NO + 1 NF.

    Un bloc auxiliaire rattrape l'ecart : taire le candidat serait plus
    trompeur que le proposer avec sa difference.
    """
    candidat = _resultat(statut="avec_adaptation", pertinence=85,
                         ecarts=("1 NO seul : ajouter un bloc auxiliaire",))
    assert analyse.est_exploitable(candidat)


def test_ecart_bloquant_reste_ecarte():
    """Bobine 24 V AC pour un besoin 24 V DC : sous le seuil, donc refuse."""
    candidat = _resultat(statut="avec_adaptation", pertinence=40,
                         ecarts=("bobine 24 V AC au lieu de 24 V DC",))
    assert not analyse.est_exploitable(candidat)


def test_seuil_de_pertinence_reglable():
    candidat = _resultat(statut="avec_adaptation", pertinence=60,
                         ecarts=("un ecart",))
    assert not analyse.est_exploitable(candidat)
    assert analyse.est_exploitable(candidat, pertinence_minimale=55)


def test_sans_ecart_prefere_a_avec_ecart():
    """Une difference constatee est un fait ; la pertinence, une appreciation."""
    adapte = _resultat(alternative="A", statut="avec_adaptation",
                       pertinence=100, ecarts=("un ecart",))
    direct = _resultat(alternative="B", pertinence=80)
    assert analyse.meilleur([adapte, direct]).alternative == "B"


def test_statut_rendu_decoule_des_ecarts():
    """Un candidat qui declare un ecart n'est pas 'complete', quoi qu'il dise."""
    donnees = rapport.construire(
        _resultat(statut="complete", ecarts=("bloc auxiliaire à ajouter",)), []
    )
    assert donnees["status"] == "avec_adaptation"


def test_rapport_separe_ecarts_et_points_a_confirmer():
    donnees = rapport.construire(
        _resultat(statut="avec_adaptation", pertinence=85,
                  ecarts=("1 NO seul",), limites=("montage à vérifier",)), []
    )
    texte = donnees["alternative_proposee"]
    assert "Écarts constatés :\n- 1 NO seul" in texte
    assert "Points à confirmer :\n- montage à vérifier" in texte
    assert "Pertinence estimée : 85 %" in texte


def test_resultat_sans_source_nest_pas_exploitable():
    """Un « complete » sans preuve est une contradiction interne."""
    resultat = _resultat(types=())
    assert not analyse.est_exploitable(resultat)


def test_resultat_sans_reference_nest_pas_exploitable():
    assert not analyse.est_exploitable(_resultat(alternative=None))


def test_not_resolved_nest_pas_exploitable():
    assert not analyse.est_exploitable(_resultat(statut="not_resolved"))


def test_source_officielle_preferee_au_revendeur():
    revendeur = _resultat(alternative="A", types=("web_secondaire",))
    officiel = _resultat(alternative="B", types=("web_officiel",))
    assert analyse.meilleur([revendeur, officiel]).alternative == "B"


def test_a_preuve_egale_le_moins_de_limites_gagne():
    charge = _resultat(alternative="A", limites=("écart 1", "écart 2"))
    direct = _resultat(alternative="B")
    assert analyse.meilleur([charge, direct]).alternative == "B"


def test_aucun_repli_si_rien_nest_etaye():
    assert analyse.meilleur([_resultat(statut="not_resolved")]) is None


@pytest.mark.parametrize("contenu, attendu", [
    ("<title>Just a moment...</title>" + "x" * 5000, "protegee"),
    ("Attention Required! Cloudflare" + "x" * 5000, "protegee"),
    ("page trop courte", "quasi vide"),
    ("<html>" + "contenu de fiche produit " * 200 + "</html>", ""),
])
def test_diagnostic_des_pages_bloquees(contenu, attendu):
    """Cas reel : un distributeur rendait 6 533 car. titres « Just a moment... »."""
    motif = robustesse.diagnostic_page(contenu)
    assert attendu in motif if attendu else motif == ""


def test_page_bloquee_signalee_distinctement():
    """« Rien trouvé » et « site bloqué » ne se lisent pas pareil."""
    def analyser(url, prompt, config):
        raise analyse.PageBloquee("page protegee contre le scraping (captcha)")

    resultats, avertissements = analyse.examiner(
        ["https://bloque.fr"], "mission", {}, analyser=analyser
    )
    assert resultats == []
    assert "Page inaccessible" in avertissements[0]
    assert "captcha" in avertissements[0]


def test_une_page_en_echec_ne_perd_pas_les_autres():
    def analyser(url, prompt, config):
        if "casse" in url:
            raise RuntimeError("Timeout 30000ms exceeded")
        return _resultat()

    resultats, avertissements = analyse.examiner(
        ["https://ok.fr", "https://casse.fr"], "mission", {}, analyser=analyser
    )
    assert len(resultats) == 1
    assert any("casse.fr" in a for a in avertissements)


# --------------------------------------------------------------------------
# Sorties
# --------------------------------------------------------------------------

def test_sortie_complete_porte_les_preuves():
    donnees = rapport.construire(_resultat(), [])
    assert donnees["status"] == "complete"
    assert "Dorval DX3 507800" in donnees["alternative_proposee"]
    assert donnees["sources"][0]["type"] == "web_officiel"
    assert donnees["sources"][0]["extrait"] == "extrait"


def test_sortie_sans_alternative_ninvente_rien():
    donnees = rapport.construire(None, ["aucune page"], produit_demande="K7C48208")
    assert donnees["status"] == "not_resolved"
    assert "aucune" in donnees["alternative_proposee"].lower()
    assert donnees["sources"] == []
    assert "K7C48208" in donnees["alternative_proposee"]


def test_entree_invalide():
    donnees = rapport.entree_invalide("Fiche introuvable : x.txt")
    assert donnees["status"] == "invalid_input"
    assert donnees["warnings"] == ["Fiche introuvable : x.txt"]


def test_markdown_annonce_les_sections_vides():
    """Une section absente se lit comme un oubli ; on la dit."""
    texte = rapport.en_markdown(rapport.construire(None, [], produit_demande="X"))
    assert "# Alternative produit" in texte
    assert "_Aucune source retenue._" in texte
    assert "_Aucun._" in texte


def test_markdown_cite_les_extraits():
    texte = rapport.en_markdown(rapport.construire(_resultat(), []))
    assert "> extrait" in texte
    assert "web_officiel" in texte


def test_ecriture_des_deux_fichiers(tmp_path):
    donnees = rapport.construire(_resultat(), ["un avertissement"])
    chemin_json, chemin_md = rapport.ecrire(tmp_path, donnees)

    assert json.loads(chemin_json.read_text(encoding="utf-8")) == donnees
    assert "# Alternative produit" in chemin_md.read_text(encoding="utf-8")


def test_ecriture_cree_le_dossier(tmp_path):
    chemin_json, _ = rapport.ecrire(tmp_path / "sous" / "dossier",
                                    rapport.entree_invalide("motif"))
    assert chemin_json.exists()


def test_accents_preserves_a_lecriture(tmp_path):
    """Sous Windows le defaut est l'ANSI, qui mutile les fiches produit."""
    resultat = _resultat()
    resultat.justification = "Pouvoir de coupure éprouvé à 6 kA"
    chemin_json, _ = rapport.ecrire(tmp_path, rapport.construire(resultat, []))
    assert "éprouvé" in chemin_json.read_text(encoding="utf-8")
