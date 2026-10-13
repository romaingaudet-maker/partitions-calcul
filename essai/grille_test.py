#!/usr/bin/env python3
"""Grille test : toutes les propositions d'une recherche guitartabs.cc pour un morceau, triées puis recoupées en une
grille de consensus, posée dans le menu des accords de la partition sous le nom « grille test ».

1. Lit la page de recherche donnée (et ses pages suivantes), garde les liens dont le titre est celui du morceau,
   quel que soit le type (accords, tablature, basse…) ou l'interprète.
2. Écarte ce qui n'est pas une grille d'accords sur des paroles : tablatures, basse, batterie, pages sans accords
   lisibles, ou dont les accords ne s'accrochent pas aux paroles de la partition.
3. Remet chaque grille dans le ton du disque, puis écarte celles qui n'ont pas de sens : trop peu d'accord avec les
   autres grilles (comparées temps par temps, chacune contre le consensus des autres).
4. Les grilles retenues votent temps par temps ; notre analyse comble ce qu'aucune ne couvre (intro, solos).
Le rapport (pages lues, raison de chaque mise à l'écart, entente) est rangé dans la partition, sans aucune parole.

Usage : DONNEES=<dépôt des données> RECHERCHE=<adresse de recherche guitartabs> TITRE=<id de la partition>
        [PUBLIER=1] grille_test.py"""
import json
import os
import re
import sys
import time
import urllib.parse

import commun as C
import grilles as G
import grilles_au_choix as X

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


def main():
    recherche, ident = os.environ["RECHERCHE"], os.environ["TITRE"]
    f = X.RESULTATS / (ident + ".json")
    emp = X.empreinte(f)
    p = json.loads(f.read_text(encoding="utf-8"))
    temps, accords, mots = X.depuis_la_partition(p)
    n = len(temps)
    rapport = {"recherche": recherche, "date": time.strftime("%Y-%m-%d %H:%M"), "pages": []}
    adresses = liens_de_recherche(recherche, p["titre"])
    print(f"{len(adresses)} propositions pour ce titre", flush=True)

    # 1-2. Lecture et premier tri : une grille d'accords accrochée aux paroles
    candidates = []
    for u in adresses:
        ligne = {"page": u, "genre": genre(u)}
        rapport["pages"].append(ligne)
        if ligne["genre"] in ("guitar pro",):
            ligne["ecartee"] = "fichier Guitar Pro, pas de texte à lire"
            continue
        try:
            if not G.admis(u):
                ligne["ecartee"] = "lecture non admise par le site"
                continue
            page = G._lire(u)
            time.sleep(1.0)                                        # sans presser le site
        except Exception as e:
            ligne["ecartee"] = f"illisible ({type(e).__name__})"
            continue
        acc = G.accords_de_la_page(page)
        ligne["accords_lus"], ligne["accords_differents"] = len(acc), len(set(acc))
        if len(acc) < 8 or len(set(acc)) < 3:
            ligne["ecartee"] = "pas une grille d'accords (tablature, notes ou accords trop rares)"
            continue
        lignes = G.lignes_avec_accords(page)
        ligne["lignes_avec_accords"] = len(lignes)
        if len(lignes) < 4:
            ligne["ecartee"] = "accords sans paroles dessous : impossible de les caler"
            continue
        cands = G.accrocher_aux_mots([(lignes, u)], mots, temps, C.lire_accord)
        if not cands:
            ligne["ecartee"] = "ses paroles ne correspondent pas à celles du morceau"
            continue
        sur = cands[0][0]
        couverts = sum(a is not None for a in sur)
        ligne["temps_couverts"] = couverts
        if couverts < 24:
            ligne["ecartee"] = "trop peu d'accords calés sur les paroles"
            continue
        # Dans le ton du disque : la transposition qui s'accorde le mieux avec notre écoute
        communs = [i for i in range(n) if sur[i] is not None and accords[i] is not None]
        d = max(range(12), key=lambda d: sum((sur[i][0] + d) % 12 == accords[i][0] for i in communs)) if communs else 0
        ligne["transposition"] = d if d <= 6 else d - 12
        candidates.append((u, [None if a is None else ((a[0] + d) % 12, a[1], None if a[2] is None else (a[2] + d) % 12) for a in sur], ligne))
    print(f"{len(candidates)} grilles d'accords calées sur les paroles", flush=True)

    # 3. Les grilles sans queue ni tête : peu d'entente avec le consensus des autres (on retire la pire, on recommence)
    gardees = list(candidates)
    while len(gardees) >= 3:
        scores = []
        for k, (u, g, ligne) in enumerate(gardees):
            autres = voter([x[1] for j, x in enumerate(gardees) if j != k], n)
            e, nb = entente(g, autres)
            ligne["entente_avec_les_autres"] = None if e is None else round(e, 2)
            scores.append((e if e is not None else 0.0, k))
        pire, k = min(scores)
        if pire >= SEUIL_SENS:
            break
        gardees[k][2]["ecartee"] = f"trop différente des autres grilles ({int(pire * 100)} % d'accords communs)"
        del gardees[k]
    if len(gardees) == 2:
        e, _ = entente(gardees[0][1], gardees[1][1])
        if e is not None and e < SEUIL_SENS:                       # deux grilles en désaccord : l'écoute départage
            def avec_ecoute(g):
                c = [i for i in range(n) if g[i] is not None and accords[i] is not None]
                return sum(cle(g[i]) == cle(accords[i]) for i in c) / max(1, len(c))
            k = min((0, 1), key=lambda k: avec_ecoute(gardees[k][1]))
            gardees[k][2]["ecartee"] = f"en désaccord avec l'autre grille ({int(e * 100)} % d'accords communs) et moins proche de l'écoute"
            del gardees[k]
    if not gardees:
        print("aucune grille retenue", flush=True)
        return 1

    # 4. Vote : chaque grille pèse son entente avec les autres
    poids = [max(0.2, x[2].get("entente_avec_les_autres") or 0.5) for x in gardees]
    consensus = voter([x[1] for x in gardees], n, poids)
    for u, g, ligne in gardees:
        ligne["retenue"] = True
    changements, avant = [], None
    for i in range(n):
        a = consensus[i]
        if a is not None:
            x = (a[0], a[1], -1 if a[2] is None else a[2])
        elif accords[i] is not None:
            x = (int(accords[i][0]), accords[i][1], -1 if accords[i][3] is None else int(accords[i][3]))
        else:
            x = None
        if x is not None and x != avant:
            changements.append([i, *x] + ([1] if a is None else []))
        avant = x if x is not None else avant
    communs = [i for i in range(n) if consensus[i] is not None and accords[i] is not None]
    accord = round(sum(consensus[i][0] == accords[i][0] for i in communs) / len(communs), 2) if communs else None
    rapport.update({"grilles_lues": len(adresses), "grilles_calees": len(candidates), "grilles_retenues": len(gardees),
                    "temps_couverts": sum(a is not None for a in consensus), "accord_avec_notre_analyse": accord})
    test = {"nom": "grille test", "accord": accord, "transposition": 0, "changements": changements,
            "sites": [x[0] for x in gardees]}
    variantes = [v for v in (p.get("variantes_accords") or []) if not v["nom"].startswith("grille test")]
    variantes.insert(0, test)
    for l in rapport["pages"]:
        print("  ", json.dumps(l, ensure_ascii=False), flush=True)
    print({k: v for k, v in rapport.items() if k != "pages"}, flush=True)

    if os.environ.get("PUBLIER"):
        # La variante et le rapport partent ensemble ; publier() n'écrit que variantes_accords, on y glisse le rapport.
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
