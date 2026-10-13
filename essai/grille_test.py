#!/usr/bin/env python3
"""Grille test : toutes les propositions d'une recherche guitartabs.cc pour un morceau, recoupées en une grille posée
dans le menu des accords de la partition sous le nom « grille test ».

1. Base : les accords changent sur le premier temps de la mesure. Chaque changement d'une grille est ramené au premier
   temps le plus proche ; deux changements dans la même mesure la partagent en deux (troisième temps).
2. Seuls les fichiers d'accords (« chords ») servent : tablatures, basse, batterie, Guitar Pro sont écartés. Ceux dont
   les accords s'accrochent aux paroles sont calés mesure par mesure et votent ; tous (calés ou non) donnent les suites
   d'accords du morceau, qui départagent les mesures douteuses et remplissent intro et solos (en suivant l'écoute).
   Une grille sans rapport avec les autres (peu d'accords communs) est écartée.
3. Renversements : les fichiers qui en écrivent (Lab/Do…) indiquent, accord par accord et selon l'enchaînement, la
   basse à ajouter là où les autres ne la donnent pas.
Le rapport (pages lues, raison de chaque mise à l'écart, renversements ajoutés) est rangé dans la partition, sans paroles.

Usage : DONNEES=<dépôt des données> RECHERCHE=<adresse de recherche guitartabs> TITRE=<id de la partition>
        [PUBLIER=1] grille_test.py"""
import json
from collections import Counter, defaultdict
import os
import re
import sys
import time
import urllib.parse

import commun as C
import grilles as G
import grilles_au_choix as X

NOMS = ["Do", "Do#", "Ré", "Mib", "Mi", "Fa", "Fa#", "Sol", "Lab", "La", "Sib", "Si"]
SEUIL_SENS = 0.45            # entente minimale d'une grille avec le consensus des autres (racine et mode, temps par temps)


def liens_de_recherche(url, titre, pages_max=6):
    """Adresses des propositions dont le titre est celui du morceau, sur la page de recherche et ses suivantes."""
    voulu = G.limace(G._court(titre), "")
    vus, a_lire, lues = [], [url], set()
    while a_lire and len(lues) < pages_max:
        u = a_lire.pop(0)
        if u in lues or not G.admis(u):
            continue
        lues.add(u)
        page = G._lire(u, 600_000)
        for l in re.findall(r'href=["\'](/tabs/[^"\'#?]+\.html)["\']', page):
            nom = l.rsplit("/", 1)[1][:-5]
            nom = re.sub(r"_ver_\d+$", "", nom)
            morceau = re.sub(r"_(crd|tab|btab|bass|drum|drm|pro|uke|ukulele|chords)$", "", nom)
            if G.limace(morceau, "") == voulu and l not in vus:
                vus.append(l)
        for suite in re.findall(r'href=["\'](/?search\.php\?[^"\'#]+)["\']', page):
            s = urllib.parse.urljoin(url, suite.replace("&amp;", "&"))
            if "page=" in s and s not in lues and s not in a_lire:
                a_lire.append(s)
    return ["https://www.guitartabs.cc" + l for l in vus]


def genre(adresse):
    nom = re.sub(r"_ver_\d+$", "", adresse.rsplit("/", 1)[1][:-5])
    m = re.search(r"_(crd|tab|btab|bass|drum|drm|pro|uke|ukulele)$", nom)
    return {"crd": "accords", "tab": "tablature", "btab": "basse", "bass": "basse", "drum": "batterie", "drm": "batterie",
            "pro": "guitar pro", "uke": "ukulélé", "ukulele": "ukulélé"}.get(m.group(1) if m else "", "autre")


def cle(a):
    return None if a is None else (a[0], a[1] in G.MINEURES)


def entente(x, y):
    communs = [i for i in range(len(x)) if x[i] is not None and y[i] is not None]
    if len(communs) < 12:
        return None, len(communs)
    return sum(cle(x[i]) == cle(y[i]) for i in communs) / len(communs), len(communs)


def voter(grilles, n, poids=None):
    sortie = [None] * n
    for i in range(n):
        voix = {}
        for k, g in enumerate(grilles):
            if g[i] is not None:
                voix.setdefault(cle(g[i]), [0.0, g[i]])[0] += 1.0 if poids is None else poids[k]
        if voix:
            sortie[i] = max(voix.values(), key=lambda v: v[0])[1]
    return sortie


def transposer(a, d):
    return None if a is None else ((a[0] + d) % 12, a[1], None if a[2] is None else (a[2] + d) % 12)


def main():
    recherche, ident = os.environ["RECHERCHE"], os.environ["TITRE"]
    f = X.RESULTATS / (ident + ".json")
    emp = X.empreinte(f)
    p = json.loads(f.read_text(encoding="utf-8"))
    temps, accords, mots = X.depuis_la_partition(p)
    n = len(temps)
    # Les mesures : premier temps de chacune ; une mesure de 4 temps offre aussi son 3e temps (demi-mesure)
    debuts, k = [], 0
    for m in p["mesures"]:
        debuts.append((k, len(m["temps"])))
        k += len(m["temps"])
    cases = []                                              # (temps de départ, temps de fin exclu) de chaque case
    for d0, nb in debuts:
        if nb >= 4:
            cases += [(d0, d0 + nb // 2), (d0 + nb // 2, d0 + nb)]
        else:
            cases.append((d0, d0 + nb))
    case_du_temps = [0] * n
    for c, (a, b) in enumerate(cases):
        for i in range(a, b):
            case_du_temps[i] = c
    premier = [c for c, (a, _) in enumerate(cases) if any(a == d0 for d0, _ in debuts)]
    est_premier = set(premier)
    # Notre écoute, case par case : l'accord tenu sur la plus grande part de la case
    ecoute = []
    for a, b in cases:
        cpt = Counter(cle(accords[i]) for i in range(a, b) if accords[i] is not None)
        if cpt:
            c = cpt.most_common(1)[0][0]
            ecoute.append(next((int(accords[i][0]), accords[i][1], None) for i in range(a, b) if cle(accords[i]) == c))
        else:
            ecoute.append(None)

    rapport = {"recherche": recherche, "date": time.strftime("%Y-%m-%d %H:%M"), "pages": []}
    adresses = liens_de_recherche(recherche, p["titre"])
    print(f"{len(adresses)} propositions pour ce titre", flush=True)

    # 1. Les fichiers d'accords seulement
    fichiers = []                                           # (adresse, suite brute des accords, lignes, rapport)
    for u in adresses:
        ligne = {"page": u, "genre": genre(u)}
        rapport["pages"].append(ligne)
        if ligne["genre"] != "accords":
            ligne["ecartee"] = "pas un fichier d'accords (" + ligne["genre"] + ")"
            continue
        try:
            if not G.admis(u):
                ligne["ecartee"] = "lecture non admise par le site"
                continue
            page = G._lire(u)
            time.sleep(1.0)
        except Exception as e:
            ligne["ecartee"] = f"illisible ({type(e).__name__})"
            continue
        suite = [a for a in (C.lire_accord(x) for x in G.accords_de_la_page(page)) if a]
        ligne["accords_lus"] = len(suite)
        if len(suite) < 8 or len({cle(a) for a in suite}) < 3:
            ligne["ecartee"] = "trop peu d'accords lisibles"
            continue
        ligne["renversements"] = sum(1 for a in suite if a[2] is not None)
        fichiers.append((u, suite, G.lignes_avec_accords(page), ligne))

    # 2a. Les fichiers calés sur les paroles, ramenés aux premiers temps, case par case
    cales = []                                              # (adresse, accord par case ou None, rapport, transposition)
    for u, suite, lignes, ligne in fichiers:
        cands = G.accrocher_aux_mots([(lignes, u)], mots, temps, C.lire_accord) if len(lignes) >= 4 else []
        if not cands or sum(a is not None for a in cands[0][0]) < 24:
            ligne["calee"] = False
            continue
        sur = cands[0][0]
        communs = [i for i in range(n) if sur[i] is not None and accords[i] is not None]
        d = max(range(12), key=lambda d: sum((sur[i][0] + d) % 12 == accords[i][0] for i in communs)) if communs else 0
        sur = [transposer(a, d) for a in sur]
        par_case = [None] * len(cases)
        couverte = [False] * len(cases)
        for i in range(n):
            if sur[i] is not None:
                couverte[case_du_temps[i]] = True
        # Chaque changement va au premier temps le plus proche : sur les temps 1-2, celui de sa mesure ; sur les temps
        # 3-4, celui de la mesure suivante (accord écrit sur une syllabe qui précède le temps fort). Deux changements
        # qui tombent sur le même premier temps se partagent la mesure : le second prend le 3e temps.
        poses = {}
        for i in range(n):
            if sur[i] is None or (i and sur[i - 1] == sur[i]):
                continue
            m = max(x for x, (d0, _) in enumerate(debuts) if d0 <= i)
            d0, nb = debuts[m]
            if i - d0 >= max(1, nb // 2) and m + 1 < len(debuts):
                m += 1
            c = case_du_temps[debuts[m][0]]
            if c in poses and cle(poses[c]) != cle(sur[i]) and c + 1 < len(cases) and c + 1 not in est_premier and c + 1 not in poses:
                c += 1
            poses[c] = sur[i]
        courant = None
        for c in range(len(cases)):
            courant = poses.get(c, courant)
            if couverte[c] or c in poses:
                par_case[c] = courant
        ligne.update({"calee": True, "transposition": d if d <= 6 else d - 12, "cases_couvertes": sum(x is not None for x in par_case)})
        cales.append([u, par_case, ligne, d])
    print(f"{len(fichiers)} fichiers d'accords, dont {len(cales)} calés sur les paroles", flush=True)

    # Écarter les grilles sans rapport : on retire la pire tant qu'elle s'entend mal avec les autres
    while len(cales) >= 3:
        scores = []
        for k, (u, g, ligne, _) in enumerate(cales):
            e, _ = entente(g, voter([x[1] for j, x in enumerate(cales) if j != k], len(cases)))
            ligne["entente_avec_les_autres"] = None if e is None else round(e, 2)
            scores.append((e if e is not None else 0.0, k))
        pire, k = min(scores)
        if pire >= SEUIL_SENS:
            break
        cales[k][2]["ecartee"] = f"trop différente des autres grilles ({int(pire * 100)} % d'accords communs)"
        del cales[k]

    # 2b. Les suites d'accords de tous les fichiers d'accords, dans le ton du disque
    histo = Counter(cle(a) for _, g, _, _ in cales for a in g if a is not None) or Counter(cle(a) for a in ecoute if a)
    suites = []
    # Une grille écartée du vote (calée trop loin des autres) garde ses suites d'accords et ses renversements : c'est
    # son placement qui était douteux, pas forcément ses accords.
    for u, suite, lignes, ligne in fichiers:
        d = next((x[3] for x in cales if x[0] == u), None)
        if d is None and ligne.get("calee"):
            d = ligne["transposition"] % 12
        if d is None:                                       # non calé : le ton où ses accords recouvrent le mieux les autres
            d = max(range(12), key=lambda d: sum(histo.get(cle(transposer(a, d)), 0) for a in suite))
            ligne["transposition"] = d if d <= 6 else d - 12
        s = [transposer(a, d) for a in suite]
        suites.append((u, [a for j, a in enumerate(s) if j == 0 or cle(a) != cle(s[j - 1]) or a[2] != s[j - 1][2]]))
    enchainements = Counter()
    for _, s in suites:
        for x, y in zip(s, s[1:]):
            enchainements[(cle(x), cle(y))] += 1
    vocabulaire = Counter(cle(a) for _, s in suites for a in s)
    exemplaire = {}
    for _, s in suites:
        for a in s:
            exemplaire.setdefault(cle(a), (a[0], a[1], None))

    # Vote case par case ; à défaut, la suite d'accords et l'écoute
    poids = [max(0.2, x[2].get("entente_avec_les_autres") or 0.6) for x in cales]
    grille, comblee = [None] * len(cases), [False] * len(cases)
    for c in range(len(cases)):
        voix = defaultdict(float)
        for (u, g, _, _), w in zip(cales, poids):
            if g[c] is not None:
                voix[cle(g[c])] += w
        if voix:
            meilleurs = sorted(voix.items(), key=lambda v: -v[1])
            choix = meilleurs[0][0]
            if len(meilleurs) > 1 and meilleurs[1][1] >= 0.8 * meilleurs[0][1] and c > 0 and grille[c - 1]:
                # ex æquo : l'enchaînement le plus courant dans les fichiers d'accords
                choix = max([m for m, v in meilleurs if v >= 0.8 * meilleurs[0][1]],
                            key=lambda m: enchainements[(cle(grille[c - 1]), m)])
            grille[c] = next(g[c] for g, _ in ((x[1], 0) for x in cales) if g[c] is not None and cle(g[c]) == choix)
        elif c in est_premier or (c > 0 and grille[c - 1] is None):
            e = ecoute[c]
            if e is not None:
                if cle(e) in vocabulaire:
                    grille[c] = exemplaire[cle(e)]
                else:                                       # l'écoute hors des suites connues : l'accord connu de même fondamentale
                    proches = [m for m in vocabulaire if m[0] == e[0]]
                    grille[c] = exemplaire[max(proches, key=lambda m: vocabulaire[m])] if proches else e
                comblee[c] = True
        if grille[c] is None and c > 0:                     # 3e temps sans changement : l'accord du 1er temps continue
            grille[c], comblee[c] = grille[c - 1], comblee[c - 1]

    # 3. Renversements : appris des fichiers qui en écrivent, selon l'accord et ce qui l'entoure
    avec_renv = [(u, s) for u, s in suites if any(a[2] is not None for a in s)]
    for l in rapport["pages"]:
        if any(l["page"] == u for u, _ in avec_renv):
            l["source_des_renversements"] = True
    par_contexte, par_accord = defaultdict(Counter), defaultdict(Counter)
    for _, s in avec_renv:
        for j, a in enumerate(s):
            av = cle(s[j - 1]) if j else None
            ap = cle(s[j + 1]) if j + 1 < len(s) else None
            par_contexte[(av, cle(a), ap)][a[2]] += 1
            par_accord[cle(a)][a[2]] += 1
    ajoutes = Counter()
    sequence = [c for c in range(len(cases)) if grille[c] is not None and (c == 0 or grille[c - 1] is None or cle(grille[c]) != cle(grille[c - 1]))]
    for x, c in enumerate(sequence):
        a = grille[c]
        if a[2] is not None:
            continue
        av = cle(grille[sequence[x - 1]]) if x else None
        ap = cle(grille[sequence[x + 1]]) if x + 1 < len(sequence) else None
        basse = None
        ctx = par_contexte.get((av, cle(a), ap))
        if ctx and sum(ctx.values()) >= 2:
            b, nb = ctx.most_common(1)[0]
            if b is not None and nb / sum(ctx.values()) >= 0.5:
                basse = b
        elif par_accord.get(cle(a)):
            b, nb = par_accord[cle(a)].most_common(1)[0]
            if b is not None and nb / sum(par_accord[cle(a)].values()) >= 0.6:
                basse = b
        if basse is not None:
            fin = sequence[x + 1] if x + 1 < len(sequence) else len(cases)
            for y in range(c, fin):
                if grille[y] is not None and cle(grille[y]) == cle(a):
                    grille[y] = (a[0], a[1], basse)
            ajoutes[f"{NOMS[a[0]]}{'m' if a[1] in G.MINEURES else ''}/{NOMS[basse]}"] += 1

    # La grille en changements d'accords, aux temps des cases
    changements, avant = [], None
    for c, (a, _) in enumerate(cases):
        g = grille[c]
        if g is None:
            continue
        x = (int(g[0]), g[1], -1 if g[2] is None else int(g[2]))
        if x != avant:
            changements.append([a, *x] + ([1] if comblee[c] else []))
        avant = x
    hors_premier = sum(1 for ch in changements if not any(ch[0] == d0 for d0, _ in debuts))
    communs = [c for c in range(len(cases)) if grille[c] is not None and not comblee[c] and ecoute[c] is not None]
    accord = round(sum(grille[c][0] == ecoute[c][0] for c in communs) / len(communs), 2) if communs else None
    for u, g, ligne, _ in cales:
        ligne["retenue"] = True
    rapport.update({"propositions": len(adresses), "fichiers_d_accords": len(fichiers), "calees": len(cales),
                    "avec_renversements": len(avec_renv), "renversements_ajoutes": dict(ajoutes),
                    "cases_par_les_grilles": sum(1 for c in range(len(cases)) if grille[c] is not None and not comblee[c]),
                    "cases_par_l_ecoute": sum(comblee), "cases": len(cases),
                    "changements_hors_premier_temps": hors_premier, "accord_avec_notre_analyse": accord})
    for l in rapport["pages"]:
        print("  ", json.dumps(l, ensure_ascii=False), flush=True)
    print({k: v for k, v in rapport.items() if k != "pages"}, flush=True)
    if not cales:
        print("aucune grille calée : rien de publié", flush=True)
        return 1
    test = {"nom": "grille test", "accord": accord, "transposition": 0, "changements": changements, "sites": [x[0] for x in cales]}
    variantes = [v for v in (p.get("variantes_accords") or []) if not v["nom"].startswith("grille test")]
    variantes.insert(0, test)

    if os.environ.get("PUBLIER"):
        X.publier({ident: (emp, variantes)})
        f2 = X.RESULTATS / (ident + ".json")
        q = json.loads(f2.read_text(encoding="utf-8"))
        if any(v["nom"] == "grille test" for v in q.get("variantes_accords", [])):
            q["grille_test"] = rapport
            mesures = q.pop("mesures")
            f2.write_text(json.dumps({**q, "mesures": mesures}, ensure_ascii=False, indent=1), encoding="utf-8")
            X.git("add", str(f2.relative_to(X.RACINE)))
            X.git("commit", "-q", "-m", "Grille test : rapport")
            for _ in range(5):
                if X.git("push", "-q", "origin", "HEAD:main").returncode == 0:
                    break
                X.git("pull", "-q", "--rebase", "origin", "main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
