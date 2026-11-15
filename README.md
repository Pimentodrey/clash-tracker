# QG du clan — Clash Tracker

Tableau de bord partagé du clan : scores des membres, guerre, classement France, historique.

- Les données sont collectées automatiquement toutes les heures par GitHub Actions (`scripts/collect.py`).
- Le site est servi par GitHub Pages depuis le dossier `docs/`.
- La clé API Clash Royale est stockée dans le secret `CR_TOKEN` (jamais dans le code).
- Pour changer de clan : modifier `clan_tag` dans `config.json`.
