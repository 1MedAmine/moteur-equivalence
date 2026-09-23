# -*- coding: utf-8 -*-
"""La consigne metier de B2 : trouver UNE alternative, ou n'en proposer aucune.

Objectif repris de la specification de la generation precedente (`docs/specs/
2026-08-13-alternative-b1-design.md`) : a partir d'une fiche technique, rendre
l'alternative la plus pertinente, sa justification et ses limites, ou le statut
`not_resolved` si les preuves ne suffisent pas. Seul le moteur change — la generation precedente
passait par un rédacteur agentique, celle-ci par un extracteur de contenu.

POURQUOI CE MODULE N'EST PAS UNE COPIE DE LA MISSION DE LA GENERATION PRECEDENTE

Les deux moteurs ne traitent pas le prompt de la meme facon.

Le redacteur agentique recevait la mission entiere et redigeait un rapport
libre. ScrapeGraphAI, lui, injecte ce qu'on lui passe en `prompt` a l'interieur
de son propre gabarit (`prompts/generate_answer_node_prompts.py`) :

    You are a website scraper ...
    If you don't find the answer put as value "NA".
    Make sure the output is a valid json format ...
    OUTPUT INSTRUCTIONS: {format_instructions}
    USER QUESTION: {question}
    WEBSITE CONTENT: {content}

Trois consequences, qui dictent la forme retenue ici :

  1. le cadrage « scraper + JSON » appartient a la bibliotheque. La mission ne
     redemande donc NI du JSON, NI de ne pas mettre de backticks : ce serait
     redondant, et deux consignes de format qui se repondent se contredisent
     tot ou tard ;
  2. la structure de sortie se pilote par le schema Pydantic passe en `schema=`
     au graphe, d'ou sont derivees les `format_instructions`. C'est le schema,
     pas le texte, qui garantit les champs — d'ou le soin mis aux descriptions
     de `Alternative` plus bas, qui sont lues par le modele ;
  3. le seul garde-fou anti-invention de la bibliotheque est « mets NA si tu ne
     trouves pas ». C'est tres en dessous de ce que ce domaine exige : la V4
     documente des references produit fabriquees de toutes pieces, y compris
     celle du produit d'origine. Les regles de preuve doivent donc etre portees
     par la mission, ici. C'est la raison d'etre principale de ce module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import List, Optional

from pydantic import BaseModel, Field

from candidats import CandidateLead
from compatibilite import criteres_notables
from modeles import RequirementSet


class Source(BaseModel):
    """Une preuve. Sans URL reellement visitee, ce n'est pas une source."""

    url: str = Field(
        description="URL exacte de la page consultee, copiee telle quelle. "
                    "Jamais une URL reconstituee ou supposee."
    )
    extrait: str = Field(
        description="Passage litteral de la page qui etaye l'affirmation, "
                    "copie sans reformulation."
    )
    type: str = Field(
        description="'web_officiel' si la page appartient au fabricant du "
                    "produit cite (son site, sa fiche produit, son catalogue) ; "
                    "'web_secondaire' pour distributeur, revendeur ou tiers."
    )


class Alternative(BaseModel):
    """Le resultat attendu, tel que la specification d'origine le decrit."""

    produit_demande: str = Field(
        description="Le produit d'origine identifie a partir de la fiche : "
                    "marque, reference et fonction. Si la fiche ne permet pas "
                    "de l'identifier, le dire au lieu de le deviner."
    )
    statut: str = Field(
        description="'complete' si l'alternative preserve TOUS les criteres "
                    "essentiels ; 'avec_adaptation' si elle convient mais "
                    "presente un ou plusieurs ecarts declares (accessoire a "
                    "ajouter, caracteristique differente) ; 'not_resolved' si "
                    "aucune alternative n'est etayee par une source."
    )
    pertinence: int = Field(
        default=0,
        description="Part des criteres essentiels du produit demande que "
                    "l'alternative preserve, de 0 a 100. Un ecart compensable "
                    "par un accessoire coute peu ; un ecart qui empeche le "
                    "remplacement en l'etat (nature de la tension de commande, "
                    "nombre de poles, calibre) coute beaucoup.",
    )
    ecarts: List[str] = Field(
        default_factory=list,
        description="Differences CONSTATEES dans les sources entre le produit "
                    "demande et l'alternative, chacune avec ce qu'elle impose "
                    "(accessoire a ajouter, verification a mener). A ne pas "
                    "confondre avec les limites, qui sont des points non "
                    "documentes.",
    )
    alternative: Optional[str] = Field(
        default=None,
        description="Marque et reference exacte de l'alternative retenue, "
                    "copiee litteralement depuis une source. Laisser vide si "
                    "le statut est 'not_resolved'.",
    )
    justification: str = Field(
        description="En quoi cette alternative preserve les criteres essentiels "
                    "du produit demande, critere par critere, en s'appuyant sur "
                    "les sources. Si statut 'not_resolved', expliquer ce qui "
                    "manque pour conclure."
    )
    limites: List[str] = Field(
        default_factory=list,
        description="Ecarts connus, points a verifier, adaptations necessaires. "
                    "Une liste vide signifie qu'aucun ecart n'a ete constate, "
                    "pas qu'aucun n'existe.",
    )
    sources: List[Source] = Field(
        default_factory=list,
        description="Les pages effectivement consultees qui etayent le resultat.",
    )


# Regles de preuve. Transposees de `_MISSION` (V4), reduites a ce qui tient
# dans une consigne injectee : la V4 peut se permettre une procedure longue,
# ici chaque ligne concurrence le gabarit de la bibliotheque.
_REGLES = """RÈGLES IMPÉRATIVES

- Une référence proposée doit être copiée telle quelle depuis le contenu d'une
  page réellement consultée. Ne jamais composer une référence à partir d'un
  guide de codification, ne jamais compléter une référence partielle, ne jamais
  proposer une référence « plausible ».
- Chaque source citée doit être une page effectivement présente dans le contenu
  fourni. Une URL reconstituée de mémoire, même si elle semble exister, est une
  invention.
- Si aucune alternative n'est étayée par une source, répondre statut
  'not_resolved' et laisser l'alternative vide. Un résultat vide est le résultat
  correct ; inventer une référence pour remplir le champ est la seule faute
  grave.
- La configuration (nombre de pôles, présence du neutre, tension, calibre) doit
  être IDENTIQUE à celle du produit demandé, et cette identité doit apparaître
  explicitement dans la source. Une configuration différente disqualifie le
  candidat.
- Une valeur supérieure n'est pas automatiquement équivalente.
- Ne jamais confondre le produit principal, sa gamme et ses accessoires.
- Un critère que la source ne mentionne pas explicitement n'est PAS vérifié :
  il doit apparaître dans les limites comme « à confirmer », jamais être
  présenté comme confirmé, et jamais être déduit du fait qu'il serait
  « standard » pour cette gamme.

TROIS CATÉGORIES, À NE PAS MÉLANGER

- POINT À CONFIRMER : la source ne documente pas ce critère. Il va dans les
  limites. Le statut reste 'complete'.
- ÉCART CONSTATÉ : la source documente ce critère et il DIFFÈRE du produit
  demandé. Il va dans les écarts — jamais dans les limites — avec ce qu'il
  impose concrètement. Le statut devient 'avec_adaptation' : le candidat n'est
  pas rejeté, il est proposé avec sa différence énoncée. C'est le cas d'un
  contact auxiliaire manquant qu'un bloc additionnel rétablit, ou d'une plage
  de réglage plus large.
- REJET : aucune source n'étaye de candidat, ou les écarts sont tels que le
  produit ne peut pas tenir la fonction demandée. Statut 'not_resolved'.

ÉVALUER LA PERTINENCE (0 à 100)

Partir de 100 et retrancher selon ce que l'écart coûte réellement :
- écart qu'un accessoire courant rattrape (contact auxiliaire à ajouter,
  bornier différent) : retrancher peu ;
- écart qui impose une vérification ou une adaptation de câblage : retrancher
  modérément ;
- écart qui empêche le remplacement en l'état — nature de la tension de
  commande (AC au lieu de DC), nombre de pôles, calibre, courbe — : retrancher
  massivement, et descendre sous 50.

Ne jamais gonfler la pertinence pour faire passer un candidat : elle sert
justement à ce que le lecteur décide en connaissance de cause."""


def construire_prompt_requete(description: str, marque: Optional[str] = None) -> str:
    """Rend le prompt qui fabrique la requete de recherche, et lui seul.

    Sciemment separe de `construire_mission`. `SearchGraph` fait l'inverse — il
    donne toute la mission a son generateur de requete — et le resultat est
    mauvais : a l'essai du 2026-08-14, une mission de ~1900 caracteres, faite
    surtout de regles, a produit une requete qui a ramene des outils
    electroportatifs et un distributeur d'essuie-mains partageant un numero avec
    la reference cherchee. Une requete se fabrique a partir du produit, pas a
    partir des consignes de redaction.
    """
    vise = marque.strip() if marque and marque.strip() else ""
    chez = f" chez {vise}" if vise else ""
    # Sans marque visee, le modele comble le vide avec ce qu'il connait du
    # produit d'origine : a l'essai du 2026-08-14, une fiche « Kerion,
    # disjoncteur 2P 16A » a produit la requete « ... SC60N », la gamme
    # d'origine — et la recherche a rendu le produit de depart, Wikipedia et
    # eBay. On lui retire donc explicitement le droit de nommer une marque.
    consigne_marque = (
        f"- si la gamme equivalente de {vise} est connue, la nommer : c'est ce "
        "qui mene aux fiches produit plutot qu'aux catalogues ;"
        if vise else
        "- aucune marque n'est imposee : ne nommer AUCUNE marque et AUCUNE "
        "gamme, decrire le produit par ses seules caracteristiques ;"
    )
    return f"""A partir de la fiche technique ci-dessous, ecris UNE requete de
recherche Web destinee a trouver la FICHE PRODUIT d'un equivalent{chez}.

La cible est la page d'UN produit precis, portant sa reference et ses
caracteristiques — pas une page de categorie, pas une liste de resultats
marchands.

Contraintes :
- entre 5 et 10 mots, les plus discriminants (type de produit, calibre,
  configuration, courbe) ;
- ne mentionner NI la marque, NI la gamme, NI la reference du produit
  d'origine : la requete doit ramener des concurrents, or ces mots ramenent le
  produit d'origine lui-meme ;
{consigne_marque}
- ne pas ecrire « equivalent », « alternative » ni « remplacement » : ces mots
  ne figurent pas sur les fiches produit recherchees ;
- ne pas ecrire de mots de navigation marchande (acheter, prix, pas cher,
  catalogue, gamme, tous les).

Repondre par la requete seule, sans guillemets ni phrase d'introduction.

FICHE :
{description.strip()}"""


def construire_mission(description: str, marque: Optional[str] = None) -> str:
    """Rend la mission a passer en `prompt` au graphe ScrapeGraphAI.

    `description` est le texte de la fiche technique du produit demande. Elle
    est citee telle quelle : c'est la source des criteres a preserver, et la
    reformuler reviendrait a decider a la place du modele ce qui compte.

    `marque` restreint la recherche a un fabricant quand l'appelant en vise un.
    Absente, la recherche reste libre — la generation precedente faisait de meme.
    """
    if not description or not description.strip():
        raise ValueError("La description du produit demande est vide.")

    cible = (
        f"Chercher l'alternative chez {marque}."
        if marque and marque.strip()
        else "Chercher l'alternative chez n'importe quel fabricant."
    )

    return f"""Identifier UNE seule alternative industrielle au produit décrit ci-dessous.

{cible}

PRODUIT DEMANDÉ (fiche technique) :
{description.strip()}

DÉMARCHE
1. Identifier le produit demandé à partir de la fiche : marque, référence, fonction.
2. Relever ses critères essentiels : fonction, configuration, caractéristiques
   critiques, montage, normes.
3. Retenir l'alternative la mieux étayée par les pages consultées — une seule,
   la plus pertinente, pas une liste.
4. Justifier critère par critère. Énoncer séparément les écarts constatés et
   les points restant à confirmer, puis évaluer la pertinence.

{_REGLES}"""


def construire_mission_criteres(
    description: str,
    source_hint: str | None = None,
) -> str:
    """Demande à ScrapeGraphAI le jeu de critères figé avant la recherche."""
    if not description or not description.strip():
        raise ValueError("La description du produit demandé est vide.")
    selection = ""
    if source_hint and source_hint.strip():
        selection = f"""

INDICE DE SÉLECTION : le fichier fourni s'appelle `{source_hint.strip()}`.
Utilise ce nom uniquement pour choisir un seul produit lorsque son identité
exacte est aussi écrite dans la fiche. Tu dois l'ignorer si le nom ne correspond
littéralement à aucun produit. Quand un tableau contient plusieurs colonnes ou
variantes, ne fusionne jamais leurs valeurs : extrais uniquement la colonne du
produit ainsi sélectionné."""
    return f"""Analyse la fiche technique ci-dessous et construis les critères immuables
du produit demandé. Renseigne `product` avec le type et la référence du produit.
Renseigne `origin_brand` avec sa marque et sa gamme d'origine lorsqu'elles sont
explicitement présentes ; laisse ce champ vide si elles sont absentes. Chaque
critère doit avoir un identifiant stable, un libellé,
la valeur exactement demandée et un indicateur `critical`. Un critère est
critique uniquement lorsqu'une différence empêcherait le remplacement direct ;
les performances indicatives, matériaux usuels et informations d'entretien ne
le sont pas sans exigence explicite de la fiche. Écris toujours `critical`, même
quand sa valeur est `false`. Ne complète
aucune information absente et ne cherche encore aucune alternative.{selection}

FICHE TECHNIQUE :
{description.strip()}"""


def construire_mission_complement_criteres(
    requirements: RequirementSet,
    orphan_context: str,
) -> str:
    """Demande uniquement les critères signalés par le compteur mécanique."""
    if not orphan_context.strip():
        raise ValueError("Le contexte des spécifications orphelines est vide.")
    return f"""Complète le jeu de critères ci-dessous uniquement à partir des
extraits orphelins fournis. Retourne seulement les critères réellement absents.
Pour chaque ajout, copie dans `requested_value` la valeur exacte de l'extrait et
dans `evidence_excerpt` un passage littéral qui la contient. Ne modifie, ne
supprime et ne reformule aucun critère existant. Si un extrait est illisible,
n'invente rien et ne retourne aucun ajout pour cet extrait.

CRITÈRES DÉJÀ EXTRAITS :
{requirements.model_dump_json(indent=2)}

EXTRAITS ORPHELINS :
{orphan_context.strip()}"""


def construire_mission_audit(
    requirements: RequirementSet,
    marque_cible: Optional[str],
    page_url: str,
    *,
    authorized_candidates: Sequence[CandidateLead] = (),
) -> str:
    """Demande les faits et preuves d'une page, jamais un score libre."""
    cible = marque_cible.strip() if marque_cible and marque_cible.strip() else "toute marque concurrente"
    instruction = (
        f"Cherche des candidats chez {cible}. Pour chaque candidat, copie la marque et la\n"
        "référence telles qu'elles apparaissent dans la page."
    )
    conclusion = (
        "Une liste de candidats vide est la bonne réponse si la page ne porte "
        "pas de fiche produit exploitable."
    )
    if authorized_candidates:
        identities = "\n".join(
            f"- candidate_index {index}: {lead.brand} {lead.reference}"
            for index, lead in enumerate(authorized_candidates)
        )
        instruction = f"""Les candidats suivants sont déjà déclarés par le code après confirmation
littérale de leur marque et de leur référence dans la page :

{identities}

Tu ne décides pas si ces candidats existent et tu ne recopies ni leur marque ni
leur référence. Pour chaque `candidate_index`, remplis uniquement `criteria`.
Les variantes, accessoires, gammes voisines et substitutions sont interdits."""
        conclusion = (
            "Chaque candidat déclaré doit avoir une entrée `candidate_criteria`. "
            "Un critère absent de la page reçoit `not_proven`."
        )
    # Les critères d'identité d'origine sont retirés : demander à un modèle de
    # prouver qu'un Norel est « Kerion Electric » est une consigne
    # contradictoire, et `evaluate_candidates` les classe de toute façon
    # `non_applicable`. Mesure du 2026-08-27 : les audits revenaient avec des
    # critères techniques entièrement absents de la réponse.
    notables = criteres_notables(requirements)
    return f"""Analyse uniquement le contenu de la page réellement visitée suivante :
{page_url}

{instruction} Couvre exactement tous les
critères immuables ci-dessous, sans changer `requested_value` :
{notables.model_dump_json(indent=2)}

Pour chaque critère, utilise uniquement l'un des statuts suivants :
- `proven` : la valeur demandée est explicitement confirmée par la page, ou
  comprise dans une plage publiée par la page ;
- `not_proven` : la page ne permet pas de conclure ;
- `incompatible` : la page publie une valeur ou une plage qui exclut la valeur demandée.
  Ce statut exige un `observed_value` non vide copié depuis la page.

Si la page énonce un état catégoriel différent pour la même propriété, le
statut est obligatoirement `incompatible`, jamais `not_proven`. Par exemple,
`shielded on both sides`, `open` ou `without seals` est incompatible avec une
demande `sealed` ou `with seals on both sides`; inversement, ne transforme pas
ce constat en preuve de la valeur demandée.

Compare la fonction décrite, pas le code fabricant. Deux suffixes de référence
différents ne prouvent à eux seuls ni une incompatibilité ni une équivalence.
Par exemple, `seal on both sides` et `joints en caoutchouc double face`
expriment la même étanchéité si l'extrait concerne bien le candidat.

Tout statut `proven` ou `incompatible` exige l'URL exacte ci-dessus et un
extrait littéral non vide de cette page : copie une sous-chaîne exacte, avec sa
casse, sa ponctuation et ses retours à la ligne. `observed_value` doit lui aussi
être une sous-chaîne exacte de cet extrait. Pour une valeur textuelle `proven`,
l'extrait doit énoncer explicitement la même propriété et la même valeur ;
une formulation synonyme claire est acceptable, mais ne déduis jamais un
type, une fonction ou une propriété d'un matériau voisin. Ne cite
aucune autre URL, ne reconstruis aucune référence et ne calcule aucun score.

Pour chaque preuve, vérifie dans cet ordre : le produit et variante concernés,
la propriété demandée, la propriété réellement décrite, puis la valeur observée
et son unité. Une valeur identique pour une autre propriété ne prouve rien.
Une négation ou une formulation opposée est un écart, jamais une preuve. Les
alternatives, recommandations, accessoires, produits voisins et références
croisées peuvent faire découvrir une piste mais ne prouvent aucune
caractéristique pour elle.
{conclusion}"""


def construire_mission_decouverte(
    requirements: RequirementSet,
    marque_cible: Optional[str],
    page_url: str,
) -> str:
    """Distingue les identités repérées des preuves de compatibilité."""
    cible = marque_cible.strip() if marque_cible and marque_cible.strip() else "toute marque concurrente"
    return f"""Analyse uniquement le corpus local associé à {page_url}.
Repère chez {cible} les marques et références produit écrites littéralement.
Place ces identités dans `leads`, même si les caractéristiques techniques sont
insuffisantes. Une piste sert uniquement à une recherche ultérieure et n'est
jamais une preuve. Si la page permet aussi de couvrir exactement tous les
critères ci-dessous avec des extraits littéraux, place cet audit dans `audit`;
sinon laisse `audit` vide. N'invente ni marque, ni référence, ni valeur, ni URL,
et ne calcule aucun score. Une liste `leads` vide est valide.

CRITÈRES IMMUTABLES :
{criteres_notables(requirements).model_dump_json(indent=2)}"""
