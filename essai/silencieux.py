#!/usr/bin/env python3
"""Analyse silencieuse, sans ordinateur. Pour chaque demande (demandes/*.json : artiste, titre, video), confie la
vidéo YouTube au service Magic Chords (https://magic-chords.dev), qui sait la lire depuis ses serveurs.

Le service rend trois choses, toutes utilisées :
- le son, posé dans audio/ pour notre chaîne d'analyse (analyse.py) ; il n'est jamais conservé dans le dépôt ;
- sa propre lecture des temps, des mesures et des accords ;
- les mots qu'il a entendus (Whisper), avec leurs instants.

Avec ces deux lectures et les vraies paroles (LRCLIB), une ESQUISSE de la partition est publiée tout de suite,
avant même que nos outils d'analyse soient installés. Les lectures du service sont ensuite gardées de côté
(travail/) : notre analyse les recoupe avec les siennes.

Python pur, sans module à installer."""
import json
from datetime import datetime, timezone
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import commun as C

ICI = Path(__file__).resolve().parent
RACINE = Path(os.environ.get("DONNEES") or ICI.parent)      # les données peuvent vivre dans un autre dépôt que le code
RESULTATS = RACINE / "resultats"
TRAVAIL = RACINE / "travail"
SERVICE = "https://magic-chords.dev"
API = SERVICE + "/api/v1"
AGENT = "partitions-perso (usage personnel)"
EXTENSIONS = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
              "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/flac": ".flac", "audio/ogg": ".ogg",
              "audio/opus": ".opus", "audio/webm": ".webm", "video/webm": ".webm", "video/mp4": ".m4a"}
EN_COURS = ("processing", "queued", "pending", "running", "started", "")


def appel(methode, chemin, corps=None):
    req = urllib.request.Request(API + chemin, method=methode, data=json.dumps(corps).encode() if corps is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as rep:
            return rep.status, json.loads(rep.read() or b"null"), dict(rep.headers)
    except urllib.error.HTTPError as e:
        texte = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(texte), dict(e.headers)
        except ValueError:
            return e.code, {"texte": texte[:300]}, dict(e.headers)
    except Exception as e:                                   # coupure réseau passagère : on réessaiera au tour suivant
        return 0, {"texte": repr(e)[:200]}, {}


def propre(s):
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", s)).strip()


def nom_fichier(artiste, titre):
    """Même règle que la page « Ma playlist » : le nom donne l'identifiant de la partition."""
    return propre(artiste).replace(" - ", ", ") + " - " + propre(titre)


def identite(d):
    """(artiste, titre, identifiant) tels que l'analyse les relira dans le nom du fichier audio."""
    nom = nom_fichier(d.get("artiste", ""), d["titre"])
    artiste, titre = [x.strip() for x in nom.split(" - ", 1)]
    return artiste, titre, C.identifiant(f"{artiste} {titre}")


def lancer(genre, video):
    """Dépose une tâche (analyze : son + temps + accords ; transcribe : mots entendus). Renvoie (n° de tâche, quota restant)."""
    for essai in range(4):
        code, rep, entetes = appel("POST", f"/{genre}/url", {"url": "https://www.youtube.com/watch?v=" + video, "language": ""})
        attente = str(entetes.get("Retry-After") or entetes.get("retry-after") or "")
        if code == 429 and essai < 3 and attente.isdigit() and int(attente) <= 150:
            print(f"  le service demande de patienter {attente} s", flush=True)      # limite de cadence, pas le quota de l'heure
            time.sleep(int(attente) + 3)
            continue
        break
    reste = entetes.get("X-Ratelimit-Remaining") or entetes.get("x-ratelimit-remaining")
    if code == 429:
        raise RuntimeError("le service a atteint son quota d'analyses pour cette heure"
                           + (f" (nouvel essai possible dans {int(attente) // 60 + 1} min)" if attente.isdigit() else "")
                           + ". Relance l'analyse un peu plus tard.")
    if code >= 300 or not isinstance(rep, dict) or not rep.get("job_id"):
        raise RuntimeError(f"le service a refusé la vidéo (code {code}) : {json.dumps(rep, ensure_ascii=False)[:200]}")
    return rep["job_id"], (int(reste) if str(reste or "").isdigit() else None)


def attendre(taches, son_recu=None, limite=1200):
    """Suit plusieurs tâches en même temps. `son_recu` est appelée une fois, dès que l'analyse a dépassé le
    téléchargement de la vidéo : elle peut renvoyer une tâche de plus à suivre (les mots entendus).
    Renvoie ({genre: résultat ou None}, {genre: message d'erreur du service})."""
    t0, resultats, messages, erreurs = time.time(), {}, {}, {}
    while len(resultats) < len(taches):
        time.sleep(5)
        for genre, tache in list(taches.items()):
            if genre in resultats:
                continue
            code, etat, _ = appel("GET", f"/jobs/{tache}")
            etat = etat if isinstance(etat, dict) else {}
            statut = str(etat.get("status", "")).lower()
            if etat.get("message") and etat["message"] != messages.get(genre):
                messages[genre] = etat["message"]
                print(f"   {int(time.time() - t0):>3} s  {genre} : {messages[genre][:160]}", flush=True)
            if genre == "analyse" and son_recu and statut in EN_COURS and messages.get(genre) and "download" not in messages[genre].lower():
                suite, son_recu = son_recu(), None
                if suite:
                    taches["mots"] = suite
            if statut == "complete":
                code, res, _ = appel("GET", f"/jobs/{tache}/result")
                resultats[genre] = res if code == 200 and isinstance(res, dict) else None
                print(f"   {int(time.time() - t0):>3} s  {genre} : terminé", flush=True)
            elif code and statut not in EN_COURS:
                erreurs[genre] = str(etat.get("message") or statut)
                resultats[genre] = None
            elif time.time() - t0 > limite:
                erreurs[genre] = f"pas de réponse en {limite // 60} minutes"
                resultats[genre] = None
    return resultats, erreurs


def rapatrier(adresse, d):
    """Télécharge le son préparé par le service et le pose dans audio/."""
    req = urllib.request.Request(adresse if adresse.startswith("http") else SERVICE + adresse, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=300) as rep:
        genre = (rep.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        donnees = rep.read()
    if len(donnees) < 200_000:
        raise RuntimeError(f"son reçu trop petit ({len(donnees)} octets, {genre})")
    fichier = RACINE / "audio" / (nom_fichier(d.get("artiste", ""), d["titre"]) + EXTENSIONS.get(genre, ".m4a"))
    fichier.write_bytes(donnees)
    return fichier, len(donnees), genre


def esquisse(d, magic, entendus, duree, phrases=()):
    """Première version de la partition, tirée des seules lectures du service : ses temps, ses mesures, ses accords,
    et les vraies paroles calées sur les mots qu'il a entendus. Provisoire : notre analyse la remplace ensuite."""
    artiste, titre, ident = identite(d)
    temps, barres = magic["temps"], set(magic["barres"])
    num, n = [], 0
    for i in range(len(temps)):
        n += i in barres
        num.append(n)
    ecarts = sorted(b - a for a, b in zip(temps, temps[1:]))
    battue = ecarts[len(ecarts) // 2]
    fins = temps[1:] + [temps[-1] + battue]
    accords = []
    for a, b in zip(temps, fins):
        acc = C.accord_a(magic["accords"], (a + b) / 2)
        accords.append((acc[0], acc[1], 0.5, acc[2]) if acc else None)
    bilan = {"id": ident, "artiste": artiste, "titre": titre, "durees_s": {}, "alertes": [], "complet": True,
             "methodes": {"temps": "Magic Chords", "accords": "Magic Chords", "mots": "paroles LRCLIB calées sur Whisper"},
             "source": {"source": "YouTube, sans écoute", "video": d["video"], "chaine": artiste, "duree": duree},
             "duree_audio": round(duree or 0, 1)}
    mots = []
    try:
        # paroles publiées seulement (au besoin sans horodatage, calées sur la transcription) : jamais la transcription seule
        lignes, bilan["paroles"] = C.chercher_paroles({"artiste": artiste, "titre": titre}, duree or d.get("duree"), entendus)
        mots, bilan["position_extrait"] = C.caler_sur_transcription(lignes, entendus)
    except Exception as e:
        bilan["alertes"].append(f"paroles : {str(e)[:200] or type(e).__name__}")
    # Les grilles publiées en ligne, accrochées aux paroles qu'on vient de placer et transposées dans le ton entendu :
    # là où elles parlent (passages chantés), leurs accords remplacent ceux du service, moins sûrs.
    if mots:
        try:
            import grilles as G
            avec = []
            G.chercher_grilles(artiste, titre, en_tout=30, avec_paroles=avec)
            sur, infos = G.accords_sur_les_mots(avec, mots, temps, accords, C.lire_accord)
            bilan["grilles_en_ligne_sur_les_mots"] = infos
            if infos.get("retenues"):
                accords = [(g[0], g[1], 0.6, g[2]) if g else a for g, a in zip(sur, accords)]
                bilan["methodes"]["accords"] = "grilles en ligne calées sur les paroles, Magic Chords ailleurs"
            print("  grilles accrochées aux mots :", {k: v for k, v in infos.items() if k != "detail"}, flush=True)
            for g in infos.get("detail", []):
                print("     ", g, flush=True)
        except Exception as e:
            print(f"  grilles en ligne : {type(e).__name__} {str(e)[:120]}", flush=True)
    sub = 4 if battue >= 0.6 else 2
    mesures = C.assembler(temps, num, accords, mots, sub)
    bilan.update({"tempo": round(60.0 / battue, 1), "temps_par_mesure": magic["metre"], "subdivision": sub,
                  "tonalite": magic["tonalite"], "nb_mesures": len(mesures), "etat": "ok", "format": 2,
                  "provisoire": {"etape": "esquisse"}})
    return bilan, mesures


def deja_complete(ident):
    """Vrai si une partition achevée existe déjà pour ce titre : une nouvelle analyse ne doit pas la remplacer
    par une version provisoire."""
    f = RESULTATS / (ident + ".json")
    try:
        ancien = json.loads(f.read_text(encoding="utf-8"))
        return ancien.get("etat") == "ok" and ancien.get("complet") and not ancien.get("provisoire")
    except Exception:
        return False


def publier(message):
    if os.environ.get("PUBLIER"):
        subprocess.run(["bash", str(ICI / "publier.sh"), message], check=False)


def traiter(d):
    t0 = time.time()
    artiste, titre, ident = identite(d)
    def demander_mots():
        """Les mots entendus : seconde tâche, lancée quand la vidéo a bien été lue, si le quota le permet."""
        try:
            return lancer("transcribe", d["video"])[0]
        except Exception as e:
            print(f"  mots entendus non demandés : {e}", flush=True)

    for essai in (1, 2):
        tache, reste = lancer("analyze", d["video"])
        print(f"  quota restant cette heure : {reste if reste is not None else '?'}", flush=True)
        res, erreurs = attendre({"analyse": tache}, demander_mots if (reste is None or reste >= 1) else None)
        analyse = res.get("analyse")
        if analyse and analyse.get("audio_url"):
            break
        raison = erreurs.get("analyse", "réponse vide")
        if essai == 1 and "authentication" in raison.lower() and (reste is None or reste >= 1):
            print("  YouTube a demandé une connexion au service : nouvel essai dans 45 s", flush=True)
            time.sleep(45)                                   # ce refus est souvent passager
            continue
        if "authentication" in raison.lower():
            raise RuntimeError("YouTube réclame une connexion pour laisser lire cette vidéo, le service n'a donc pas pu "
                               "la récupérer. Réessaie plus tard, ou dépose le fichier audio du titre.")
        raise RuntimeError("le service n'a pas pu lire la vidéo : " + raison[:200])
    duree = float(analyse.get("duration") or 0) or d.get("duree")
    fichier, taille, genre = rapatrier(analyse["audio_url"], d)
    Path(str(fichier) + ".origine.json").write_text(json.dumps(
        {"source": "YouTube, sans écoute", "video": d["video"], "duree": duree}), encoding="utf-8")
    print(f"  son reçu : {taille / 1e6:.1f} Mo ({genre}), {duree} s, en {int(time.time() - t0)} s", flush=True)
    # Les lectures du service sont gardées pour être recoupées avec les nôtres (hors du dépôt).
    TRAVAIL.mkdir(exist_ok=True)
    (TRAVAIL / (ident + ".magic.json")).write_text(json.dumps(analyse), encoding="utf-8")
    if res.get("mots"):
        (TRAVAIL / (ident + ".whisper.json")).write_text(json.dumps(res["mots"]), encoding="utf-8")
    magic, entendus = C.lire_magic(analyse), C.lire_whisper(res.get("mots"))
    print(f"  lectures du service : {len(magic['temps']) if magic else 0} temps, {len(magic['accords']) if magic else 0} accords, "
          f"{len(entendus)} mots entendus", flush=True)
    if not magic:
        print("  esquisse impossible : le service n'a pas rendu de grille de temps", flush=True)
    else:
        bilan, mesures = esquisse(d, magic, entendus, duree, C.lignes_de_whisper(res.get("mots")))
        bilan["durees_s"]["esquisse"] = round(time.time() - t0, 1)
        if deja_complete(ident):
            print("  esquisse non publiée : une partition achevée existe déjà, elle reste en place jusqu'à la nouvelle", flush=True)
            return
        C.ecrire_partition(RESULTATS, bilan, mesures)
        publier(f"Esquisse : {artiste} - {titre}")
        print(f"  esquisse publiée après {int(time.time() - t0)} s : {len(mesures)} mesures, "
              f"{sum(len(c['mots']) for m in mesures for c in m['temps'])} mots", flush=True)


def noter_depart(d):
    """Inscrit l'heure de départ de l'analyse dans le bilan du titre et la publie : la page s'en sert pour montrer
    l'avancement, sans avoir à interroger la machine qui calcule. Une partition déjà faite garde son état."""
    artiste, titre, ident = identite(d)
    try:
        bilans = json.loads((RESULTATS / "bilan.json").read_text(encoding="utf-8"))
    except Exception:
        bilans = []
    ancien = next((b for b in bilans if b.get("id") == ident), None)
    note = dict(ancien) if ancien and ancien.get("etat") == "ok" else {"id": ident, "artiste": artiste, "titre": titre, "complet": False, "etat": "en cours"}
    note["analyse_debut"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RESULTATS.mkdir(parents=True, exist_ok=True)
    C.noter_bilan(RESULTATS, note)
    publier(f"Analyse commencée : {artiste} - {titre}")


ATTENTE_MAX = 6 * 3600        # au-delà, la demande est abandonnée : le refus n'a plus rien de passager


def passager(e):
    """Vrai pour un refus qui ne tient ni au titre ni à la demande : YouTube qui écarte le service, quota de l'heure."""
    return any(x in str(e) for x in ("réclame une connexion", "quota d'analyses"))


def anciennete(d):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(d.get("demande_le")).replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return 0.0


def main():
    echecs = 0
    # Un seul titre par passage : le service compte ses analyses par machine, et chaque passage en a une neuve.
    # Les autres demandes ont chacune leur passage, lancé par aiguillage.py.
    dossier = RACINE / "demandes"
    voulue = os.environ.get("DEMANDE", "").strip()
    if voulue:                                               # plusieurs analyses tournent ensemble : chacune a sa demande
        demandes = [dossier / Path(voulue).name] if (dossier / Path(voulue).name).exists() else []
        if not demandes:
            print(f"Demande {voulue} déjà traitée (ou retirée) : rien à faire.", flush=True)
    else:
        demandes = sorted(dossier.glob("*.json"), key=lambda f: (json.loads(f.read_text(encoding="utf-8")).get("demande_le") or "", f.name))
    for f in demandes[:1]:
        d = json.loads(f.read_text(encoding="utf-8"))
        (dossier / ".traitee").write_text(f.name, encoding="utf-8")
        print(f"== {d.get('artiste')} — {d.get('titre')} ({d.get('video')})", flush=True)
        try:
            if not d.get("video") or not d.get("titre"):
                raise RuntimeError("demande incomplète : il faut le titre et l'identifiant de la vidéo")
            noter_depart(d)
            traiter(d)
        except Exception as e:
            echecs += 1
            patiente = passager(e) and anciennete(d) < ATTENTE_MAX
            if patiente:                                     # la demande reste en place : elle sera retentée toute seule
                d["tentatives"] = int(d.get("tentatives") or 0) + 1
                d["dernier_essai"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                (dossier / ".traitee").unlink()
                (dossier / ".attente").write_text(f.name, encoding="utf-8")
                print(f"  EN ATTENTE (essai n° {d['tentatives']}) : {e}", flush=True)
            else:
                print(f"  ÉCHEC : {e}", flush=True)
            try:                                             # l'état est noté pour que la page puisse le dire
                artiste, titre, ident = identite(d)
                if not deja_complete(ident):
                    note = {"id": ident, "artiste": artiste, "titre": titre, "alertes": [str(e)[:300]]}
                    if patiente:
                        note.update(complet=False, etat="en attente", attente={
                            "tentatives": d["tentatives"], "dernier_essai": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "abandon_dans_h": round((ATTENTE_MAX - anciennete(d)) / 3600, 1)})
                    else:
                        note.update(complet=True, etat="son introuvable")
                        if passager(e):
                            note["alertes"] = [f"Abandon après {int(d.get('tentatives') or 0) + 1} essais en {ATTENTE_MAX // 3600} h. " + str(e)[:300]]
                    C.noter_bilan(RESULTATS, note)
            except Exception:
                pass
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
