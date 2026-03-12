# Archive

Ce dossier contient les scripts archivés (legacy), conservés pour référence
historique mais non utilisés dans le flux principal actuel.

## Contenu

- `legacy/bubo.py`  
  Ancienne version du pipeline unifié à scoring majoritairement hardcodé.

## Pourquoi c'est archivé

- Le moteur actif recommandé est `bubo_engine.py` (déterministe et plus robuste).
- Le mode expérimental piloté LLM est `bubo_brain.py`.
- `bubo.py` est gardé uniquement pour comparaison, rollback ponctuel ou audit.

## Règle pratique

- Pour le développement courant: utiliser `bubo_engine.py`.
- Pour les tests/expérimentations LLM: utiliser `bubo_brain.py`.
- Ne réactiver `legacy/bubo.py` que si besoin explicite.
