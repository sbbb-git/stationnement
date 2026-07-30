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
| `getOpenSessionsV1` | `GetOpenSessionsInput` | tickets en cours (= vérification) |
| `getParkingSessionsV1` | `GetParkingSessionsInput` | historique |

Autres opérations repérées et non utilisées : `stopParkingSessionV1`,
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

## En cas de doute

Le programme sait interroger l'API sur elle-même :

```bash
python -m allovalet schema                                  # tous les types
python -m allovalet schema --type StartParkingSessionV1Input
```

Et quand une opération est refusée pour cause de forme, le message d'erreur
contient automatiquement la liste des champs réellement acceptés.
