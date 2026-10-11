# partitions-calcul

Code d'analyse d'un projet personnel : à partir d'un titre, il cherche les pulsations, les accords et la place des
paroles, pour en tirer une partition simplifiée (paroles, pulsations, accords).

Ce dépôt ne contient que du code. Les demandes d'analyse et les partitions produites vivent dans un dépôt privé ;
les analyses tournent ici (GitHub Actions) et y enregistrent leurs résultats.

- `essai/analyse.py` : la chaîne d'analyse (temps, accords, basse, paroles).
- `essai/silencieux.py` : récupère le son d'un titre et publie une première esquisse.
- `essai/aiguillage.py` : répartit les demandes, quatre analyses à la fois.
- `outils/banc_*.py` : bancs d'essai sur des enregistrements libres annotés à la main.
