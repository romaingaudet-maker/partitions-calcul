#!/usr/bin/env python3
"""Grilles au choix pour les partitions déjà faites : chaque partition reçoit, à côté de nos accords, les grilles
d'accords publiées en ligne pour ce titre, calées sur ses paroles (variantes_accords, voir grilles.variantes).
Les analyses récentes les calculent déjà ; ce passage complète les plus anciennes, sans réécouter le morceau : il ne
lit que les sites de grilles (règles respectées, voir grilles.py) et les mots déjà placés sur la partition.

Une partition qu'une analyse a modifiée entre-temps n'est pas touchée (on n'écrase jamais un résultat plus récent).
Un titre pour lequel aucune grille ne s'accroche reçoit une liste vide : il n'est pas réinterrogé au passage suivant.
Usage : DONNEES=<dépôt des données> [PUBLIER=1] [TITRES="id1 id2"] [TOUS=1] grilles_au_choix.py"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import commun as C
import grilles as G

RACINE = Path(os.environ.get("DONNEES") or Path(__file__).resolve().parent.parent)
RESULTATS = RACINE / "resultats"


def empreinte(f):
    return hashlib.sha1(f.read_bytes()).hexdigest()


def depuis_la_partition(p):
    """Instants des temps, accord tenu sur chaque temps et mots placés [(début, fin, mot, confiance, ligne)]."""
    cases = [c for m in p["mesures"] for c in m["temps"]]
    temps = [float(c["t"]) for c in cases]
    sub = int(p.get("subdivision") or 2)
    accords, courant, mots = [], None, []
    for i, c in enumerate(cases):
        a = c.get("accord")
        if a:
            courant = (int(a["r"]), a["q"], float(a.get("conf", 0.5)), a.get("b"))
        accords.append(courant)
        duree = (temps[i + 1] - temps[i]) if i + 1 < len(temps) else 0.5
        for k, w in enumerate(c.get("mots") or []):
            debut = temps[i] + duree * float(w.get("pos", 0)) / sub + 0.001 * k
            mots.append((debut, debut + 0.2, w["txt"], float(w.get("conf", 0.5)), int(w.get("ligne", 0))))
    return temps, accords, mots


def calculer(f):
    p = json.loads(f.read_text(encoding="utf-8"))
    if "mesures" not in p or p.get("provisoire"):
        return None
    temps, accords, mots = depuis_la_partition(p)
    if len(mots) < 10:
        return []
    avec = []
    G.chercher_grilles(p.get("artiste", ""), p.get("titre", ""), en_tout=20, avec_paroles=avec)
    if not avec:
        return []
    return G.variantes(avec, mots, temps, accords, C.lire_accord)


def git(*args):
    return subprocess.run(["git", *args], cwd=RACINE, capture_output=True, text=True)


def publier(faits):
    """faits : {id: (empreinte de la partition lue, variantes)}. Pose les variantes sur les partitions inchangées."""
    if not faits:
        return
    git("config", "user.name", "essai-partitions")
    git("config", "user.email", "actions@users.noreply.github.com")
    for essai in range(10):
        if git("fetch", "-q", "origin", "main").returncode or git("reset", "-q", "--hard", "FETCH_HEAD").returncode:
            time.sleep(3 + 4 * random.random())
            continue
        poses = []
        for ident, (emp, variantes) in faits.items():
            f = RESULTATS / (ident + ".json")
            if not f.exists() or empreinte(f) != emp:
                print(f"  {ident} : partition modifiée entre-temps, laissée telle quelle", flush=True)
                continue
            p = json.loads(f.read_text(encoding="utf-8"))
            p["variantes_accords"] = variantes
            mesures = p.pop("mesures")                     # même écriture que commun.ecrire_partition
            f.write_text(json.dumps({**p, "mesures": mesures}, ensure_ascii=False, indent=1), encoding="utf-8")
            poses.append(str(f.relative_to(RACINE)))
        if not poses:
            return
        git("add", "--sparse", *poses) if git("add", *poses).returncode else None
        n = sum(1 for _, v in faits.values() if v)
        git("commit", "-q", "-m", f"Grilles au choix : {n} partitions")
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            print(f"  publié : {len(poses)} partitions", flush=True)
            return
        time.sleep(3 + 4 * random.random())
    print("  publication impossible après 10 essais", flush=True)


def main():
    voulus = os.environ.get("TITRES", "").split()
    tous = bool(os.environ.get("TOUS"))
    fichiers = [RESULTATS / (i + ".json") for i in voulus] if voulus else sorted(RESULTATS.glob("*.json"))
    faits, t0 = {}, time.time()
    for f in fichiers:
        if f.name == "bilan.json" or not f.exists():
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "mesures" not in p or ("variantes_accords" in p and not tous and not voulus):
            continue
        emp = empreinte(f)
        print(f"== {p.get('artiste')} — {p.get('titre')}", flush=True)
        try:
            v = calculer(f)
        except Exception as e:
            print(f"  grilles impossibles : {type(e).__name__} {str(e)[:160]}", flush=True)
            continue
        if v is None:
            continue
        print(f"  {len(v)} grilles au choix" + (" : " + ", ".join(f"{g['nom']} ({g['accord']})" for g in v) if v else ""), flush=True)
        faits[p["id"]] = (emp, v)
        if os.environ.get("PUBLIER") and len(faits) >= 10:   # par paquets : l'appli les voit arriver au fur et à mesure
            publier(faits)
            faits = {}
    if os.environ.get("PUBLIER"):
        publier(faits)
    print(f"terminé en {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
