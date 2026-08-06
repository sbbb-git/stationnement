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

## 2. Donner ses identifiants et sa plaque

**Settings** → **Secrets and variables** → **Actions** → **New repository
secret**. Trois secrets à créer :

| Nom | Valeur |
|---|---|
| `PBP_USERNAME` | le téléphone du compte PayByPhone, **avec l'indicatif** : `+33612345678` (ou l'email) |
| `PBP_PASSWORD` | le mot de passe du compte |
| `PBP_PLATE` | la plaque, sans espaces ni tirets : `AB123CD` |

La plaque est un secret elle aussi : sinon elle serait écrite en clair dans
`config.yml`, et donc lisible par tous le jour où le dépôt devient public.

Un secret n'est visible de personne — pas même de vous après coup, ni dans les
journaux. C'est le même principe que l'application AlloValet, qui stocke les
identifiants PayByPhone de ses abonnés chiffrés.

## 3. Choisir ses arrondissements

**Code** → `config.yml` → l'icône **crayon**. C'est la **seule** chose à
changer : les zones. Tout le reste est déjà réglé pour le tarif Handi
parisien, gratuit.

```yaml
rules:
  - name: Chez moi
    plate: ${PBP_PLATE}      # ne pas toucher : vient du secret
    zones:                   # liste ORDONNÉE : la 1re est la zone voulue,
      - "75016"              # les suivantes sont des replis du même secteur
      - "75017"
      - "75018"
    rate: "1321271030"       # « Handi - toutes zones » : déjà bon à Paris
    duration: 24h
    renew_at: "20:01"
    max_cost_per_ticket: 0   # 0 = n'achète que si c'est gratuit
    window:
      from: "20:00"          # ne rien prendre avant cette heure
      to: "09:00"
```

Le fichier livré contient deux règles, une par secteur. En garder une seule ?
Supprimer l'autre bloc. En vouloir une troisième ? Recopier le bloc.

Deux points à comprendre :

- **Les zones de repli doivent appartenir au même secteur.** Un ticket sur
  l'une d'elles compte comme couverture de toute la règle : si le 75016 refuse,
  le 75017 fera l'affaire. Mettre le secteur entier rend le trou improbable.
- **`max_cost_per_ticket: 0` est le garde-fou.** À zéro, le programme
  n'achètera jamais rien de payant, quoi qu'il arrive. Ne pas y toucher : un
  tarif visiteur peut coûter plusieurs dizaines d'euros par jour.

En bas de page, **Commit changes**.

*Si le tarif Handi n'apparaît pas (étape 4), c'est l'occasion de le vérifier :
Actions → **Découverte** → Run workflow liste les tarifs réellement
disponibles sur une zone, avec leur `ratePolicyId` à recopier dans `rate:`.*

## 4. Vérifier

Onglet **Actions** → **Découverte** → **Run workflow**. La seconde partie du
résultat est un diagnostic complet : connexion, plaque présente sur le compte,
et pour chaque règle une zone qui donne un devis **gratuit**. Il n'achète rien.

Tant qu'il n'affiche pas `Tout est prêt ✅`, l'automatisation ne servira à rien.

Les deux échecs les plus courants, dans l'ordre :

| Ce qu'affiche le diagnostic | Ce qu'il faut faire |
|---|---|
| `Connexion refusée` | l'identifiant est le **téléphone avec l'indicatif** (`+336…`) ou l'email — pas le numéro seul |
| `aucun tarif` / `plaque absente du compte` | la **carte mobilité inclusion doit être enregistrée sur le compte PayByPhone**, pour cette plaque. Ça se fait dans l'application PayByPhone, pas ici. |

Le second est le vrai prérequis : sans la CMI attachée au véhicule côté
PayByPhone, le tarif Handi n'existe simplement pas, et rien de tout ceci ne
peut fonctionner.

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
