#!/usr/bin/env python3
"""Analyse d'un titre : audio -> paroles synchronisées -> temps et mesures -> accords -> voix isolée ->
placement des mots -> JSON + rapport lisible.

Chaque étape est isolée : un échec est noté dans le rapport et n'arrête pas le reste.
L'audio n'est jamais enregistré dans le dépôt.

Plusieurs moteurs lisent le même enregistrement et leurs résultats sont RECOUPÉS (détail dans CHOIX.md) :
- mesures : Beat This!, les barres de mesure du service Magic Chords, et les changements d'harmonie ;
- accords : modèle BTC sur le mix, BTC sur l'accompagnement sans la voix, modèle ChordNet, accords du service ;
- basse   : énergie grave de l'accompagnement et du mix, et basse lue par ChordNet, pour noter les renversements (Do/Sol) ;
- mots    : alignement forcé MMS sur la voix isolée, comparé aux mots entendus par Whisper.

La partition est LIVRÉE PAR ÉTAPES pour ne pas faire attendre : la grille (temps, accords, paroles provisoires)
dès qu'elle est prête, puis la première page avec ses mots calés, puis la partition complète.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

from commun import lire_accord  # noqa: E402
from commun import (FAMILLE_MINEURE, QUALITES, _json, accord_a, assembler, caler_sur_transcription, chercher_paroles,  # noqa: F401
                    ecrire_partition, identifiant, lignes_de_whisper, lire_harte, lire_magic, lire_whisper, mot_nu)

ICI = Path(__file__).resolve().parent
RACINE = Path(os.environ.get("DONNEES") or ICI.parent)      # les données peuvent vivre dans un autre dépôt que le code
TRAVAIL = RACINE / "travail"          # audio et pistes séparées, jamais commité
RESULTATS = RACINE / "resultats"
SUIVI = None                                      # fonction appelée au début de chaque étape (écoute locale)
PUBLICATION = None                                # fonction (bilan, message) appelée à chaque version livrée de la partition
TRANCHE = 60.0                                    # durée de la « première page » : la voix y est isolée et calée d'abord
SOURCE = os.environ.get("SOURCE", "deezer")       # "deezer" (extraits de 30 s) ou "youtube"
DOSSIER_BTC = Path(os.environ.get("DOSSIER_BTC", TRAVAIL / "btc"))
DOSSIER_MODELES = Path(os.environ.get("DOSSIER_MODELES", Path.home() / ".cache" / "torch" / "separation"))

INTERVALLES = {"min": (0, 3, 7), "maj": (0, 4, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "min6": (0, 3, 7, 9),
               "maj6": (0, 4, 7, 9), "min7": (0, 3, 7, 10), "minmaj7": (0, 3, 7, 11), "maj7": (0, 4, 7, 11),
               "7": (0, 4, 7, 10), "dim7": (0, 3, 6, 9), "hdim7": (0, 3, 6, 10), "sus2": (0, 2, 7), "sus4": (0, 5, 7)}
SUFFIXES = {"maj": "M", "min": "m", "dim": "dim", "aug": "aug", "min6": "m6", "maj6": "6", "min7": "m7",
            "minmaj7": "mM7", "maj7": "M7", "7": "7", "dim7": "dim7", "hdim7": "m7♭5", "sus2": "sus2", "sus4": "sus4"}
NOTES_DIESES = ["Do", "Do♯", "Ré", "Ré♯", "Mi", "Fa", "Fa♯", "Sol", "Sol♯", "La", "La♯", "Si"]
NOTES_BEMOLS = ["Do", "Ré♭", "Ré", "Mi♭", "Mi", "Fa", "Sol♭", "Sol", "La♭", "La", "Si♭", "Si"]
NOTES_MIXTES = ["Do", "Do♯", "Ré", "Mi♭", "Mi", "Fa", "Fa♯", "Sol", "La♭", "La", "Si♭", "Si"]
NOTES_SANS_ARMURE_MAJEUR = ["Do", "Ré♭", "Ré", "Mi♭", "Mi", "Fa", "Fa♯", "Sol", "La♭", "La", "Si♭", "Si"]
NOTES_SANS_ARMURE_MINEUR = ["Do", "Do♯", "Ré", "Ré♯", "Mi", "Fa", "Fa♯", "Sol", "Sol♯", "La", "Si♭", "Si"]
TONS_DIESES = {7, 2, 9, 4, 11, 6}      # toniques majeures dont l'armure est en dièses


# ---------------------------------------------------------------- téléchargement
def telecharger_youtube(t):
    """Morceau complet depuis YouTube Music (bloqué depuis les serveurs GitHub lors de l'essai)."""
    requete = f"{t['artiste']} {t['titre']}"
    wav = TRAVAIL / (t["id"] + ".wav")
    for f in TRAVAIL.glob(t["id"] + ".*"):
        f.unlink()
    r = subprocess.run(["yt-dlp", "https://music.youtube.com/search?q=" + urllib.parse.quote(requete) + "#songs",
                        "--playlist-items", "1", "-f", "bestaudio/best", "-x", "--audio-format", "wav",
                        "--no-progress", "-o", str(TRAVAIL / (t["id"] + ".%(ext)s"))], capture_output=True, text=True)
    if r.returncode != 0 or not wav.exists():
        raise RuntimeError((r.stderr.strip().splitlines() or ["échec sans message"])[-1][:300])
    return wav, {"source": "YouTube Music", "duree": None}


def telecharger_extrait(t):
    """Extrait officiel de 30 s : Deezer d'abord, catalogue iTunes en repli (aucune clé nécessaire)."""
    essais, trouve = [], None
    for q in (f'artist:"{t["artiste"]}" track:"{t["titre"]}"', f'{t["artiste"]} {t["titre"]}'):
        try:
            donnees = _json("https://api.deezer.com/search?limit=10&q=" + urllib.parse.quote(q))
            avec = [d for d in donnees.get("data", []) if d.get("preview")]
            if avec:
                d = avec[0]
                trouve = (d["preview"], {"source": "Deezer (extrait)", "video": d.get("title"),
                                         "chaine": d.get("artist", {}).get("name"), "url": d.get("link"),
                                         "duree": d.get("duration")})
                break
            essais.append("Deezer : " + json.dumps(donnees, ensure_ascii=False)[:150])
        except Exception as e:
            essais.append("Deezer : " + repr(e)[:150])
    if not trouve:
        try:
            q = urllib.parse.urlencode({"term": f'{t["artiste"]} {t["titre"]}', "entity": "song", "limit": 10, "country": "FR"})
            avec = [d for d in _json("https://itunes.apple.com/search?" + q).get("results", []) if d.get("previewUrl")]
            if avec:
                d = avec[0]
                trouve = (d["previewUrl"], {"source": "iTunes (extrait)", "video": d.get("trackName"),
                                            "chaine": d.get("artistName"), "url": d.get("trackViewUrl"),
                                            "duree": round((d.get("trackTimeMillis") or 0) / 1000)})
            else:
                essais.append("iTunes : aucun extrait")
        except Exception as e:
            essais.append("iTunes : " + repr(e)[:150])
    if not trouve:
        raise RuntimeError(" | ".join(essais))
    url, infos = trouve
    brut, wav = TRAVAIL / (t["id"] + ".extrait"), TRAVAIL / (t["id"] + ".wav")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as rep:
        brut.write_bytes(rep.read())
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(brut), "-ar", "44100", "-ac", "2", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("conversion : " + r.stderr[-200:])
    return wav, infos


EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".aiff", ".webm"}


def titres_deposes(dossier):
    """Fichiers audio déposés dans audio/. Le nom « Artiste - Titre.ext » sert à trouver les paroles ;
    à défaut on lit les étiquettes du fichier."""
    titres = []
    for f in sorted(dossier.glob("*")) if dossier.exists() else []:
        if f.suffix.lower() not in EXTENSIONS:
            continue
        artiste, titre = "", f.stem
        if " - " in f.stem:
            artiste, titre = [x.strip() for x in f.stem.split(" - ", 1)]
        else:
            r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
                               capture_output=True, text=True)
            try:
                tags = {k.lower(): v for k, v in json.loads(r.stdout)["format"].get("tags", {}).items()}
                artiste, titre = tags.get("artist", ""), tags.get("title", f.stem)
            except Exception:
                pass
        t = {"id": identifiant(f"{artiste} {titre}"), "artiste": artiste, "titre": titre, "fichier": str(f)}
        origine = Path(str(f) + ".origine.json")                # posé par l'analyse silencieuse : d'où vient le son
        if origine.exists():
            t["origine"] = json.loads(origine.read_text(encoding="utf-8"))
        titres.append(t)
    return titres


def convertir_fichier(t):
    if t.get("direct"):                                     # son déjà au bon format (écoute locale)
        return Path(t["fichier"]), {"source": "Écoute locale", "video": None, "chaine": t["artiste"], "duree": None}
    wav = TRAVAIL / (t["id"] + ".wav")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", t["fichier"], "-vn", "-ar", "44100", "-ac", "2", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not wav.exists():
        raise RuntimeError("fichier illisible : " + r.stderr[-200:])
    o = t.get("origine") or {}
    return wav, {"source": o.get("source", "Fichier déposé"), "video": o.get("video", Path(t["fichier"]).name), "chaine": t["artiste"],
                 "duree": o.get("duree")}


# ------------------------------------------------------------------- séparation
_separateur = None


def separer_voix(wav):
    """Isole la voix. Renvoie ({'voix': chemin, 'instrumental': chemin}, méthode)."""
    global _separateur
    try:
        if _separateur is None:
            from audio_separator.separator import Separator
            _separateur = Separator(output_dir=str(TRAVAIL / "pistes"), model_file_dir=str(DOSSIER_MODELES),
                                    output_format="WAV", log_level=30)
            _separateur.load_model(model_filename="Kim_Vocal_2.onnx")
        sorties = [Path(f) if Path(f).is_absolute() else TRAVAIL / "pistes" / f for f in _separateur.separate(str(wav))]
        voix = next(f for f in sorties if "(Vocals)" in f.name)
        inst = next((f for f in sorties if "(Instrumental)" in f.name), None)
        return {"voix": voix, "instrumental": inst}, "MDX-Net Kim Vocal 2"
    except Exception as e:
        print("  séparateur MDX indisponible, repli Demucs :", repr(e)[:300], flush=True)
        sortie = TRAVAIL / "pistes"
        r = subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals", "-d", "cpu",
                            "-o", str(sortie), str(wav)], capture_output=True, text=True)
        dossier = sortie / "htdemucs" / wav.stem
        if r.returncode != 0 or not (dossier / "vocals.wav").exists():
            raise RuntimeError((r.stderr.strip().splitlines() or ["échec Demucs"])[-1][:300])
        return {"voix": dossier / "vocals.wav", "instrumental": dossier / "no_vocals.wav"}, "Demucs (repli)"


def couper(wav, debut, fin, nom):
    """Copie le passage [debut, fin] (secondes ; fin None = jusqu'au bout) dans un nouveau fichier de travail."""
    import soundfile as sf
    sr = sf.info(str(wav)).samplerate
    y, _ = sf.read(str(wav), start=int(debut * sr), stop=None if fin is None else int(fin * sr), always_2d=True)
    sortie = TRAVAIL / nom
    sf.write(str(sortie), y, sr)
    return sortie


def raccorder(debut, suite, coupe, avance, nom):
    """Recolle deux pistes séparées par tranches : `debut` couvre le morceau jusqu'après `coupe`, `suite` commence
    `avance` secondes avant `coupe`. Fondu de 50 ms au raccord."""
    import soundfile as sf
    ya, sr = sf.read(str(debut), always_2d=True)
    yb, _ = sf.read(str(suite), always_2d=True)
    na, f = int(coupe * sr), int(0.05 * sr)
    yb = yb[int(avance * sr):]
    if len(ya) >= na + f and len(yb) >= f and ya.shape[1] == yb.shape[1]:
        r = np.linspace(0.0, 1.0, f)[:, None]
        yb[:f] = ya[na:na + f] * (1 - r) + yb[:f] * r
    sortie = TRAVAIL / "pistes" / nom
    sf.write(str(sortie), np.concatenate([ya[:na], yb]), sr)
    return sortie


# ------------------------------------------------------------- temps et mesures
_battue = None


def detecter_temps(wav):
    """Temps et premiers temps bruts. Renvoie (temps, premiers_temps, méthode)."""
    global _battue
    try:
        if _battue is None:
            from beat_this.inference import File2Beats
            _battue = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        temps, premiers = _battue(str(wav))
        return np.asarray(temps, float), np.asarray(premiers, float), "Beat This!"
    except Exception as e:  # repli sans modèle : moins fiable, pas de vrais premiers temps
        print("  Beat This! indisponible, repli librosa :", repr(e)[:200], flush=True)
        import librosa
        y, sr = librosa.load(str(wav), sr=22050, mono=True)
        _, images = librosa.beat.beat_track(y=y, sr=sr)
        temps = librosa.frames_to_time(images, sr=sr)
        force = librosa.onset.onset_strength(y=y, sr=sr)
        phase = max(range(4), key=lambda p: force[images[p::4]].mean() if len(images[p::4]) else 0)
        return temps, temps[phase::4], "librosa (repli, mesures supposées à 4 temps)"


def temps_librosa(y, sr):
    """Seconde lecture des temps, par une méthode classique sans modèle (sert seulement au recoupement)."""
    import librosa
    _, images = librosa.beat.beat_track(y=y, sr=sr)
    return librosa.frames_to_time(images, sr=sr)


def comparer_temps(a, b, tolerance=0.07):
    """Compare deux lectures des temps sur leur partie commune : part des temps qui coïncident (à 70 ms près)
    et rapport des tempos (2 ou 0,5 = l'une bat deux fois plus vite que l'autre)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 8 or len(b) < 8:
        return None
    lo, hi = max(a[0], b[0]) - 0.1, min(a[-1], b[-1]) + 0.1
    a, b = a[(a >= lo) & (a <= hi)], b[(b >= lo) & (b <= hi)]
    if len(a) < 8 or len(b) < 8:
        return None
    pa = float((np.abs(a[:, None] - b[None, :]).min(1) <= tolerance).mean())
    pb = float((np.abs(b[:, None] - a[None, :]).min(1) <= tolerance).mean())
    return {"accord": round(2 * pa * pb / max(1e-9, pa + pb), 2),
            "rapport_tempo": round(float(np.median(np.diff(a)) / np.median(np.diff(b))), 2)}


def numeroter(pos):
    """N° de mesure de chaque temps d'après sa position : une mesure commence quand la position revient à 0
    ou quand la suite est rompue. La mesure 0 est la levée."""
    pos = np.asarray(pos)
    debut = np.zeros(len(pos), bool)
    debut[1:] = (pos[1:] == 0) | (pos[1:] != pos[:-1] + 1)
    debut[0] = pos[0] == 0
    return np.cumsum(debut)


def recouper_mesures(temps, pos, longueur, magic, choix_bruts):
    """Où tombe le premier temps de la mesure ? Trois lectures votent : Beat This! (position 0 par construction),
    les barres de mesure du service, et l'harmonie (les accords changent surtout sur le premier temps).
    Si les deux autres s'accordent nettement contre Beat This!, on décale la mesure. Renvoie (pos, num, infos)."""
    temps, pos = np.asarray(temps, float), np.asarray(pos)
    votes, parts = {"beat_this": 0}, {}
    if magic and magic.get("barres") and magic.get("metre") == longueur:
        tb = np.asarray(magic["temps"])[magic["barres"]]
        ecart = np.abs(temps[:, None] - tb[None, :])
        proches = ecart.argmin(0)[ecart.min(0) <= 0.12]
        if len(proches) >= 8:
            h = np.bincount(pos[proches], minlength=longueur)
            votes["magic"], parts["magic"] = int(h.argmax()), round(float(h.max() / h.sum()), 2)
    if choix_bruts is not None:
        c = np.asarray(choix_bruts)
        chgt = np.where((c[1:] != c[:-1]) & (c[1:] < 24) & (c[:-1] < 24))[0] + 1
        if len(chgt) >= 12:
            h = np.bincount(pos[chgt], minlength=longueur)
            votes["harmonie"], parts["harmonie"] = int(h.argmax()), round(float(h.max() / h.sum()), 2)
    q = 0
    if (votes.get("magic") is not None and votes.get("magic") == votes.get("harmonie") and votes["magic"] != 0
            and parts["magic"] >= 0.6 and parts["harmonie"] >= 1.6 / longueur):
        q = votes["magic"]
        pos = (pos - q) % longueur
    return pos, numeroter(pos), {"votes": votes, "parts": parts, "decalage_retenu": int(q)}


def regulariser_mesures(temps, premiers):
    """Place chaque temps dans sa mesure à partir des premiers temps détectés, qui comportent des
    oublis et des faux positifs. On cherche la suite de positions la plus plausible : normalement la
    position avance de 1 et revient à 0 après une mesure complète ; une mesure plus courte ou plus
    longue reste possible mais coûte cher, donc n'apparaît que si la détection y insiste.
    Renvoie (n° de mesure par temps, position dans la mesure par temps, temps par mesure, infos).
    La mesure 0 est la levée (temps avant le premier « un »)."""
    temps = np.asarray(temps, float)
    n = len(temps)
    idx = sorted({int(np.argmin(np.abs(temps - p))) for p in premiers})
    marque = np.zeros(n, bool)
    marque[idx] = True
    ecarts = np.diff(idx) if len(idx) >= 2 else np.array([4])
    valeurs, comptes = np.unique(ecarts[(ecarts >= 2) & (ecarts <= 7)] if ((ecarts >= 2) & (ecarts <= 7)).any() else [4],
                                 return_counts=True)
    candidats = [int(v) for v, c in zip(valeurs, comptes) if c >= 0.25 * comptes.max()]

    def chemin(L, rupture=3.0):
        gain = lambda i, pos: (1.0 if pos == 0 else -1.0) if marque[i] else (-0.6 if pos == 0 else 0.0)
        score = np.array([gain(0, q) for q in range(L)])
        retour = np.zeros((n, L), int)
        for i in range(1, n):
            nouveau = np.empty(L)
            for q in range(L):
                normal = score[(q - 1) % L]
                if q == 0:                                   # recommencer une mesure avant la fin de la précédente
                    autre = int(np.argmax(score))
                    if score[autre] - rupture > normal:
                        nouveau[q], retour[i, q] = score[autre] - rupture + gain(i, q), autre
                        continue
                nouveau[q], retour[i, q] = normal + gain(i, q), (q - 1) % L
            score = nouveau
        pos = np.zeros(n, int)
        pos[-1] = int(np.argmax(score))
        for i in range(n - 1, 0, -1):
            pos[i - 1] = retour[i, pos[i]]
        return float(score.max()), pos

    resultats = [(chemin(L), L) for L in candidats]
    (total, pos), longueur = max(resultats, key=lambda r: r[0][0] / max(1, marque.sum()))
    debut = np.zeros(n, bool)                                # un temps ouvre une mesure si sa position est 0,
    debut[1:] = (pos[1:] == 0) | (pos[1:] != pos[:-1] + 1)   # ou si la suite a été rompue
    debut[0] = pos[0] == 0
    num = np.cumsum(debut)
    irregulieres = int(sum(1 for m in range(1, num.max()) if (num == m).sum() != longueur))
    return num, pos, longueur, {"premiers_detectes": len(idx), "coherence": round(float((marque & (pos == 0)).sum() / max(1, marque.sum())), 2),
                                "mesures_irregulieres": irregulieres, "candidats": candidats}


def detecter_subdivision(y, sr, temps):
    """Binaire ou ternaire ? On mesure la force des attaques au milieu du temps (binaire),
    aux tiers (ternaire) et aux quarts (doubles croches). Renvoie (subdivision d'affichage, infos)."""
    import librosa
    hop = 256
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    env = env / (np.median(env) + 1e-9)

    def force(fractions):
        v = []
        for a, b in zip(temps[:-1], temps[1:]):
            for f in fractions:
                i = int(round((a + f * (b - a)) * sr / hop))
                v.append(env[max(0, i - 2):i + 3].max() if i < len(env) else 0)
        return float(np.mean(v)) if v else 0.0

    demi, tiers, quarts = force([1 / 2]), force([1 / 3, 2 / 3]), force([1 / 4, 3 / 4])
    duree_temps = float(np.median(np.diff(temps)))
    ternaire = tiers > 1.3 * demi and tiers > 1.3 * quarts
    sub = 3 if ternaire else (4 if duree_temps >= 0.6 else 2)
    return sub, {"force_demi": round(demi, 2), "force_tiers": round(tiers, 2), "force_quarts": round(quarts, 2),
                 "ternaire": bool(ternaire)}


# ---------------------------------------------------------------------- accords
_btc = None


def _charger_btc():
    """Modèle BTC (transformeur bidirectionnel, 170 classes d'accords), poids fournis par ses auteurs."""
    global _btc
    if _btc is None:
        import torch
        if not (DOSSIER_BTC / "btc_model.py").exists():
            try:
                subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/jayg996/BTC-ISMIR19",
                                str(DOSSIER_BTC)], check=True)
            except Exception:                               # pas de git sur la machine : archive zip
                import io
                import zipfile
                with urllib.request.urlopen("https://github.com/jayg996/BTC-ISMIR19/archive/refs/heads/master.zip", timeout=300) as rep:
                    archive = zipfile.ZipFile(io.BytesIO(rep.read()))
                racine = archive.namelist()[0].split("/")[0]
                archive.extractall(DOSSIER_BTC.parent)
                shutil.rmtree(DOSSIER_BTC, ignore_errors=True)
                (DOSSIER_BTC.parent / racine).rename(DOSSIER_BTC)
        for nom, typ in (("float", float), ("int", int), ("bool", bool)):   # le code d'origine vise un ancien numpy
            if not hasattr(np, nom):
                setattr(np, nom, typ)
        sys.path.insert(0, str(DOSSIER_BTC))
        from btc_model import BTC_model
        config = dict(feature_size=144, timestep=108, num_chords=170, input_dropout=0.2, layer_dropout=0.2,
                      attention_dropout=0.2, relu_dropout=0.2, num_layers=8, num_heads=4, hidden_size=128,
                      total_key_depth=128, total_value_depth=128, filter_size=128, loss="ce", probs_out=False)
        poids = torch.load(str(DOSSIER_BTC / "test" / "btc_model_large_voca.pt"), map_location="cpu", weights_only=False)
        modele = BTC_model(config=config)
        modele.load_state_dict(poids["model"])
        modele.eval()
        _btc = (modele, float(poids["mean"]), float(poids["std"]))
    return _btc


def probabilites_accords(y, sr=22050):
    """Log-probabilités des 170 classes d'accords, image par image (une image = 93 ms).
    Fenêtres de 10 s qui se chevauchent de moitié, pour éviter les défauts aux raccords."""
    import librosa
    import torch
    modele, moyenne, ecart = _charger_btc()
    accordage = float(librosa.estimate_tuning(y=y, sr=sr))
    if abs(accordage) > 0.08:                               # enregistrement pas au diapason : on le recale
        y = librosa.effects.pitch_shift(y, sr=sr, n_steps=-accordage)
    cqt = librosa.cqt(y, sr=sr, n_bins=144, bins_per_octave=24, hop_length=2048)
    x = (np.log(np.abs(cqt) + 1e-6).T - moyenne) / ecart
    n, T = len(x), 108
    somme, poids = np.zeros((n, 170)), np.zeros((n, 1))
    triangle = np.minimum(np.arange(1, T + 1), np.arange(T, 0, -1))[:, None].astype(float)
    with torch.no_grad():
        for s in range(0, max(1, n - T // 2), T // 2):
            bloc = x[s:s + T]
            k = len(bloc)
            if k < T:
                bloc = np.pad(bloc, ((0, T - k), (0, 0)))
            cache, _ = modele.self_attn_layers(torch.tensor(bloc, dtype=torch.float32)[None])
            lp = torch.log_softmax(modele.output_layer.output_projection(cache), -1)[0].numpy()
            somme[s:s + k] += lp[:k] * triangle[:k]
            poids[s:s + k] += triangle[:k]
    return somme / poids, 2048 / sr, accordage


def energie_par_temps(logp, pas, temps):
    """Log-probabilités BTC moyennées sur chaque temps : (temps, 169) = 168 accords + « aucun accord »."""
    temps = np.asarray(temps, float)
    n = len(temps)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if n > 1 else 0.5))
    centres = (np.arange(len(logp)) + 0.5) * pas
    E = np.zeros((n, 169))
    for i in range(n):
        dans = np.where((centres >= temps[i]) & (centres < fins[i]))[0]
        if not len(dans):
            dans = [int(np.clip(round((temps[i] + fins[i]) / 2 / pas), 0, len(logp) - 1))]
        moy = logp[dans].mean(0)
        E[i, :168], E[i, 168] = moy[:168], moy[169]                     # classe « inconnu » écartée
    return E


def _log_softmax(x):
    x = x - x.max(1, keepdims=True)
    return x - np.log(np.exp(x).sum(1, keepdims=True))


MINEURES = [k for k, q in enumerate(QUALITES) if q in FAMILLE_MINEURE]
MAJEURES = [k for k, q in enumerate(QUALITES) if q not in FAMILLE_MINEURE]


def familles(E):
    """Ramène les 168 accords BTC à 24 familles (fondamentale × tierce majeure ou mineure) + « aucun » : c'est
    sur ce terrain commun que les moteurs sont comparés. Renvoie des log-probabilités (temps, 25)."""
    F = np.empty((len(E), 25))
    for r in range(12):
        bloc = E[:, r * 14:(r + 1) * 14]
        for f, cols in ((0, MAJEURES), (1, MINEURES)):
            b = bloc[:, cols]
            m = b.max(1, keepdims=True)
            F[:, 2 * r + f] = (m + np.log(np.exp(b - m).sum(1, keepdims=True)))[:, 0]
    F[:, 24] = E[:, 168]
    return _log_softmax(F)


def chroma_par_temps(y, sr, temps, accordage=0.0):
    """Énergie de chacune des 12 notes sur chaque temps, pour la lecture par gabarits. On écoute de do3 à si6 :
    le grave est laissé de côté, sinon la basse d'un accord renversé se ferait passer pour sa fondamentale."""
    import librosa
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=2048, fmin=librosa.note_to_hz("C3"), n_bins=48, bins_per_octave=12,
                           tuning=accordage))
    C = librosa.decompose.hpss(C, kernel_size=(17, 9))[0]              # on garde ce qui tient (notes), pas ce qui claque (batterie)
    poids = np.array([1.0, 1.0, 0.8, 0.5])[:, None, None]
    ch = (C.reshape(4, 12, -1) * poids).sum(0)                         # (12, images)
    temps = np.asarray(temps, float)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if len(temps) > 1 else 0.5))
    centres = (np.arange(ch.shape[1]) + 0.5) * 2048 / sr
    sortie = np.zeros((len(temps), 12))
    for i in range(len(temps)):
        dans = (centres >= temps[i]) & (centres < fins[i])
        if dans.any():
            sortie[i] = ch[:, dans].mean(1)
    sortie = np.log1p(10 * sortie / (sortie.max() + 1e-9))
    return sortie / (np.linalg.norm(sortie, axis=1, keepdims=True) + 1e-9)


def notes_fines(y, sr, temps):
    """Notes jouées sur chaque temps, touche par touche (88 touches de piano, la0 = colonne 0), lues par un modèle
    de transcription (Basic Pitch). Bien plus net que l'énergie par note : un do et un si voisins ne bavent pas
    l'un sur l'autre. Sert à savoir si la note qui distingue deux lectures d'un accord est réellement jouée."""
    import librosa
    import onnxruntime as ort
    if sr != 22050:
        y = librosa.resample(np.asarray(y, np.float32), orig_sr=sr, target_sr=22050)
    N, R = 43844, 7680                                                 # fenêtre du modèle, recouvrement (30 images)
    pas = N - R
    y = np.concatenate([np.zeros(R // 2, np.float32), np.asarray(y, np.float32)])
    n = int(np.ceil(len(y) / pas))
    y = np.concatenate([y, np.zeros(n * pas + R - len(y), np.float32)])
    session = ort.InferenceSession(str(ICI / "modeles" / "notes.onnx"), providers=["CPUExecutionProvider"])
    entree = session.get_inputs()[0].name
    morceaux = []
    for i in range(0, n, 16):
        F = np.stack([y[k * pas:k * pas + N] for k in range(i, min(n, i + 16))])[:, :, None]
        morceaux.append(session.run(["StatefulPartitionedCall:1"], {entree: F})[0][:, 15:-15])
    A = np.concatenate(morceaux).reshape(-1, 88)                       # (images à 86 par seconde, 88)
    temps = np.asarray(temps, float)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if len(temps) > 1 else 0.5))
    sortie = np.zeros((len(temps), 88), np.float32)
    for i in range(len(temps)):
        a, b = int(temps[i] * 22050 / 256), max(int(fins[i] * 22050 / 256), int(temps[i] * 22050 / 256) + 1)
        if a < len(A):
            sortie[i] = A[a:b].mean(0)
    return sortie


def gabarits(ch, nettete=8.0):
    """Lecture d'accords sans modèle appris : chaque accord parfait est un gabarit (fondamentale, tierce, quinte)
    comparé aux notes entendues. Moteur simple, mais indépendant de BTC. Renvoie des log-probabilités (temps, 25)."""
    S = np.empty((len(ch), 25))
    for r in range(12):
        f, q, M, m = ch[:, r], ch[:, (r + 7) % 12], ch[:, (r + 4) % 12], ch[:, (r + 3) % 12]
        S[:, 2 * r] = f + 0.9 * q + 0.8 * M - 0.5 * m
        S[:, 2 * r + 1] = f + 0.9 * q + 0.8 * m - 0.5 * M
    S[:, 24] = S[:, :24].mean(1)
    return _log_softmax(nettete * S)


def accords_chordnet(wav):
    """Troisième lecture des accords : ChordNet (Jiang et al., ISMIR 2019), ensemble de cinq réseaux qui décompose
    chaque accord en fondamentale, basse et couleur. Indépendant de BTC, et il lit la basse : précieux pour les
    renversements. Renvoie des segments (début, fin, (fondamentale, qualité, basse) ou None)."""
    from lv_chordia.chord_recognition import chord_recognition
    return [(float(s["start_time"]), float(s["end_time"]), lire_harte(s["chord"])) for s in chord_recognition(str(wav))]


def lecture_segments(segments, temps, assurance=0.5):
    """Accords donnés par segments (ChordNet, service) ramenés à nos temps.
    Renvoie (log-probabilités (temps, 25), temps renseignés, basse par temps ou -1)."""
    temps = np.asarray(temps, float)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if len(temps) > 1 else 0.5))
    F, connu = np.full((len(temps), 25), np.log(1 / 25)), np.zeros(len(temps), bool)
    basses = np.full(len(temps), -1)
    for i, (a, b) in enumerate(zip(temps, fins)):
        acc = accord_a(segments, (a + b) / 2)
        if acc:
            F[i] = np.log((1 - assurance) / 24)
            F[i, 2 * acc[0] + (1 if acc[1] in FAMILLE_MINEURE else 0)] = np.log(assurance)
            connu[i] = True
            if acc[2] is not None:
                basses[i] = acc[2]
    return F, connu, basses


def couleurs_segments(segments, temps):
    """Accord complet (fondamentale, qualité) lu par un moteur à segments sur chacun de nos temps, ou None."""
    temps = np.asarray(temps, float)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if len(temps) > 1 else 0.5))
    return [(lambda a: (a[0], a[1]) if a else None)(accord_a(segments, (a + b) / 2)) for a, b in zip(temps, fins)]


POIDS_MOTEURS = {"btc_mix": 1.0, "btc_sans_voix": 1.0, "chordnet": 1.0, "magic_chords": 0.7, "gabarits": 0.5, "sans_basse": 0.0}
COUT_CHANGEMENT = (0.4, 1.2, 2.5)     # prix d'un changement d'accord : sur un premier temps, au milieu de la mesure, ailleurs
POIDS_SUR_PEDALE = 0.25     # poids d'un moteur qui lit l'accord de la note tenue par la basse (réglé sur le banc d'essai : à 0,5, Fa♯/Si et Mi/Si se perdaient)


def appuyer_sur_les_reprises(L, chemin, pos, longueur, seuil=0.7, conteste=None, pedale=None):
    """Un couplet ou un refrain revient plusieurs fois avec la même grille. Là où les moteurs hésitent dans une
    reprise, les autres reprises aident à trancher. Deux mesures « se répondent » quand les huit mesures qui les
    entourent portent les mêmes accords, à 70 % des temps au moins ; les lectures d'une mesure sont alors mêlées
    à celles des mesures qui lui répondent. La mesure elle-même pèse 3, l'ensemble de ses reprises 2,5 au plus :
    une lecture unanime sur place n'est jamais renversée, seule une hésitation l'est. Dans une mesure où les
    moteurs se contredisent (`conteste`), elle ne pèse plus que 1,5 : ses reprises ont alors le dernier mot.
    Un refrain repris un ton plus haut (changement de tonalité en fin de morceau) reste une reprise : si les huit
    mesures ne se répondent pas telles quelles, on les compare transposées, de 1 à 11 demi-tons. La ressemblance exigée
    est alors plus forte (80 %) et le passage doit porter trois accords différents au moins, car un accord tenu
    ressemble à n'importe quel autre accord tenu une fois transposé. Les lectures de la reprise sont transposées
    d'autant avant d'être mêlées.
    Sur une basse tenue (`pedale` : note tenue sur chaque temps, ou -1), les accords du dessus sont mal lus et deux
    reprises d'un même passage peuvent différer sans être différentes : un temps où les deux passages tiennent la même
    note de basse compte pour moitié dans la ressemblance, même si les accords lus diffèrent.
    Renvoie (lectures appuyées, nombre de mesures qui ont des reprises, variantes), où `variantes` (temps, 25) compte,
    pour chaque temps, les accords que ses reprises portent au même endroit quand ils diffèrent du sien."""
    pos = np.asarray(pos)
    variantes = np.zeros((len(pos), 25))
    debuts = [int(i) for i in np.flatnonzero(pos == 0) if i + longueur <= len(pos) and (pos[i:i + longueur] == np.arange(longueur)).all()]
    M = len(debuts)
    if M < 12:
        return L, 0, variantes
    sig = np.array([chemin[d:d + longueur] for d in debuts])
    tenue = np.array([np.asarray(pedale)[d:d + longueur] for d in debuts]) if pedale is not None else np.full(sig.shape, -1)
    suite = np.array([b - a == longueur for a, b in zip(debuts, debuts[1:])] + [False])   # la mesure suivante est-elle contiguë ?
    L2, avec = L.copy(), 0
    transposee = lambda x, d: np.where(x < 24, (x + 2 * d) % 24, 24)
    sigs = [transposee(sig, d) for d in range(12)]                      # sigs[d][b] : la mesure b montée de d demi-tons

    def lecture(b, d):                                                  # lectures de la mesure b, montées de d demi-tons
        X = L[debuts[b]:debuts[b] + longueur]
        return X if d == 0 else np.concatenate([np.roll(X[:, :24], 2 * d, axis=1), X[:, 24:]], axis=1)

    def chercher(a, essais):
        trouvees = []
        for b in range(M):
            if abs(a - b) < 4:
                continue
            trouve = False
            for d, fenetres in essais:
                for fenetre in fenetres:
                    ks = [k for k in fenetre if 0 <= a + k < M and 0 <= b + k < M
                          and (k == 0 or (suite[min(a, a + k):max(a, a + k)].all() and suite[min(b, b + k):max(b, b + k)].all()))]
                    total = longueur * len(ks)
                    if total < 6 * longueur:
                        continue
                    ici = np.concatenate([sig[a + k] for k in ks])
                    if d and len(set(ici[ici < 24].tolist())) < 3:
                        continue
                    la = np.concatenate([sigs[d][b + k] for k in ks])
                    basse_ici, basse_la = np.concatenate([tenue[a + k] for k in ks]), np.concatenate([tenue[b + k] for k in ks])
                    memes = int((ici == la).sum()) + 0.5 * int(((ici != la) & (basse_ici >= 0) & (basse_la >= 0)
                                                                & (basse_ici == (basse_la + d) % 12)).sum())
                    if memes >= (seuil if d == 0 else max(seuil, 0.8)) * total:
                        trouvees.append((b, d))
                        trouve = True
                        break
                if trouve:
                    break
        return trouvees

    for a in range(M):
        # D'abord les reprises dans le même ton ; à défaut seulement, les reprises transposées. La fenêtre est alors
        # aussi essayée vers l'avant seul et vers l'arrière seul : juste après un changement de tonalité, les mesures
        # d'avant ne sont pas transposées et feraient manquer la reprise.
        reprises = chercher(a, [(0, [range(-3, 5)])]) \
            or chercher(a, [(d, [range(-3, 5), range(0, 6), range(-5, 1)]) for d in range(1, 12)])
        if reprises:
            avec += 1
            dispute = lambda m: conteste is not None and bool(conteste[debuts[m]:debuts[m] + longueur].any())
            # Une reprise où les moteurs s'accordent est un bon témoin ; une reprise elle-même disputée pèse trois fois moins
            # (sinon deux passages mal lus de la même façon se confortent l'un l'autre).
            poids = np.array([0.3 if dispute(b) else 1.0 for b, _ in reprises])
            poids *= min(1.0, 2.5 / poids.sum())
            soi = 1.5 if dispute(a) else 3.0
            # Sur une basse tenue, lire l'accord de cette basse ne prouve rien (les moteurs la suivent) : un passage qui
            # y lit autre chose est le meilleur témoin, et celui qui lit l'accord de la basse pèse quatre fois moins.
            suiveur = lambda m: np.where((tenue[m] >= 0) & (sig[m] < 24) & (sig[m] // 2 == tenue[m]), POIDS_SUR_PEDALE, 1.0)[:, None]
            pa = soi * suiveur(a)
            pb = [w * suiveur(b) for w, (b, d) in zip(poids, reprises)]
            somme = pa * L[debuts[a]:debuts[a] + longueur] + sum(x * lecture(b, d) for x, (b, d) in zip(pb, reprises))
            L2[debuts[a]:debuts[a] + longueur] = somme / (pa + sum(pb))
            for b, d in reprises:
                autre = sigs[d][b]
                for k in np.flatnonzero((sig[a] != autre) & (autre < 24)):
                    variantes[debuts[a] + k, autre[k]] += 1
    return L2, avec, variantes


_regle_octave = None


def regle_de_l_octave():
    """Pour chaque degré tenu par la basse, les accords qui le portent et leur fréquence (essai/regle_octave.json,
    tiré d'annotations faites à la main, voir outils/regle_octave.py)."""
    global _regle_octave
    if _regle_octave is None:
        try:
            _regle_octave = json.loads((ICI / "regle_octave.json").read_text(encoding="utf-8"))
        except Exception:
            _regle_octave = {}
    return _regle_octave


def reinterpreter_sur_la_basse(chemin, L, parts, pos, tonalite, poids=1.0, marge=0.5, nettete=0.3, journal=None):
    """Un accord renversé est souvent pris pour l'accord de sa basse : devant Do/Mi, les moteurs écrivent Mi mineur
    (mesuré sur le banc d'essai : un temps renversé sur trois). Pour chaque plage d'accord dont la basse est nette, on
    compare donc l'accord retenu à ceux qui contiennent aussi cette note de basse, comme tierce, quinte ou septième :
    chacun est noté d'après ce que les moteurs lisaient (leurs seconds choix comptent) et d'après la règle de
    l'octave, qui dit quels accords portent d'habitude ce degré de la gamme à la basse. Le mieux noté l'emporte s'il
    dépasse l'accord retenu de `marge` ; la basse sera posée ensuite par poser_basses.
    ESSAI NON RETENU (banc d'essai, neuf titres) : sans effet. Les seconds choix des moteurs ne désignent pas le bon
    accord (il est en moyenne 4 points derrière, contre 8 pour un faux renversement, mais les accords non renversés
    sont vingt fois plus nombreux), et la règle de l'octave tirée des annotations donne partout l'avantage à l'état
    fondamental. Gardé pour être rejoué quand la lecture des notes sera plus fine ; l'analyse ne l'appelle pas.
    Renvoie (chemin, nombre de plages réécrites)."""
    regle = regle_de_l_octave().get((tonalite or {}).get("mode", ""), {}) if tonalite else {}
    chemin, n, faits, i = chemin.copy(), len(chemin), 0, 0
    pos = np.asarray(pos)
    while i < n:
        j = i
        while j < n and chemin[j] == chemin[i]:
            j += 1
        c = int(chemin[i])
        if c < 24 and parts is not None:
            v = (parts[i:j] * np.where(pos[i:j] == 0, 2.0, 1.0)[:, None]).sum(0)
            b = int(v.argmax())
            if v[b] / max(1e-9, v.sum()) >= nettete and int(parts[i].argmax()) == b:
                def contient(f):                               # la basse est-elle la fondamentale, la tierce, la quinte ou la 7e mineure ?
                    r, mineur = f // 2, f % 2 == 1
                    return (b - r) % 12 in (0, 3 if mineur else 4, 7, 10)
                if contient(c):
                    def note(f):
                        s = float(L[i:j, f].mean())
                        if regle:
                            d = str((b - tonalite["tonique"]) % 12)
                            cle = f"{(f // 2 - tonalite['tonique']) % 12},{'m' if f % 2 else 'M'}"
                            s += poids * float(np.log(regle.get(d, {}).get(cle, 0.0) + 0.01))
                        return s
                    candidats = [f for f in range(24) if contient(f)]
                    meilleur = max(candidats, key=note)
                    ok = meilleur != c and note(meilleur) - note(c) >= marge
                    if ok:
                        if journal is not None:
                            journal.append(f"temps {i}-{j - 1} : {nom_accord((c // 2, 'min' if c % 2 else 'maj'))} relu "
                                           f"{nom_accord((meilleur // 2, 'min' if meilleur % 2 else 'maj'))} sur basse de {NOTES_MIXTES[b]}")
                        chemin[i:j] = meilleur
                        faits += 1
        i = j
    return chemin, faits


def familles_de_la_gamme(tonalite):
    """Les six accords parfaits d'une gamme (I ii iii IV V vi), plus le V majeur du relatif mineur."""
    T = (tonalite["tonique"] + (3 if tonalite["mode"] == "mineur" else 0)) % 12       # tonique du ton majeur
    return {2 * T, 2 * ((T + 5) % 12), 2 * ((T + 7) % 12), 2 * ((T + 2) % 12) + 1, 2 * ((T + 4) % 12) + 1,
            2 * ((T + 9) % 12) + 1, 2 * ((T + 4) % 12)}


def lire_grille(texte_ou_liste):
    """Accords d'une grille fournie (notation Do Ré Mi ou C D E), dans l'ordre : [(fondamentale, qualité)]."""
    jetons = texte_ou_liste if isinstance(texte_ou_liste, list) else re.split(r"[\s|,;\-–—]+", str(texte_ou_liste))
    noms = {"do": "C", "ré": "D", "re": "D", "mi": "E", "fa": "F", "sol": "G", "la": "A", "si": "B"}
    sortie = []
    for j in jetons:
        j = str(j).strip().replace("♯", "#").replace("♭", "b")
        m = re.match(r"^(Do|Ré|Re|Mi|Fa|Sol|La|Si)(?![a-zé])", j)          # majuscule exigée : « la », « si » sont aussi des mots
        if m:
            j = noms[m.group(1).lower()] + j[m.end():]
        a = lire_accord(j) if re.match(r"^[A-G](#|b)?", j) else None
        if a:
            sortie.append((a[0], a[1]))
            if re.match(r"^[A-G](#|b)?5(/|$)", j):                      # accord sans tierce (« D5 ») : ni majeur ni mineur
                sortie.append((a[0], "min"))
    return sortie


def caler_grille(grille, chemin, tonalites=()):
    """Une grille trouvée ailleurs peut être écrite dans un autre ton (capodastre, autre version), ou être celle d'un
    autre morceau. On la compare donc en degrés : sa tonalité est estimée d'après ses propres accords, celle du morceau
    d'après ce qu'on entend (plus les `tonalites` d'autres sources), et la grille est transposée de l'une à l'autre.
    Un I-IV-V en Sol et un I-IV-V en Si sont alors la même chose ; une grille dont les degrés diffèrent ne passe pas,
    même si une autre transposition la ferait coïncider par hasard.
    On ne l'écoute que si elle ressemble au morceau dans les deux sens : ses accords couvrent 70 % des temps
    entendus, et 60 % de ses accords sont réellement entendus (au moins 2 % des temps chacun). Sinon None.
    Renvoie (familles de la grille transposée, demi-tons, part des temps couverts, part de ses accords entendus)."""
    fam = lambda r, q: 2 * (r % 12) + (1 if q in FAMILLE_MINEURE else 0)
    majeur = lambda t: (t["tonique"] + (3 if t["mode"] == "mineur" else 0)) % 12       # tonique de la gamme majeure
    entendus = np.bincount(chemin[chemin < 24], minlength=24).astype(float)
    if len({fam(r, q) for r, q in grille}) < 3 or not entendus.sum():
        return None
    ton_grille = estimer_tonalite([(r, "min" if q in FAMILLE_MINEURE else "maj") for r, q in grille])
    ici = [estimer_tonalite([(int(c) // 2, "min" if c % 2 else "maj") if c < 24 else None for c in chemin])] + list(tonalites)
    if not ton_grille or not any(ici):
        return None
    meilleur = None
    for d in sorted({(majeur(t) - majeur(ton_grille)) % 12 for t in ici if t}):
        F = {fam(r + d, q) for r, q in grille}
        couverte = float(sum(entendus[f] for f in F) / entendus.sum())
        entendue = float(np.mean([entendus[f] >= 0.02 * entendus.sum() for f in F]))
        note = min(couverte, entendue + 0.1)
        if meilleur is None or note > meilleur[4] + 1e-9:
            meilleur = (F, d, couverte, entendue, note)
    return meilleur[:4] if meilleur[2] >= 0.7 and meilleur[3] >= 0.6 else None


def arbitrage_par_les_notes(lectures, notes, pos, autres=(), variantes=None, pedale=None):
    """Là où les moteurs se contredisent, on retourne aux notes elles-mêmes, mais seulement pour départager les
    accords qu'ils proposent : chaque candidat est noté d'après la présence de sa fondamentale, de sa tierce et de sa
    quinte dans ce qu'on entend (hors basse), et l'absence de la tierce opposée. Utilisé seul sur tout le morceau, ce
    procédé confond un accord et son relatif ; restreint aux candidats des moteurs, il sert de juge de paix.
    autres : suites d'accords par temps à compter aussi parmi les candidats (ce que proposent les reprises).
    variantes ((temps, 25)) : accords que les autres passages de la même section portent au même endroit. Deux
    reprises d'une section qui ne portent pas le même accord, c'est aussi un désaccord, même si les moteurs sont
    unanimes sur chacune : les notes sont alors consultées entre l'accord lu ici et celui des reprises, sur la mesure
    plutôt que sur le temps, et avec un poids moitié moindre (une reprise peut réellement varier).
    Renvoie (log-probabilités (temps, 25) nulles hors des temps contestés, masque des temps contestés)."""
    n = len(pos)
    voix = np.zeros((n, 25))
    tenue = None if pedale is None else np.asarray(pedale)
    for k, (F, connu) in lectures.items():
        c = F.argmax(1)
        ok = np.ones(n, bool) if connu is None else connu
        poids = np.full(n, POIDS_MOTEURS[k])
        if tenue is not None:                                # sur une basse tenue, lire l'accord de la basse pèse peu :
            if k == "sans_basse":                            # ce n'est pas une vraie contradiction avec qui lit autre chose
                poids = np.where(tenue >= 0, poids, 0.0)
            else:
                poids = np.where((tenue >= 0) & (c < 24) & (c // 2 == tenue), poids * POIDS_SUR_PEDALE, poids)
        elif k == "sans_basse":
            poids = poids * 0.0
        np.add.at(voix, (np.arange(n)[ok], c[ok]), poids[ok])
    part = voix.max(1) / np.maximum(voix.sum(1), 1e-9)
    conteste = part < 0.6
    A = np.zeros((n, 25))
    varie = np.zeros(n, bool) if variantes is None else (variantes[:, :24].sum(1) > 0) & ~conteste
    if notes is None or not (conteste.any() or varie.any()):
        return A, conteste
    # Sur une basse tenue, les notes ne sont pas consultées : même la basse retirée, ses harmoniques y dominent et
    # donnent toujours raison à l'accord de la basse (constaté sur le banc d'essai). Moteurs et reprises y décident seuls.
    libre = np.ones(n, bool) if pedale is None else np.asarray(pedale) < 0
    S = np.empty((n, 24))
    for r in range(12):
        f, q, M, m = notes[:, r], notes[:, (r + 7) % 12], notes[:, (r + 4) % 12], notes[:, (r + 3) % 12]
        S[:, 2 * r] = f + 0.9 * q + 0.8 * M - 0.5 * m
        S[:, 2 * r + 1] = f + 0.9 * q + 0.8 * m - 0.5 * M
    for i in np.flatnonzero(conteste & libre):
        a, b = max(0, i - 2), min(n, i + 3)
        candidats = sorted({int(c) for c in np.flatnonzero(voix[a:b].sum(0) > 0) if c < 24}
                           | {int(c) for suite in autres for c in suite[a:b] if c < 24})
        if len(candidats) < 2:
            continue
        note = 8.0 * S[max(0, i - 1):min(n, i + 2)][:, candidats].mean(0)       # lissé sur trois temps
        note = note - note.max()
        logp = note - np.log(np.exp(note).sum())
        A[i, :] = logp.min() - 1.0                                              # hors candidats : écarté
        A[i, candidats] = logp
        A[i, 24] = logp.min()
    pos = np.asarray(pos)
    for i in np.flatnonzero(varie & libre):
        a = i
        while a > 0 and pos[a] != 0 and varie[a - 1]:
            a -= 1
        b = i + 1
        while b < n and pos[b] != 0 and varie[b]:
            b += 1
        candidats = sorted({int(voix[i].argmax())} - {24} | {int(c) for c in np.flatnonzero(variantes[i, :24] > 0)})
        if len(candidats) < 2:
            continue
        note = 4.0 * S[a:b][:, candidats].mean(0)                               # la mesure entière, à mi-poids
        note = note - note.max()
        A[i, candidats] = note - np.log(np.exp(note).sum())
        autres_c = [c for c in range(25) if c not in candidats]
        A[i, autres_c] = A[i, candidats].min()
    return A, conteste


def recouper_accords(lectures, pos, longueur, detail=None, couleurs=None, pedale=None, journal=None,
                     notes=None, tonalite_externe=None, grille=None, basse=None, relire=None):
    """Un accord par temps, par recoupement de plusieurs lectures.
    lectures : {moteur: (log-probabilités (temps, 25), temps renseignés ou None)}.
    Les lectures sont additionnées (chacune avec son poids), puis on cherche la meilleure suite d'accords sachant
    qu'un changement est probable sur un premier temps, possible au milieu de la mesure, rare ailleurs.
    La qualité fine (7e, sus4…) vient de BTC (`detail`, (temps, 169)), choisie une fois par plage d'accord.
    Une couleur (7e, sus4, dim…) n'est écrite que si un autre moteur (`couleurs` : {moteur: [(fondamentale, qualité)
    par temps]}) la lit aussi sur la moitié de la plage au moins : BTC seul en entend là où il n'y en a pas.
    Pédale (`pedale` : note tenue par la basse isolée sur chaque temps, ou -1) : quand la basse garde longtemps la
    même note, un moteur qui lit l'accord de cette note ne fait peut-être que suivre la basse ; son avis compte alors
    quatre fois moins, ce qui laisse passer les accords qui changent au-dessus (Fa, Sol/Fa, Fa…).
    D'autres canaux corroborent le vote, chacun avec un poids modeste :
    - `notes` ((temps, 12), ce qu'on entend hors basse) : juge de paix entre les candidats là où les moteurs se
      contredisent (voir arbitrage_par_les_notes) ;
    - la gamme : quand la tonalité tirée de nos accords et celle d'une autre source (`tonalite_externe`) désignent
      la même gamme, ses accords reçoivent un léger avantage ; sans cette concordance, aucun ;
    - `grille` : grilles d'accords trouvées en ligne ([(accords, origine)]). Celle qui, une fois transposée au mieux,
      ressemble le plus à ce qu'on entend voit ses accords favorisés et les autres défavorisés ; si aucune ne
      ressemble assez, elles sont toutes ignorées ;
    - les reprises d'une même grille dans le morceau (voir appuyer_sur_les_reprises).
    La confiance d'un accord est la part des moteurs qui, chacun de son côté, lisaient la même famille.
    Renvoie ([(fondamentale, qualité, confiance) ou None], infos)."""
    pos = np.asarray(pos)
    n = len(pos)
    nb_btc = max(1, sum(1 for k in lectures if k.startswith("btc")))
    suit = {}                                                           # temps où un moteur lit l'accord de la note pédale
    if pedale is not None:
        pedale = np.asarray(pedale)
        for k, (F, _) in lectures.items():
            c = F.argmax(1)
            suit[k] = (pedale >= 0) & (c < 24) & (c // 2 == pedale)
    # La lecture « sans la basse » (BTC sur la musique privée de sa basse) ne compte que sur une basse tenue : c'est là
    # que les autres moteurs suivent la basse, et qu'elle seule entend l'accord du dessus (Si♭ sur pédale de Do).
    def poids_de(k, F):
        if k == "sans_basse":
            return F * (np.where(pedale >= 0, 1.0, 0.0) if pedale is not None else 0.0)[:, None] if pedale is not None else F * 0.0
        return F * np.where(suit[k], POIDS_SUR_PEDALE, 1.0)[:, None] if k in suit else F
    L = sum(POIDS_MOTEURS[k] * poids_de(k, F) for k, (F, _) in lectures.items()) / nb_btc

    def chemin_de(L):
        cout = np.where(pos == 0, COUT_CHANGEMENT[0], np.where((longueur % 2 == 0) & (pos == longueur // 2), COUT_CHANGEMENT[1], COUT_CHANGEMENT[2]))
        score, retour = L[0].copy(), np.zeros((n, 25), int)
        for i in range(1, n):
            meilleur = int(np.argmax(score))
            changer = score[meilleur] - cout[i]
            retour[i] = np.where(score >= changer, np.arange(25), meilleur)
            score = np.maximum(score, changer) + L[i]
        chemin = np.zeros(n, int)
        chemin[-1] = int(np.argmax(score))
        for i in range(n - 1, 0, -1):
            chemin[i - 1] = retour[i, chemin[i]]
        return chemin

    chemin = chemin_de(L)
    canaux, reprises_infos, conteste = {}, None, None
    nom_f = lambda c: "-" if c >= 24 else nom_accord((int(c) // 2, "min" if c % 2 else "maj"))
    try:
        # 1. Les reprises : là où les moteurs se contredisent, les autres passages de la même grille proposent leur lecture.
        _, conteste = arbitrage_par_les_notes(lectures, None, pos, pedale=pedale)
        canaux["temps_contestes"] = int(conteste.sum())
        depart, propose = chemin, chemin
        L_appuye, avec, variantes = appuyer_sur_les_reprises(L, chemin, pos, longueur, conteste=conteste, pedale=pedale)
        if avec:
            propose = chemin_de(L_appuye)
            # Second passage : les reprises sont recherchées d'après la grille déjà appuyée, où les passages mal lus
            # ressemblent davantage à leurs reprises ; les lectures mêlées restent celles des moteurs.
            L_second, avec2, variantes2 = appuyer_sur_les_reprises(L, propose, pos, longueur, conteste=conteste, pedale=pedale)
            if avec2 >= avec:
                L_appuye, avec, variantes, propose = L_second, avec2, variantes2, chemin_de(L_second)
            L = L_appuye
            reprises_infos = {"mesures_avec_reprises": avec, "temps_changes": int((propose != depart).sum())}
        # 2. Les notes départagent les candidats : ceux des moteurs et celui des reprises.
        varie = (variantes[:, :24].sum(1) > 0) & ~conteste
        if avec:
            reprises_infos["temps_qui_varient_entre_reprises"] = int(varie.sum())
        if notes is not None and (conteste.any() or varie.any()):
            L = L + arbitrage_par_les_notes(lectures, notes, pos, autres=(propose, depart), variantes=variantes, pedale=pedale)[0]
            canaux["arbitrage_par_les_notes"] = True
        # 3. La gamme, si deux sources indépendantes la désignent.
        ici = estimer_tonalite([(int(c) // 2, "min" if c % 2 else "maj") if c < 24 else None for c in depart])
        if ici and tonalite_externe and familles_de_la_gamme(ici) == familles_de_la_gamme(tonalite_externe):
            # Un morceau peut changer de tonalité en route (dernier refrain un ton plus haut) : l'avantage n'est donné
            # qu'aux passages dont les accords, pris seize mesures à la fois, désignent bien cette gamme.
            prime = np.zeros(25)
            prime[list(familles_de_la_gamme(ici))] = 0.25
            dans_le_ton, large = np.ones(n, bool), 8 * longueur
            for a in range(0, n, longueur):
                local = estimer_tonalite([(int(c) // 2, "min" if c % 2 else "maj") if c < 24 else None
                                          for c in depart[max(0, a - large):a + large]])
                dans_le_ton[a:a + longueur] = not local or familles_de_la_gamme(local) == familles_de_la_gamme(ici)
            L = L + np.where(dans_le_ton[:, None], prime, 0.0)
            canaux["gamme_concordante"] = nom_accord((ici["tonique"], "min" if ici["mode"] == "mineur" else "maj"))
            if not dans_le_ton.all():
                canaux["temps_dans_une_autre_tonalite"] = int((~dans_le_ton).sum())
        elif ici and tonalite_externe:
            canaux["gamme_concordante"] = None                          # deux tonalités différentes : pas d'avantage
        # 4. Les grilles trouvées en ligne : on n'écoute que celle qui ressemble le plus au morceau, si elle lui ressemble.
        if grille:
            calees = [(caler_grille(accords, depart, [tonalite_externe] if tonalite_externe else ()), origine) for accords, origine in grille]
            calees = [(c, o) for c, o in calees if c]
            if calees:
                calee, origine = max(calees, key=lambda x: min(x[0][2], x[0][3]))
                prime = np.full(25, -0.4)
                prime[list(calee[0])], prime[24] = 0.4, 0.0
                L = L + prime
                canaux["grille_en_ligne"] = {"origine": origine, "accords": len(calee[0]), "transposition_demi_tons": calee[1],
                                             "part_couverte": round(calee[2], 2), "part_de_ses_accords_entendus": round(calee[3], 2),
                                             "grilles_lues": len(grille), "grilles_ressemblantes": len(calees)}
            else:
                canaux["grille_en_ligne"] = f"{len(grille)} lues, aucune ne ressemble assez à ce qu'on entend"
        chemin = chemin_de(L)
        if basse is not None and relire is not False:            # accords renversés pris pour l'accord de leur basse
            chemin, relus = reinterpreter_sur_la_basse(chemin, L, basse, pos, ici or tonalite_externe, journal=journal, **(relire or {}))
            canaux["accords_relus_sur_leur_basse"] = relus
        canaux["temps_changes_par_les_canaux"] = int((depart != chemin).sum())
        if journal is not None and (depart != chemin).any():
            journal.append("canaux : " + ", ".join(f"temps {int(i)} {nom_f(depart[i])}→{nom_f(chemin[i])}" for i in np.flatnonzero(depart != chemin)[:80]))
    except Exception:
        traceback.print_exc()
    # Un même accord écrit tantôt majeur, tantôt mineur : quand l'une des deux écritures est nettement minoritaire
    # (un tiers des temps au plus) et qu'un moteur au moins lisait l'autre à cet endroit, on s'aligne sur la plus
    # fréquente. Une guitare sans tierce franche se lit « majeur » par défaut (Ré au lieu de Rém). Un morceau qui
    # joue vraiment les deux (Do puis Dom) les garde : tous les moteurs y lisent le changement.
    choix_moteurs = {k: F.argmax(1) for k, (F, _) in lectures.items()}
    for r in range(12):
        nM, nm = int((chemin == 2 * r).sum()), int((chemin == 2 * r + 1).sum())
        if not nM or not nm or min(nM, nm) > 0.35 * (nM + nm):
            continue
        rare, courant = (2 * r, 2 * r + 1) if nM < nm else (2 * r + 1, 2 * r)
        i = 0
        while i < n:
            j = i
            while j < n and chemin[j] == chemin[i]:
                j += 1
            if chemin[i] == rare and any(float((c[i:j] == courant).mean()) >= 0.5 for c in choix_moteurs.values()):
                if journal is not None:
                    journal.append(f"{nom_accord((r, 'min' if rare % 2 else 'maj'))} écrit {nom_accord((r, 'min' if courant % 2 else 'maj'))} "
                                   f"(temps {i}-{j - 1}) : écriture habituelle de cet accord dans le morceau")
                chemin[i:j] = courant
            i = j
    accord = np.zeros(n)                                                # moteurs d'accord avec le choix final
    presents = np.zeros(n)
    for k, (F, connu) in lectures.items():
        connu = np.ones(n, bool) if connu is None else connu
        if k in suit:                                                   # un moteur qui suivait la basse n'est pas compté contre l'accord retenu
            connu = connu & ~(suit[k] & (F.argmax(1) != chemin))
        accord += (F.argmax(1) == chemin) & connu
        presents += connu
    resultat, i = [None] * n, 0
    while i < n:
        j = i
        while j < n and chemin[j] == chemin[i]:
            j += 1
        c = int(chemin[i])
        if c < 24:
            r, mineur = c // 2, c % 2 == 1
            cols = MINEURES if mineur else MAJEURES
            q = QUALITES[cols[int(np.argmax(detail[i:j, r * 14:(r + 1) * 14][:, cols].sum(0)))]] if detail is not None \
                else ("min" if mineur else "maj")
            if q not in ("maj", "min") and couleurs:
                appuis = [k for k, liste in couleurs.items()
                          if sum(1 for x in range(i, j) if liste[x] == (r, q)) >= 0.5 * (j - i)]
                if not appuis:
                    simple = "min" if mineur else "maj"
                    if journal is not None:
                        journal.append(f"{nom_accord((r, q))} écrit {nom_accord((r, simple))} (temps {i}-{j - 1}) : couleur lue par BTC seul")
                    q = simple
            a, p = float(accord[i:j].sum()), float(max(1.0, presents[i:j].sum()))
            conf = a / p - (0.05 if 2 * a <= p else 0.0)                # la moitié des moteurs ou moins : accord douteux
            for x in range(i, j):
                resultat[x] = (r, q, round(max(0.0, conf), 2))
        i = j
    infos = {"moteurs": sorted(lectures), "accord_moyen": round(float((accord / np.maximum(1, presents)).mean()), 2),
             "accords_douteux": round(float(np.mean([a is not None and a[2] < 0.5 for a in resultat])), 2)}
    if reprises_infos:
        infos["reprises"] = reprises_infos
    if canaux:
        infos["canaux"] = canaux
    seuls = {k: v for k, v in lectures.items() if k.startswith("btc")}
    if seuls and len(seuls) < len(lectures):                           # ce que le recoupement a changé par rapport à BTC seul
        infos["temps_changes_par_recoupement"] = round(float((chemin_de(
            sum(POIDS_MOTEURS[k] * F for k, (F, _) in seuls.items()) / nb_btc) != chemin).mean()), 2)
    return resultat, infos


# ------------------------------------------------------------------------ basse
def isoler_basse(wav):
    """Isole la piste de basse (Demucs) : sur elle, la note tenue par la basse se lit sans ambiguïté, alors que dans
    le mélange la grosse caisse et le bas des guitares la brouillent. Renvoie le chemin de la piste."""
    sortie = TRAVAIL / "pistes"
    r = subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "bass", "-d", "cpu",
                        "-o", str(sortie), str(wav)], capture_output=True, text=True)
    piste = sortie / "htdemucs" / Path(wav).stem / "bass.wav"
    if r.returncode != 0 or not piste.exists():
        raise RuntimeError((r.stderr.strip().splitlines() or ["échec Demucs"])[-1][:300])
    return piste


def basse_par_temps(y, sr, temps, accordage=0.0):
    """Quelle note tient la basse sur chaque temps ? On mesure l'énergie des fondamentales graves (do1 à ré3) en leur
    ajoutant celle de leurs harmoniques, après avoir gommé les coups brefs (grosse caisse).
    Renvoie (temps, 12) : part de chaque note dans le grave."""
    import librosa
    from scipy.ndimage import median_filter
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=1024, fmin=librosa.note_to_hz("C1"), n_bins=60, bins_per_octave=12,
                           tuning=accordage))
    C = median_filter(C, size=(1, 7))
    S = C[:27] + 0.5 * C[12:39] + 0.33 * C[19:46] + 0.25 * C[24:51]     # saillance de chaque fondamentale candidate
    temps = np.asarray(temps, float)
    fins = np.append(temps[1:], temps[-1] + (np.median(np.diff(temps)) if len(temps) > 1 else 0.5))
    centres = (np.arange(S.shape[1]) + 0.5) * 1024 / sr
    parts = np.zeros((len(temps), 12))
    for i in range(len(temps)):
        dans = (centres >= temps[i]) & (centres < fins[i])
        if dans.any():
            s = (S[:, dans] ** 2).sum(1)
            for k in range(27):
                parts[i, k % 12] += s[k]
    return parts / (parts.sum(1, keepdims=True) + 1e-12)


def notes_pedales(parts, duree=6, force=0.3):
    """Note que la basse tient sur chaque temps quand elle la garde au moins `duree` temps de suite (un temps
    illisible isolé ne rompt pas la tenue), sinon -1."""
    notes = np.where(parts.max(1) >= force, parts.argmax(1), -1)
    for i in range(1, len(notes) - 1):
        if notes[i] < 0 and notes[i - 1] >= 0 and notes[i - 1] == notes[i + 1]:
            notes[i] = notes[i - 1]
    sortie, i = np.full(len(notes), -1), 0
    while i < len(notes):
        j = i
        while j < len(notes) and notes[j] == notes[i]:
            j += 1
        if notes[i] >= 0 and j - i >= duree:
            sortie[i:j] = notes[i]
        i = j
    return sortie


def poser_basses(accords, parts, parts2=None, moteur=None, journal=None, fiable=False):
    """Accords renversés : quand, sous un accord, la basse tient une autre note que la fondamentale pendant au moins
    deux temps (ou tout l'accord), on la note (Do/Sol). Jusqu'à trois lectures de la basse sont recoupées :
    - parts  : énergie grave de la piste principale (l'accompagnement sans la voix quand on l'a) ;
    - parts2 : la même mesure sur une autre piste (le mix) ;
    - moteur : la basse lue par ChordNet, par temps (-1 : pas de renversement).
    Une basse nette (plus exigeante si la note n'appartient pas à l'accord) doit être confirmée par une autre lecture ;
    une basse seulement probable est retenue si ChordNet la donne aussi.
    fiable : `parts` vient de la piste de basse isolée. Une basse très nette y suffit alors sans autre confirmation,
    et une pédale (la basse garde la note qu'elle tenait sous l'accord précédent) est notée dès qu'elle est probable :
    La, Fa/La, Sol/La. Quand la basse tient la septième d'un accord de septième, on écrit l'accord simple sur sa
    basse (Sol/Fa plutôt que Sol7) : c'est la basse qui apportait cette septième.
    Renvoie (accords avec basse : (fondamentale, qualité, confiance, basse ou None), nombre de renversements)."""
    n, sortie, i, nb = len(accords), [None] * len(accords), 0, 0
    tenue = None                                             # note que tenait la basse à la fin de l'accord précédent
    lue = parts.argmax(1)
    for x in range(1, n - 1):                                # un temps illisible entre deux temps de même basse la garde
        if parts[x].max() < 0.3 and lue[x - 1] == lue[x + 1]:
            lue[x] = lue[x - 1]
    autres = (parts2 is not None) + (moteur is not None)
    while i < n:
        j = i
        while j < n and (accords[j][:2] if accords[j] else None) == (accords[i][:2] if accords[i] else None):
            j += 1
        if accords[i]:
            r, q = accords[i][:2]
            notes_accord = {(r + x) % 12 for x in INTERVALLES[q]} | {(r + 10) % 12}
            b = lue[i:j]
            for x in range(i, j):
                sortie[x] = (*accords[x][:3], None)
            a = i
            while a < j:
                z = a
                while z < j and b[z - i] == b[a - i]:
                    z += 1
                note = int(b[a - i])
                force, racine = float(parts[a:z, note].mean()), float(parts[a:z, r].mean())
                assez_long = (z - a) >= 2 or (z - a) == (j - i)
                nette = force >= (0.40 if note in notes_accord else 0.55) and force >= 1.8 * racine
                probable = force >= 0.30 and force >= 1.3 * racine
                v2 = parts2 is not None and int(parts2[a:z].mean(0).argmax()) == note
                vm = moteur is not None and float(np.mean(np.asarray(moteur[a:z]) == note)) >= 0.5
                pedale = fiable and note == tenue and probable
                seule = fiable and force >= 0.6 and force >= 2.5 * racine
                if note != r and assez_long and ((nette and (v2 or vm or not autres)) or (probable and vm) or seule or pedale):
                    nb += 1
                    q2 = {"7": "maj", "min7": "min"}.get(q, q) if note == (r + 10) % 12 else "maj" if (q == "maj7" and note == (r + 11) % 12) else q
                    for x in range(a, z):
                        sortie[x] = (accords[x][0], q2, accords[x][2], note)
                    if journal is not None:
                        journal.append(f"{nom_accord((r, q2, 0, note))} sur {z - a} temps (basse {force:.2f}, fondamentale {racine:.2f}"
                                       f"{', autre piste' if v2 else ''}{', ChordNet' if vm else ''}{', pédale' if pedale else ''})")
                if force >= 0.3:
                    tenue = note
                a = z
        i = j
    return sortie, nb


def caler_sur_le_premier_temps(accords, pos, longueur, journal=None):
    """Les accords changent normalement sur le premier temps de la mesure. Deux retouches (voir aussi plus bas, le
    changement entendu un temps trop tôt). Une mesure coupée en son milieu dont la
    première moitié prolonge exactement l'accord de la mesure précédente (même accord, même basse) n'est le plus
    souvent qu'un changement entendu en retard ou en avance : il est ramené sur un premier temps. Si la mesure
    suivante commence par le nouvel accord, c'est lui qui arrivait trop tôt et l'ancien garde toute la mesure ;
    sinon le nouvel accord prend toute la mesure (il disparaîtrait autrement).
    Deux vrais accords dans une mesure (Mi puis Fa♯ après Do♯m, basse qui descend) ne sont pas touchés : leur première
    moitié n'est pas l'accord d'avant."""
    cle = lambda a: None if a is None else (a[0], a[1], a[3] if len(a) > 3 else None)
    pos, sortie, n, faits, moitie = np.asarray(pos), list(accords), len(accords), 0, longueur // 2
    # Accord entendu un temps trop tôt : il commence sur le dernier temps d'une mesure et se prolonge dans la suivante
    # (les chansons anticipent souvent le changement d'une croche). Il est écrit sur le premier temps qui suit.
    # Un accord de passage d'un seul temps (Solm7, Lam7 sur le 4e temps, puis Si♭M) n'est pas concerné : il ne dure pas.
    for d in np.flatnonzero(pos == 0):
        d = int(d)
        if d < 2 or longueur < 3 or pos[d - 1] != longueur - 1 or pos[d - 2] != longueur - 2:
            continue
        if sortie[d] is not None and sortie[d - 2] is not None and cle(sortie[d - 1]) == cle(sortie[d]) != cle(sortie[d - 2]) \
                and (d + 1 >= n or cle(sortie[d + 1]) == cle(sortie[d])):
            sortie[d - 1] = sortie[d - 2]
            faits += 1
            if journal is not None:
                journal.append(f"temps {d - 1} : changement entendu un temps trop tôt, ramené sur le premier temps ({nom_accord(sortie[d])})")
    if longueur % 2 or longueur < 4:
        return sortie, faits
    for d in np.flatnonzero(pos == 0):
        d = int(d)
        if d == 0 or d + longueur > n or not (pos[d:d + longueur] == np.arange(longueur)).all():
            continue
        A, B = [cle(x) for x in sortie[d:d + moitie]], [cle(x) for x in sortie[d + moitie:d + longueur]]
        if len(set(A)) != 1 or len(set(B)) != 1 or A[0] == B[0] or A[0] is None or B[0] is None or A[0] != cle(sortie[d - 1]):
            continue
        suivant = cle(sortie[d + longueur]) if d + longueur < n else None
        if suivant == B[0]:
            sortie[d + moitie:d + longueur] = [sortie[d]] * moitie
        else:
            sortie[d:d + moitie] = [sortie[d + moitie]] * moitie
        faits += 1
        if journal is not None:
            journal.append(f"temps {d}-{d + longueur - 1} : changement ramené sur le premier temps ({nom_accord(sortie[d])})")
    return sortie, faits


def pedale_au_dessus(accords, notes, pos, longueur, journal=None, part_mini=0.5, rang_maxi=4.0):
    """Pédale tenue par un autre instrument que la basse : une guitare répète la tonique pendant qu'une autre change
    d'accord et que la basse la suit (Dom, Dom, Dom, La♭M… avec un Do répété tout du long). On l'écrit comme une
    pédale : La♭M/Do.
    Reconnue quand un ou deux mesures d'un autre accord s'intercalent entre deux accords de même fondamentale p, et
    que p reste présente dans le médium (`notes`, ce qu'on entend hors basse) : au moins la moitié de sa part dans les
    accords voisins, et parmi les quatre notes les plus fortes. Sans pédale, la tierce d'un accord n'y atteint pas le
    tiers de cette part (mesuré sur le banc d'essai). Les deux accords voisins doivent durer une mesure au moins.
    Jamais quand p est la quinte de l'accord intercalé (Fam entre deux Dom) : une quinte sonne toujours aussi fort que
    la note tenue, on ne peut pas les distinguer, et la basse fait alors foi.
    Renvoie (accords, nombre de plages réécrites)."""
    if notes is None:
        return accords, 0
    N = np.asarray(notes, float)
    N = N / (N.sum(1, keepdims=True) + 1e-12)
    sortie, n, faits = list(accords), len(accords), 0
    plages, i = [], 0                                       # plages d'un même accord (fondamentale, qualité, basse)
    cle = lambda a: None if a is None else (a[0], a[1], a[3] if len(a) > 3 else None)
    while i < n:
        j = i
        while j < n and cle(sortie[j]) == cle(sortie[i]):
            j += 1
        plages.append((i, j))
        i = j
    racine = lambda k: None if sortie[plages[k][0]] is None else sortie[plages[k][0]][0]
    part = lambda a, b, p: float(N[a:b, p].mean())
    rang = lambda a, b, p: float(np.mean([(N[x] > N[x, p]).sum() + 1 for x in range(a, b)]))
    k = 1
    while k < len(plages) - 1:
        p = racine(k - 1)
        fin = k                                             # une ou plusieurs plages intercalées, deux mesures au plus
        while fin < len(plages) - 1 and racine(fin) is not None and racine(fin) != p and plages[fin][1] - plages[k][0] <= 2 * longueur:
            fin += 1
        if p is None or fin == k or racine(fin) != p or plages[fin - 1][1] - plages[k][0] > 2 * longueur:
            k += 1
            continue
        a, b = plages[k][0], plages[fin - 1][1]
        voisins = [x for x in list(range(max(plages[k - 1][0], a - 2 * longueur), a)) + list(range(b, min(plages[fin][1], b + 2 * longueur)))]
        reference = float(N[voisins, p].mean()) if voisins else 0.0
        # la note tenue doit être celle d'un accord installé : une mesure entière au moins de chaque côté
        installe = plages[k - 1][1] - plages[k - 1][0] >= longueur and plages[fin][1] - plages[fin][0] >= longueur
        ok = installe and reference >= 0.15 and part(a, b, p) >= part_mini * reference and rang(a, b, p) <= rang_maxi
        for q in range(k, fin):
            acc = sortie[plages[q][0]]
            basse = acc[3] if len(acc) > 3 else None
            if (p - acc[0]) % 12 == 7 or basse == p:        # p quinte de l'accord, ou déjà à la basse
                ok = False
        if ok:
            for x in range(a, b):
                sortie[x] = (*sortie[x][:3], p)
            faits += 1
            if journal is not None:
                journal.append(f"temps {a}-{b - 1} : pédale de {NOTES_MIXTES[p]} tenue au-dessus de la basse "
                               f"(part {part(a, b, p):.2f} contre {reference:.2f} autour, rang {rang(a, b, p):.1f})")
        k = fin
    return sortie, faits


def estimer_tonalite(accords):
    """Tonalité par profils de Krumhansl sur les notes des accords trouvés (sert à l'orthographe ♯/♭)."""
    histo = np.zeros(12)
    for a in accords:
        if a:
            for k, i in enumerate(INTERVALLES[a[1]]):
                histo[(a[0] + i) % 12] += 1.5 if k == 0 else 1.0
    if not histo.sum():
        return None
    majeur = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    mineur = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    scores = [(float(np.corrcoef(np.roll(p, t), histo)[0, 1]), t, m)
              for t in range(12) for p, m in ((majeur, "majeur"), (mineur, "mineur"))]
    _, tonique, mode = max(scores)
    return {"tonique": int(tonique), "mode": mode}


def nom_accord(a, tonalite=None):
    if not a:
        return ""
    relatif = None if not tonalite else (tonalite["tonique"] + (3 if tonalite["mode"] == "mineur" else 0)) % 12
    notes = NOTES_DIESES if relatif in TONS_DIESES else NOTES_MIXTES if relatif in (None, 0) else NOTES_BEMOLS
    # Hors des notes de la gamme (ou sans armure pour trancher), l'accord s'écrit selon sa nature : majeur en bémols
    # (Mi♭M, La♭M, Si♭M), mineur en dièses (Fa♯m, Sol♯m). Un accord de la gamme (I ii iii IV V vi) suit l'armure : Si♭m en La♭ majeur.
    if notes is NOTES_MIXTES or {0: False, 2: True, 4: True, 5: False, 7: False, 9: True}.get((a[0] - relatif) % 12) != (a[1] in FAMILLE_MINEURE):
        notes = NOTES_SANS_ARMURE_MINEUR if a[1] in FAMILLE_MINEURE else NOTES_SANS_ARMURE_MAJEUR
    return notes[a[0]] + SUFFIXES[a[1]] + ("/" + nom_basse(notes, a[0], a[3]) if len(a) > 3 and a[3] is not None else "")


def nom_basse(notes, r, b):
    """La basse s'écrit d'après l'accord qu'elle porte : la tierce de Si est Ré♯, pas Mi♭."""
    lettres, hauteurs = ["Do", "Ré", "Mi", "Fa", "Sol", "La", "Si"], [0, 2, 4, 5, 7, 9, 11]
    i = lettres.index(notes[r].rstrip("♯♭"))
    l = (i + [0, 1, 1, 2, 2, 3, 4, 4, 5, 5, 6, 6][(b - r) % 12]) % 7
    alt = (b - hauteurs[l] + 6) % 12 - 6
    nom = lettres[l] + ("♯" if alt > 0 else "♭" if alt < 0 else "")
    return notes[b] if abs(alt) > 1 or nom in ("Si♯", "Mi♯", "Do♭", "Fa♭") else nom       # jamais d'écriture savante


# ----------------------------------------------------------- placement des mots
_aligneur = None


def _charger_aligneur():
    global _aligneur
    if _aligneur is None:
        import torchaudio
        lot = torchaudio.pipelines.MMS_FA
        modele = lot.get_model(with_star=True)
        modele.eval()
        _aligneur = (modele, lot.get_dict(), lot.sample_rate)
    return _aligneur


def normaliser(mot, alphabet):
    m = mot.lower().replace("œ", "oe").replace("æ", "ae").replace("’", "'")
    m = "".join(c for c in unicodedata.normalize("NFD", m) if unicodedata.category(c) != "Mn")
    return "".join(c for c in m if c in alphabet and c not in "-*")


def aligner_segment(emission, mots_norm, joker, alphabet):
    """Alignement forcé d'une suite de mots sur un morceau d'émission, avec un joker avant et après
    qui absorbe ce qui n'appartient pas au texte. Renvoie [(image début, image fin, confiance)] par mot."""
    import torch
    import torchaudio
    jetons = [joker] + [alphabet[c] for m in mots_norm for c in m] + [joker]
    chemin, scores = torchaudio.functional.forced_align(emission, torch.tensor([jetons], dtype=torch.int32), blank=0)
    plages = [p for p in torchaudio.functional.merge_tokens(chemin[0], scores[0].exp()) if p.token != joker]
    resultat, k = [], 0
    for m in mots_norm:
        p = plages[k:k + len(m)]
        k += len(m)
        duree = max(1, sum(x.end - x.start for x in p))
        resultat.append((p[0].start, p[-1].end, float(sum(x.score * (x.end - x.start) for x in p) / duree)))
    return resultat


def aligner_bord(emission, affiches, normes, cote, joker, alphabet, seuil=0.35):
    """Ligne coupée par un bord de l'extrait : une partie de ses mots n'est pas dans l'audio.
    On essaie toutes les fins de ligne possibles (bord de début) ou tous les débuts (bord de fin),
    chacune alignée séparément, et on garde celle dont la confiance cumulée au-dessus du seuil est
    la plus forte. Renvoie (alignement, mots gardés), éventuellement vides."""
    n, meilleur = len(normes), (0.0, [], [])
    for k in range(n):
        choix = slice(k, n) if cote == "debut" else slice(0, n - k)
        try:
            al = aligner_segment(emission, normes[choix], joker, alphabet)
        except Exception:
            continue
        score = sum(c - seuil for _, _, c in al)
        if score > meilleur[0]:
            meilleur = (score, al, affiches[choix])
    return meilleur[1], meilleur[2]


def meilleur_enchainement(etats):
    """etats : [(n° de ligne, n° d'écart, début de la fenêtre en s, note, fin de la fenêtre)], triés par ligne. Choisit
    au plus un écart par ligne de sorte que les lignes retenues se suivent dans le temps (à écart égal, l'ordre des
    paroles suffit ; d'un écart à l'autre, une ligne ne commence qu'une fois la précédente finie, à une seconde près)
    et que la somme des notes soit la plus haute. Renvoie {n° de ligne: n° d'écart}."""
    meilleur, d_ou = [0.0] * len(etats), [-1] * len(etats)
    for x, (j, k, debut, note, _fin) in enumerate(etats):
        for y in range(x):
            ji, ki, di, _, fi = etats[y]
            if ji < j and (di <= debut if ki == k else fi - 1.0 <= debut) and meilleur[y] > meilleur[x]:
                meilleur[x], d_ou[x] = meilleur[y], y
        meilleur[x] += note
    choix, x = {}, int(np.argmax(meilleur)) if etats else -1
    while x >= 0:
        choix[etats[x][0]] = etats[x][1]
        x = d_ou[x]
    return choix


def placer_mots(voix_wav, lignes, extrait, decalage_connu=None, jusqua=None, indice=None, indices=None):
    """Renvoie ([(début, fin, mot, confiance, n° de ligne)], infos).
    Passe 1 (extraits seulement) : on essaie chaque passage des paroles et on garde celui qui colle
    le mieux, ce qui donne le décalage entre l'extrait et le morceau.
    Passe 2 : chaque ligne est alignée dans sa propre fenêtre, déduite de son horodatage. Les lignes
    coupées par un bord de l'extrait perdent les mots qui ne sont pas dans l'audio.
    decalage_connu : écart déjà mesuré entre les horodatages des paroles et ce fichier (on ne le remesure pas).
    indice : le même écart, estimé d'après les mots entendus par Whisper. Il confirme une mesure peu sûre, ou la
    remplace : sans lui, des paroles décalées de dix secondes mais mesurées avec un score un peu faible restaient
    décalées, et la fin du texte tombait hors du morceau. Les écarts valent « horodatage des paroles − instant réel ».
    indices : le même écart ligne par ligne, quand la transcription laisse penser qu'il change en cours de morceau
    (paroles d'une autre édition : pont raccourci, refrain en moins). Chaque écart ainsi proposé est essayé pour
    chaque ligne sur le son, puis on retient l'enchaînement le mieux reconnu qui garde les lignes dans l'ordre des
    paroles : une ligne ne peut pas être chantée avant celle qui la précède. Une ligne qui ne trouve pas sa place
    dans cet ordre (refrain absent de cette édition) n'est pas posée.
    jusqua : ne traiter que les lignes qui finissent avant cet instant (première page d'un morceau entier)."""
    import torch
    import torchaudio
    modele, alphabet, sr = _charger_aligneur()
    joker = alphabet["*"]
    onde, sr0 = torchaudio.load(str(voix_wav))
    onde = onde.mean(0, keepdim=True)
    if sr0 != sr:
        onde = torchaudio.functional.resample(onde, sr0, sr)
    duree = onde.size(1) / sr
    morceaux = []
    with torch.inference_mode():
        for a in range(0, onde.size(1), 30 * sr):                      # par tranches de 30 s (mémoire)
            if onde.size(1) - a >= sr // 10:
                morceaux.append(modele(onde[:, a:a + 30 * sr])[0])
    emission = torch.cat(morceaux, dim=1)
    pas = duree / emission.size(1)

    textes = []                                                        # par ligne : [(affiché, normalisé)]
    for _, texte in lignes:
        paires = [(w, normaliser(w, alphabet)) for w in texte.split()]
        textes.append([(w, n) for w, n in paires if n])

    def reperer(em, duree_em, autour=None):
        """Cherche quel passage des paroles correspond à ce morceau d'émission.
        Renvoie (instant du morceau où commence ce morceau d'émission, score, score suivant, essais, passe 1)."""
        essais = []
        for i in range(len(lignes)):
            if not textes[i] or (autour is not None and abs(lignes[i][0] - autour) > 15.0):
                continue
            fin = lignes[i][0] + duree_em - 2.0
            groupe = [(w, n, j) for j in range(i, len(lignes)) if lignes[j][0] < fin for w, n in textes[j]]
            if len(groupe) < 4:
                continue
            try:
                al = aligner_segment(em, [n for _, n, _ in groupe], joker, alphabet)
            except Exception:
                continue
            essais.append((float(np.mean([c for _, _, c in al])), i, groupe, al))
        if not essais:
            return None
        essais.sort(key=lambda e: -e[0])
        score, _i0, groupe, al = essais[0]
        debuts, p1 = {}, {}
        for (w, n, j), (a, b, c) in zip(groupe, al):
            p1.setdefault(j, []).append((a * pas, b * pas, w, c))
            debuts.setdefault(j, a * pas)
        internes = sorted(debuts)[1:-1] or sorted(debuts)
        return (float(np.median([lignes[j][0] - debuts[j] for j in internes])), score,
                essais[1][0] if len(essais) > 1 else None, len(essais), p1)

    infos, passe1, decalage = {}, {}, 0.0
    if extrait:
        r = reperer(emission, duree)
        if r is None:
            raise RuntimeError("aucun passage des paroles n'a pu être aligné sur l'extrait")
        decalage, score, suivant, n_essais, passe1 = r
        infos = {"debut_extrait_dans_morceau": round(decalage, 1), "score": round(score, 2),
                 "score_suivant": round(suivant, 2) if suivant is not None else None, "passages_essayes": n_essais}
    elif decalage_connu is not None:
        decalage, infos = float(decalage_connu), {"decalage_paroles_s": round(float(decalage_connu), 2)}
    elif duree > 100:
        # Morceau entier : les horodatages des paroles peuvent être décalés d'une durée fixe par rapport
        # à ce fichier (autre édition, silence en tête). On le mesure sur 30 s prises vers le premier quart.
        chantees = [t for (t, _), tx in zip(lignes, textes) if tx and t >= 0.25 * duree]
        if chantees:
            c0 = max(0.0, chantees[0] - 2.0)
            r = reperer(emission[:, int(c0 / pas):int((c0 + 30.0) / pas)], 30.0, autour=c0 + 2.0)
            if r is not None:
                ecart = r[0] - c0
                infos = {"decalage_paroles_s": round(ecart, 2), "score": round(r[1], 2)}
                if r[1] >= 0.35 and 0.3 < abs(ecart) <= 25.0:
                    decalage = ecart
                elif indice is not None and abs(ecart - indice) <= 3.5 and 0.3 < abs(ecart) <= 25.0:
                    decalage, infos["confirme_par"] = ecart, "transcription"
        if decalage == 0.0 and indice is not None and 0.6 <= abs(indice) <= 25.0 and (not infos or infos.get("score", 0) < 0.35):
            decalage = float(indice)                                   # mesure absente ou peu sûre : on suit la transcription
            infos["decalage_retenu_s"], infos["d_apres"] = round(decalage, 2), "transcription"

    mots, blocs, retires, repris, deplacees = [], [], 0, 0, 0

    def essayer(j, d, d_suiv):
        """Aligne la ligne j dans la fenêtre que donne l'écart d. Renvoie (mots, début, fin, bord, mots retirés), ou None."""
        t_ligne = lignes[j][0]
        t_suiv = lignes[j + 1][0] if j + 1 < len(lignes) else t_ligne + 15.0
        a_th = t_ligne - d
        b_th = max(a_th + 1.0, min(t_suiv - d_suiv, a_th + 20.0))
        if b_th < 0.3 or a_th > duree - 0.3:
            return None                                                # ligne hors de l'audio
        if jusqua is not None and b_th > jusqua:
            return None                                                # au-delà de la première page
        a, b = max(0.0, a_th - 0.8), min(duree, b_th + 0.8)
        ia = int(a / pas)
        ib = max(ia + 2, int(b / pas))
        affiches, normes, ote = [w for w, _ in textes[j]], [n for _, n in textes[j]], 0
        coupe_debut = extrait and a_th < -0.2
        coupe_fin = extrait and t_suiv - d > duree + 0.2 and a_th > duree - 8.0
        try:
            if coupe_debut or coupe_fin:
                al, gardes = aligner_bord(emission[:, ia:ib], affiches, normes,
                                          "debut" if coupe_debut else "fin", joker, alphabet)
                ote, affiches = len(affiches) - len(gardes), gardes
            else:
                al = aligner_segment(emission[:, ia:ib], normes, joker, alphabet)
            ligne = [(a + s * pas, a + e * pas, w, c) for (s, e, c), w in zip(al, affiches)]
        except Exception:
            ligne = []
        return ligne, a_th, b_th, "debut" if coupe_debut else "fin" if coupe_fin else None, ote

    sure = lambda l: float(np.mean([m[3] for m in l])) if l else 0.0
    # Écarts à essayer : l'écart général, puis ceux que la transcription propose ailleurs dans le morceau.
    ecarts = [decalage]
    if indices and not extrait:
        for d in indices:
            if d is not None and abs(d) <= 120.0 and all(abs(d - e) > 3.0 for e in ecarts) and len(ecarts) < 5:
                ecarts.append(float(d))
    essais = {j: {k: essayer(j, d, d) for k, d in enumerate(ecarts)} for j in range(len(lignes)) if textes[j]}
    choix = {j: 0 for j, e in essais.items() if e[0] is not None}
    if len(ecarts) > 1:
        # Meilleur enchaînement : lignes dans l'ordre, instants croissants, somme des confiances la plus haute.
        # Une ligne n'est déplacée à un autre écart que si le son l'y reconnaît un minimum : sinon c'est du hasard.
        etats = [(j, k, e[1], sure(e[0]) + 0.05, e[2]) for j in sorted(essais) for k, e in essais[j].items()
                 if e is not None and (k == 0 or sure(e[0]) >= 0.1)]
        choix = meilleur_enchainement(etats)
        deplacees = sum(1 for k in choix.values() if k)
        absentes = [j for j in essais if j not in choix]
        infos.update({"ecarts_essayes_s": [round(e, 1) for e in ecarts], "lignes_non_posees": len(absentes)})
        print(f"    écarts essayés : {[round(e, 1) for e in ecarts]} ; {deplacees} lignes à un autre écart que le général, "
              f"{len(absentes)} lignes sans place dans l'ordre des paroles : {absentes}", flush=True)
    for j in sorted(choix):
        ligne, a_th, b_th, bord, ote = essais[j][choix[j]]
        retires += ote
        if bord is None and choix[j] == 0 and len(passe1.get(j, [])) == len(textes[j]) and (
                not ligne or np.mean([m[3] for m in passe1[j]]) > np.mean([m[3] for m in ligne]) + 0.03):
            ligne, repris = passe1[j], repris + 1                      # la passe 1 était meilleure pour cette ligne
        print(f"    ligne {j:>3} : fenêtre {a_th:6.1f} → {b_th:6.1f} s, {len(textes[j])} mots, {len(ligne)} gardés, "
              f"confiance {sure(ligne):.2f}"
              + (" (bord début)" if bord == "debut" else "") + (" (bord fin)" if bord == "fin" else "")
              + (f" (écart {ecarts[choix[j]]:+.1f} s)" if choix[j] else "")
              + ("" if not ligne else f", de {ligne[0][0]:.2f} à {ligne[-1][1]:.2f} s"), flush=True)
        blocs.append((j, ligne, bord))
    # Un mot rescapé d'une ligne coupée ne doit pas empiéter sur la ligne voisine, alignée en entier.
    for n, (j, ligne, bord) in enumerate(blocs):
        if bord == "debut":
            suivants = [m[0] for _, l, b in blocs[n + 1:] if b is None for m in l]
            if suivants:
                garde = [m for m in ligne if m[1] <= min(suivants) + 0.05]
                retires, ligne = retires + len(ligne) - len(garde), garde
        elif bord == "fin":
            precedents = [m[1] for _, l, b in blocs[:n] if b is None for m in l]
            if precedents:
                garde = [m for m in ligne if m[0] >= max(precedents) - 0.05]
                retires, ligne = retires + len(ligne) - len(garde), garde
        mots += [(d, f, w, round(c, 2), j) for d, f, w, c in ligne]
    if len(ecarts) > 1:
        # Les lignes viennent d'être mises dans l'ordre : leurs mots ne doivent pas s'entremêler d'une ligne à l'autre.
        for n in range(1, len(mots)):
            if mots[n][0] < mots[n - 1][0] + 0.02:
                d = mots[n - 1][0] + 0.02
                mots[n] = (d, max(d + 0.05, mots[n][1]), *mots[n][2:])
    else:
        mots.sort(key=lambda m: m[0])
    infos.update({"mots_hors_extrait_retires": retires, "lignes_reprises_passe1": repris})
    if deplacees:
        infos["lignes_a_ecart_local"] = deplacees
    return mots, infos


def recouper_mots(mots, entendus, proche=0.35, loin=0.6):
    """Compare chaque mot aligné (MMS) au même mot entendu par Whisper, s'il existe au voisinage.
    - Les deux lectures tombent d'accord (à 0,35 s) : le mot est confirmé, sa confiance remonte.
    - Elles divergent nettement (plus de 0,6 s) alors que notre alignement était lui-même peu sûr : si Whisper est
      sûr de lui, on prend son instant, à condition de ne pas bousculer l'ordre des mots ; sinon le mot est marqué
      douteux. Un mot que notre alignement plaçait avec assurance n'est pas remis en cause.
    Renvoie (mots, infos)."""
    if not entendus or not mots:
        return mots, None
    par_mot = {}
    for k, (d, f, w, p) in enumerate(entendus):
        par_mot.setdefault(mot_nu(w), []).append(k)
    pris, sortie, compares, confirmes, corriges, douteux = set(), [], 0, 0, 0, 0
    for i, (d, f, w, c, j) in enumerate(mots):
        cand = [k for k in par_mot.get(mot_nu(w), []) if k not in pris and abs(entendus[k][0] - d) <= 1.5]
        if cand:
            k = min(cand, key=lambda k: abs(entendus[k][0] - d))
            pris.add(k)
            dw, fw, _, pw = entendus[k]
            compares += 1
            ecart = abs(dw - d)
            if ecart <= proche:
                c, confirmes = max(c, 0.6), confirmes + 1
            elif ecart > loin:
                avant = sortie[-1][0] if sortie else 0.0
                apres = mots[i + 1][0] if i + 1 < len(mots) else dw + 1.0
                if c < 0.3 and pw >= 0.5 and avant <= dw <= apres:
                    d, f, c, corriges = dw, fw, 0.45, corriges + 1
                elif c < 0.5:
                    c, douteux = min(c, 0.25), douteux + 1
        sortie.append((d, f, w, round(float(c), 2), j))
    return sortie, {"mots_compares": compares, "confirmes": confirmes, "repris_de_whisper": corriges, "divergents": douteux,
                    "part_confirmee": round(confirmes / max(1, compares), 2)}


# ------------------------------------------------------------------- assemblage
def rendu_texte(mesures, tonalite, limite=None):
    """Grille lisible : une ligne par mesure, « / » = même accord, mots à droite."""
    lignes = []
    for m in mesures[:limite]:
        acc = " ".join((nom_accord((c["accord"]["r"], c["accord"]["q"], 0, c["accord"].get("b")), tonalite) if c["accord"] else "/").ljust(7)
                       for c in m["temps"])
        mots = " ".join(w["txt"] for c in m["temps"] for w in c["mots"])
        lignes.append(f"{m['n']:>3} | {acc}| {mots}".rstrip())
    return "\n".join(lignes)


# ------------------------------------------------------------------------ titre
def deja_complete(ident):
    """Vrai si une partition achevée existe déjà pour ce titre : on ne la remplace pas par des versions provisoires."""
    try:
        ancien = json.loads((RESULTATS / (ident + ".json")).read_text(encoding="utf-8"))
        return bool(ancien.get("etat") == "ok" and ancien.get("complet") and not ancien.get("provisoire"))
    except Exception:
        return False


def lectures_du_service(ident):
    """Lectures du service Magic Chords gardées par silencieux.py (temps, accords, mots entendus), s'il y en a."""
    def lire(nom):
        try:
            return json.loads((TRAVAIL / f"{ident}.{nom}.json").read_text(encoding="utf-8"))
        except Exception:
            return None
    a, m = lire("magic"), lire("whisper")
    return (lire_magic(a) if a else None), (lire_whisper(m) if m else []), (lignes_de_whisper(m) if m else [])


def traiter(t):
    import librosa
    import soundfile as sf
    extrait = not t.get("fichier") and SOURCE == "deezer"
    bilan = {"id": t["id"], "artiste": t["artiste"], "titre": t["titre"], "durees_s": {}, "alertes": [], "methodes": {},
             "complet": not extrait, "recoupement": {}}
    depart = time.time()

    def etape(nom, fonction, *args, **kw):
        debut = time.time()
        if SUIVI:
            SUIVI(nom)
        try:
            return fonction(*args, **kw)
        except Exception as e:
            bilan["alertes"].append(f"{nom} : {str(e)[:400] or type(e).__name__}")
            traceback.print_exc()
            return None
        finally:
            bilan["durees_s"][nom] = round(bilan["durees_s"].get(nom, 0) + time.time() - debut, 1)
            print(f"  {nom} : {round(time.time() - debut, 1)} s", flush=True)

    res = etape("téléchargement", convertir_fichier if t.get("fichier") else
                telecharger_extrait if extrait else telecharger_youtube, t)
    if res is None:
        bilan["etat"] = "téléchargement impossible"
        return bilan
    wav, bilan["source"] = res
    duree = sf.info(str(wav)).duration
    bilan["duree_audio"] = round(duree, 1)
    magic, entendus, phrases = lectures_du_service(t["id"])
    par_etapes = duree > TRANCHE + 40 and not extrait          # morceau entier : la première page est livrée d'abord
    livrer_provisoire = par_etapes and not deja_complete(t["id"])

    # Les paroles viennent toujours d'une version publiée, jamais de la seule transcription : Whisper invente des mots
    # sur une voix chantée, et une partition aux paroles fausses est pire qu'une partition sans paroles.
    res = etape("paroles", chercher_paroles, t, bilan["source"].get("duree") or duree, entendus)
    lignes, bilan["paroles"] = res if res else ([], None)
    # Source des paroles en panne (et non « aucune version ») : on y reviendra avant le placement définitif des mots.
    paroles_a_reprendre = res is None and not any(a.startswith("paroles :") and "aucune version" in a for a in bilan["alertes"])
    if res:
        print(f"  paroles : {bilan['paroles']}", flush=True)
    elif not paroles_a_reprendre:
        bilan["alertes"] = [a for a in bilan["alertes"] if not a.startswith("paroles :")] + [
            "paroles : aucune version publiée n'a été trouvée pour ce titre ; la partition donne les accords sans les paroles"]

    # ------------------------------------------------------------ temps et mesures
    res = etape("temps", detecter_temps, wav)
    if (res is None or len(res[0]) < 8) and magic:              # Beat This! en panne : la grille du service prend le relais
        res = (np.asarray(magic["temps"]), np.asarray(magic["temps"])[magic["barres"]], "Magic Chords (repli)")
    if res is None or len(res[0]) < 8:
        bilan["etat"] = "grille rythmique impossible"
        return bilan
    temps, premiers, bilan["methodes"]["temps"] = res
    num, pos, longueur, bilan["mesures_infos"] = regulariser_mesures(temps, premiers)
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    sub, bilan["subdivision_infos"] = detecter_subdivision(y, sr, temps)

    def recoupe_temps():
        infos = {}
        autres = {"librosa": temps_librosa(y, sr)}
        if magic:
            autres["magic_chords"] = magic["temps"]
        for nom, tps in autres.items():
            c = comparer_temps(temps, tps)
            if c:
                infos[nom] = c
        if magic and magic.get("metre") and magic["metre"] != longueur:
            infos["mesure_selon_magic_chords"] = magic["metre"]
        doubles = [nom for nom, c in infos.items() if isinstance(c, dict) and (abs(c["rapport_tempo"] - 2) < 0.1 or abs(c["rapport_tempo"] - 0.5) < 0.05)]
        if "magic_chords" in doubles:                      # le suivi classique seul se trompe trop souvent d'octave pour alerter
            bilan["alertes"].append("tempo : les autres lectures battent deux fois plus vite ou plus lentement ; "
                                    "la pulsation retenue est peut-être à doubler ou à diviser")
        return infos
    bilan["recoupement"]["temps"] = etape("temps (recoupement)", recoupe_temps)

    # ------------------------------------------------------------ accords : lectures du mix
    lectures, detail, accordage, logp_mix, pas_btc = {}, None, 0.0, None, None

    def lire_mix():
        nonlocal detail, accordage, logp_mix, pas_btc
        logp_mix, pas_btc, accordage = probabilites_accords(y, sr)
        bilan["accordage_demi_tons"] = round(accordage, 2)
        if abs(accordage) > 0.35:
            bilan["alertes"].append(f"accordage à mi-chemin entre deux demi-tons ({accordage:+.2f}) : "
                                    "les accords peuvent être notés un demi-ton trop haut ou trop bas")
        detail = energie_par_temps(logp_mix, pas_btc, temps)
        lectures["btc_mix"] = (familles(detail), None)
        return True
    etape("accords (BTC, mix)", lire_mix)

    basses_chordnet, couleurs = None, {}

    def lire_chordnet():
        nonlocal basses_chordnet
        segments = accords_chordnet(wav)
        F, connu, basses_chordnet = lecture_segments(segments, temps, 0.6)
        lectures["chordnet"] = (F, connu)
        couleurs["chordnet"] = couleurs_segments(segments, temps)
        return True
    etape("accords (ChordNet)", lire_chordnet)
    if not lectures:                                           # aucun modèle disponible : lecture de secours par gabarits
        def lire_gabarits():
            lectures["gabarits"] = (gabarits(chroma_par_temps(y, sr, temps, accordage)), None)
            return True
        etape("accords (gabarits)", lire_gabarits)
    if magic and magic.get("accords"):
        lectures["magic_chords"] = lecture_segments(magic["accords"], temps, 0.5)[:2]
        couleurs["magic_chords"] = couleurs_segments([(a, b, (c[0], c[1])) if c else (a, b, None) for a, b, c in magic["accords"]], temps)

    # Le premier temps de la mesure : Beat This!, le service et l'harmonie votent.
    if lectures:
        bruts = next(iter(lectures.values()))[0].argmax(1) if "btc_mix" not in lectures else lectures["btc_mix"][0].argmax(1)
        r = etape("mesures (recoupement)", recouper_mesures, temps, pos, longueur, magic, bruts)
        if r:
            pos, num, bilan["recoupement"]["mesures"] = r
            print("  premier temps de la mesure :", bilan["recoupement"]["mesures"], flush=True)

    journal_basses = []
    grilles_en_ligne = []

    def chercher_en_ligne():
        import grilles as G
        for accords, origine in G.chercher_grilles(t["artiste"], t["titre"]):
            lue = lire_grille(accords)
            if len(lue) >= 8:
                grilles_en_ligne.append((lue, origine))
        return len(grilles_en_ligne)
    if not extrait:
        r = etape("grilles en ligne", chercher_en_ligne)
        bilan["alertes"] = [a for a in bilan["alertes"] if not a.startswith("grilles en ligne")]     # canal facultatif : ses pannes ne sont pas des alertes
        print(f"  grilles en ligne : {r or 0} retenues pour comparaison", flush=True)

    def garder_pour_le_banc(notes, pedale, parts_piste, hors_basse, y_piste):
        """Banc d'essai : pour les titres listés dans resultats/banc/titres.txt, les lectures intermédiaires (aucun son)
        sont gardées, afin d'essayer une autre façon de décider des accords en quelques secondes, sans refaire l'analyse.
        S'y ajoute une lecture de BTC sur la musique privée de sa basse, qui ne sert pour l'instant qu'au banc."""
        liste = Path("resultats/banc/titres.txt")
        if not liste.exists() or t["id"] not in liste.read_text(encoding="utf-8").split():
            return
        import pickle
        donnees = {"temps": np.asarray(temps), "pos": np.asarray(pos), "longueur": longueur, "detail": detail, "notes": notes,
                   "pedale": pedale, "basse_piste": parts_piste, "basse_melange": basse_par_temps(y, sr, temps, accordage),
                   "basses_chordnet": basses_chordnet, "couleurs": couleurs, "tonalite_externe": (magic or {}).get("tonalite"),
                   "lectures": {k: (F.astype(np.float32), connu) for k, (F, connu) in lectures.items()}}
        if logp_mix is not None:
            logp_sb, _, _ = probabilites_accords(hors_basse, sr)
            e = energie_par_temps(logp_sb, pas_btc, temps)
            donnees["detail_sans_basse"] = e.astype(np.float32)
            donnees["btc_sans_basse"] = familles(e).astype(np.float32)
        donnees["detail"] = None if detail is None else detail.astype(np.float32)
        try:                                                            # lecture fine des notes : accompagnement, puis basse seule
            donnees["notes_fines"] = notes_fines(hors_basse, sr, temps)
            donnees["basse_fine"] = notes_fines(y_piste, sr, temps)
        except Exception:
            traceback.print_exc()
        liste.parent.mkdir(parents=True, exist_ok=True)
        with open(liste.parent / (t["id"] + ".pickle"), "wb") as f:
            pickle.dump(donnees, f)
        print(f"  banc d'essai : lectures gardées ({len(temps)} temps)", flush=True)

    def decider_accords(y_basse, y_basse2=None, y_piste_basse=None):
        """Recoupe les lectures disponibles, puis pose les basses. Renvoie les accords par temps.
        y_piste_basse : la basse isolée, quand on l'a ; elle devient la lecture principale de la basse."""
        if not lectures:
            return [None] * len(temps)
        journal_couleurs, pedale, parts_piste = [], None, None
        if y_piste_basse is not None:
            try:
                parts_piste = basse_par_temps(y_piste_basse, sr, temps, accordage)
                pedale = notes_pedales(parts_piste)
                print(f"  pédales : {int((pedale >= 0).sum())} temps sur {len(pedale)}", flush=True)
            except Exception:
                traceback.print_exc()
        notes = None
        try:
            hors_basse = y_basse
            if y_piste_basse is not None:                               # on retire la basse : ses harmoniques flattent l'accord de la pédale
                k = min(len(y_basse), len(y_piste_basse))
                hors_basse = y_basse[:k] - y_piste_basse[:k]
            notes = chroma_par_temps(hors_basse, sr, temps, accordage)
        except Exception:
            traceback.print_exc()
        acc, infos = recouper_accords(lectures, pos, longueur, detail, couleurs, pedale, journal_couleurs,
                                      notes=notes, tonalite_externe=(magic or {}).get("tonalite"), grille=grilles_en_ligne)
        infos["couleurs_retirees"] = sum(1 for l in journal_couleurs if "BTC seul" in l)
        infos["modes_alignes"] = sum(1 for l in journal_couleurs if "écriture habituelle" in l)
        bilan["recoupement"]["accords"] = infos
        for ligne in journal_couleurs:
            print("    ", ligne, flush=True)
        for nom, (F, connu) in lectures.items():
            choix = F.argmax(1)
            print(f"  accords ({nom:13}) :", " ".join(
                "-" if (c >= 24 or (connu is not None and not connu[i])) else nom_accord((c // 2, "min" if c % 2 else "maj"))
                for i, c in enumerate(choix)), flush=True)
        try:
            del journal_basses[:]
            if parts_piste is not None:
                parts, parts2 = parts_piste, basse_par_temps(y_basse, sr, temps, accordage)
            else:
                parts = basse_par_temps(y_basse, sr, temps, accordage)
                parts2 = basse_par_temps(y_basse2, sr, temps, accordage) if y_basse2 is not None else None
            print(f"  basse ({'piste isolée' if parts_piste is not None else 'mélange'}) :", " ".join(
                NOTES_MIXTES[int(v.argmax())] if v.max() >= 0.3 else "·" for v in parts), flush=True)
            acc, nb = poser_basses(acc, parts, parts2, basses_chordnet, journal_basses, fiable=parts_piste is not None)
            acc, ramenes = caler_sur_le_premier_temps(acc, pos, longueur, journal_basses)
            infos["changements_ramenes_sur_le_premier_temps"] = ramenes
            acc, tenues = pedale_au_dessus(acc, notes, pos, longueur, journal_basses)
            infos["pedales_au_dessus_de_la_basse"] = tenues
            sur_fondamentale = float(np.mean([a is not None and int(parts[i].argmax()) == a[0] for i, a in enumerate(acc)]))
            bilan["recoupement"]["basse"] = {"renversements": nb, "lectures": 1 + (parts2 is not None) + (basses_chordnet is not None), "piste_isolee": parts_piste is not None,
                                             "basse_sur_la_fondamentale": round(sur_fondamentale, 2),
                                             "nettete_mediane": round(float(np.median(parts.max(1))), 2)}
        except Exception as e:
            bilan["alertes"].append(f"basse : {str(e)[:200] or type(e).__name__}")
            traceback.print_exc()
            acc = [(*a[:3], None) if a else None for a in acc]
        print("  accords (retenus)       :", " ".join(nom_accord(a) or "-" for a in acc), flush=True)
        if y_piste_basse is not None:
            try:
                garder_pour_le_banc(notes, pedale, parts_piste, hors_basse, y_piste_basse)
            except Exception:
                traceback.print_exc()
        for ligne in journal_basses[:120]:
            print("    renversement :", ligne, flush=True)
        return acc
    accords = etape("accords (recoupement)", decider_accords, y) or [None] * len(temps)

    # ------------------------------------------------------------ mots provisoires et livraison
    decalage_whisper, decalages_lignes = None, None
    provisoires = []
    if lignes:
        r = etape("paroles (calage provisoire)", caler_sur_transcription, lignes, entendus)
        if r:
            provisoires, infos_cal = r
            # Un décalage qui se détache au vote est fiable même si la transcription a des trous ; « aucun décalage »
            # ne l'est que si la moitié des mots ont été retrouvés à leur place.
            if entendus and (abs(infos_cal.get("decalage_paroles_s", 0.0)) >= 0.6
                             or infos_cal.get("retrouves_dans_transcription", 0) >= 0.5 * max(1, infos_cal.get("mots", 0))):
                decalage_whisper = -float(infos_cal.get("decalage_paroles_s", 0.0))   # même sens que placer_mots : paroles − réel
            if entendus and infos_cal.get("decalages_par_ligne"):
                decalages_lignes = [-x for x in infos_cal["decalages_par_ligne"]]
            print("  paroles et transcription :", infos_cal, "| décalage retenu :", decalage_whisper, flush=True)

    def livrer(mots, etape_nom=None, **plus):
        """Écrit la partition dans son état du moment. etape_nom : version provisoire (« grille », « premiere-page »)."""
        tonalite = estimer_tonalite(accords)
        mesures = assembler(temps, num, accords, mots, sub)
        bilan.update({"tempo": round(60.0 / float(np.median(np.diff(temps))), 1), "temps_par_mesure": longueur,
                      "subdivision": sub, "tonalite": tonalite, "nb_mesures": len(mesures), "etat": "ok", "format": 2})
        bilan.pop("provisoire", None)
        if etape_nom:
            bilan["provisoire"] = {"etape": etape_nom, "apres_s": round(time.time() - depart), **plus}
        bilan["durees_s"]["total"] = round(time.time() - depart, 1)
        ecrire_partition(RESULTATS, bilan, mesures)
        (RESULTATS / (t["id"] + ".txt")).write_text(rendu_texte(mesures, tonalite) + "\n", encoding="utf-8")
        if PUBLICATION and etape_nom:
            try:
                PUBLICATION(bilan, f"{'Grille' if etape_nom == 'grille' else 'Première page'} : {t['artiste']} - {t['titre']}")
            except Exception as e:
                print("  publication intermédiaire impossible :", repr(e)[:200], flush=True)
        print(f"  >> partition livrée ({etape_nom or 'complète'}) après {round(time.time() - depart)} s", flush=True)

    if livrer_provisoire:
        livrer(provisoires, "grille", mots_cales_jusqua=0)

    # ------------------------------------------------------------ voix isolée, par tranches
    def noter_mots(mots):
        if mots:
            bilan["mots"] = {"total": len(mots), "confiance_mediane": round(float(np.median([m[3] for m in mots])), 2),
                             "part_confiance_faible": round(sum(m[3] < 0.3 for m in mots) / len(mots), 2)}

    pistes = None
    if par_etapes:
        debut_wav = etape("voix isolée (première page)", lambda: separer_voix(couper(wav, 0, TRANCHE + 1.0, t["id"] + ".debut.wav")))
        if debut_wav:
            pistes_debut, bilan["methodes"]["voix"] = debut_wav
            if lignes and livrer_provisoire:
                r = etape("placement des mots (première page)", placer_mots, pistes_debut["voix"], lignes, False,
                          decalage_connu=decalage_whisper or 0.0, jusqua=TRANCHE - 0.5)
                if r and r[0]:
                    mots_debut = recouper_mots(r[0], entendus)[0]
                    faites = {m[4] for m in mots_debut}
                    limite = max(m[1] for m in mots_debut)
                    reste = [m for m in provisoires if m[4] not in faites]
                    livrer(sorted(mots_debut + reste, key=lambda m: m[0]), "premiere-page", mots_cales_jusqua=round(limite, 1))
            suite = etape("voix isolée (suite)", lambda: separer_voix(couper(wav, TRANCHE - 2.0, None, t["id"] + ".suite.wav")))
            if suite:
                try:
                    pistes = {k: raccorder(pistes_debut[k], suite[0][k], TRANCHE, 2.0, f"{t['id']}.{k}.wav")
                              for k in ("voix", "instrumental") if pistes_debut.get(k) and suite[0].get(k)}
                    if "voix" not in pistes:
                        pistes = None
                except Exception as e:
                    bilan["alertes"].append(f"raccord des tranches : {str(e)[:200] or type(e).__name__}")
                    traceback.print_exc()
                    pistes = None
    if pistes is None:
        res = etape("voix isolée", separer_voix, wav)
        pistes, bilan["methodes"]["voix"] = res if res else (None, bilan["methodes"].get("voix"))

    # ------------------------------------------------------------ accords : lecture sans la voix, basse sur l'accompagnement
    if pistes and pistes.get("instrumental"):
        def lire_sans_voix():
            nonlocal detail
            yi, _ = librosa.load(str(pistes["instrumental"]), sr=22050, mono=True)
            if logp_mix is not None:
                logp_i, _, _ = probabilites_accords(yi, sr)
                e = energie_par_temps(logp_i, pas_btc, temps)
                lectures["btc_sans_voix"] = (familles(e), None)
                detail = (detail + e) / 2
            if "gabarits" in lectures:
                lectures["gabarits"] = (gabarits(chroma_par_temps(yi, sr, temps, accordage)), None)   # relus sans la voix
            return yi
        yi = etape("accords (BTC, sans la voix)", lire_sans_voix)
        if yi is not None:
            def lire_basse():
                yb, _ = librosa.load(str(isoler_basse(wav)), sr=22050, mono=True)
                return yb
            yb = etape("basse isolée", lire_basse)
            accords = etape("accords (recoupement)", decider_accords, yi, y, yb) or accords

    # ------------------------------------------------------------ mots : alignement complet, recoupé avec Whisper
    mots = []
    if paroles_a_reprendre:                                    # la source des paroles était en panne au départ : nouvel essai
        res = etape("paroles (nouvel essai)", chercher_paroles, t, bilan["source"].get("duree") or duree, entendus)
        bilan["alertes"] = [a for a in bilan["alertes"] if not a.startswith("paroles")]
        if res:
            lignes, bilan["paroles"] = res
            print(f"  paroles : retrouvées au second essai ({len(lignes)} lignes)", flush=True)
        else:
            bilan["alertes"].append("paroles : la source des paroles n'a pas répondu ; la partition donne les accords sans les "
                                    "paroles. Refaire l'analyse plus tard les ajoutera.")
    if lignes:
        if paroles_a_reprendre and entendus:                   # les paroles viennent d'arriver : leur décalage reste à mesurer
            try:
                ic = caler_sur_transcription(lignes, entendus)[1]
                if abs(ic.get("decalage_paroles_s", 0.0)) >= 0.6 or ic.get("retrouves_dans_transcription", 0) >= 0.5 * max(1, ic.get("mots", 0)):
                    decalage_whisper = -float(ic.get("decalage_paroles_s", 0.0))
                decalages_lignes = [-x for x in ic["decalages_par_ligne"]] if ic.get("decalages_par_ligne") else None
            except Exception:
                traceback.print_exc()
        res = etape("placement des mots", placer_mots, pistes["voix"] if pistes else wav, lignes, extrait, indice=decalage_whisper,
                    indices=decalages_lignes)
        if res:
            mots, bilan["position_extrait"] = res
            if res[1].get("lignes_non_posees"):
                bilan["alertes"].append(f"paroles : elles suivent une autre édition que cette vidéo ; {res[1]['lignes_non_posees']} "
                                        "lignes n'y ont pas trouvé leur place et ne sont pas écrites")
            r = recouper_mots(mots, entendus)
            mots = r[0]
            if r[1]:
                bilan["recoupement"]["mots"] = r[1]
                print("  mots recoupés avec Whisper :", r[1], flush=True)
        elif provisoires:                                       # l'alignement a échoué : on garde le calage provisoire
            mots = provisoires
            bilan["alertes"].append("mots placés d'après la transcription seulement (alignement indisponible)")
        noter_mots(mots)
    livrer(mots)
    return bilan


def ecrire_rapport(bilans):
    r = ["# Essai d'analyse", ""]
    for b in bilans:
        r += [f"## {b.get('artiste')} — {b.get('titre')}" + (" (morceau entier)" if b.get("complet") else " (extrait)"),
              "", f"- État : **{b.get('etat')}**"]
        s = b.get("source")
        if s:
            r.append(f"- Source : {s['source']} — « {s.get('video')} » ({s.get('chaine')}), {s.get('duree')} s")
        if b.get("paroles"):
            r.append(f"- Paroles : {b['paroles']['nb_lignes']} lignes synchronisées")
        if b.get("duree_audio"):
            r.append(f"- Durée analysée : {b['duree_audio']} s")
        if b.get("tempo") and b.get("mesures_infos") and b.get("subdivision_infos"):   # une esquisse n'a pas ces mesures
            mi, si = b["mesures_infos"], b["subdivision_infos"]
            r.append(f"- Rythme ({b['methodes'].get('temps')}) : {b['tempo']} à la minute, {b['temps_par_mesure']} temps par mesure, "
                     f"{'ternaire' if si['ternaire'] else 'binaire'} (demi {si['force_demi']}, tiers {si['force_tiers']}, "
                     f"quarts {si['force_quarts']}), cohérence des premiers temps {int(mi['coherence'] * 100)} %, "
                     f"{mi.get('mesures_irregulieres', 0)} mesures irrégulières")
        if b.get("tonalite"):
            ton = b["tonalite"]
            r.append(f"- Tonalité estimée : {nom_accord((ton['tonique'], 'maj'), ton)} {ton['mode']}, "
                     f"accordage {b.get('accordage_demi_tons')} demi-ton"
                     + (f", accord mix / sans voix {int(b['accords_infos']['accord_mix_vs_sans_voix'] * 100)} %"
                        if b.get("accords_infos") else ""))
        if b.get("mots"):
            m, p = b["mots"], b.get("position_extrait", {})
            r.append(f"- Mots ({b['methodes'].get('voix')}) : {m['total']} placés, confiance médiane {m['confiance_mediane']}, "
                     f"{int(m['part_confiance_faible'] * 100)} % à confiance faible, {p.get('mots_hors_extrait_retires')} hors extrait "
                     f"retirés, {p.get('lignes_reprises_passe1')} lignes reprises de la passe 1")
            if p.get("decalage_paroles_s") is not None:
                r.append(f"- Décalage mesuré entre les paroles et ce fichier : {p['decalage_paroles_s']} s (score {p['score']})")
            if p.get("debut_extrait_dans_morceau") is not None:
                r.append(f"- Extrait situé à {p['debut_extrait_dans_morceau']} s du morceau "
                         f"(score {p['score']}, suivant {p['score_suivant']})")
        rc = b.get("recoupement") or {}
        if rc.get("accords"):
            a = rc["accords"]
            r.append(f"- Accords recoupés entre {len(a['moteurs'])} moteurs ({', '.join(a['moteurs'])}) : accord moyen "
                     f"{int(a['accord_moyen'] * 100)} %, {int(a['accords_douteux'] * 100)} % de temps douteux"
                     + (f", {int(a['temps_changes_par_recoupement'] * 100)} % changés par rapport à BTC seul" if "temps_changes_par_recoupement" in a else ""))
        if rc.get("temps") or rc.get("mesures"):
            r.append(f"- Temps recoupés : {json.dumps(rc.get('temps'), ensure_ascii=False)} ; premier temps : {json.dumps(rc.get('mesures'), ensure_ascii=False)}")
        if rc.get("basse"):
            r.append(f"- Renversements notés : {rc['basse']['renversements']} ({rc['basse']['lectures']} lecture(s) de la basse)")
        if rc.get("mots"):
            m = rc["mots"]
            r.append(f"- Mots recoupés avec Whisper : {m['mots_compares']} comparés, {int(m['part_confirmee'] * 100)} % confirmés, "
                     f"{m['repris_de_whisper']} repris, {m['divergents']} divergents")
        if b.get("durees_s"):
            r.append("- Durées (s) : " + ", ".join(f"{k} {v}" for k, v in b["durees_s"].items()))
        for a in b.get("alertes", []):
            r.append(f"- ⚠️ {a}")
        grille = RESULTATS / (b["id"] + ".txt")
        if b.get("etat") == "ok" and grille.exists():
            lignes = grille.read_text().rstrip().splitlines()
            r += ["", "```"] + lignes[:24] + ([f"… ({len(lignes) - 24} mesures de plus)"] if len(lignes) > 24 else []) + ["```"]
        r.append("")
    (RESULTATS / "rapport.md").write_text("\n".join(r), encoding="utf-8")
    (RESULTATS / "bilan.json").write_text(json.dumps(bilans, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    global PUBLICATION
    if os.environ.get("PUBLIER"):                           # sur GitHub : chaque version provisoire est poussée dans le dépôt
        PUBLICATION = lambda bilan, message: subprocess.run(["bash", str(ICI / "publier.sh"), message], check=False)
    TRAVAIL.mkdir(exist_ok=True)
    RESULTATS.mkdir(exist_ok=True)
    titres = titres_deposes(RACINE / "audio")
    if not titres and (ICI / "TEST_COMPLET").exists():      # essai sur le morceau d'exemple fourni avec BTC
        _charger_btc()
        titres = [{"id": "exemple-complet", "artiste": "(essai)", "titre": "Morceau d'exemple entier",
                   "fichier": str(DOSSIER_BTC / "test" / "example.mp3")}]
    if not titres:
        titres = json.loads((ICI / "titres.json").read_text())
    ancien = RESULTATS / "bilan.json"
    bilans = {b["id"]: b for b in json.loads(ancien.read_text(encoding="utf-8"))} if ancien.exists() else {}
    faits = set()
    for t in titres:
        print(f"== {t['artiste']} — {t['titre']}", flush=True)
        with open(TRAVAIL / "miens.txt", "a", encoding="utf-8") as m:      # ce passage possède ce titre (voir publier.py)
            m.write(t["id"] + "\n")
        if ancien.exists():                                 # le bilan a pu être complété entre-temps (esquisse, autre titre)
            bilans = {b["id"]: b for b in json.loads(ancien.read_text(encoding="utf-8"))} | {
                k: v for k, v in bilans.items() if k in faits}
        try:
            bilans[t["id"]] = traiter(t)
        except Exception as e:
            traceback.print_exc()
            bilans[t["id"]] = {k: v for k, v in t.items() if k != "fichier"} | {"etat": "erreur inattendue", "alertes": [repr(e)[:400]]}
        faits.add(t["id"])
        ecrire_rapport(list(bilans.values()))


if __name__ == "__main__":
    main()
