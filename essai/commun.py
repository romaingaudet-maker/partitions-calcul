#!/usr/bin/env python3
"""Outils communs à l'esquisse (silencieux.py) et à l'analyse (analyse.py), en Python pur : aucun module
à installer, pour que l'esquisse puisse partir pendant que les gros outils d'analyse s'installent.

- paroles synchronisées (LRCLIB) ;
- lecture des réponses du service Magic Chords (temps, mesures, accords, mots transcrits par Whisper) ;
- calage des vraies paroles sur les mots transcrits ;
- assemblage des mesures et tenue du bilan."""
import difflib
import json
import time
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUALITES = ["min", "maj", "dim", "aug", "min6", "maj6", "min7", "minmaj7", "maj7", "7", "dim7", "hdim7", "sus2", "sus4"]
FAMILLE_MINEURE = {"min", "dim", "min6", "min7", "minmaj7", "dim7", "hdim7"}      # tierce mineure ; les autres : majeure
NOTES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _json(url, essais=4):
    """Lit du JSON à une adresse. Un service saturé (429, 5xx) ou une coupure réseau sont retentés après quelques
    secondes : LRCLIB répond souvent « 503 » un court instant, et y renoncer prive la partition de ses paroles."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "fr-FR,fr;q=0.9"})
    for essai in range(essais):
        try:
            with urllib.request.urlopen(req, timeout=60) as rep:
                return json.load(rep)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504, 520, 522, 524) or essai == essais - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if essai == essais - 1:
                raise
        time.sleep((3, 8, 15, 15)[min(essai, 3)])


def identifiant(texte):
    t = "".join(c for c in unicodedata.normalize("NFD", texte.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "titre"


# ---------------------------------------------------------------------- paroles
def analyser_lrc(texte):
    lignes = []
    for brut in texte.splitlines():
        m = re.match(r"\s*\[(\d+):(\d+(?:[.:]\d+)?)\]\s*(.*)", brut)
        if m:
            sec = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
            lignes.append((sec, m.group(3).strip()))
    return sorted(lignes)


HABILLAGE_TITRE = re.compile(r"\s+[-–—|:]\s+(clip|vid[eé]o|official|officiel|officielle|audio|lyrics?|paroles|hd|hq|4k|remaster\w*|"
                             r"version|live|full album)\b.*$", re.I)


def titre_nu(titre):
    """Titre débarrassé de ce que les plateformes y ajoutent : parenthèses, « - Clip officiel », « | Official Video »…"""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", titre).strip() or titre
    return HABILLAGE_TITRE.sub("", t).strip() or t


def horodater_par_transcription(texte, transcrits):
    """Des paroles publiées sans horodatage reçoivent un instant par ligne grâce aux mots que Whisper a entendus :
    les deux suites de mots sont mises en correspondance dans l'ordre du morceau ; une ligne commence à l'instant de
    son premier mot retrouvé, et les lignes sans mot retrouvé sont réparties entre leurs voisines.
    Renvoie [(instant, ligne)], ou None si trop peu de mots se retrouvent (moins de 30 %) pour s'y fier."""
    lignes = [l.strip() for l in texte.splitlines() if l.strip() and not re.match(r"^[\[\(].*[\]\)]$", l.strip())]
    mots = [(j, mot_nu(w)) for j, l in enumerate(lignes) for w in l.split() if mot_nu(w)]
    if len(lignes) < 4 or len(mots) < 20 or not transcrits:
        return None
    entendus = [mot_nu(w[2]) for w in transcrits]
    blocs = difflib.SequenceMatcher(None, [m for _, m in mots], entendus, autojunk=False).get_matching_blocks()
    instants, retrouves = {}, 0
    for b in blocs:
        for k in range(b.size):
            j = mots[b.a + k][0]
            retrouves += 1
            if j not in instants:                            # premier mot retrouvé de la ligne : on recule d'autant de mots qu'il en précède
                avant = sum(1 for x in range(b.a + k - 1, -1, -1) if mots[x][0] == j)
                instants[j] = max(0.0, transcrits[b.b + k][0] - 0.35 * avant)
    if retrouves < 0.3 * len(mots) or len(instants) < 3:
        return None
    connus = sorted(instants)
    sortie = []
    for j, l in enumerate(lignes):
        if j in instants:
            t = instants[j]
        else:
            avant = max((c for c in connus if c < j), default=None)
            apres = min((c for c in connus if c > j), default=None)
            if avant is None:
                t = max(0.0, instants[apres] - 3.0 * (apres - j))
            elif apres is None:
                t = instants[avant] + 3.0 * (j - avant)
            else:
                t = instants[avant] + (instants[apres] - instants[avant]) * (j - avant) / (apres - avant)
        if sortie and t <= sortie[-1][0]:
            t = sortie[-1][0] + 0.5
        sortie.append((round(t, 2), l))
    return sortie


def chercher_paroles(t, duree, transcrits=None):
    """Paroles publiées du morceau, horodatées à la ligne. Dans l'ordre :
    1. LRCLIB, version synchronisée la plus proche en durée (artiste et titre exacts, puis titre nettoyé, puis
       recherche libre) ;
    2. à défaut, des paroles publiées sans horodatage (LRCLIB, puis lyrics.ovh), horodatées d'après les mots entendus
       par Whisper (`transcrits`).
    On ne se rabat jamais sur la seule transcription : mieux vaut pas de paroles que des paroles inventées."""
    court = titre_nu(t["titre"])
    requetes = [{"artist_name": t["artiste"], "track_name": t["titre"]}]
    if court != t["titre"]:
        requetes.append({"artist_name": t["artiste"], "track_name": court})
    requetes.append({"q": f"{t['artiste']} {court}"})
    candidats, sans_temps = [], []
    nu = mot_nu(t["artiste"])
    for i, r in enumerate(requetes):
        try:
            tous = _json("https://lrclib.net/api/search?" + urllib.parse.urlencode(r))
        except Exception:
            if i == 0:
                raise
            continue
        if i == len(requetes) - 1 and len(requetes) > 1:      # recherche libre : on exige le bon artiste et une durée voisine
            tous = [x for x in tous if nu and (nu in mot_nu(x.get("artistName") or "") or mot_nu(x.get("artistName") or "") in nu)
                    and (not duree or abs((x.get("duration") or 0) - duree) <= 12)]
        sans_temps += [x for x in tous if x.get("plainLyrics") and not x.get("syncedLyrics")]
        trouves = [x for x in tous if x.get("syncedLyrics")]
        if trouves:
            candidats = trouves
            break
    if candidats:
        choix = min(candidats, key=lambda r: abs((r.get("duration") or 0) - (duree or 0)))
        lignes = analyser_lrc(choix["syncedLyrics"])
        return lignes, {"source": "LRCLIB", "duree_lrclib": choix.get("duration"),
                        "ecart_duree": round((choix.get("duration") or 0) - (duree or 0), 1), "nb_lignes": len(lignes)}
    # Pas de version synchronisée : paroles publiées sans horodatage, calées sur ce que Whisper a entendu.
    textes = [("LRCLIB (texte)", x["plainLyrics"]) for x in sorted(sans_temps, key=lambda r: abs((r.get("duration") or 0) - (duree or 0)))[:2]]
    try:
        d = _json("https://api.lyrics.ovh/v1/" + urllib.parse.quote(t["artiste"]) + "/" + urllib.parse.quote(court), essais=2)
        if d.get("lyrics"):
            textes.append(("lyrics.ovh", d["lyrics"]))
    except Exception:
        pass
    for source, texte in textes:
        lignes = horodater_par_transcription(texte, transcrits) if transcrits else None
        if lignes:
            return lignes, {"source": source, "horodatage": "d'après la transcription", "nb_lignes": len(lignes)}
    if textes and not transcrits:
        raise RuntimeError("aucune version publiée synchronisée ; un texte existe mais la transcription manque pour le caler")
    raise RuntimeError("aucune version publiée trouvée" if not textes else "aucune version publiée ne correspond à ce qui est chanté")


def mot_nu(mot):
    """Mot réduit à ses lettres et chiffres, sans accents ni majuscules : sert à comparer deux lectures."""
    m = mot.lower().replace("œ", "oe").replace("æ", "ae")
    m = "".join(c for c in unicodedata.normalize("NFD", m) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", m)


# ---------------------------------------------------------- service Magic Chords
def lire_accord(label):
    """« F », « Dm », « A#7 », « G/B », « N » -> (fondamentale 0-11, qualité, basse ou None), ou None."""
    m = re.match(r"^\s*([A-G])([#b♯♭]?)([^/]*)(?:/([A-G])([#b♯♭]?))?\s*$", str(label or ""))
    if not m:
        return None
    alt = lambda s: 1 if s in ("#", "♯") else -1 if s in ("b", "♭") else 0
    r = (NOTES[m.group(1)] + alt(m.group(2))) % 12
    s = m.group(3).strip().replace(":", "")
    table = {"": "maj", "maj": "maj", "M": "maj", "m": "min", "min": "min", "-": "min", "7": "7", "maj7": "maj7", "M7": "maj7",
             "m7": "min7", "min7": "min7", "dim": "dim", "°": "dim", "dim7": "dim7", "m7b5": "hdim7", "hdim7": "hdim7", "ø": "hdim7",
             "aug": "aug", "+": "aug", "sus2": "sus2", "sus4": "sus4", "sus": "sus4", "6": "maj6", "maj6": "maj6", "m6": "min6",
             "min6": "min6", "mM7": "minmaj7", "mmaj7": "minmaj7", "minmaj7": "minmaj7", "9": "7", "m9": "min7", "maj9": "maj7",
             "add9": "maj", "5": "maj"}
    q = table.get(s) or ("min" if (s.startswith("m") and not s.startswith("maj")) else "maj")
    b = (NOTES[m.group(4)] + alt(m.group(5))) % 12 if m.group(4) else None
    return r, q, (b if b != r else None)


DEGRES = {"1": 0, "2": 2, "3": 4, "4": 5, "5": 7, "6": 9, "7": 11, "9": 2, "11": 5, "13": 9}
QUALITES_HARTE = {"maj": "maj", "min": "min", "dim": "dim", "aug": "aug", "maj7": "maj7", "min7": "min7", "7": "7", "dim7": "dim7",
                  "hdim7": "hdim7", "minmaj7": "minmaj7", "maj6": "maj6", "min6": "min6", "sus2": "sus2", "sus4": "sus4",
                  "9": "7", "11": "7", "13": "7", "maj9": "maj7", "min9": "min7", "maj13": "maj7", "min11": "min7", "min13": "min7",
                  "5": "maj", "1": "maj", "": "maj"}


def lire_harte(label):
    """Accord en notation de Harte, celle des modèles de recherche : « C:maj », « A:min7/b7 », « G:maj/3 », « N ».
    Renvoie (fondamentale 0-11, qualité, basse ou None), ou None s'il n'y a pas d'accord."""
    m = re.match(r"^\s*([A-G])([#b]*)(?::([a-z0-9]+))?(?:\([^)]*\))?(?:/([#b]*)(\d+))?\s*$", str(label or ""))
    if not m:
        return None
    r = (NOTES[m.group(1)] + m.group(2).count("#") - m.group(2).count("b")) % 12
    q = QUALITES_HARTE.get(m.group(3) or "maj")
    if q is None:
        q = "min" if (m.group(3) or "").startswith("min") else "maj"
    b = None
    if m.group(5) in DEGRES:
        b = (r + DEGRES[m.group(5)] + m.group(4).count("#") - m.group(4).count("b")) % 12
    return r, q, (b if b != r else None)


def lire_magic(d):
    """Réduit la réponse d'analyse du service à ce qui nous sert. Renvoie None si elle est inutilisable."""
    try:
        bs = d.get("beat_structure") or {}
        temps = [float(x) for x in bs.get("beat_positions") or []]
        ligne = bs.get("barline_timeline") or {}
        barres = sorted({int(i) for i in ligne.get("barline_indices") or [] if 0 <= int(i) < len(temps)})
        accords = []
        for s in d.get("segments") or []:
            a = lire_accord(s.get("label"))
            accords.append((float(s["start"]), float(s["end"]), a))
        if len(temps) < 8:
            return None
        ton = lire_accord(d.get("key"))
        return {"temps": temps, "barres": barres, "metre": int(ligne.get("meter") or 4), "accords": accords,
                "tempo": float(d.get("tempo") or 0) or None, "duree": float(d.get("duration") or 0) or None,
                "tonalite": {"tonique": ton[0], "mode": "mineur" if ton[1] in FAMILLE_MINEURE else "majeur"} if ton else None,
                "confiance_mesures": ligne.get("confidence")}
    except Exception:
        return None


def lignes_de_whisper(d):
    """À défaut de paroles publiées, les phrases que Whisper a entendues servent de paroles : [(instant, texte)].
    Elles peuvent comporter des mots faux ; la partition le signale."""
    lignes = []
    for s in (d or {}).get("segments") or []:
        try:
            texte = " ".join(str(s.get("text") or "").split())
            if mot_nu(texte):
                lignes.append((float(s["start"]), texte))
        except Exception:
            continue
    return sorted(lignes)


def lire_whisper(d):
    """Mots transcrits par le service : [(début, fin, mot, probabilité)]."""
    mots = []
    for w in (d or {}).get("words") or []:
        try:
            if mot_nu(w["word"]):
                mots.append((float(w["start"]), float(w["end"]), w["word"].strip(), float(w.get("probability") or 0)))
        except Exception:
            continue
    return sorted(mots)


def accord_a(accords, t):
    """Accord du service à l'instant t (recherche dans des segments triés)."""
    lo, hi = 0, len(accords) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if t < accords[mid][0]:
            hi = mid - 1
        elif t >= accords[mid][1]:
            lo = mid + 1
        else:
            return accords[mid][2]
    return None


# ------------------------------------------------- vraies paroles sur mots transcrits
def decalage_global(lignes, vrais, transcrits, entendus):
    """Écart (instant entendu − horodatage des paroles) commun à tout le morceau, ou 0.
    Chaque mot assez long des paroles « vote » pour les écarts qui le séparent de ses occurrences entendues ; le vrai
    décalage est celui qui recueille nettement le plus de voix, les rencontres fortuites s'éparpillant. La méthode
    tolère une transcription pleine de trous ou d'erreurs, là où une comparaison mot à mot échoue."""
    if not transcrits:
        return 0.0
    ou = {}
    for i, m in enumerate(entendus):
        if len(m) >= 4:
            ou.setdefault(m, []).append(transcrits[i][0])
    par_ligne = {}
    for j, _, m in vrais:
        par_ligne.setdefault(j, []).append(m)
    votes = []
    for j, mots in par_ligne.items():
        t0 = lignes[j][0]
        duree = min((lignes[j + 1][0] - t0) if j + 1 < len(lignes) else 6.0, 12.0)
        for k, m in enumerate(mots):
            t = t0 + duree * k / max(1, len(mots))                  # instant supposé du mot dans sa ligne
            votes.extend(e - t for e in ou.get(m, ()) if abs(e - t) <= 30.0)
    if len(votes) < 8:
        return 0.0
    import collections
    urnes = collections.Counter(round(v) for v in votes)
    voix = lambda c: urnes[c - 1] + urnes[c] + urnes[c + 1]
    centre = max(urnes, key=voix)
    rivaux = [voix(c) for c in urnes if abs(c - centre) > 3]
    if voix(centre) < 8 or (rivaux and voix(centre) < 1.6 * max(rivaux)):
        return 0.0                                                  # pas de décalage qui se détache
    proches = sorted(v for v in votes if abs(v - centre) <= 1.5)
    decalage = proches[len(proches) // 2]
    return 0.0 if abs(decalage) < 0.6 or abs(decalage) > 25 else float(decalage)


def decalages_locaux(lignes, vrais, transcrits, entendus, portee=75.0):
    """Écart (instant entendu − horodatage des paroles) ligne par ligne, ou None si la transcription ne permet pas
    de le dire. Les paroles publiées suivent souvent une autre édition que la vidéo : intro plus longue, pont
    raccourci, refrain en moins. Le décalage change alors en cours de morceau, et un écart unique place bien le
    début mais mal la suite. Chaque ligne vote avec ses deux voisines de chaque côté ; une ligne sans majorité nette
    (refrain répété, mots mal entendus) prend l'écart de la ligne sûre la plus proche."""
    import collections
    if not transcrits or not lignes:
        return None
    ou = {}
    for i, m in enumerate(entendus):
        if len(m) >= 4:
            ou.setdefault(m, []).append(transcrits[i][0])
    par_ligne = {}
    for j, _, m in vrais:
        par_ligne.setdefault(j, []).append(m)
    votes = {}
    for j, mots in par_ligne.items():
        t0 = lignes[j][0]
        duree = min((lignes[j + 1][0] - t0) if j + 1 < len(lignes) else 6.0, 12.0)
        votes[j] = [e - (t0 + duree * k / max(1, len(mots))) for k, m in enumerate(mots) for e in ou.get(m, ())
                    if abs(e - (t0 + duree * k / max(1, len(mots)))) <= portee]
    surs = {}
    for j in par_ligne:
        urne = [v for x in range(j - 2, j + 3) for v in votes.get(x, ())]
        if len(urne) < 5:
            continue
        compte = collections.Counter(round(v) for v in urne)
        voix = lambda c: compte[c - 1] + compte[c] + compte[c + 1]
        centre = max(compte, key=voix)
        rivaux = [voix(c) for c in compte if abs(c - centre) > 3]
        if voix(centre) >= 5 and (not rivaux or voix(centre) >= 1.6 * max(rivaux)):
            proches = sorted(v for v in urne if abs(v - centre) <= 1.5)
            surs[j] = proches[len(proches) // 2]
    # une ligne « sûre » en désaccord avec ses deux voisines sûres, d'accord entre elles, s'est trompée de refrain
    ordre = sorted(surs)
    for a, j, b in zip(ordre, ordre[1:], ordre[2:]):
        if abs(surs[a] - surs[b]) <= 3.0 and abs(surs[j] - surs[a]) > 6.0:
            surs[j] = (surs[a] + surs[b]) / 2
    # les instants obtenus doivent rester dans l'ordre des paroles
    ordre, garde = sorted(surs), {}
    for j in ordre:
        if not garde or lignes[j][0] + surs[j] > lignes[max(garde)][0] + garde[max(garde)] - 0.5:
            garde[j] = surs[j]
    if len(garde) < 3:
        return None
    connus = sorted(garde)
    return [float(garde[min(connus, key=lambda c: (abs(c - j), c > j))]) for j in range(len(lignes))]


def caler_sur_transcription(lignes, transcrits):
    """Donne un instant à chaque mot des vraies paroles (lignes horodatées de LRCLIB) en s'appuyant sur les mots
    que Whisper a entendus : même mot, au voisinage de sa ligne. Les mots sans équivalent sont répartis entre
    leurs voisins. Renvoie ([(début, fin, mot, confiance, n° de ligne)], infos).
    Confiance 0.5 : mot retrouvé dans la transcription ; 0.15 : placé par interpolation."""
    vrais = [(j, w, mot_nu(w)) for j, (_, texte) in enumerate(lignes) for w in texte.split() if mot_nu(w)]
    if not vrais:
        return [], {}
    entendus = [mot_nu(w[2]) for w in transcrits]
    # Décalage global entre les horodatages des paroles et cet enregistrement (autre édition, silence en tête).
    decalage = decalage_global(lignes, vrais, transcrits, entendus)
    locaux = decalages_locaux(lignes, vrais, transcrits, entendus)
    ecart = lambda j: decalage           # les écarts locaux ne servent qu'au placement final, où l'écoute les départage
    resultat, trouves, k = [], 0, 0
    while k < len(vrais):
        j = vrais[k][0]
        fin_k = k
        while fin_k < len(vrais) and vrais[fin_k][0] == j:
            fin_k += 1
        mots = vrais[k:fin_k]
        t0 = lignes[j][0] + ecart(j)
        t1 = (lignes[j + 1][0] + ecart(j + 1)) if j + 1 < len(lignes) else t0 + 12.0
        t1 = max(t1, t0 + 1.0)
        t1 = min(t1, t0 + 20.0)
        cand = [i for i, w in enumerate(transcrits) if t0 - 2.5 <= w[0] <= t1 + 2.5]
        instants = [None] * len(mots)
        if cand:
            blocs = difflib.SequenceMatcher(None, [m[2] for m in mots], [entendus[i] for i in cand], autojunk=False).get_matching_blocks()
            for b in blocs:
                for x in range(b.size):
                    instants[b.a + x] = transcrits[cand[b.b + x]][:2]
        connus = [(i, v) for i, v in enumerate(instants) if v]
        trouves += len(connus)
        debut_l = max(0.0, min(t0, connus[0][1][0]) if connus else t0)
        fin_l = max(t1 - 0.3, connus[-1][1][1]) if connus else t1 - 0.3
        fin_l = max(fin_l, debut_l + 0.2 * len(mots))
        for i, (_, w, _) in enumerate(mots):
            if instants[i]:
                d, f, c = instants[i][0], instants[i][1], 0.5
            else:                                   # entre le mot connu d'avant et celui d'après, à parts égales
                avant = max((x for x in connus if x[0] < i), default=None, key=lambda x: x[0])
                apres = min((x for x in connus if x[0] > i), default=None, key=lambda x: x[0])
                ia, ta = (avant[0], avant[1][0]) if avant else (0, debut_l)
                ib, tb = (apres[0], apres[1][0]) if apres else (len(mots), fin_l)
                d = ta + (tb - ta) * (i - ia) / max(1, ib - ia)
                f, c = d + 0.2, 0.15
            resultat.append((round(max(0.0, d), 3), round(max(0.0, f), 3), w, c, j))
        k = fin_k
    # jamais deux mots dans le désordre : un mot ne commence pas avant le précédent
    for i in range(1, len(resultat)):
        if resultat[i][0] < resultat[i - 1][0]:
            d = resultat[i - 1][0] + 0.02
            resultat[i] = (d, max(d + 0.05, resultat[i][1]), *resultat[i][2:])
    return resultat, {"mots": len(vrais), "retrouves_dans_transcription": trouves, "decalage_paroles_s": round(decalage, 2),
                      "decalages_par_ligne": [round(x, 2) for x in locaux] if locaux else None}


# ------------------------------------------------------------------- assemblage
def assembler(temps, num, accords, mots, sub):
    """Mesures -> temps -> accord (si changement) + mots calés sur la subdivision du temps.
    accords : par temps, (fondamentale, qualité, confiance[, basse]) ou None."""
    temps = [float(x) for x in temps]
    n = len(temps)
    ecarts = sorted(b - a for a, b in zip(temps, temps[1:])) or [0.5]
    fins = temps[1:] + [temps[-1] + ecarts[len(ecarts) // 2]]
    cases = [{"t": round(t, 3), "accord": None, "mots": []} for t in temps]
    precedent = None
    for i, a in enumerate(accords):
        basse = a[3] if a and len(a) > 3 else None
        cle = (a[0], a[1], basse) if a else None
        if a and cle != precedent:
            cases[i]["accord"] = {"r": int(a[0]), "q": a[1], "conf": float(a[2])}
            if basse is not None:
                cases[i]["accord"]["b"] = int(basse)
        precedent = cle if a else precedent
    k = 0
    for debut, _fin, mot, conf, ligne in sorted(mots, key=lambda m: m[0]):
        while k + 1 < n and temps[k + 1] <= debut:
            k += 1
        i = k
        case = int(round((debut - temps[i]) / max(fins[i] - temps[i], 1e-6) * sub))
        if case >= sub:
            i, case = (i + 1, 0) if i + 1 < n else (i, sub - 1)
        cases[i]["mots"].append({"txt": mot, "pos": max(0, case), "conf": conf, "ligne": ligne})
    mesures = []
    for i, c in enumerate(cases):
        if not mesures or mesures[-1]["n"] != int(num[i]):
            mesures.append({"n": int(num[i]), "temps": []})
        mesures[-1]["temps"].append(c)
    return mesures


# ------------------------------------------------------------------------ bilan
def noter_bilan(resultats, bilan):
    """Inscrit (ou remplace) l'état d'un titre dans resultats/bilan.json, que lit la page « Ma playlist »."""
    f = Path(resultats) / "bilan.json"
    try:                                                       # ce passage possède ce titre : publier.py n'enregistre que les siens
        travail = Path(resultats).parent / "travail"
        travail.mkdir(exist_ok=True)
        with open(travail / "miens.txt", "a", encoding="utf-8") as m:
            m.write(bilan["id"] + "\n")
    except OSError:
        pass
    try:
        bilans = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except ValueError:
        bilans = []
    # L'heure de départ de l'analyse suit le titre tant que sa partition est provisoire : la page en tire l'avancement.
    ancien = next((b for b in bilans if b.get("id") == bilan["id"]), None)
    if ancien and ancien.get("analyse_debut") and "analyse_debut" not in bilan and (bilan.get("provisoire") or bilan.get("etat") == "en attente"):
        bilan = {**bilan, "analyse_debut": ancien["analyse_debut"]}
    bilans = [b for b in bilans if b.get("id") != bilan["id"]] + [bilan]
    f.write_text(json.dumps(bilans, ensure_ascii=False, indent=1), encoding="utf-8")


def ecrire_partition(resultats, bilan, mesures):
    """Écrit la partition (bilan + mesures) et met le bilan à jour."""
    Path(resultats).mkdir(parents=True, exist_ok=True)
    (Path(resultats) / (bilan["id"] + ".json")).write_text(
        json.dumps({**bilan, "mesures": mesures}, ensure_ascii=False, indent=1), encoding="utf-8")
    noter_bilan(resultats, bilan)
