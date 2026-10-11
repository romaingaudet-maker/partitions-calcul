#!/usr/bin/env python3
"""Publie dans le dépôt ce que ce passage a produit, et rien d'autre.
Plusieurs analyses tournent en même temps, chacune sur sa machine : elles ne doivent pas se gêner en enregistrant.
Chaque passage ne possède que les fichiers de ses titres (partition, journaux, lectures du banc d'essai), leur ligne
dans resultats/bilan.json et sa demande. Pour publier, il se remet à l'état du dépôt, y repose ses fichiers, fusionne
ses lignes du bilan, puis pousse ; si une autre analyse a poussé entre-temps, il recommence. Aucun conflit possible :
on ne fusionne jamais deux versions d'un même fichier.

Usage : publier.py "message" [--fin] [--journaux DOSSIER]
--fin : fin du passage (retire la demande traitée, garde la demande mise en attente, refait la page de lecture)."""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RACINE = Path(os.environ.get("DONNEES") or Path(__file__).resolve().parent.parent)   # dépôt des données
RESULTATS = RACINE / "resultats"
BRANCHE = os.environ.get("GITHUB_REF_NAME") or "main"


def git(*args, verifier=False):
    return subprocess.run(["git", *args], cwd=RACINE, capture_output=True, text=True, check=verifier)


def main():
    args = sys.argv[1:]
    message = args[0] if args and not args[0].startswith("--") else "Partition en cours"
    fin = "--fin" in args
    journaux = Path(args[args.index("--journaux") + 1]) if "--journaux" in args else None
    liste = RACINE / "travail" / "miens.txt"
    miens = list(dict.fromkeys(liste.read_text(encoding="utf-8").split())) if liste.exists() else []
    if journaux and journaux.is_dir():                         # journaux du passage, rangés par titre
        (RESULTATS / "journaux").mkdir(parents=True, exist_ok=True)
        for f in journaux.glob("journal-*.txt"):
            shutil.copy(f, RESULTATS / "journaux" / f"{miens[0] if len(miens) == 1 else 'essai'}-{f.stem[8:]}.txt")
    # Ce que ce passage possède
    fichiers = []
    for ident in miens:
        fichiers += [f for f in RESULTATS.glob(ident + ".*") if f.is_file()]
        fichiers += list((RESULTATS / "banc").glob(ident + ".*")) + list((RESULTATS / "journaux").glob(ident + "-*.txt"))
    fichiers += list((RESULTATS / "journaux").glob("essai-*.txt")) if len(miens) != 1 else []
    retirer, garder = None, None
    dossier = RACINE / "demandes"
    if fin and (dossier / ".traitee").exists():
        retirer = dossier / (dossier / ".traitee").read_text(encoding="utf-8").strip()
    if fin and (dossier / ".attente").exists():
        garder = dossier / (dossier / ".attente").read_text(encoding="utf-8").strip()
        if garder.exists():
            fichiers.append(garder)
    try:
        bilan = json.loads((RESULTATS / "bilan.json").read_text(encoding="utf-8"))
    except Exception:
        bilan = []
    lignes = {b["id"]: b for b in bilan if b.get("id") in miens}
    if not fichiers and not lignes and not retirer:
        return 0
    git("config", "user.name", "essai-partitions")
    git("config", "user.email", "actions@users.noreply.github.com")
    with tempfile.TemporaryDirectory() as tmp:
        copies = []
        for k, f in enumerate(dict.fromkeys(fichiers)):
            shutil.copy(f, Path(tmp) / str(k))
            copies.append((Path(tmp) / str(k), f))
        for essai in range(10):
            if git("fetch", "-q", "origin", BRANCHE).returncode or git("reset", "-q", "--hard", "FETCH_HEAD").returncode:
                time.sleep(3 + 4 * random.random())
                continue
            chemins = []
            for copie, f in copies:
                f.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(copie, f)
                chemins.append(str(f.relative_to(RACINE)))
            if lignes:                                         # le bilan du dépôt, avec les lignes de ce passage
                f = RESULTATS / "bilan.json"
                try:
                    depot = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
                except ValueError:
                    depot = []
                depot = [b for b in depot if b.get("id") not in lignes] + list(lignes.values())
                f.write_text(json.dumps(depot, ensure_ascii=False, indent=1), encoding="utf-8")
                chemins.append("resultats/bilan.json")
            if retirer is not None and retirer.exists():
                retirer.unlink()
                chemins.append(str(retirer.relative_to(RACINE)))
            if fin:
                subprocess.run([sys.executable, str(RACINE / "essai" / "page.py")], cwd=RACINE, capture_output=True)
                chemins.append("resultats/partitions.html")
            git("add", "-A", "--", *[c for c in chemins if (RACINE / c).exists() or c.startswith("demandes/")])
            if git("diff", "--cached", "--quiet").returncode == 0:
                return 0
            git("commit", "-q", "-m", message)
            if git("push", "-q", "origin", "HEAD:" + BRANCHE).returncode == 0:
                return 0
            time.sleep(1 + 5 * random.random())               # une autre analyse a publié entre-temps : on recommence
    print("  publication impossible après dix essais", flush=True)
    return 0 if not fin else 1


if __name__ == "__main__":
    sys.exit(main())
