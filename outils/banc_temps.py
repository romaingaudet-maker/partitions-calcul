#!/usr/bin/env python3
"""Banc d'essai du poste « pulsations » : détecte les temps et les premiers temps d'extraits annotés à la main
(GTZAN, 30 secondes, dix genres ; annotations github.com/TempoBeatDownbeat/gtzan_tempo_beat) et mesure l'accord.
Mesures usuelles (mir_eval) : F-mesure des temps à ±70 ms, F-mesure des premiers temps, tempo juste / double / moitié,
nombre de temps par mesure.
Usage : banc_temps.py [extraits par genre=4]"""
import json
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import numpy as np

CODE = Path(__file__).resolve().parent.parent
RACINE = Path(os.environ.get("DONNEES") or CODE)      # dépôt des données (résultats du banc)
sys.path.insert(0, str(CODE / "essai"))
import analyse as A  # noqa: E402

SOURCES = ["https://huggingface.co/datasets/marsyas/gtzan/resolve/main/data/genres.tar.gz",
           "https://huggingface.co/datasets/marsyas/gtzan/resolve/refs%2Fconvert%2Fparquet/data/genres.tar.gz",
           "http://opihi.cs.uvic.ca/sound/genres.tar.gz"]
GENRES = ["blues", "classical", "country", "disco", "hiphop", "jazz", "metal", "pop", "reggae", "rock"]


def extraire(voulus, dossier):
    """Lit l'archive au fil de l'eau et n'en garde que les extraits voulus."""
    for url in SOURCES:
        try:
            trouves = 0
            req = urllib.request.Request(url, headers={"User-Agent": "partitions-personnelles/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
                for m in tar:
                    nom = Path(m.name).name
                    if nom in voulus:
                        (dossier / nom).write_bytes(tar.extractfile(m).read())
                        trouves += 1
                        if trouves == len(voulus):
                            break
            if trouves:
                print(f"{trouves} extraits lus depuis {url.split('/')[2]}", flush=True)
                return trouves
        except Exception as e:
            print(f"source indisponible ({url.split('/')[2]}) : {type(e).__name__} {str(e)[:120]}", flush=True)
    return 0


def main():
    import mir_eval
    par_genre = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    numeros = [int(round(x)) for x in np.linspace(7, 93, par_genre)]
    voulus = {f"{g}.{n:05d}.wav": (g, n) for g in GENRES for n in numeros}
    dossier = A.TRAVAIL / "gtzan"
    dossier.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/TempoBeatDownbeat/gtzan_tempo_beat", str(dossier / "annotations")], check=False)
    if not extraire(set(voulus), dossier):
        print("Aucun extrait disponible.", flush=True)
        return 1
    resultats = []
    for nom, (genre, n) in sorted(voulus.items()):
        f, ann = dossier / nom, dossier / "annotations" / "beats" / f"gtzan_{genre}_{n:05d}.beats"
        if not f.exists() or not ann.exists():
            continue
        lignes = [l.split() for l in ann.read_text().splitlines() if l.strip()]
        ref = np.array([float(l[0]) for l in lignes])
        ref_un = np.array([float(l[0]) for l in lignes if len(l) > 1 and l[1] == "1"])
        metre_ref = max((int(l[1]) for l in lignes if len(l) > 1), default=0)
        try:
            temps, premiers, methode = A.detecter_temps(f)
            num, pos, longueur, infos = A.regulariser_mesures(temps, premiers)
            nos_un = np.asarray(temps)[np.asarray(pos) == 0]
            r = {"extrait": nom, "genre": genre,
                 "f_temps": round(float(mir_eval.beat.f_measure(mir_eval.beat.trim_beats(ref), mir_eval.beat.trim_beats(temps))), 3),
                 "f_premiers_temps": round(float(mir_eval.beat.f_measure(mir_eval.beat.trim_beats(ref_un), mir_eval.beat.trim_beats(nos_un))), 3) if len(ref_un) > 1 else None,
                 "f_premiers_temps_bruts": round(float(mir_eval.beat.f_measure(mir_eval.beat.trim_beats(ref_un), mir_eval.beat.trim_beats(premiers))), 3) if len(ref_un) > 1 else None,
                 "rapport_tempo": round(float(np.median(np.diff(ref)) / np.median(np.diff(temps))), 2) if len(temps) > 2 and len(ref) > 2 else None,
                 "temps_par_mesure": int(longueur), "temps_par_mesure_annote": metre_ref}
        except Exception as e:
            r = {"extrait": nom, "genre": genre, "erreur": repr(e)[:200]}
        resultats.append(r)
        print(r, flush=True)
        (RACINE / "resultats" / "banc").mkdir(parents=True, exist_ok=True)
        (RACINE / "resultats" / "banc" / "poste-temps.json").write_text(json.dumps(resultats, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
