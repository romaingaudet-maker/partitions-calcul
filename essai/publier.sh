#!/usr/bin/env bash
# Publie l'état courant des partitions de ce passage sans attendre la fin de l'analyse (voir publier.py).
# Appelé par silencieux.py (esquisse) et analyse.py (grille, première page) quand la variable PUBLIER est posée.
python3 "$(dirname "$0")/publier.py" "${1:-Partition en cours}" || true
exit 0
