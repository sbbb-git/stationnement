# L'API PayByPhone réelle

Notes de relevé, pour pouvoir refaire le travail si l'API change.

## Comment ça a été établi

L'application web `m.paybyphone.com` est une app **Flutter**. Son bundle
`main.dart.js` (~15 Mo) contient en clair les noms d'opérations GraphQL, les
noms de types d'entrée et les champs de réponse.

```bash
curl -s https://m.paybyphone.com/main.dart.js -o main.dart.js
grep -oE '\b[a-z][A-Za-z0-9]{3,45}V[0-9]\b' main.dart.js | sort -u   # opérations
grep -oE '"[A-Z][A-Za-z0-9]{3,50}(Input|Request)"' main.dart.js | sort -u  # types
```

Les champs d'un objet se lisent dans les sérialiseurs Dart, sous la forme
`s.i(0,"<champ>",…)` regroupés dans une même fonction `p(){…}`.

## Sonde des endpoints (30/07/2026)

| Appel | Réponse | Conclusion |
|---|---|---|
| `POST auth.paybyphoneapis.com/token` (identifiants bidons) | `400 invalid_grant` | vivant, `grant_type=password` + `client_id=paybyphone_web` accepté |
| `POST consumer.paybyphoneapis.com/uapi/graphql` (sans jeton) | `401` | vivant, c'est l'API actuelle |
| `GET consumer.paybyphoneapis.com/parking/accounts` | `404 page not found` | **l'API REST v2 n'existe plus** |

La dernière ligne explique pourquoi une implémentation basée sur la doc REST
reverse-engineerée de 2015 ne peut pas fonctionner, quels que soient les
identifiants.

Aucune protection anti-bot n'a bloqué ces appels depuis une IP de datacenter,
bien que le site charge un capteur HUMAN/PerimeterX
(`PXbDaeXV9t.human.min.v1.js`) pour l'interface web elle-même.

## Opérations utilisées ici

| Opération | Type d'entrée | Rôle |
|---|---|---|
| `getVehiclesV3` | `GetVehiclesInput` | véhicules du compte |
| `getRateOptionsV1` | `GetRateOptionsInput` | tarifs d'une zone (dont CMI/PMR) |
| `getPaymentAccountsV1` | `GetPaymentAccountsInput` | moyen de paiement enregistré |
| `createQuotesV1` | `[QuoteRequestInput!]!` | devis → **quoteId** |
| `startParkingSessionV1` | `StartParkingSessionV1Input` | **achat** à partir du quoteId |
| `renewParkingSessionV1` | `RenewParkingSessionV1Input` | renouvellement d'une session |
| `extendParkingSessionV1` | `ExtendParkingSessionV1Input` | prolongation |
| `getParkingSessionsV1` | `GetParkingSessionsInput` | tickets en cours **et** historique |

**Piège vérifié contre le vrai compte :** `getOpenSessionsV1` n'est *pas* la
liste des tickets de voirie. Il renvoie un `AutopaySessionResponse` — les
parkings en ouvrage. Les tickets de voirie sont dans `getParkingSessionsV1`,
dont l'entrée est `{periodType, offset?, limit?}` avec `periodType` une
énumération (`Current` / `Historic`).

Autres opérations repérées et non utilisées : `stopParkingSessionV1`,
`getOpenSessionsV1`,
`getRateOptionsRenewalV1`, `applyEligibilityV1`, `getEligibilitiesV1`,
`getLocationsV1`, `createJobV1` / `getJobV1`.

## `QuoteRequestInput.details`

Champs, dans l'ordre du sérialiseur de l'application. Seuls `locationId`,
`ratePolicyId` et `parkingQuoteOperation` sont émis systématiquement ; les
autres sont optionnels.

```
locationId  advertisedLocationId  ratePolicyId  parkingQuoteOperation
durationTimeUnit  durationQuantity  licensePlate  stall  parkingSessionId
expireTime  desiredStartTime  userSelectablePromotionId  isRenewal
paymentAccountId  paymentCardType  paymentScope
```

`parkingQuoteOperation` vaut `Start`, `Renew` ou `Extend`.

## Champs d'une session (`getOpenSessionsV1`)

```
parkingSessionId  status  statusDetail  type  locationId  startTime  stall
expireTime  stopTime  isStoppable  fpsApplies  isExtendable  isRenewable
renewableAfter  maxStayState  vehicle { id legacyVehicleId licensePlate
countryCode type jurisdiction }  ratePolicy { ratePolicyId type }  totalCost
segments  feesApplied  couponApplied  jobId  productType  location
```

`isRenewable` et `renewableAfter` sont l'essentiel : l'API dit elle-même quand
une session peut être reprise. C'est ce que le programme suit.

## Champs d'un tarif (`getRateOptionsV1`)

```
name  type  ratePolicyId  maxStayStatus  maxStayEndTime
effectiveMaxStayDuration { quantity timeUnit }  acceptedTimeUnits  areas
eligibilityEndDate  parkingNotAllowedReason  restrictionPeriods
renewalParking  fps  profile  availablePromotions  timeSteps
vehicleRegistrationFound  isVehicleRegistrationMissing
```

Il n'y a **pas** de notion de tarif « par défaut » : sans `rate:` dans la
config, on prend le premier tarif renvoyé par la zone.

## Relevé direct depuis l'application (source la plus sûre)

Le raccourci décisif : ouvrir `m.paybyphone.com` dans Chrome, `F12` → onglet
**Network**, filtrer sur `graphql`, agir dans l'application, puis lire l'onglet
**Payload** de chaque requête. On y voit la requête exacte, sans rien deviner.
(Ne pas recopier l'en-tête `Authorization` : c'est un jeton d'accès au compte.)

Relevés ainsi, et donc certains :

```jsonc
// tickets — en cours comme passés, même opération
{"input": {"periodType": "CURRENT",  "offset": 0, "limit": 10}}
{"input": {"periodType": "HISTORIC", "offset": 0, "limit": 50}}

// tarifs d'une zone pour une plaque
{"input": {"locationId": "75019", "licensePlate": "AB123CD"}}

// ACHAT — le devis porte déjà zone, plaque, durée et tarif
{"input": {"request": {"quoteId": "c2056a3d-a2f0-4b83-8531-76e646608e7b"}}}
```

`StartParkingSessionV1Input` ne contient qu'un champ, `request`, lui-même
réduit au `quoteId`. Toute forme « à plat » est rejetée.

Après l'achat, l'application enchaîne `createJobV1` puis interroge `getJobV1`
en boucle : c'est la capture du paiement. Un ticket gratuit n'en a pas besoin —
`startParkingSessionV1` renvoie déjà `parkingSessionId` et `expireTime`, et la
vérification passe par `getParkingSessionsV1`.

`getOpenSessionsV1` renvoie bien de l'autopay : ses champs sont `sessionId`,
`providerSessionRef`, `plate`, `vendorLotId`, `poeQuoteId`… rien à voir avec la
voirie.

- `periodType` est une énumération **en majuscules** : `CURRENT` / `HISTORIC` ;
- `getLocationsV1` prend `$input: GetLocationInput!` — type au **singulier** —
  avec `{locationId: "75001"}` ;
- une session porte `location { advertisedLocationId name isStallBased }` : le
  numéro affiché sur l'horodateur peut différer de l'identifiant interne, donc
  un ticket se reconnaît sur l'un **ou** l'autre.

## L'achat n'est pas fini tant que la capture n'a pas eu lieu

C'est le piège le plus coûteux de tout ce travail, et il se produit **deux
fois** dans la même chaîne :

```
createQuotesV1         →  un devis. N'achète rien.
startParkingSessionV1  →  une session EN ATTENTE. N'active rien.
createJobV1            →  la capture. C'est elle qui rend le ticket réel.
getJobV1               →  suivi de la capture.
getParkingSessionsV1   →  vérification.
```

Sans `createJobV1`, `startParkingSessionV1` renvoie pourtant un
`parkingSessionId` et aucune erreur. Un balayage des vingt arrondissements a
donné dix-neuf « achats acceptés » et **zéro ticket créé**, ni en cours ni
dans l'historique.

La ligne du job, telle que l'application la construit :

```jsonc
{"input": {"request": {"lineItems": [{
  "productType": "PARKING",
  "productReferenceId": "<parkingSessionId>",
  "vendorId": "<legacyVendorId de la zone>",
  "endingTime": "<expireTime>",
  "isEarlyCapture": false,
  "required": true,
  "metadata": "<chaîne, pas un objet>"
}]}}}
```

Deux détails qui coûtent chacun un aller-retour :

- **`metadata` est déclaré `String`.** L'objet renvoyé par
  `startParkingSessionV1` doit être retransmis **sérialisé**, sinon l'API
  répond `String cannot parse the given literal of type ObjectValueNode`.
- **Montant nul : ni `amount`, ni `paymentMethod`.** Le code de l'application
  écarte lui-même ces deux champs quand le prix est zéro.

`isEarlyCapture` et `metadata` proviennent de la réponse d'achat ; `vendorId`
de `getLocationsV1`.

## Une contrainte par zone, pas globale

Malgré son nom, « Handi - toutes zones » ne dispense pas d'un ticket par
arrondissement. L'API répond `409 VehicleAlreadyParked` sur une zone déjà
couverte, et accepte les autres. L'historique du compte le confirme : deux
tickets par jour, un dans le 16e et un dans le 8e.

## Formes d'entrée : ne pas deviner

Le bundle donne les **noms** des types d'entrée, mais rarement la liste de leurs
champs : ils sont construits à l'exécution. Deviner est dangereux — un champ
inventé fait rejeter toute la requête par GraphQL.

Deux relevés sûrs, obtenus en remontant aux sites d'appel :

- `getParkingSessionsV1` prend `{periodType, offset, limit}` — relevé dans le
  sérialiseur `axR` de l'application ;
- `{locationId, vehicleId, plate}` appartient à `getPoeLookupQuoteV1`, **pas**
  à `getRateOptionsV1` — piège classique.

Pour le reste, le client n'essaie plus de deviner : il introspecte le type
d'entrée, met le résultat en cache, et n'envoie que les champs qui existent
vraiment. On peut donc lui proposer plusieurs orthographes plausibles
(`plate` et `licensePlate`) : l'API tranche. Si l'introspection est fermée,
on envoie la requête telle quelle et l'erreur reste explicite.

## En cas de doute

Le programme sait interroger l'API sur elle-même :

```bash
python -m allovalet schema                                  # tous les types
python -m allovalet schema --type StartParkingSessionV1Input
```

Et quand une opération est refusée pour cause de forme, le message d'erreur
contient automatiquement la liste des champs réellement acceptés.
