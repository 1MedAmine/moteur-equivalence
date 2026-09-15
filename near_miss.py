# -*- coding: utf-8 -*-
"""Qualifier un candidat rejete comme direction de recherche.

Une incompatibilite prouvee sur un candidat proche n'est pas seulement un motif
de rejet : elle dit quel attribut chercher autrement. Ce module decide quels
candidats rejetes meritent une recherche ciblee, et par quel chemin.

Il ne compose aucune requete, ne priorise rien et ne deduplique rien : ces
responsabilites appartiennent aux etapes suivantes. Il ne produit jamais de
preuve — une direction de recherche reste une intention.

Mesure fondatrice (XZ07-20-10-13, 2026-08-19) : trois criteres annonces
compatibles reposaient sur une citation reconstruite par le modele, refusee par
le contrat de preuve. Seule l'incompatibilite de bobine s'appuyait sur un
extrait litteral. C'etait le seul fait solide de cet audit — et il suffisait a
indiquer quoi chercher.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from modeles import CandidateEvaluation, RequirementSet


class EligibilityPath(str, Enum):
    """Par quel chemin un candidat rejete devient une direction de recherche."""

    #: Plusieurs criteres compatibles prouves attestent la proximite.
    ESTABLISHED = "ESTABLISHED"
    #: Aucun compatible prouve, mais un bloqueur unique donne la direction.
    DIRECTIONAL = "DIRECTIONAL"
    #: Aucun bloqueur : le candidat tient sur ce qui a ete lu, mais des
    #: criteres restent sans preuve. Ce sont eux qui donnent la direction.
    INCOMPLETE = "INCOMPLETE"


#: Chemin A : il faut au moins ce nombre de criteres compatibles prouves.
MIN_PROVEN_COMPATIBLE = 2

#: Chemin A : au-dela, le candidat echoue sur trop de points pour etre proche.
MAX_BLOCKERS_ESTABLISHED = 2

#: Chemin B : sans compatible pour attester la proximite, un seul bloqueur.
MAX_BLOCKERS_DIRECTIONAL = 1

#: Motifs structures. Aucun texte libre du modele n'entre ici.
REASON_ELIGIBLE = "NEAR_MISS_BLOCKING_CRITERION"
REASON_INCOMPLETE = "NEAR_MISS_UNVERIFIED_CRITERION"
REASON_NO_INCOMPATIBILITY = "NOT_ELIGIBLE_NO_PROVEN_INCOMPATIBILITY"
REASON_NO_IDENTITY = "NOT_ELIGIBLE_NO_CREDIBLE_IDENTITY"
REASON_NO_TARGET = "NOT_ELIGIBLE_NO_TARGET_VALUE"
REASON_WRONG_CATEGORY = "NOT_ELIGIBLE_WRONG_PRODUCT_CATEGORY"
REASON_TOO_MANY_BLOCKERS = "NOT_ELIGIBLE_TOO_MANY_BLOCKERS"
REASON_DIRECTIONAL_MULTI = "NOT_ELIGIBLE_DIRECTIONAL_REQUIRES_SINGLE_BLOCKER"

#: Termes qui designent le critere portant le type de produit. Ils appartiennent
#: au vocabulaire du cahier des charges, jamais a un fabricant : aucune liste de
#: categories — « contacteur », « disjoncteur », « vanne » — n'est codee ici.
CATEGORY_LABEL_HINTS: tuple[str, ...] = (
    "type de produit",
    "categorie",
    "famille de produit",
    "nature du produit",
    "usage",
    "product type",
    "category",
)

#: Criteres qui portent l'identite du produit plutot qu'une caracteristique.
#:
#: Un equivalent differe necessairement de l'origine sur ces attributs — c'est
#: la definition meme de la recherche. Les compter comme bloqueurs rendait le
#: chemin near-miss inatteignable des qu'on change de marque (mesure du
#: 2026-08-19 : `fabricant`, `reference_exacte`, `famille` disqualifiaient
#: chaque candidat Norel en TOO_MANY_BLOCKERS) et noyait le seul ecart
#: reellement exploitable.
#:
#: Ils ne sont pas non plus cherchables : une requete portant la marque
#: d'origine enverrait chercher l'inverse du besoin.
#:
#: La liste nomme des roles metier — marque, reference, famille — jamais un
#: fabricant ni un catalogue.
IDENTITY_LABEL_HINTS: tuple[str, ...] = (
    "fabricant",
    "manufacturer",
    "marque",
    "brand",
    "reference",
    "mpn",
    "part number",
    "famille",
    "family",
    "gamme",
    "serie",
    "series",
    "modele",
    "model",
)

#: Formes de recherche complementaires par critere. Elles appartiennent au
#: vocabulaire technique du domaine — un terme anglais courant pour un libelle
#: francais — et jamais a un fabricant. La cle est le libelle du cahier des
#: charges, normalise.
CRITERION_SEARCH_TERMS: dict[str, tuple[str, ...]] = {
    "tension de bobine": ("coil voltage",),
    "tension de commande": ("control voltage",),
    "courant nominal": ("rated current",),
    "nombre de poles": ("number of poles",),
    "contacts principaux": ("main contacts",),
}


@dataclass(frozen=True)
class NearMissAssessment:
    """Verdict d'eligibilite, structure pour le rapport autant que pour le code."""

    eligible: bool
    reason: str
    path: EligibilityPath | None = None
    identity: str = ""
    family: str = ""
    proven_compatible_count: int = 0
    blocking_criteria: tuple[str, ...] = ()
    unverified_criteria: tuple[str, ...] = ()
    #: Criteres sans preuve et reellement cherchables. Ils portent la direction
    #: du chemin `INCOMPLETE`, comme `blocking_criteria` porte celle des deux
    #: autres.
    missing_criteria: tuple[str, ...] = ()

    @property
    def research_criteria(self) -> tuple[str, ...]:
        """Les criteres qui portent la direction de recherche.

        Un bloqueur dit « cette valeur est fausse, cherche l'autre ». Un
        critere manquant dit « cette valeur n'est pas prouvee, va la prouver ».
        Les deux se composent en requete de la meme facon, mais ils ne
        viennent pas du meme constat.
        """
        if self.path is EligibilityPath.INCOMPLETE:
            return self.missing_criteria
        return self.blocking_criteria

    def as_diagnostic(self) -> dict:
        """Projection sure : ni fragment de page, ni reponse brute du modele."""
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "eligibility_path": self.path.value if self.path else None,
            "identity": self.identity,
            "family": self.family,
            "proven_compatible_count": self.proven_compatible_count,
            "blocking_criteria": list(self.blocking_criteria),
            "missing_criteria": list(self.missing_criteria),
        }


@dataclass(frozen=True)
class TargetedResearchTask:
    """Une direction de recherche, jamais une preuve.

    Une tache par bloqueur : deux criteres incompatibles appellent deux
    recherches distinctes, chacune avec sa propre valeur cible.
    """

    candidate_identity: str
    product_family: str
    blocking_criterion: str
    observed_value: str
    target_value: str
    search_queries: tuple[str, ...]
    reason: str
    eligibility_path: str
    signature: str
    proven_compatible_count: int = 0
    blocker_count: int = 1
    priority: int = 0

    @property
    def family(self) -> str:
        """Ancre de recherche : la famille si observee, sinon l'identite."""
        return self.product_family or self.candidate_identity

    def as_diagnostic(self) -> dict:
        """Projection sure : aucun fragment de page ni reponse brute du modele."""
        return {
            "candidate_identity": self.candidate_identity,
            "product_family": self.product_family,
            "blocking_criterion": self.blocking_criterion,
            "observed_value": self.observed_value,
            "target_value": self.target_value,
            "search_queries": list(self.search_queries),
            "reason": self.reason,
            "eligibility_path": self.eligibility_path,
            "signature": self.signature,
            "priority": self.priority,
        }


def normalize_key(value: str) -> str:
    """Unifie Unicode, casse et separateurs — pour les cles, jamais les requetes."""
    decompose = unicodedata.normalize("NFKD", str(value or ""))
    sans_accent = "".join(c for c in decompose if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", sans_accent.casefold(), flags=re.UNICODE))


def task_signature(family_or_identity: str, criterion: str, target_value: str) -> str:
    """Cle de deduplication stable.

    Deux variantes fautives d'une meme famille sur le meme critere appellent la
    meme recherche : leur signature doit donc coincider. La cle porte la
    famille, jamais la reference complete du candidat — sinon `XZ07-20-10-13`
    et `XZ07-20-10-14` produiraient deux fois la meme requete.
    """
    return "|".join((
        normalize_key(family_or_identity),
        normalize_key(criterion),
        normalize_key(target_value),
    ))


def _termes_de_recherche(label: str) -> tuple[str, ...]:
    complementaires = CRITERION_SEARCH_TERMS.get(normalize_key(label), ())
    return (label, *complementaires)


def build_queries(
    identity: str,
    family: str,
    criterion_label: str,
    target_value: str,
) -> tuple[str, ...]:
    """Compose les requetes ciblees.

    Chaque requete porte obligatoirement les trois composants : l'ancre
    d'identite, le critere bloquant sous une forme recherchable, et la valeur
    cible citee telle qu'ecrite au cahier des charges.

    La valeur cible n'est ni convertie ni normalisee : `24 V DC` reste
    `24 V DC`. Aucune reference n'est derivee — l'ancre est ce qui a ete
    reellement observe, et la variante recherchee sera decouverte par le
    moteur, pas fabriquee ici.
    """
    cible = str(target_value or "").strip()
    if not cible:
        return ()

    ancres = [valeur for valeur in (family, identity) if str(valeur or "").strip()]
    if not ancres:
        return ()

    requetes: list[str] = []
    for ancre, terme in zip(ancres, _termes_de_recherche(criterion_label)):
        requete = f'{ancre.strip()} {terme.strip()} "{cible}"'
        if requete not in requetes:
            requetes.append(requete)

    # Si une seule ancre existe, elle porte toutes les formes du critere.
    if len(ancres) == 1:
        for terme in _termes_de_recherche(criterion_label)[1:]:
            requete = f'{ancres[0].strip()} {terme.strip()} "{cible}"'
            if requete not in requetes:
                requetes.append(requete)

    return tuple(requetes)


def assess_candidates(
    assessed: Sequence[tuple[CandidateEvaluation, dict]],
    requirements: RequirementSet,
) -> tuple[NearMissAssessment, ...]:
    """Verdict d'eligibilite pour chaque candidat, retenu comme ecarte.

    `build_targeted_tasks` ne rend que les taches : un candidat ecarte y
    disparait sans dire pourquoi. Cette fonction expose les deux, de sorte
    qu'un rejet reste explicable — sinon on remplace une panne opaque par un
    rejet opaque, ce qui ne vaut pas mieux.
    """
    return tuple(
        assess_near_miss(evaluation, requirements, identity)
        for evaluation, identity in assessed
    )


def build_targeted_tasks(
    assessed: Sequence[tuple[CandidateEvaluation, dict]],
    requirements: RequirementSet,
) -> tuple[TargetedResearchTask, ...]:
    """Transforme les candidats rejetes eligibles en directions de recherche.

    L'ordre est deterministe et ne depend jamais de l'ordre d'arrivee : plus de
    criteres compatibles prouves d'abord, moins de bloqueurs ensuite, puis la
    signature en dernier depart.

    Cette fonction ne decide pas quelles taches entrent dans le budget
    disponible : la repartition des `logical_queries` appartient a l'etape
    suivante.
    """
    labels = _labels_par_id(requirements)
    cibles = _valeurs_cibles(requirements)
    observees = {}

    taches: list[TargetedResearchTask] = []
    for evaluation, identity in assessed:
        assessment = assess_near_miss(evaluation, requirements, identity)
        if not assessment.eligible or assessment.path is None:
            continue

        observees = {
            item.requirement_id: item.observed_value
            for item in evaluation.candidate.criteria
        }
        ancre = assessment.family or assessment.identity

        motif = (
            REASON_INCOMPLETE
            if assessment.path is EligibilityPath.INCOMPLETE
            else REASON_ELIGIBLE
        )

        for bloqueur in assessment.research_criteria:
            libelle = labels.get(bloqueur, bloqueur)
            cible = str(cibles.get(bloqueur, "") or "").strip()
            requetes = build_queries(
                assessment.identity, assessment.family, libelle, cible
            )
            if not requetes:
                continue
            taches.append(TargetedResearchTask(
                candidate_identity=assessment.identity,
                product_family=assessment.family,
                blocking_criterion=bloqueur,
                observed_value=str(observees.get(bloqueur, "") or ""),
                target_value=cible,
                search_queries=requetes,
                reason=f"{motif}:{bloqueur}",
                eligibility_path=assessment.path.value,
                signature=task_signature(ancre, bloqueur, cible),
                proven_compatible_count=assessment.proven_compatible_count,
                blocker_count=len(assessment.blocking_criteria),
            ))

    # Meme cle que la selection : deux ordres divergents rendraient la priorite
    # affichee incoherente avec celle qui depense reellement le budget.
    ordonnees = sorted(taches, key=_ordre_de_priorite)
    return tuple(
        TargetedResearchTask(**{**vars(tache), "priority": rang})
        for rang, tache in enumerate(ordonnees)
    )


@dataclass(frozen=True)
class TargetedSelection:
    """Ce qui a ete retenu, et ce qui a ete ecarte en le disant."""

    selected: tuple[TargetedResearchTask, ...] = ()
    queries: tuple[str, ...] = ()
    deduplicated: tuple[str, ...] = ()
    already_seen: tuple[str, ...] = ()
    reserved_directional: str = ""

    def as_diagnostic(self) -> dict:
        return {
            "selected_signatures": [item.signature for item in self.selected],
            "queries": list(self.queries),
            "deduplicated": list(self.deduplicated),
            "already_seen": list(self.already_seen),
            "reserved_directional": self.reserved_directional,
        }


#: Ordre entre chemins, du plus proche du but au plus speculatif.
#:
#: `INCOMPLETE` tient deja sur tout ce qui a pu etre lu : lui trouver sa preuve
#: manquante peut resoudre la mission. `ESTABLISHED` porte au contraire une
#: incompatibilite prouvee — il ne peut pas etre la reponse, seulement designer
#: une variante a chercher. `DIRECTIONAL` n'a meme pas de compatible prouve.
_RANG_DE_CHEMIN: dict[str, int] = {
    EligibilityPath.INCOMPLETE.value: 0,
    EligibilityPath.ESTABLISHED.value: 1,
    EligibilityPath.DIRECTIONAL.value: 2,
}


def _ordre_de_priorite(tache: TargetedResearchTask) -> tuple:
    """Cle deterministe, independante de l'ordre d'arrivee."""
    return (
        _RANG_DE_CHEMIN.get(tache.eligibility_path, len(_RANG_DE_CHEMIN)),
        -tache.proven_compatible_count,
        tache.blocker_count,
        tache.signature,
    )


def select_targeted_queries(
    tasks: Sequence[TargetedResearchTask],
    *,
    remaining_logical_queries: int,
    seen_signatures: frozenset[str] | set[str] | None = None,
) -> TargetedSelection:
    """Repartit un budget deja decide entre les taches ciblees.

    Fonction pure : elle ne contacte rien et ne cree aucun budget. Elle
    repartit `remaining_logical_queries` et s'arrete des qu'il est epuise.

    L'ordre `ESTABLISHED`-first eliminerait systematiquement les taches
    directionnelles — precisement les candidats dont le seul fait prouve est
    l'incompatibilite. Une place est donc reservee au meilleur `DIRECTIONAL`
    des que deux requetes au moins restent disponibles. Avec une seule place,
    `ESTABLISHED` reste prioritaire.
    """
    budget = max(0, int(remaining_logical_queries))
    deja_vues = set(seen_signatures or ())

    retenues: list[TargetedResearchTask] = []
    dupliquees: list[str] = []
    ignorees: list[str] = []
    vues: set[str] = set()

    for tache in sorted(tasks, key=_ordre_de_priorite):
        if tache.signature in deja_vues:
            if tache.signature not in ignorees:
                ignorees.append(tache.signature)
            continue
        if tache.signature in vues:
            if tache.signature not in dupliquees:
                dupliquees.append(tache.signature)
            continue
        vues.add(tache.signature)
        retenues.append(tache)

    if not budget or not retenues:
        return TargetedSelection(
            deduplicated=tuple(dupliquees),
            already_seen=tuple(ignorees),
        )

    directionnel = EligibilityPath.DIRECTIONAL.value
    autres = [t for t in retenues if t.eligibility_path != directionnel]
    directionnelles = [t for t in retenues if t.eligibility_path == directionnel]

    # La reservation ne joue que si plusieurs chemins coexistent et qu'il reste
    # au moins deux places : avec une seule, le chemin le mieux classe gagne.
    reserve = (
        directionnelles[0]
        if autres and directionnelles and budget >= 2
        else None
    )

    ordre: list[TargetedResearchTask] = []
    if reserve is not None:
        ordre.append(reserve)
    ordre.extend(t for t in retenues if t is not reserve)

    # Une requete primaire par tache d'abord : sans cela, une seule tache
    # monopoliserait le budget avec ses formulations secondaires.
    primaires: list[tuple[TargetedResearchTask, str]] = []
    secondaires: list[tuple[TargetedResearchTask, str]] = []
    for tache in ordre:
        for rang, requete in enumerate(tache.search_queries):
            (primaires if rang == 0 else secondaires).append((tache, requete))

    requetes: list[str] = []
    selectionnees: list[TargetedResearchTask] = []
    for tache, requete in (*primaires, *secondaires):
        if len(requetes) >= budget:
            break
        requetes.append(requete)
        if tache not in selectionnees:
            selectionnees.append(tache)

    return TargetedSelection(
        selected=tuple(selectionnees),
        queries=tuple(requetes),
        deduplicated=tuple(dupliquees),
        already_seen=tuple(ignorees),
        reserved_directional=reserve.signature if reserve is not None else "",
    )


def _labels_par_id(requirements: RequirementSet) -> dict[str, str]:
    return {item.id: item.label for item in requirements.criteria}


def _valeurs_cibles(requirements: RequirementSet) -> dict[str, str]:
    return {item.id: item.requested_value for item in requirements.criteria}


def _identifiants_par_label(requirements: RequirementSet) -> dict[str, str]:
    return {item.label: item.id for item in requirements.criteria}


def _est_critere_identite(label: str) -> bool:
    """Le critere designe-t-il l'identite du produit plutot qu'une propriete ?

    La comparaison porte sur les mots du libelle, pas sur une sous-chaine :
    « Reference exacte » est une identite, « Indice de protection » n'en est
    pas une malgre le mot « protection ». Un libelle compose comme
    « part number » est cherche tel quel.
    """
    normalise = normalize_key(label)
    if not normalise:
        return False
    mots = set(normalise.split())
    for indice in IDENTITY_LABEL_HINTS:
        indice_normalise = normalize_key(indice)
        if " " in indice_normalise:
            if indice_normalise in normalise:
                return True
        elif indice_normalise in mots:
            return True
    return False


def _est_critere_de_categorie(label: str) -> bool:
    """Le critere porte-t-il le type de produit plutot qu'une caracteristique ?

    La detection s'appuie sur le libelle du cahier des charges. Elle reste donc
    valable pour un domaine quelconque — vannes, moteurs, capteurs — sans
    qu'aucune categorie ne soit enumeree en production.
    """
    normalise = label.strip().casefold()
    return any(indice in normalise for indice in CATEGORY_LABEL_HINTS)


def assess_near_miss(
    evaluation: CandidateEvaluation,
    requirements: RequirementSet,
    identity: dict | None = None,
) -> NearMissAssessment:
    """Decide si un candidat rejete ouvre une recherche ciblee, et par quel chemin.

    `identity` porte l'identite corroboree et la famille observee. Aucune des
    deux n'est deduite ni inventee ici : elles proviennent d'`IdentityExtractor`
    et du contenu reellement recupere.
    """
    identity = identity or {}
    identifiant = str(identity.get("identity", "") or "").strip()
    famille = str(identity.get("family", "") or "").strip()

    summary = evaluation.summary
    par_label = _identifiants_par_label(requirements)
    labels = _labels_par_id(requirements)
    cibles = _valeurs_cibles(requirements)

    # Le contrat de preuve a deja retrograde les citations refusees : elles
    # figurent dans `unverified_criteria` et ne comptent jamais comme compatibles.
    inverifies = tuple(
        par_label.get(label, label) for label in summary.unverified_criteria
    )
    # Seuls les ecarts techniques donnent une direction : un equivalent differe
    # forcement de l'origine sur la marque, la reference et la famille, et ces
    # attributs ne sont pas cherchables comme un critere.
    bloqueurs = tuple(
        identifiant
        for identifiant in (
            par_label.get(label, label) for label in summary.incompatible_criteria
        )
        if not _est_critere_identite(labels.get(identifiant, identifiant))
    )
    compatibles = len(summary.proven_criteria)

    # `not_proven` (le modele n'a rien etabli) et `unverified` (le contrat de
    # preuve a retrograde une citation reconstruite) disent la meme chose : la
    # preuve reste a trouver. Les memes filtres que pour les bloqueurs
    # s'appliquent — un critere d'identite ou de categorie ne se cherche pas,
    # et sans valeur cible la requete n'aurait rien a citer.
    sans_preuve = dict.fromkeys(
        par_label.get(label, label)
        for label in (*summary.not_proven_criteria, *summary.unverified_criteria)
    )
    manquants = tuple(
        item
        for item in sans_preuve
        if not _est_critere_identite(labels.get(item, item))
        and not _est_critere_de_categorie(labels.get(item, ""))
        and str(cibles.get(item, "") or "").strip()
    )

    commun = {
        "identity": identifiant,
        "family": famille,
        "proven_compatible_count": compatibles,
        "blocking_criteria": bloqueurs,
        "unverified_criteria": inverifies,
        "missing_criteria": manquants,
    }

    def refus(motif: str) -> NearMissAssessment:
        return NearMissAssessment(eligible=False, reason=motif, **commun)

    # 1. Sans identite ni famille, la requete n'aurait rien a ancrer, quel que
    #    soit le chemin.
    if not identifiant and not famille:
        return refus(REASON_NO_IDENTITY)

    # 2. Sans fait negatif prouve, deux situations opposees se presentent.
    #    Un candidat deja bien etaye dont il ne manque que des preuves reste
    #    exploitable : ce sont ses criteres non prouves qui donnent la
    #    direction. Mesure du 2026-08-20 : `4KBL103001R8110` sortait avec
    #    quatre compatibles prouves et zero bloqueur, et ne produisait aucune
    #    recherche — la mission a fini sans candidat prouvable.
    if not bloqueurs:
        if compatibles >= MIN_PROVEN_COMPATIBLE and manquants:
            return NearMissAssessment(
                eligible=True,
                reason=f"{REASON_INCOMPLETE}:{','.join(manquants)}",
                path=EligibilityPath.INCOMPLETE,
                **commun,
            )
        return refus(REASON_NO_INCOMPATIBILITY)

    # 3. Une mauvaise categorie signale une erreur de recherche, pas une
    #    variante a trouver : chercher dans la mauvaise famille gaspille le
    #    budget.
    if any(_est_critere_de_categorie(labels.get(item, "")) for item in bloqueurs):
        return refus(REASON_WRONG_CATEGORY)

    # 4. Chaque bloqueur doit porter une valeur cible : c'est ce que la requete
    #    ira chercher.
    if any(not str(cibles.get(item, "") or "").strip() for item in bloqueurs):
        return refus(REASON_NO_TARGET)

    # 5. Trop de bloqueurs : le candidat n'est plus proche, quel que soit le
    #    chemin.
    if len(bloqueurs) > MAX_BLOCKERS_ESTABLISHED:
        return refus(REASON_TOO_MANY_BLOCKERS)

    if compatibles >= MIN_PROVEN_COMPATIBLE:
        chemin = EligibilityPath.ESTABLISHED
    elif len(bloqueurs) <= MAX_BLOCKERS_DIRECTIONAL:
        # Sans critere compatible pour attester la proximite, un bloqueur unique
        # reste exploitable : c'est lui qui porte la direction.
        chemin = EligibilityPath.DIRECTIONAL
    else:
        return refus(REASON_DIRECTIONAL_MULTI)

    return NearMissAssessment(
        eligible=True,
        reason=f"{REASON_ELIGIBLE}:{','.join(bloqueurs)}",
        path=chemin,
        **commun,
    )
