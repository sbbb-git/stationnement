# Stationnement Flowbird automatique

Achète un ticket d'1 minute chaque soir à 20h01 pour les zones 75016 et 75008.

---

## Mise en place (une seule fois)

### Étape 1 — Mettre le code sur GitHub

```bash
cd /Users/sacha/Desktop/Stationnement
git init
git add .
git commit -m "init"
gh repo create stationnement --private --source=. --push
```

### Étape 2 — Ajouter tes identifiants Flowbird sur GitHub

1. Aller sur ton repo GitHub → **Settings** → **Secrets and variables** → **Actions**
2. Cliquer **New repository secret** et ajouter :
   - `FLOWBIRD_EMAIL` → ton email Flowbird
   - `FLOWBIRD_PASSWORD` → ton mot de passe Flowbird

### Étape 3 — C'est tout

Le script tourne automatiquement chaque soir à 20h01.  
Tu peux voir les résultats dans l'onglet **Actions** de ton repo GitHub.

---

## En cas de problème

Les screenshots de chaque étape sont sauvegardés dans l'onglet **Actions → ton run → Artifacts**.  
Télécharge-les pour voir exactement où le script a bloqué.

---

## Tester sur ton Mac (optionnel)

```bash
cp .env.example .env
# Remplir .env avec tes vrais identifiants

pip install -r requirements.txt
playwright install chromium

# Tester en voyant le navigateur s'ouvrir :
HEADLESS=false python parking.py --zone 75016
```
