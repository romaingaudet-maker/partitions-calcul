#!/usr/bin/env python3
"""Répartit les demandes d'analyse : une machine par demande, quatre à la fois au plus.
Lancé quand une demande arrive, quand une analyse se termine et, tant que des demandes patientent, par la relance.
Regarde les demandes (demandes/*.json) et les analyses déjà en route (leur nom porte celui de la demande), puis lance
une analyse pour chaque demande libre tant qu'il reste de la place. Une demande refusée il y a moins de treize
minutes pour une raison passagère (YouTube écarte le service) n'est pas relancée tout de suite.
Python pur ; parle à GitHub par la commande gh."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(os.environ.get("DONNEES") or Path(__file__).resolve().parent.parent)   # dépôt des données
ENSEMBLE = 4
PATIENCE = 13 * 60
BRANCHE = os.environ.get("GITHUB_REF_NAME") or "main"


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        print("  gh :", r.stderr.strip()[:200], flush=True)
    return r


def en_route():
    """Noms des demandes dont l'analyse est en route (en file ou en cours)."""
    r = gh("api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/silencieux.yml/runs?per_page=40")
    try:
        runs = json.loads(r.stdout).get("workflow_runs", [])
    except ValueError:
        return None
    return [str(x.get("display_title") or "")[8:] for x in runs
            if x.get("status") != "completed" and str(x.get("display_title") or "").startswith("Analyse ")]


def age(texte):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(texte).replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return 1e9


def main():
    demandes = []
    for f in (RACINE / "demandes").glob("*.json"):
        try:
            demandes.append((f.name, json.loads(f.read_text(encoding="utf-8"))))
        except ValueError:
            continue
    demandes.sort(key=lambda x: (x[1].get("demande_le") or "", x[0]))
    route = en_route()
    if route is None:
        print("État des analyses illisible : rien n'est lancé.", flush=True)
        return 1
    print(f"{len(demandes)} demande(s), {len(route)} analyse(s) en route", flush=True)
    place, lancees = ENSEMBLE - len(route), 0
    for nom, d in demandes:
        if place <= 0:
            break
        if nom in route or age(d.get("dernier_essai")) < PATIENCE:
            continue
        if gh("workflow", "run", "silencieux.yml", "--ref", BRANCHE, "-f", "demande=" + nom).returncode == 0:
            print("  lancée :", nom, flush=True)
            place, lancees = place - 1, lancees + 1
    if lancees:
        time.sleep(10)          # le temps que GitHub inscrive ces analyses : le prochain aiguillage doit les voir
    return 0


if __name__ == "__main__":
    sys.exit(main())
