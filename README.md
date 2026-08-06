# Stationnement automatique

Un ticket **Handi toujours en cours** dans deux secteurs de Paris : celui du
**75008** et celui du **75016**. Relais **à 20h05 pile** chaque soir, rien
avant 20h00, rattrapage pendant la nuit si un trou apparaît, et **repli sur la
zone voisine** si la zone voulue refuse.

Construit sur le modèle d'[AlloValet](https://allovalet.com/), pour un usage
strictement personnel.

**Pour l'installer avec une autre voiture et un autre compte PayByPhone :
[INSTALLATION.md](INSTALLATION.md)** — dix minutes, dans le navigateur, sans
rien installer.

---

## Les deux secteurs, et leurs replis

Un ticket pris n'importe où dans un secteur couvre tout le secteur. La
configuration liste donc les zones **dans l'ordre où on les veut** : la
première est la zone visée, les suivantes servent quand elle refuse.

| Secteur | On essaie, dans cet ordre |
|---|---|
| 75001–75011 | **75008** → 75007 → 75006 → … → 75001 → 75009 → 75010 → 75011 |
| 75012–75020 | **75016** → 75017 → 75018 → 75019 → 75020 → 75015 → … → 75012 |

Concrètement :

- un ticket sur le 75007 **compte** comme couverture du secteur du 8e — la
  règle est satisfaite, aucun second ticket n'est pris ;
- si le 75008 refuse — tarif absent, devis rejeté, véhicule déjà stationné,
  tarif devenu payant — on descend la liste jusqu'à ce qu'une zone accepte ;
- on s'arrête à la **première** qui donne un ticket : jamais deux pour une même
  règle ;
- après un refus, le compte est **relu** avant d'essayer ailleurs : un
  « véhicule déjà stationné » veut dire *couvert*, pas *essaie à côté*.

Pour qu'il n'y ait aucun ticket, il faudrait donc que les onze zones d'un
secteur refusent le même jour.

---

## Ce que fait AlloValet, et ce qui est repris ici

Leur application (`app.allovalet.com`) est un front Next.js devant un backend
Python. Deux variables d'environnement de leur code disent l'essentiel :
`PBP_ENCRYPTION_KEY` et `FASTAPI_INTERNAL_URL`. Autrement dit : **ils stockent
les identifiants PayByPhone de l'utilisateur, chiffrés, et un service se
connecte à son compte pour piloter l'API.** Pas d'OAuth, pas d'API partenaire.

Leur promesse, mot pour mot : « vous définissez vos règles une fois — jours,
horaires, véhicules — et AlloValet renouvelle vos tickets récurrents **avant
qu'ils n'expirent** », pour les tarifs « Résident, Visiteur, **CMI**, 2RM,
Pro ».

| AlloValet | Ici |
|---|---|
| Identifiants PayByPhone stockés chiffrés | secrets GitHub Actions |
| Backend qui tourne en continu | GitHub Actions, dense autour de 20h |
| Règles véhicule / zone / type de ticket | `config.yml` |
| Renouvellement avant expiration | idem, avec rattrapage si trou |
| SmartPark, tableau de bord, multi-véhicules | non repris — inutile ici |
| 6,35 €/véhicule/mois | 0 € |

---

## Pourquoi les versions précédentes ne marchaient pas

Ce n'était pas un problème de réglage. En sondant les endpoints :

```
GET  consumer.paybyphoneapis.com/parking/accounts   →  404 page not found
POST consumer.paybyphoneapis.com/uapi/graphql       →  401  (vivante)
POST auth.paybyphoneapis.com/token                  →  400 invalid_grant (vivante)
```

**L'API REST utilisée jusque-là n'existe plus.** Elle venait d'une
documentation reverse-engineerée de 2015. Aucune version bâtie dessus ne
pouvait aboutir, quels que soient les identifiants.

Et il manquait la mutation d'achat : l'ancien script s'arrêtait à
`createQuotesV1` en affichant `✅ Ticket OK`, alors qu'un devis n'achète rien.

Le vrai enchaînement, relevé dans le bundle Flutter de l'application
PayByPhone — méthode et détails dans [docs/api-paybyphone.md](docs/api-paybyphone.md) :

```
createQuotesV1         →  un devis, et surtout un quoteId
startParkingSessionV1  →  l'achat, à partir de ce quoteId
getParkingSessionsV1   →  vérification que le ticket existe vraiment
```

Un ticket n'est déclaré pris que lorsqu'il a été **relu depuis le serveur**.

---

## Comment la couverture est garantie

À chaque passage, une seule question : **faut-il un ticket ?**

Rien ne se prend **avant 20h00** : c'est la seule heure où un ticket finit
vraiment, et un ticket pris dans la journée s'arrêterait au même 20h00.

| Situation (entre 20h00 et 09h00) | Décision |
|---|---|
| Aucun ticket dans le secteur | on en prend un **immédiatement** |
| Le ticket expire dans moins de 25 min | on le reprend **avant** le trou |
| Il est 20h01 passé et le ticket ne tient pas jusqu'à demain 20h01 | rendez-vous quotidien |
| Entre 09h00 et 20h00 | rien |

« Un ticket » veut dire : sur **n'importe quelle zone du secteur**, pas
seulement la zone préférée.

Le rendez-vous n'a lieu qu'une fois par soir. Quand une session en cours est
renouvelable — l'API le dit elle-même avec `isRenewable` — on la renouvelle au
lieu d'en empiler une seconde.

### L'heure est tenue par le programme, pas par GitHub

Relevé sur quatre jours : **GitHub n'a honoré que 16 des 56 passages
programmés** chaque jour, avec des trous de trois heures. En demander plus n'y
change rien. Ce qui marche, c'est qu'un passage **attende** :

- neuf créneaux entre 19h30 et 20h05 ; le premier honoré **dort jusqu'à 20h05**
  puis agit — il suffit donc d'un seul ;
- passé 20h05, plus aucune attente : le ticket a expiré, chaque minute compte.
  Douze créneaux entre 20h et 21h servent alors de filet ;
- une veille toutes les deux heures le reste du temps, pour le tableau de bord
  et pour rattraper un trou nocturne.

Été comme hiver : les créneaux couvrent 17h-20h UTC, ce qui encadre 20h00 de
Paris dans les deux décalages. Environ 1 000 minutes d'Actions par mois, sur
les 2 000 gratuites d'un dépôt privé.

---

## Mise en route

Faite. Le code est sur `main`, les secrets `PBP_USERNAME` et `PBP_PASSWORD`
sont en place, et le workflow tourne.

Le workflow se déclenche aussi à chaque modification poussée sur `main`, ce qui
permet de vérifier un changement sans attendre le prochain créneau.

### Comment on est prévenu d'un échec

**Une issue GitHub s'ouvre toute seule**, et se referme au premier passage
réussi. Choisi plutôt qu'une notification pour trois raisons :

- elle **persiste** tant que le problème dure — impossible de la rater, alors
  qu'une notification passée est perdue ;
- elle **se referme seule**, donc sa seule présence signifie « en panne
  maintenant », sans avoir à interpréter un historique ;
- **rien à installer** : GitHub relaie déjà les issues par mail au
  propriétaire du dépôt.

Elle contient le lien vers le log du passage, où figure le diagnostic complet.
Une seule issue reste ouverte à la fois : les passages suivants ne la
dupliquent pas.

**Éprouvée pour de vrai.** Une alarme jamais déclenchée ne prouve rien : un
échec a donc été provoqué volontairement (passage #120, 31/07/2026), après la
prise de tickets pour ne pas toucher à la couverture.

| Étape du passage #120 | Résultat |
|---|---|
| Vérifier la couverture | ✅ les tickets ont été pris |
| Épreuve de l'alarme | ❌ échec volontaire |
| Ouvrir l'alerte | ✅ **issue #1 créée** |
| Passage suivant → Refermer l'alerte | ✅ commentée et refermée |

Le déclencheur reste en place mais dormant : il n'agit que si le message de
commit contient `[test-alerte]`. Un test verrouille cette condition, pour
qu'un échec volontaire ne puisse jamais survenir sur un passage ordinaire.

---

## L'interface

Deux façons de regarder, selon qu'on a la machine sous la main ou juste un
téléphone.

### Sur le téléphone : rien à installer

Chaque passage publie **le résumé de l'état en tête de son log** : quelle zone
couvre quelle règle, jusqu'à quand, et ce que fera le prochain passage. Visible
depuis l'appli GitHub, y compris quand le passage a échoué.

```
| Règle              | Couvert par | Expire      | Reste  | Prochaine action |
| Secteur 8e — Handi | ↪️ 75007    | 31/07 11:13 | 1 h 29 | rien à faire     |
| Secteur 16e — Handi| ✅ 75016    | 31/07 16:53 | 7 h 09 | rien à faire     |
```

`✅` = la zone voulue · `↪️` = un repli · `❌` = découvert.

### Sur la machine : voir **et** modifier

```bash
python -m allovalet ui        # →  http://127.0.0.1:8787
```

Une page unique, sans dépendance ni service à héberger :

- l'état de chaque secteur — temps restant, zone qui couvre, chaîne des replis
  avec la zone active en vert ;
- les tickets en cours et les derniers passages ;
- l'éditeur des automatisations : zones, ordre des replis, heure du
  rendez-vous, durée, activation. **Une config invalide n'est jamais
  enregistrée** — elle est relue avant écriture ;
- deux boutons : *Simuler* (n'achète rien) et *Prendre les tickets maintenant*.

Elle modifie `config.yml`, le fichier que lit GitHub Actions : il faut le
**pousser** pour que l'automatisation en tienne compte.

---

## Les commandes

```bash
python -m allovalet ui                     # l'interface
python -m allovalet doctor                 # diagnostic complet, rien acheté
python -m allovalet run [--dry-run]        # un passage
python -m allovalet status                 # tickets en cours et état des règles
python -m allovalet summary                # l'état en Markdown
python -m allovalet rates --zone 75016     # libellés de tarifs de la zone
python -m allovalet park --zone 75016 --duration 24h   # ticket manuel
python -m allovalet schema                 # forme exacte attendue par l'API
```

`doctor` est le point d'entrée : il contrôle la config, la connexion, les
véhicules du compte, les tickets en cours, puis descend la liste des zones de
chaque règle jusqu'à en trouver une qui donne un devis gratuit avec `quoteId`.
Il n'achète rien.

---

## La configuration

```yaml
rules:
  - name: Secteur 8e — Handi
    plate: ${PBP_PLATE}     # secret GitHub, pas dans le dépôt
    zones:                  # liste ORDONNÉE : la 1re est la zone voulue,
      - "75008"             # les suivantes sont les replis du même secteur
      - "75007"
      - "75006"
    rate: "1321271030"      # « Handi - toutes zones » — `allovalet rates` le donne
    duration: 24h
    renew_at: "20:01"       # rendez-vous quotidien
    max_cost_per_ticket: 0  # n'achète que si c'est gratuit
```

Une seule zone ? `location: "75016"` suffit, comme avant.

Options globales : `timezone`, `country`, `renew_margin_minutes`, `notify`.
Une règle peut aussi porter `window` (jours et heures d'activité) et `stall`.

---

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest tests -q
```

113 tests, sans réseau. Un faux serveur GraphQL rejoue le moteur réel :
connexion, jeton périmé, tarifs, devis, achat via `quoteId`, renouvellement,
vérification, achat fantôme, introspection et élagage des champs inconnus —
le faux serveur rejette tout champ hors schéma, comme le vrai.

Sont verrouillés en plus :

- **les replis** — repli effectif quand la zone voulue refuse, un ticket de
  repli qui compte comme couverture, un seul ticket par règle, et le refus
  « déjà stationné » qui ne fait pas acheter à côté ;
- **la garantie de couverture** — nuit, trou, rendez-vous non rejoué ;
- **l'interface** — elle est interrogée par HTTP comme le ferait un
  navigateur : une config cassée n'est jamais écrite, et son verdict est
  exactement celui du moteur ;
- **les fichiers livrés** — zones et ordre des replis, horaire, cron, alerte :
  testés sur `config.yml` et le workflow, pas sur des exemples.

---

## État réel, vérifié contre le compte

**Chaîne complète prouvée le 31/07/2026.** Des tickets ont été réellement
créés, vérifiés en les relisant depuis le compte :

```
=== 17 ticket(s) créé(s) ===
  • zone 75001 … 75020  <plaque>  CUSTOM  jusqu'à 31/07 20:00 ← NOUVEAU
```

| Étape | État |
|---|---|
| Connexion | ✅ |
| Lecture des tickets | ✅ |
| Tarifs de la zone | ✅ |
| Devis + `quoteId` | ✅ `1 Days → 0,00 €` |
| Achat | ✅ |
| **Capture (`createJobV1`)** | ✅ — sans elle, rien n'existe |
| Vérification | ✅ |

### Les quatre découvertes qui ont débloqué

Aucune n'était devinable depuis le code seul :

1. `getOpenSessionsV1` renvoie de l'**autopay**, pas la voirie. Les tickets
   sont dans `getParkingSessionsV1`.
2. Le tarif ne s'appelle pas « CMI » mais **« Handi - toutes zones »**,
   `ratePolicyId 1321271030`.
3. **`startParkingSessionV1` ne finalise rien.** Il crée une session en
   attente ; c'est `createJobV1` qui la rend réelle. Dix-neuf « achats
   acceptés » avaient produit zéro ticket.
4. `metadata` est déclaré `String` : l'objet renvoyé par l'achat doit être
   retransmis **sérialisé**.

Malgré son nom, le tarif exige **un ticket par arrondissement** : l'API répond
`409 VehicleAlreadyParked` sur une zone déjà couverte.

### La méthode

Lire les requêtes réelles dans l'onglet réseau de `m.paybyphone.com`, et faire
tourner le workflow à chaque correction poussée sur `main` — avec un
diagnostic automatique en cas d'échec. Chaque passage rapportait précisément
quoi corriger.

