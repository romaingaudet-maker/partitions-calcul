#!/usr/bin/env python3
"""Ébauche : la toute première version d'une partition, publiée en quelques secondes, avant toute écoute.
Elle ne lit que ce qui est déjà écrit ailleurs : les paroles publiées et horodatées à la ligne (LRCLIB), et les grilles
d'accords de quelques sites, accrochées à ces paroles et recoupées entre elles (voir grilles.py). Les mots sont
répartis dans leur ligne et la pulsation est une pulsation d'attente, régulière : les vrais temps, la place exacte des
accords et des mots viennent avec les étapes suivantes (esquisse, grille, partition), qui remplacent l'ébauche.
Rien n'est publié si une partition existe déjà, si les paroles horodatées manquent ou si aucune grille ne s'accroche.
Aucun module à installer : ce passage tourne dès le début, pendant que le reste se prépare."""
import json
import os
import sys
import time
from pathlib import Path

import commun as C
import grilles as G
import silencieux as S

BATTUE = 0.5                                                 # pulsation d'attente (120 à la noire), quatre temps par mesure


def ebaucher(d):
    t0 = time.time()
    artiste, titre, ident = S.identite(d)
    if S.deja_complete(ident) or (S.RESULTATS / (ident + ".json")).exists():
        print("  ébauche inutile : une partition existe déjà", flush=True)
        return None
    avec = []
    G.chercher_grilles(artiste, titre, en_tout=20, avec_paroles=avec)
    if not avec:
        print("  pas d'ébauche : aucune grille en ligne ne porte ses accords sur des paroles", flush=True)
        return None
    lignes, source = C.chercher_paroles({"artiste": artiste, "titre": titre}, d.get("duree"), None)
    lignes = [(t, x) for t, x in lignes if x.strip()]
    if len(lignes) < 4:
        print("  pas d'ébauche : pas de paroles horodatées", flush=True)
        return None
    mots = []                                               # les mots, répartis dans leur ligne
    for j, (t, texte) in enumerate(lignes):
        ws = texte.split()
        fin = min(lignes[j + 1][0] if j + 1 < len(lignes) else t + 6.0, t + max(2.0, 0.45 * len(ws)))
        for k, w in enumerate(ws):
            a = t + (fin - t) * k / max(1, len(ws))
            mots.append((round(a, 3), round(a + 0.2, 3), w, 0.15, j))
    duree = float(d.get("duree") or 0) or lignes[-1][0] + 10.0
    temps = [round(i * BATTUE, 3) for i in range(int(duree / BATTUE) + 1)]
    sur, infos = G.recouper_entre_elles(G.accrocher_aux_mots(avec, mots, temps, C.lire_accord), len(temps))
    print("  grilles recoupées entre elles :", {k: v for k, v in infos.items() if k != "detail"}, flush=True)
    for g in infos.get("detail", []):
        print("     ", g, flush=True)
    if infos.get("temps_couverts", 0) < 16:
        print("  pas d'ébauche : les grilles ne s'accrochent pas aux paroles", flush=True)
        return None
    accords, courant = [], None
    for a in sur:                                           # hors des passages couverts, l'accord précédent est tenu
        courant = (a[0], a[1], 0.4, a[2]) if a else courant
        accords.append(courant)
    num = [i // 4 + 1 for i in range(len(temps))]
    mesures = C.assembler(temps, num, accords, mots, 2)
    bilan = {"id": ident, "artiste": artiste, "titre": titre, "durees_s": {"ebauche": round(time.time() - t0, 1)}, "alertes": [],
             "complet": True, "etat": "ok", "format": 2, "paroles": source, "grilles_en_ligne_sur_les_mots": infos,
             "methodes": {"temps": "pulsation d'attente", "accords": "grilles en ligne recoupées entre elles", "mots": "paroles LRCLIB, réparties dans leur ligne"},
             "source": {"source": "aucune écoute encore", "video": d.get("video"), "chaine": artiste, "duree": d.get("duree")},
             "duree_audio": round(duree, 1), "tempo": round(60.0 / BATTUE, 1), "temps_par_mesure": 4, "subdivision": 2,
             "tonalite": G.tonalite_de(sur), "nb_mesures": len(mesures), "provisoire": {"etape": "ebauche"}}
    C.ecrire_partition(S.RESULTATS, bilan, mesures)
    S.publier(f"Ébauche : {artiste} - {titre}")
    print(f"  ébauche publiée après {time.time() - t0:.0f} s : {len(mesures)} mesures, {len(mots)} mots, "
          f"{sum(1 for m in mesures for c in m['temps'] if c['accord'])} accords", flush=True)
    return bilan


if __name__ == "__main__":
    # DEMANDE : le fichier d'une demande, ou « * » pour toutes celles qui attendent (l'aiguillage les ébauche dès leur
    # arrivée, sans attendre qu'une machine d'analyse se libère).
    voulue = os.environ.get("DEMANDE", "").strip()
    dossier = S.RACINE / "demandes"
    fichiers = sorted(dossier.glob("*.json")) if voulue == "*" else [dossier / Path(voulue).name] if voulue else []
    fichiers = [f for f in fichiers if f.exists()]
    if not fichiers:
        print("Ébauche : pas de demande à traiter.", flush=True)
    for f in fichiers:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(f"== Ébauche : {d.get('artiste')} — {d.get('titre')}", flush=True)
            ebaucher(d)
        except Exception as e:                              # l'ébauche est un bonus : son échec ne gêne rien
            print(f"  ébauche impossible : {type(e).__name__} {str(e)[:160]}", flush=True)
