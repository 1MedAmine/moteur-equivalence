# Journal de conception — cinq générations d'un moteur d'équivalence

Trouver une référence commandable équivalente à un produit industriel demandé.
Le problème paraît simple. Il a demandé cinq architectures successives, dont
quatre ont été abandonnées. Ce document raconte ce que chacune a appris, parce
que le code publié ne garde que la dernière et qu'une architecture ne se juge
pas sans ses échecs.

Les chiffres cités sont des mesures réelles prises pendant le projet. Les
produits et les fabricants ne sont jamais nommés : ce qui compte est le mode de
panne, pas l'identité du catalogue sur lequel il s'est manifesté.

---

## Le problème, tel qu'il s'est révélé

Un client demande un produit. Le produit n'est pas au catalogue, ou plus
fabriqué, ou seulement disponible chez un concurrent. Il faut proposer autre
chose, et le proposer avec assez de certitude pour qu'un devis parte.

Trois exigences, découvertes dans cet ordre :

1. **Trouver** des candidats plausibles sur le web ou dans des catalogues.
2. **Prouver** que le candidat existe vraiment et porte bien les
   caractéristiques annoncées.
3. **Dire à quel point** c'est prouvé, plutôt que de répondre oui ou non.

La première génération a résolu la première exigence, la deuxième la seconde,
et il a fallu attendre la cinquième pour que la troisième tienne sans que les
deux autres s'effondrent.

---

## Génération 2 — classer plutôt que jeter

La première version utilisable posait déjà la question centrale : que faire
d'un candidat vraisemblable mais mal prouvé ?

Sa réponse : le montrer, avec une réserve. Un candidat sortait classé —
remplacement direct, alternative sous condition, non vérifiable — et
l'opérateur voyait la nuance. Quatre mécanismes portaient ce classement :
reconnaissance de série, complément de critères, score, amorce par marques
concurrentes.

**Ce qu'elle a appris :** un classement nuancé est utilisable, un verdict
binaire ne l'est pas. Cette leçon a été oubliée à la génération suivante, et
il a fallu la réapprendre.

## Génération 3 — la preuve stricte, et son coût

La troisième génération a remplacé le classement par une validation
déterministe : du code, pas un modèle de langage, vérifiait qu'une référence
citée apparaissait littéralement dans une page réellement téléchargée. Les
motifs de rejet étaient explicites et nombreux : référence inexacte,
incomplète, source non consultée, citation introuvable dans la page,
fabricant non prouvé, aucune caractéristique rattachée.

C'était juste, et inexploitable. La validation étant binaire, un candidat
correct mais imparfaitement cité était **jeté**, pas rétrogradé. Les
recherches rendaient zéro résultat là où la génération précédente en montrait
trois avec des réserves.

**Ce qu'elle a appris :** la rigueur de la preuve et l'utilité du résultat
sont deux axes distincts. Durcir l'un sans prévoir de rattrapage sur l'autre
produit un outil irréprochable et inutile.

## Génération 4 — l'agent, et la longue liste des pannes

La quatrième génération a branché un agent de recherche autonome devant le
validateur de la troisième, en réutilisant le classement souple de la
deuxième pour les candidats rejetés faute de preuve, jamais pour ceux rejetés
pour contradiction.

C'est la génération la plus instructive, parce que presque tout y a cassé au
moins une fois.

**Le mauvais usage d'un outil coûteux.** Le moteur de recherche profonde a
d'abord été branché comme un outil dans la boucle de l'agent : chaque appel
relançait un cycle complet. Mesure : 736 secondes pour zéro résultat. Corrigé
en le lançant **une fois en amont**, ses pages étant injectées dans la mémoire
de l'agent, qui démarre alors sur un corpus déjà téléchargé.

**L'agent ignorait ce qu'on lui donnait.** Huit pages injectées, et l'agent
repartait chercher à l'aveugle : rien dans sa mission ne lui disait qu'un
corpus l'attendait. Une phrase ajoutée en tête de mission a débloqué la
chaîne. Le correctif tient en une ligne ; le diagnostic a demandé plusieurs
runs.

**Le modèle comme variable d'architecture.** Un premier modèle ratait l'appel
d'outil en boucle, pour 956 secondes par produit. Un banc d'essai réel (45
appels concurrents) a départagé les candidats : médiane 88,7 s pour l'un,
23,1 s et 22,5 s pour deux autres. Le choix du modèle s'est fait sur mesure,
pas sur réputation — et un modèle mauvais à l'appel d'outil s'est révélé bon
en rédaction, mécanisme complètement différent.

**Les serveurs qui se contredisent.** Deux fabricants bloquaient de façon
opposée : l'un répondait aux requêtes sans en-tête et refusait celles qui en
portaient un, l'autre exactement l'inverse. Aucun jeu d'en-têtes fixe ne
satisfaisait les deux. Solution : essayer les deux, garder la première
réponse valide.

**Le modèle qui écrit ET qui juge.** Un mode de fonctionnement laissait le
modèle rédiger le rapport et attribuer lui-même les niveaux de confiance. Sur
un cas réel, les quatre candidats annoncés « équivalents directs » avec une
confiance « élevée » étaient tous faux, vérifiés un par un contre les fiches
constructeur. Deux d'entre eux portaient **le même code produit chez deux
fabricants concurrents** — signal net de fabrication pure, deux concurrents ne
partagent jamais un code.

C'est le constat le plus important de tout le projet : **une auto-évaluation
par le modèle n'est pas une preuve**, et un niveau de confiance affiché sans
vérification indépendante est pire qu'aucun niveau de confiance, parce qu'il
se lit comme une garantie.

Le correctif minimal a été de vérifier, après rédaction, que chaque référence
citée apparaît textuellement dans une source réellement téléchargée. Sa limite
a été documentée le jour même : il attrape les références inventées, jamais
une référence réelle citée pour la mauvaise gamme.

**La recherche profonde, et son prix.** Une refonte a combiné recherche
récursive multi-niveaux et catalogues locaux. Résultat mesuré : 2 820 secondes
par produit, soit 47 minutes, pour un rapport effectivement meilleur — un
candidat correctement rejeté sur un pouvoir de coupure réel inférieur à celui
demandé, les autres prudemment classés sous réserve plutôt qu'en équivalents.
Le mode rapide précédent tenait en 597 secondes avec les mêmes catalogues.
Quarante-sept minutes par produit n'est pas un outil de travail : la recherche
profonde est redevenue une option explicite, jamais le défaut.

**Le cas qui a tout résumé.** Un catalogue documentait ses références par un
guide de codification : la référence commandable ne figure nulle part telle
quelle, elle s'assemble segment par segment. Trois obstacles successifs ont dû
tomber : l'extraction PDF rendait les segments dans le désordre (corrigé en
regroupant les mots par coordonnées visuelles plutôt que par ordre de flux) ;
le vérificateur cherchait le signal de dérivation au mauvais endroit du
rapport ; et le crédit accordé à une référence dérivée dépendait d'une
formulation du modèle, non déterministe. Séquence des temps sur ce cas :
470,4 s, puis 244,9 s, puis 256,8 s, et 156,2 s au run de confirmation, une
fois la recherche web sautée quand le catalogue local suffit.

**Ce qu'elle a appris :** presque toutes les pannes venaient des jointures —
entre l'agent et son corpus, entre le rédacteur et le vérificateur, entre
l'extraction PDF et la lecture du modèle. Très peu venaient des composants
eux-mêmes.

## Génération 5 — séparer découvrir et prouver

La cinquième génération a tiré la conséquence de la quatrième : **aucun
composant de découverte ne peut prononcer un verdict**. Un adaptateur trouve
des pages et propose des candidats. La décision d'équivalence appartient à un
validateur séparé, qui extrait les preuves cellule par cellule.

Une politique de source explicite accompagnait cette séparation : une preuve
directe n'est acceptée que depuis un domaine fabricant explicitement autorisé.
Un distributeur ou une place de marché peut suggérer une référence, jamais la
prouver.

Les composants de découverte étaient interchangeables et lancés hors
processus, avec des limites réseau explicites.

**Ce qu'elle a appris :** la bonne frontière n'est pas entre les outils, elle
est entre **découvrir** et **prouver**. C'est la seule décision d'architecture
des cinq générations qui ait survécu telle quelle.

## Génération B2 — la recherche adaptative, publiée

La génération publiée dans ce dépôt garde la séparation de la cinquième et
remplace la boucle d'agent par une **recherche adaptative** : des vagues de
requêtes successives, bornées par un budget explicite, chaque vague étant
construite à partir de ce que la précédente a manqué.

Ce qui a été gardé des générations précédentes :

- **Le classement nuancé** de la deuxième : un candidat sort prouvé,
  partiellement prouvé ou incompatible, jamais accepté ou jeté.
- **La preuve littérale** de la troisième : une compatibilité est déterministe,
  calculée par du code à partir de pages réellement téléchargées.
- **La séparation découvrir/prouver** de la cinquième.
- **La leçon des jointures** de la quatrième : les budgets, les rejets et les
  états intermédiaires sont tous mesurés et rendus dans le diagnostic.

Ce qui a été abandonné : la rédaction d'un rapport par un modèle. Le moteur
publié ne demande jamais à un modèle de juger. Il lui demande de lire une page
et d'en extraire des valeurs ; la comparaison, le score et le classement sont
du code.

---

## Les invariants, après cinq essais

Cinq architectures, quatre abandons, et quatre règles qui n'ont jamais été
remises en cause depuis :

1. **Une preuve est une page qu'on a réellement téléchargée.** Pas une page
   citée, pas une page résumée : téléchargée, et dont on peut relire le texte.
2. **Celui qui cherche ne juge pas.** La découverte propose, un code
   déterministe dispose.
3. **Un résultat incertain se classe, il ne se jette pas.** L'opérateur décide
   avec la nuance sous les yeux ; l'outil ne décide pas à sa place.
4. **Aucune marque n'est connue du moteur.** La connaissance de marque vit
   dans un fichier de configuration, jamais dans le code — sinon le moteur
   devient bon sur ses cas de test et sur rien d'autre.

La quatrième règle a été vérifiée par un audit dédié, terme par terme, sur
l'ensemble du moteur de la quatrième génération : aucune ligne de code, aucun
texte envoyé au modèle ne contenait le nom d'un fabricant de test ou une
référence attendue. Les mentions trouvées dans des commentaires illustratifs
ont été rendues génériques le jour même.

---

## Ce que ce journal ne contient pas

Les cas de test réels, les noms des fabricants concernés, les références
exactes et les catalogues sur lesquels tout cela a été mesuré ne sont pas
publiés. Ils appartiennent au contexte commercial dans lequel le projet a été
construit. Les mesures de temps, les modes de panne et les décisions
d'architecture, eux, ne dépendent d'aucun de ces noms — c'est précisément ce
que l'audit d'anti-spécialisation cherchait à garantir.
