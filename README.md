# Stationnement automatique

Un ticket **CMI toujours en cours** dans le **75016** et le **75008**, pour la
plaque AB123CD. Renouvellement au rendez-vous de **20h01**, et rattrapage
automatique si un trou apparaît.

Construit sur le modèle d'[AlloValet](https://allovalet.com/), pour un usage
strictement personnel.

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
| Backend qui tourne en continu | GitHub Actions, toutes les 30 min |
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

À chaque passage, une seule question par zone : **faut-il un ticket ?**

| Situation | Décision |
|---|---|
| Aucun ticket en cours | on en prend un **immédiatement**, quelle que soit l'heure |
| Le ticket expire dans moins de 25 min | on le reprend **avant** le trou |
| Il est 20h01 passé et le ticket ne tient pas jusqu'à demain 20h01 | rendez-vous quotidien |
| Sinon | rien |

Le rendez-vous n'a lieu qu'une fois par soir. Quand une session en cours est
renouvelable — l'API le dit elle-même avec `isRenewable` — on la renouvelle au
lieu d'en empiler une seconde.

Le workflow tourne toutes les 30 min, 24 h/24, avec un passage toutes les
10 min autour de 20h01. Comme il se réveille à chaque `HH:01`, il tombe sur
20h01 heure de Paris été comme hiver, sans logique de changement d'heure. Un
trou dure donc au pire une trentaine de minutes. Environ 1 000 minutes
d'Actions par mois, sur les 2 000 gratuites d'un dépôt privé.

---

## Mise en route

**1. Fusionner cette branche dans `main`.** GitHub ne déclenche les tâches
planifiées que depuis la branche par défaut.

**2. Deux secrets** — *Settings → Secrets and variables → Actions* :

| Nom | Valeur |
|---|---|
| `PBP_USERNAME` | numéro PayByPhone avec indicatif (`+336…`) ou email |
| `PBP_PASSWORD` | mot de passe PayByPhone |

**3. Un essai à blanc** — *Actions → Stationnement → Run workflow*, case
**Simuler**. Le log dit ce qu'il ferait sans rien acheter.

---

## Les commandes

```bash
python -m allovalet doctor                 # diagnostic complet, rien acheté
python -m allovalet run [--dry-run]        # un passage
python -m allovalet status                 # tickets en cours et état des règles
python -m allovalet rates --zone 75016     # libellés de tarifs de la zone
python -m allovalet park --zone 75016 --duration 24h   # ticket manuel
python -m allovalet schema                 # forme exacte attendue par l'API
```

`doctor` est le point d'entrée : il contrôle la config, la connexion, les
véhicules du compte, les tickets en cours, puis pour chaque règle le tarif, le
devis et la présence d'un `quoteId`. Il n'achète rien.

---

## La configuration

```yaml
rules:
  - name: 16e — CMI
    plate: AB123CD
    location: "75016"       # numéro affiché sur l'horodateur
    rate: CMI               # type ou nom du tarif — `allovalet rates` le donne
    duration: 24h
    renew_at: "20:01"       # rendez-vous quotidien
    max_cost_per_ticket: 0  # n'achète que si c'est gratuit
```

Options globales : `timezone`, `country`, `renew_margin_minutes`, `notify`.
Une règle peut aussi porter `window` (jours et heures d'activité) et `stall`.

---

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest tests -q
```

73 tests, sans réseau. Un faux serveur GraphQL rejoue le moteur réel :
connexion, jeton périmé, tarifs, devis, achat via `quoteId`, renouvellement,
vérification, achat fantôme, introspection et élagage des champs inconnus —
le faux serveur rejette tout champ hors schéma, comme le vrai. Plus la
garantie de couverture
(nuit, trou, rendez-vous non rejoué) et l'horaire réellement configuré — ces
deux derniers testés sur les fichiers livrés, pas sur des exemples.

---

## État réel, vérifié contre le compte

Premier passage contre le vrai compte le 30/07/2026 :

- **la connexion fonctionne** (`Connecté ✅`) — identifiants, `grant_type=password`
  et `client_id=paybyphone_web` sont les bons ;
- la lecture des tickets échouait parce que j'utilisais `getOpenSessionsV1`,
  qui renvoie un `AutopaySessionResponse` (parkings en ouvrage). C'est
  `getParkingSessionsV1` qu'il faut, avec `{periodType: Current}`. Corrigé.

Le reste de la chaîne — tarifs, devis, achat, vérification — n'a pas encore pu
s'exécuter : il faut un nouveau passage.

Ce qui a été fait pour que ça ne dépende pas de mes suppositions : **le client
ne devine plus la forme des requêtes.** Il introspecte chaque type d'entrée,
met le résultat en cache, et n'envoie que les champs qui existent réellement.
On peut donc lui proposer plusieurs orthographes (`plate` et `licensePlate`) :
c'est l'API qui tranche. Si l'introspection est fermée, la requête part telle
quelle et l'erreur affiche les champs acceptés.

Un prérequis que seul le titulaire du compte peut confirmer : la plaque doit
être enregistrée sur le compte PayByPhone et le **droit CMI y être rattaché**,
sinon aucun tarif CMI n'apparaîtra — ni ici, ni chez AlloValet.
