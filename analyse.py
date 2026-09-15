# -*- coding: utf-8 -*-
"""Examiner les pages une a une, puis retenir le meilleur resultat.

Une page = un appel `SmartScraperGraph`, seul graphe qui preserve le schema des
preuves. L'agregation est faite ici, en Python : elle consiste a choisir entre
des resultats deja etayes, pas a les reinterpreter — la confier au modele
rouvrirait la porte a l'invention que la mission ferme.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
from collections.abc import Mapping, Sequence
from typing import Callable, List, Literal, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from indisponibilite import AvertissementAnalyse, est_limitation_llm

from pydantic import BaseModel, ValidationError

import robustesse
from candidats import (
    CandidateLead,
    CandidateProposal,
    DiscoveryDocument,
    DiscoveryEnvelope,
    PageAuditEnvelope,
    RejectedCandidate,
    build_discovery_document,
    filter_page_audit,
    parse_candidate_proposals,
)
from mission import Alternative
from mission import construire_mission_audit, construire_mission_decouverte
from modeles import CandidateAudit, CriterionAudit, PageAudit, RequirementSet
from planification import _run_graph_with_single_json_retry
from scraping import PageContent

# Une page dont on n'a rien tire ne doit pas faire echouer la recherche : les
# resultats d'un moteur contiennent toujours des pages protegees, vides ou hors
# sujet. On note l'incident et on continue.
Avertissement = str


class PageBloquee(Exception):
    """La page a repondu, mais sans livrer son contenu (anti-robot, mur, vide)."""


class PageAuditExtractionError(ValueError):
    """Echec d'audit qui ne conserve qu'un diagnostic structurel sur."""

    def __init__(self, diagnostic: dict[str, str]) -> None:
        self.safe_diagnostics = tuple(sanitize_page_audit_diagnostics([diagnostic]))
        if not self.safe_diagnostics:
            self.safe_diagnostics = ({
                "stage": "page_audit",
                "path": "$",
                "issue": "invalid_structure",
                "action": "aborted",
            },)
        first = self.safe_diagnostics[0]
        super().__init__(
            f"Echec structurel de PageAudit au chemin {first['path']}."
        )


_PAGE_AUDIT_PATH = re.compile(
    r"(?:\$|page_url|candidates(?:\[\d+\])?"
    r"(?:\.(?:brand|reference|criteria|deviations|limitations|"
    r"requirement_id|requested_value|observed_value|status|proofs|"
    r"url|excerpt|type)(?:\[\d+\])?)*)"
)
_PAGE_AUDIT_ISSUES = frozenset({
    "invalid_item", "invalid_structure", "json_parse_error",
})
_PAGE_AUDIT_ACTIONS = frozenset({
    "aborted", "discarded", "retried", "retry_failed",
})


_PROOF_TYPES = frozenset({"web_officiel", "web_secondaire"})
_PROOF_OFFICIAL_SYNONYMS = frozenset({
    "officiel", "official", "web_official", "site_officiel",
    "fabricant", "manufacturer", "constructeur",
})


def _recuperer_preuves_mal_formees(
    payload: object,
    *,
    page_url: str,
    prefix: str,
    diagnostics: list[dict[str, str]],
) -> object:
    """Repare la forme d'une preuve sans jamais en inventer la substance.

    Deux ecarts de forme reviennent constamment, et chacun faisait perdre le
    critere entier — extrait litteral compris. Mesure du 2026-08-27 sur quatre
    runs reels : 102 des 108 preuves rejetees relevaient de l'un des deux.

    1. La preuve est rendue en chaine nue, l'extrait seul, sans objet autour.
       Elle est alors rattachee a la page en cours d'audit : cette URL n'est pas
       devinee, c'est celle que le prompt impose et la seule que le graphe ait
       lue.
    2. Le champ `type` sort du contrat (`official`, `distributor`, une variante
       francaise). Il retombe sur `web_secondaire`, jamais l'inverse.

    Rien de tout cela n'accorde de credit : `compatibilite` continue d'exiger
    que l'extrait figure litteralement dans la page recuperee, et le credit
    officiel reste accorde par le domaine du fabricant.

    """
    if not isinstance(payload, Mapping):
        return payload
    proofs = payload.get("proofs")
    if not isinstance(proofs, list):
        return payload
    normalisees: list[object] = []
    for index, proof in enumerate(proofs):
        if isinstance(proof, str):
            extrait = proof.strip()
            if not extrait or not page_url:
                normalisees.append(proof)
                continue
            diagnostics.append({
                "stage": "page_audit",
                "path": f"{prefix}.proofs[{index}]",
                "issue": "invalid_item",
                "action": "discarded",
            })
            normalisees.append({
                "url": page_url,
                "excerpt": extrait,
                "type": "web_secondaire",
            })
            continue
        if not isinstance(proof, Mapping):
            normalisees.append(proof)
            continue
        brut = proof.get("type")
        valeur = brut.strip().casefold() if isinstance(brut, str) else ""
        if valeur in _PROOF_TYPES:
            normalisees.append(proof)
            continue
        retenue = (
            "web_officiel" if valeur in _PROOF_OFFICIAL_SYNONYMS
            else "web_secondaire"
        )
        diagnostics.append({
            "stage": "page_audit",
            "path": f"{prefix}.proofs[{index}].type",
            "issue": "invalid_item",
            "action": "discarded",
        })
        normalisees.append({**dict(proof), "type": retenue})
    return {**dict(payload), "proofs": normalisees}


def _safe_page_audit_path(value: object) -> str:
    path = value if isinstance(value, str) else ""
    return path if _PAGE_AUDIT_PATH.fullmatch(path) else "$"


def sanitize_page_audit_diagnostics(value: object) -> list[dict[str, str]]:
    """Allowlist de frontiere : jamais de contenu ni de reponse brute."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    sanitized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        issue = item.get("issue")
        action = item.get("action")
        if (
            item.get("stage") == "page_audit"
            and isinstance(path, str)
            and _safe_page_audit_path(path) == path
            and issue in _PAGE_AUDIT_ISSUES
            and action in _PAGE_AUDIT_ACTIONS
        ):
            sanitized.append({
                "stage": "page_audit",
                "path": path,
                "issue": str(issue),
                "action": str(action),
            })
    return sanitized


def _contenu_recupere(graphe) -> str:
    """Rend le texte que le graphe a effectivement charge.

    Lu dans l'etat du graphe (`doc`, sortie de FetchNode) plutot que rechargé :
    aucun appel reseau supplementaire.
    """
    try:
        docs = graphe.get_state("doc")
    except Exception:
        return ""
    if not docs:
        return ""
    return " ".join(getattr(d, "page_content", "") or "" for d in docs)


def _analyser_page(url: str, prompt: str, config: dict) -> Alternative:
    from scrapegraphai.graphs import SmartScraperGraph

    graphe = SmartScraperGraph(prompt=prompt, source=url, config=config,
                               schema=Alternative)
    brut = graphe.run()

    # Un resultat vide sur une page bloquee n'est pas une absence d'equivalent :
    # c'est une absence de donnees. Les deux doivent se lire differemment dans
    # le rapport, sans quoi « rien trouve » devient impossible a interpreter.
    resultat = Alternative.model_validate(brut) if isinstance(brut, dict) else None
    if resultat is None or not resultat.sources:
        motif = robustesse.diagnostic_page(_contenu_recupere(graphe))
        if motif:
            raise PageBloquee(motif)
    if resultat is None:
        raise ValueError(f"reponse inattendue ({type(brut).__name__})")
    return resultat


def examiner(
    urls: List[str],
    prompt: str,
    config: dict,
    *,
    analyser: Callable[[str, str, dict], Alternative] = _analyser_page,
) -> Tuple[List[Alternative], List[Avertissement]]:
    """Analyse chaque page et rend les resultats exploitables et les incidents.

    `analyser` est injecte pour que les tests se passent de reseau.
    """
    resultats: List[Alternative] = []
    avertissements: List[Avertissement] = []

    for url in urls:
        try:
            resultats.append(analyser(url, prompt, config))
        except PageBloquee as exc:
            avertissements.append(f"Page inaccessible ({url}) : {exc}")
        except ValidationError as exc:
            avertissements.append(
                f"Reponse non conforme au schema pour {url} : "
                f"{exc.error_count()} champ(s) en cause."
            )
        except Exception as exc:
            avertissements.append(f"Page non exploitee ({url}) : {exc}")

    return resultats, avertissements


def _sources_officielles(resultat: Alternative) -> int:
    return sum(1 for s in resultat.sources if s.type == "web_officiel")


# Seuil de pertinence en dessous duquel un candidat n'est plus propose.
#
# 75 par defaut : un ecart qu'un accessoire rattrape — un contact auxiliaire a
# ajouter, par exemple — laisse un candidat largement au-dessus, et il doit
# etre propose avec sa difference plutot que tu. Un ecart bloquant (bobine AC
# pour un besoin DC) descend sous 50 et reste ecarte.
PERTINENCE_MINIMALE = 75

STATUTS_PROPOSES = ("complete", "avec_adaptation")


def est_exploitable(resultat: Alternative,
                    pertinence_minimale: int = PERTINENCE_MINIMALE) -> bool:
    """Un resultat ne compte que s'il propose, prouve, et tient le seuil.

    Le statut seul ne suffit pas : un `complete` sans reference ou sans source
    est une contradiction interne, qu'on traite comme un non-resultat plutot
    que de la propager.

    `avec_adaptation` est accepte au meme titre que `complete` : un candidat
    qui convient moyennant un accessoire est une reponse utile, a condition que
    l'ecart soit enonce. Le taire serait plus trompeur que le proposer.
    """
    if resultat.statut not in STATUTS_PROPOSES:
        return False
    if not (resultat.alternative and resultat.alternative.strip()):
        return False
    if not resultat.sources:
        return False
    # Un `complete` sans ecart n'a pas besoin d'annoncer sa pertinence pour
    # etre retenu ; c'est l'ecart qui appelle une mesure.
    if resultat.statut == "complete" and not resultat.ecarts:
        return True
    return resultat.pertinence >= pertinence_minimale


def _rang(resultat: Alternative) -> tuple:
    """Cle de tri, du plus convaincant au moins convaincant.

    Un equivalent sans ecart passe avant un equivalent qui en a un, quelle que
    soit sa pertinence annoncee : la difference constatee est un fait, la
    pertinence une appreciation. Ensuite vient la preuve constructeur — une
    fiche du fabricant vaut mieux qu'un revendeur pour affirmer une reference.
    """
    return (
        len(resultat.ecarts),
        -resultat.pertinence,
        -_sources_officielles(resultat),
        len(resultat.limites),
        -len(resultat.sources),
    )


def meilleur(resultats: List[Alternative],
             pertinence_minimale: int = PERTINENCE_MINIMALE) -> Optional[Alternative]:
    """Rend le resultat le mieux etaye, ou None si aucun ne l'est.

    Aucun repli : si rien n'est exploitable, B2 ne propose rien. Un resultat
    vide reste un resultat correct — mais depuis l'ajout de `avec_adaptation`,
    il est reserve aux cas ou il n'y a vraiment rien, et non plus aux cas ou le
    candidat demandait un accessoire.
    """
    exploitables = [r for r in resultats
                    if est_exploitable(r, pertinence_minimale)]
    if not exploitables:
        return None
    return sorted(exploitables, key=_rang)[0]


@dataclass(frozen=True)
class PageAnalysis:
    """Audit ScrapeGraphAI et contenu exact qui permet de vérifier ses preuves."""

    page_url: str
    content: str
    audit: PageAudit
    proposals: tuple[CandidateProposal, ...] = ()
    proposal_rejections: tuple[RejectedCandidate, ...] = ()
    mode: Literal["audit", "discovery", "targeted"] = "audit"
    validation_diagnostics: tuple[dict[str, str], ...] = ()


def _validation_path(prefix: str, location: Sequence[object]) -> str:
    path = prefix
    for part in location:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _append_item_diagnostics(
    error: ValidationError,
    *,
    prefix: str,
    diagnostics: list[dict[str, str]],
) -> None:
    for detail in error.errors(include_url=False, include_context=False):
        diagnostics.append({
            "stage": "page_audit",
            "path": _validation_path(prefix, detail["loc"]),
            "issue": "invalid_item",
            "action": "discarded",
        })


def _validate_page_audit_items(
    payload: object,
    *,
    page_url: str,
    diagnostics: list[dict[str, str]],
) -> PageAudit:
    """Valide chaque candidat et critere sans sacrifier leurs voisins valides."""
    if isinstance(payload, PageAudit):
        return payload.model_copy(update={"page_url": page_url})
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    if not isinstance(payload, Mapping):
        raise PageAuditExtractionError({
            "stage": "page_audit",
            "path": "$",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None
    if not isinstance(payload.get("candidates"), list):
        raise PageAuditExtractionError({
            "stage": "page_audit",
            "path": "candidates",
            "issue": "invalid_structure",
            "action": "aborted",
        }) from None

    valid_candidates: list[CandidateAudit] = []
    for candidate_index, candidate_payload in enumerate(payload["candidates"]):
        candidate_prefix = f"candidates[{candidate_index}]"
        if isinstance(candidate_payload, CandidateAudit):
            valid_candidates.append(candidate_payload)
            continue
        if not isinstance(candidate_payload, Mapping):
            diagnostics.append({
                "stage": "page_audit",
                "path": candidate_prefix,
                "issue": "invalid_item",
                "action": "discarded",
            })
            continue

        raw_criteria = candidate_payload.get("criteria")
        if not isinstance(raw_criteria, list):
            diagnostics.append({
                "stage": "page_audit",
                "path": f"{candidate_prefix}.criteria",
                "issue": "invalid_item",
                "action": "discarded",
            })
            continue

        valid_criteria: list[CriterionAudit] = []
        for criterion_index, criterion_payload in enumerate(raw_criteria):
            criterion_prefix = f"{candidate_prefix}.criteria[{criterion_index}]"
            try:
                criterion = (
                    criterion_payload
                    if isinstance(criterion_payload, CriterionAudit)
                    else CriterionAudit.model_validate(
                        _recuperer_preuves_mal_formees(
                            criterion_payload,
                            page_url=page_url,
                            prefix=criterion_prefix,
                            diagnostics=diagnostics,
                        )
                    )
                )
            except ValidationError as error:
                _append_item_diagnostics(
                    error,
                    prefix=criterion_prefix,
                    diagnostics=diagnostics,
                )
                continue
            valid_criteria.append(criterion)

        if not valid_criteria:
            if not raw_criteria:
                diagnostics.append({
                    "stage": "page_audit",
                    "path": f"{candidate_prefix}.criteria",
                    "issue": "invalid_item",
                    "action": "discarded",
                })
            continue

        try:
            candidate = CandidateAudit.model_validate({
                **dict(candidate_payload),
                "criteria": valid_criteria,
            })
        except ValidationError as error:
            _append_item_diagnostics(
                error,
                prefix=candidate_prefix,
                diagnostics=diagnostics,
            )
            continue
        valid_candidates.append(candidate)

    return PageAudit(page_url=page_url, candidates=valid_candidates)


def _page_audit_structure_diagnostic(error: ValidationError) -> dict[str, str]:
    """Réduit une erreur Pydantic à son emplacement, jamais à sa valeur."""
    details = error.errors(include_url=False, include_context=False)
    location = details[0].get("loc", ()) if details else ()
    path = _validation_path("", location).lstrip(".") or "$"
    return {
        "stage": "page_audit",
        "path": _safe_page_audit_path(path),
        "issue": "invalid_structure",
        "action": "aborted",
    }


def _default_page_graph(**kwargs):
    from scrapegraphai.graphs import SmartScraperGraph

    return SmartScraperGraph(**kwargs)


def _run_page_graph_with_retry(
    *,
    prompt: str,
    source: str,
    graph_config: dict,
    schema: type,
    graph_factory: Callable[..., object],
    diagnostics: list[dict[str, str]],
) -> tuple[object, object]:
    graphs: list[object] = []

    def capture_graph(**kwargs):
        graph = graph_factory(**kwargs)
        graphs.append(graph)
        return graph

    try:
        raw = _run_graph_with_single_json_retry(
            prompt=prompt,
            source=source,
            graph_config=graph_config,
            schema=schema,
            graph_factory=capture_graph,
            stage="page_audit",
            diagnostics=diagnostics,
            path_sanitizer=_safe_page_audit_path,
            error_factory=PageAuditExtractionError,
        )
    except ValidationError as error:
        raise PageAuditExtractionError(
            _page_audit_structure_diagnostic(error)
        ) from None
    return raw, graphs[-1]


def analyser_page(
    page: PageContent,
    requirements: RequirementSet,
    target_brand: str | None,
    graph_config: dict,
    *,
    mode: Literal["audit", "discovery", "targeted"] = "audit",
    document: DiscoveryDocument | None = None,
    authorized_candidates: tuple[CandidateLead, ...] = (),
    graph_factory: Callable[..., object] = _default_page_graph,
) -> PageAnalysis:
    """Fait analyser une page par ScrapeGraphAI, sans lui déléguer la preuve."""
    validation_diagnostics: list[dict[str, str]] = []
    if mode == "discovery":
        discovery_document = document or build_discovery_document(
            url=page.url,
            title=page.title,
            snippets=(),
            content=page.content,
            rank=1,
        )
        raw, graph = _run_page_graph_with_retry(
            prompt=construire_mission_decouverte(requirements, target_brand, page.url),
            source=discovery_document.prompt_source,
            graph_config=graph_config,
            schema=DiscoveryEnvelope,
            graph_factory=graph_factory,
            diagnostics=validation_diagnostics,
        )
        if isinstance(raw, DiscoveryEnvelope):
            envelope = raw
            audit = (
                _validate_page_audit_items(
                    envelope.audit,
                    page_url=page.url,
                    diagnostics=validation_diagnostics,
                )
                if envelope.audit is not None
                else PageAudit(page_url=page.url)
            )
        elif isinstance(raw, Mapping):
            envelope = DiscoveryEnvelope.model_validate({
                "leads": raw.get("leads", []),
                "audit": None,
            })
            audit = (
                _validate_page_audit_items(
                    raw["audit"],
                    page_url=page.url,
                    diagnostics=validation_diagnostics,
                )
                if raw.get("audit") is not None
                else PageAudit(page_url=page.url)
            )
        else:
            envelope = DiscoveryEnvelope.model_validate(raw)
            audit = (
                _validate_page_audit_items(
                    envelope.audit,
                    page_url=page.url,
                    diagnostics=validation_diagnostics,
                )
                if envelope.audit is not None
                else PageAudit(page_url=page.url)
            )
        audit = audit.model_copy(update={"page_url": page.url})
        # Le graphe analyse ici un corpus composite (URL, titre, extraits et
        # contenu borne). Son etat interne n'est donc jamais le contenu brut
        # d'une page visitee et ne peut pas servir a verifier une preuve.
        content = page.content
        audit = (
            filter_page_audit(
                audit,
                page_url=page.url,
                title=page.title,
                content=content,
                document=discovery_document,
            )
            if content.strip()
            else PageAudit(page_url=page.url)
        )
        parsed = parse_candidate_proposals(envelope.leads)
        return PageAnalysis(
            page_url=page.url,
            content=content,
            audit=audit,
            proposals=parsed.proposals,
            proposal_rejections=parsed.rejected,
            mode=mode,
            validation_diagnostics=tuple(validation_diagnostics),
        )

    source = page.content if page.content else page.url
    prompt = (
        construire_mission_audit(
            requirements,
            target_brand,
            page.url,
            authorized_candidates=authorized_candidates,
        )
        if mode == "targeted"
        else construire_mission_audit(requirements, target_brand, page.url)
    )
    try:
        raw, graph = _run_page_graph_with_retry(
            prompt=prompt,
            source=source,
            graph_config=graph_config,
            schema=PageAuditEnvelope,
            graph_factory=graph_factory,
            diagnostics=validation_diagnostics,
        )
        audit = _validate_page_audit_items(
            raw,
            page_url=page.url,
            diagnostics=validation_diagnostics,
        )
    except PageAuditExtractionError as error:
        # Un JSON totalement cassé garde le chemin historique : l'appelant
        # isole la page et expose le diagnostic de reprise. Une structure JSON
        # valide mais globalement invalide reste, elle, représentable par un
        # audit vide et son chemin sûr dans le rapport.
        if (
            not page.content.strip()
            or error.safe_diagnostics[0]["issue"] == "json_parse_error"
        ):
            raise
        return PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url),
            mode=mode,
            validation_diagnostics=error.safe_diagnostics,
        )
    content = page.content or _contenu_recupere(graph)
    if not content.strip():
        raise PageBloquee("page vide après les récupérations Scrapling et ScrapeGraphAI")
    if mode == "targeted":
        audit = filter_page_audit(
            audit,
            page_url=page.url,
            title=page.title,
            content=content,
            document=document,
            authorized_candidates=authorized_candidates,
            targeted=True,
        )
    return PageAnalysis(
        page_url=page.url,
        content=content,
        audit=audit,
        mode=mode,
        validation_diagnostics=tuple(validation_diagnostics),
    )


def _safe_page_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _contexte_echec(page: PageContent, mode: str) -> str:
    """Contexte sûr par construction : rien n'y vient du message d'exception.

    Le nom de la classe seul ne distingue pas les deux échecs qui se ressemblent
    dans le rapport. Une page sans contenu part vers ScrapeGraphAI sous forme
    d'URL : c'est Chromium qui navigue, et son échec remonte en `RuntimeError`
    nu. Une page avec contenu n'ouvre aucune connexion : le même `RuntimeError`
    y désigne alors le modèle ou le schéma. Savoir laquelle des deux on lit
    évite de chercher la panne du mauvais côté.
    """
    if mode == "discovery":
        source = "document"
    else:
        source = "contenu" if page.content else "url"
    return f"mode={mode}, source={source}, {len(page.content)} caractères récupérés"


def examiner_pages(
    pages: list[PageContent],
    requirements: RequirementSet,
    target_brand: str | None,
    graph_config: dict,
    *,
    workers: int = 2,
    mode: Literal["audit", "discovery", "targeted"] = "audit",
    documents_by_url: dict[str, DiscoveryDocument] | None = None,
    authorized_by_url: dict[str, tuple[CandidateLead, ...]] | None = None,
    graph_factory: Callable[..., object] = _default_page_graph,
) -> tuple[list[PageAnalysis], list[str]]:
    """Analyse au plus deux pages en parallèle et isole chaque échec."""
    indexed: list[tuple[int, PageAnalysis]] = []
    warnings: list[str] = []
    maximum = min(max(workers, 1), 2)
    with ThreadPoolExecutor(max_workers=maximum) as executor:
        futures = {
            executor.submit(
                analyser_page,
                page,
                requirements,
                target_brand,
                graph_config,
                mode=mode,
                document=(documents_by_url or {}).get(page.url),
                authorized_candidates=(authorized_by_url or {}).get(page.url, ()),
                graph_factory=graph_factory,
            ): (index, page)
            for index, page in enumerate(pages)
        }
        for future in as_completed(futures):
            index, page = futures[future]
            try:
                indexed.append((index, future.result()))
            except Exception as error:
                structural_context = ""
                if isinstance(error, PageAuditExtractionError):
                    diagnostic = error.safe_diagnostics[0]
                    structural_context = (
                        f", stage={diagnostic['stage']}, path={diagnostic['path']}"
                    )
                warnings.append(AvertissementAnalyse(
                    f"Page non exploitée ({_safe_page_url(page.url)}) : "
                    f"{type(error).__name__} "
                    f"[{_contexte_echec(page, mode)}{structural_context}].",
                    rate_limited=est_limitation_llm(error),
                ))
    indexed.sort(key=lambda item: item[0])
    return [item for _, item in indexed], warnings
