# L'installer pour soi

Pour une autre voiture, un autre compte PayByPhone, d'autres zones. Aucun
logiciel à installer : tout se fait dans le navigateur, y compris depuis un
téléphone. Compter dix minutes.

Il faut : un compte **GitHub** (gratuit) et ses identifiants **PayByPhone**.

---

## 1. Copier le projet

Sur la page du dépôt, bouton **Fork** en haut à droite → **Create fork**.

Vous obtenez votre propre copie, indépendante. Tout ce qui suit se passe chez
vous ; le dépôt d'origine ne voit rien de votre compte.

Puis, dans **votre** copie, onglet **Actions** → bouton
**I understand my workflows, go ahead and enable them**. GitHub désactive les
automatisations d'un fork par sécurité, il faut le lui autoriser une fois.

## 2. Donner ses identifiants PayByPhone

**Settings** → **Secrets and variables** → **Actions** → **New repository
secret**. Deux secrets à créer :

| Nom | Valeur |
|---|---|
| `PBP_USERNAME` | le téléphone du compte PayByPhone, **avec l'indicatif** : `+33612345678` (ou l'email) |
| `PBP_PASSWORD` | le mot de passe du compte |

Un secret n'est visible de personne — pas même de vous après coup, ni dans les
journaux. C'est le même principe que l'application AlloValet, qui stocke les
identifiants PayByPhone de ses abonnés chiffrés.

## 3. Trouver l'identifiant de son tarif

Onglet **Actions** → workflow **Découverte** → **Run workflow**. Indiquer la
zone (le numéro affiché sur l'horodateur, ex. `75016`) et la plaque.

Le résultat s'affiche en tête du passage :

```
  • type=CUSTOM  « Handi - toutes zones »  (ratePolicyId 1321271030, unités Days)
  • type=VIS     « Visiteur »              (ratePolicyId 75016, unités Minutes/Hours)
```

Noter le `ratePolicyId` de la ligne voulue. Si la liste est vide, la plaque
n'est pas enregistrée sur le compte PayByPhone, ou la zone n'existe pas.

## 4. Écrire ses règles

**Code** → `config.yml` → l'icône **crayon**. Une règle par secteur :

```yaml
rules:
  - name: Chez moi
    plate: AB123CD           # sa plaque, sans espaces
    zones:                   # liste ORDONNÉE : la 1re est la zone voulue,
      - "75016"              # les suivantes sont des replis du même secteur
      - "75017"
      - "75018"
    rate: "1321271030"       # le ratePolicyId de l'étape 3
    duration: 24h
    renew_at: "20:05"        # l'heure du relais quotidien
    max_cost_per_ticket: 0   # 0 = n'achète que si c'est gratuit
    window:
      from: "20:00"          # ne rien prendre avant cette heure
      to: "09:00"
```

**`max_cost_per_ticket: 0` est le garde-fou** : à zéro, le programme n'achètera
jamais rien de payant, quoi qu'il arrive. Ne le changer qu'en sachant ce qu'on
fait — un tarif visiteur peut coûter plusieurs dizaines d'euros par jour.

Les zones de repli doivent appartenir au **même secteur** : un ticket sur l'une
d'elles compte comme couverture de toute la règle. Une seule zone ? Écrire
`location: "75016"` et supprimer `zones:`.

En bas de page, **Commit changes**.

## 5. Vérifier

Onglet **Actions** → **Découverte** → **Run workflow**. La seconde partie du
résultat est un diagnostic complet : connexion, plaque présente sur le compte,
et pour chaque règle une zone qui donne un devis **gratuit**. Il n'achète rien.

Tant qu'il n'affiche pas `Tout est prêt ✅`, l'automatisation ne servira à rien.

---

## Ensuite

Ça tourne tout seul. Deux choses à connaître :

- **Le tableau de bord.** Après le premier passage, une issue s'ouvre dans
  l'onglet **Issues** : elle affiche l'état de chaque secteur, la zone qui
  couvre et jusqu'à quand. Son contenu est réécrit à chaque passage — un seul
  lien à garder en favori sur son téléphone.
- **L'alerte.** Si un renouvellement échoue, une autre issue s'ouvre toute
  seule et GitHub l'envoie par mail. Elle se referme au premier passage réussi.

Pour changer un réglage : `config.yml` → crayon → **Commit changes**. Un
passage se déclenche dans la foulée.

---

## Ce qu'il faut savoir avant de s'en servir

- **C'est un usage personnel, pas un service.** Le programme se connecte au
  compte PayByPhone avec les identifiants de son propriétaire, comme le fait
  l'application. Ne l'utiliser que sur son propre compte.
- **Un ticket gratuit reste soumis à ses conditions.** Le tarif Handi suppose
  une carte mobilité inclusion valide et le respect des règles de
  stationnement ; l'automatisation ne dispense de rien.
- **Rien n'est garanti.** GitHub peut retarder ou ne pas exécuter un passage
  programmé, et PayByPhone peut changer son API du jour au lendemain. Le
  tableau de bord et l'alerte sont là pour que ça se voie — les surveiller.
