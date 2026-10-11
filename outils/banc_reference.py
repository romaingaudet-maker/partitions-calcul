#!/usr/bin/env python3
"""Références du poste « accords » : récupère les annotations faites à la main par le Centre for Digital Music
(isophonics.net : accords avec renversements, temps et premiers temps, pour les Beatles, Queen, Carole King) et
cherche sur YouTube la piste du disque de quelques titres. Seules les annotations (du texte) sont gardées."""
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
RACINE = Path(os.environ.get("DONNEES") or CODE)      # dépôt des données (résultats du banc)
sys.path.insert(0, str(CODE / "essai"))
import commun as C  # noqa: E402

ARCHIVES = {"beatles": ["http://isophonics.net/files/annotations/The%20Beatles%20Annotations.tar.gz",
                        "https://isophonics.net/files/annotations/The%20Beatles%20Annotations.tar.gz"],
            "queen": ["http://isophonics.net/files/annotations/Queen%20Annotations.tar.gz",
                      "https://isophonics.net/files/annotations/Queen%20Annotations.tar.gz"],
            "carole-king": ["http://isophonics.net/files/annotations/Carole%20King%20Annotations.tar.gz"]}
TITRES = [("Queen", "Another One Bites The Dust"), ("Queen", "Don't Stop Me Now"), ("Queen", "We Are The Champions"),
          ("Queen", "Somebody To Love"), ("Queen", "Crazy Little Thing Called Love"), ("Queen", "Bohemian Rhapsody"),
          ("Queen", "I Want To Break Free"), ("Queen", "You're My Best Friend"), ("Queen", "A Kind Of Magic"),
          ("The Beatles", "Let It Be"), ("The Beatles", "Hey Jude"), ("The Beatles", "Come Together"),
          ("The Beatles", "Something"), ("The Beatles", "Here Comes The Sun"), ("The Beatles", "Yesterday"),
          ("The Beatles", "Help!"), ("The Beatles", "While My Guitar Gently Weeps"), ("The Beatles", "Penny Lane")]


def main():
    sortie = RACINE / "resultats" / "banc" / "reference"
    sortie.mkdir(parents=True, exist_ok=True)
    liste = []
    for nom, urls in ARCHIVES.items():
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "partitions-personnelles/1.0"})
                donnees = urllib.request.urlopen(req, timeout=120).read()
                with tarfile.open(fileobj=io.BytesIO(donnees), mode="r:*") as tar:
                    for m in tar:
                        if m.isfile() and m.name.lower().endswith((".lab", ".txt")) and m.size < 400_000:
                            genre = next((g for g in ("chordlab", "beat", "keylab", "seglab") if f"/{g}/" in m.name.lower() or m.name.lower().startswith(g + "/")), None)
                            if genre in ("chordlab", "beat", "keylab"):
                                but = sortie / nom / genre / (C.identifiant(Path(m.name).stem) + Path(m.name).suffix.lower())
                                but.parent.mkdir(parents=True, exist_ok=True)
                                but.write_bytes(tar.extractfile(m).read())
                                liste.append(f"{nom}\t{genre}\t{m.name}\t{but.name}")
                print(f"{nom} : archive lue ({len(donnees) // 1000} ko)", flush=True)
                break
            except Exception as e:
                print(f"{nom} : {url.split(':')[0]} indisponible ({type(e).__name__} {str(e)[:100]})", flush=True)
    (sortie / "fichiers.tsv").write_text("\n".join(liste), encoding="utf-8")
    videos = []
    for artiste, titre in TITRES:
        try:
            r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", f"ytsearch10:{artiste} {titre}"],
                               capture_output=True, text=True, timeout=120)
            for e in (json.loads(r.stdout).get("entries") or []) if r.returncode == 0 else []:
                chaine = e.get("channel") or e.get("uploader") or ""
                if e.get("id") and e.get("duration") and C.mot_nu(artiste) in C.mot_nu(chaine):
                    videos.append({"artiste": artiste, "titre": titre, "video": e["id"], "duree": round(e["duration"]),
                                   "chaine": chaine, "nom": e.get("title")})
        except Exception as e:
            print(f"recherche impossible pour {titre} : {type(e).__name__}", flush=True)
        print(artiste, "-", titre, ":", sum(1 for v in videos if v["titre"] == titre), "vidéos", flush=True)
    (sortie / "videos.json").write_text(json.dumps(videos, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
