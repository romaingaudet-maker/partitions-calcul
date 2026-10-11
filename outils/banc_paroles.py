#!/usr/bin/env python3
"""Banc d'essai du poste « paroles » : place les mots de chansons libres (JamendoLyrics, github.com/f90/jamendolyrics)
dont l'instant de chaque mot a été annoté à la main, puis mesure l'écart. Les lignes sont fournies avec leur
horodatage, comme le fait LRCLIB ; on mesure donc le placement des mots à l'intérieur des lignes, voix isolée comprise.
N'écrit que des nombres : aucune parole n'est affichée ni gardée.
Usage : banc_paroles.py [titres par langue=5] [langues=French,English]"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

CODE = Path(__file__).resolve().parent.parent
RACINE = Path(os.environ.get("DONNEES") or CODE)      # dépôt des données (résultats du banc)
sys.path.insert(0, str(CODE / "essai"))
import analyse as A  # noqa: E402

BASE = "https://raw.githubusercontent.com/f90/jamendolyrics/master/"
GENRES = ("Pop", "Rock", "Country", "Indie", "Reggae", "RNB", "Blues")     # chant net : le rap et le métal sont un autre exercice


def lire(chemin, binaire=False):
    with urllib.request.urlopen(BASE + urllib.parse.quote(chemin), timeout=120) as r:
        d = r.read()
    return d if binaire else d.decode("utf-8")


nu = lambda m: re.sub(r"[^\w]", "", m.lower())


def evaluer(fichier):
    nom = fichier[:-4]
    lignes_ref = list(csv.DictReader(io.StringIO(lire(f"annotations/lines/{nom}.csv"))))
    mots_ref = list(csv.DictReader(io.StringIO(lire(f"annotations/words/{nom}.csv"))))
    lignes = [(float(l["start_time"]), l["lyrics_line"]) for l in lignes_ref]
    textes = [w for w in lire(f"lyrics/{nom}.words.txt").split("\n") if w.strip()]      # un mot par ligne, dans l'ordre des instants
    if len(textes) != len(mots_ref):
        raise RuntimeError(f"annotations incohérentes : {len(textes)} mots pour {len(mots_ref)} instants")
    ref = [(float(m["word_start"]), nu(w)) for m, w in zip(mots_ref, textes) if nu(w)]
    A.TRAVAIL.mkdir(exist_ok=True)
    mp3, wav = A.TRAVAIL / "banc.mp3", A.TRAVAIL / "banc.wav"
    mp3.write_bytes(lire("mp3/" + fichier, binaire=True))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", "44100", "-ac", "2", str(wav)], check=True)
    pistes, _ = A.separer_voix(wav)
    mots, infos = A.placer_mots(pistes["voix"], lignes, False, indice=0.0)
    temps, _, _ = A.detecter_temps(wav)
    nous = [(m[0], nu(m[2]), m[3]) for m in mots if nu(m[2])]
    paires = []                                               # (instant annoté, instant posé, confiance)
    for bloc in SequenceMatcher(None, [w for _, w in ref], [w for _, w, _ in nous], autojunk=False).get_matching_blocks():
        for k in range(bloc.size):
            paires.append((ref[bloc.a + k][0], nous[bloc.b + k][0], nous[bloc.b + k][2]))
    if not paires:
        return {"titre": nom, "mots_annotes": len(ref), "poses": 0}
    e = np.array([b - a for a, b, _ in paires])
    def case(t, pas):                                         # numéro du temps (pas=1) ou du demi-temps (pas=2) où tombe l'instant
        i = int(np.clip(np.searchsorted(temps, t, side="right") - 1, 0, len(temps) - 2))
        return pas * i + min(pas - 1, int(pas * (t - temps[i]) / max(1e-3, temps[i + 1] - temps[i])))
    meme_temps = np.mean([case(a, 1) == case(b, 1) for a, b, _ in paires])
    meme_demi = np.mean([case(a, 2) == case(b, 2) for a, b, _ in paires])
    conf = np.array([c for _, _, c in paires])
    faible = conf < 0.3
    return {"titre": nom, "mots_annotes": len(ref), "poses": round(len(paires) / len(ref), 3),
            "ecart_median_s": round(float(np.median(np.abs(e))), 3), "ecart_moyen_s": round(float(np.mean(np.abs(e))), 3),
            "biais_s": round(float(np.median(e)), 3), "a_0_1_s": round(float(np.mean(np.abs(e) <= 0.1)), 3),
            "a_0_3_s": round(float(np.mean(np.abs(e) <= 0.3)), 3), "a_plus_d_1_s": round(float(np.mean(np.abs(e) > 1.0)), 3),
            "meme_temps": round(float(meme_temps), 3), "meme_demi_temps": round(float(meme_demi), 3),
            "tempo": round(60 / float(np.median(np.diff(temps))), 1),
            "mots_a_confiance_faible": round(float(faible.mean()), 3),
            "a_0_3_s_confiance_faible": round(float(np.mean(np.abs(e[faible]) <= 0.3)), 3) if faible.any() else None,
            "a_0_3_s_confiance_bonne": round(float(np.mean(np.abs(e[~faible]) <= 0.3)), 3) if (~faible).any() else None,
            "lignes_non_posees": infos.get("lignes_non_posees", 0)}


def main():
    combien = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    langues = (sys.argv[2] if len(sys.argv) > 2 else "French,English").split(",")
    titres = list(csv.DictReader(io.StringIO(lire("JamendoLyrics.csv"))))
    choisis = []
    for langue in langues:
        bons = [t for t in titres if t["Language"] == langue and t["Genre"] in GENRES
                and t["LyricOverlap"] == "false" and t["Polyphonic"] == "false" and t["NonLexical"] == "false"]
        choisis += sorted(bons, key=lambda t: t["Filepath"])[:combien]
    resultats = []
    for k, t in enumerate(choisis):
        print(f"== {k + 1}/{len(choisis)} {t['Language']} {t['Genre']}", flush=True)
        try:
            r = evaluer(t["Filepath"])
            r.update(langue=t["Language"], genre=t["Genre"])
            resultats.append(r)
            print("  ", {k: v for k, v in r.items() if k != "titre"}, flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            resultats.append({"titre": t["Filepath"], "langue": t["Language"], "genre": t["Genre"], "erreur": repr(e)[:200]})
        (RACINE / "resultats" / "banc").mkdir(parents=True, exist_ok=True)
        (RACINE / "resultats" / "banc" / "poste-paroles.json").write_text(json.dumps(resultats, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
