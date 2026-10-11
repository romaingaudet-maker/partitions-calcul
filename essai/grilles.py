#!/usr/bin/env python3
"""Cherche en ligne une grille d'accords du morceau, pour corroborer ce que l'analyse entend.
Les sites de grilles publient leurs accords en texte : on interroge le moteur de recherche de quelques-uns, on lit la
page du morceau et on n'en retient que les symboles d'accords, dans l'ordre. Ni paroles ni page ne sont gardées.
Chaque site est interrogé en se présentant pour ce qu'on est, et seulement si son robots.txt l'admet ; un refus est
accepté tel quel, sans détour. Ce canal est facultatif : chaque échec est silencieux, et c'est l'analyse qui décide
ensuite si la grille ressemble assez à ce qu'elle entend pour être écoutée (comparaison en degrés, voir caler_grille).
(La recherche d'images de grilles, essayée d'abord, ne ramenait que des images sans rapport : abandonnée.)"""
import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

AGENT = "Mozilla/5.0 (X11; Linux x86_64) partitions-personnelles/1.0"
NOTE = r"(?:[A-G](?:#|b)?|(?:Do|Ré|Re|Mi|Fa|Sol|La|Si)(?:#|b)?)"
ACCORD = re.compile(rf"^{NOTE}(?:m|min|maj|M|dim|aug|sus|add|°|\+|[0-9])*(?:/{NOTE})?$")
_robots = {}


def _lire(url, limite=3_000_000, attente=10, en_tout=20):
    """Lit une page, sans jamais y passer plus de `en_tout` secondes (un site peut répondre au compte-gouttes)."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7"})
    fin, morceaux, taille = time.monotonic() + en_tout, [], 0
    with urllib.request.urlopen(req, timeout=attente) as rep:
        while taille < limite:
            if time.monotonic() > fin:
                raise TimeoutError("page trop lente")
            bloc = rep.read(65536)
            if not bloc:
                break
            morceaux.append(bloc)
            taille += len(bloc)
    return b"".join(morceaux)[:limite].decode("utf-8", "replace")


def admis(url):
    """Le robots.txt du site nous laisse-t-il lire cette page ? Sans réponse claire, on s'abstient."""
    p = urllib.parse.urlparse(url)
    if p.netloc not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(_lire(f"{p.scheme}://{p.netloc}/robots.txt", 500_000).splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                rp.parse([])                                            # pas de robots.txt : rien n'est interdit
            else:
                print(f"      {p.netloc} : robots.txt refusé ({e.code})", flush=True)
                rp = None
        except Exception as e:
            print(f"      {p.netloc} : robots.txt illisible ({type(e).__name__})", flush=True)
            rp = None
        _robots[p.netloc] = rp
    rp = _robots[p.netloc]
    return rp is not None and rp.can_fetch(AGENT, url)


def limace(s, liaison="-"):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", liaison, s).strip(liaison)


def accords_de_la_page(page):
    """Symboles d'accords d'une page, dans l'ordre : d'après le balisage quand le site marque ses accords, sinon
    d'après les lignes faites surtout d'accords."""
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    marques = [html.unescape(m.group(1)).strip() for m in re.finditer(
        r"(?is)<(?:span|b|a|i|strong|sup|div)\b[^>]*(?:class|data-name)=[\"'][^\"']*(?:chord|accord|acorde|cifra)[^\"']*[\"'][^>]*>\s*([^<]{1,12}?)\s*<", page)]
    marques += [m.group(1) for m in re.finditer(r"data-chord=[\"']([^\"']{1,12})[\"']", page)]
    pre = [html.unescape(m.group(1)).strip() for bloc in re.findall(r"(?is)<pre\b.*?</pre>", page)
           for m in re.finditer(r"(?is)<b\b[^>]*>\s*([^<]{1,12}?)\s*</b>", bloc)]
    texte = html.unescape(re.sub(r"(?s)<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</(p|div|pre|li|tr|h\d)>", "\n", page)))
    lignes = []
    for ligne in texte.replace("♯", "#").replace("♭", "b").splitlines():
        jetons = [j for j in re.split(r"[\s|,;()\[\]–—-]+", ligne) if j]
        bons = [j for j in jetons if ACCORD.match(j)]
        if len(bons) >= 2 and len(bons) >= 0.6 * len(jetons):
            lignes.extend(bons)
    crochets = [m.group(1) for m in re.finditer(rf"\[({NOTE}[^\]\s]{{0,8}})\]", texte)]
    propre = lambda L: [a for a in (x.replace("♯", "#").replace("♭", "b") for x in L) if ACCORD.match(a)]
    return max((propre(marques), propre(pre), lignes, propre(crochets)), key=len)[:600]


def _nu(mot):
    m = unicodedata.normalize("NFD", mot.lower().replace("œ", "oe").replace("æ", "ae"))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in m if unicodedata.category(c) != "Mn"))


def lignes_avec_accords(page):
    """Les lignes chantées d'une page de grille, chacune avec ses accords accrochés aux mots sous lesquels le site les
    a écrits : [(mots réduits à leurs lettres, [(n° du mot, symbole)])]. Deux écritures sont comprises : la ligne
    d'accords posée au-dessus de la ligne de paroles (les colonnes disent quel mot), et les accords entre crochets
    au fil du texte. Les paroles ne servent qu'à retrouver la place des accords : elles ne sont ni gardées ni écrites."""
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    blocs = re.findall(r"(?is)<pre\b[^>]*>(.*?)</pre>", page)
    brut = max(blocs, key=len) if blocs else re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", page)
    texte = html.unescape(re.sub(r"(?s)<[^>]+>", "", re.sub(r"(?i)<br\s*/?>", "\n", brut)))
    texte = texte.replace("♯", "#").replace("♭", "b").replace("\t", "    ").replace("\xa0", " ")
    sortie, attente = [], None
    for ligne in texte.splitlines():
        if "[" in ligne and re.search(rf"\[{NOTE}[^\]\s]{{0,8}}\]", ligne):         # accords entre crochets, dans le texte
            mots, accords, reste = [], [], ligne
            while True:
                m = re.search(rf"\[({NOTE}[^\]\s]{{0,8}})\]", reste)
                avant = reste[:m.start()] if m else reste
                mots += [x for x in (_nu(w) for w in avant.split()) if x]
                if not m:
                    break
                if ACCORD.match(m.group(1)):
                    accords.append((len(mots), m.group(1)))
                reste = reste[m.end():]
            if mots and accords:
                sortie.append((mots, [(min(k, len(mots) - 1), a) for k, a in accords]))
            attente = None
            continue
        jetons = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", ligne)]
        if not jetons:
            continue
        propres = [(c, j.strip("|()")) for c, j in jetons if j.strip("|()")]
        bons = [(c, j) for c, j in propres if ACCORD.match(j)]
        if bons and len(bons) >= 0.6 * len(propres):
            attente = bons                                              # ligne d'accords : elle attend ses paroles
            continue
        if attente:
            places = [(c, _nu(j)) for c, j in jetons]
            places = [(c, w) for c, w in places if w]
            if places:
                accords = []
                for c, a in attente:                                    # le mot placé sous l'accord, sinon le suivant
                    k = max((x for x in range(len(places)) if places[x][0] <= c), default=0)
                    if k + 1 < len(places) and c >= places[k][0] + len(places[k][1]) + 1:
                        k += 1
                    accords.append((k, a))
                sortie.append(([w for _, w in places], accords))
        attente = None
    return sortie


def accrocher_aux_mots(pages, mots, temps, lire):
    """Accroche chaque grille aux paroles placées : même suite de mots, donc même instant (un refrain écrit une seule
    fois sur la page est retrouvé à chacune de ses reprises).
    pages : [(lignes_avec_accords(page), origine)] ; mots : [(début, fin, mot, confiance, n° de ligne)] ; temps : instants
    des temps ; lire : symbole -> (fondamentale, qualité, basse).
    Renvoie [(par temps : accord ou None hors des passages chantés couverts, origine, nombre d'accords placés)]."""
    import difflib
    n = len(temps)
    nus = [(_nu(m[2]), m[0], m[4]) for m in sorted(mots, key=lambda m: m[0])]
    nus = [x for x in nus if x[0]]
    if not nus or not pages or n < 8:
        return []
    par_ligne = {}
    for k, (_, _, j) in enumerate(nus):
        par_ligne.setdefault(j, []).append(k)

    def temps_de(t):
        a, b = 0, n - 1
        while a < b:
            m = (a + b + 1) // 2
            a, b = (m, b) if temps[m] <= t else (a, m - 1)
        return a + 1 if a + 1 < n and temps[a + 1] - t < t - temps[a] else a

    candidates = []
    for lignes, origine in pages:
        lues = [(w, [(k, lire(a)) for k, a in acc if lire(a)]) for w, acc in lignes]
        lues = [(w, acc) for w, acc in lues if acc]
        if len(lues) < 4:
            continue
        # 1. toute la chanson d'un trait : la grille et nos paroles sont la même suite de mots, aux coupes de lignes près
        plat, ancres = [], {}
        for w, acc in lues:
            for k, a in acc:
                ancres.setdefault(len(plat) + k, a)
            plat += w
        place = {}                                                      # n° de notre mot -> accord
        for b in difflib.SequenceMatcher(None, plat, [x[0] for x in nus], autojunk=False).get_matching_blocks():
            if b.size >= 3:
                for x in range(b.size):
                    if b.a + x in ancres:
                        place[b.b + x] = ancres[b.a + x]
        # 2. les lignes restées sans accord (refrain écrit une seule fois sur la page) : la ligne de la grille la plus proche
        for j, ks in par_ligne.items():
            if any(k in place for k in ks) or len(ks) < 3:
                continue
            nos = [nus[k][0] for k in ks]
            note, choix = max(((difflib.SequenceMatcher(None, w, nos, autojunk=False).ratio(), x) for x, (w, _) in enumerate(lues)), default=(0, None))
            if note >= 0.7:
                w, acc = lues[choix]
                vers = {}
                for b in difflib.SequenceMatcher(None, w, nos, autojunk=False).get_matching_blocks():
                    for x in range(b.size):
                        vers[b.a + x] = b.b + x
                for k, a in acc:
                    proche = min(vers, key=lambda x: abs(x - k), default=None)
                    if proche is not None and abs(proche - k) <= 2:
                        place.setdefault(ks[min(len(ks) - 1, max(0, vers[proche] + k - proche))], a)
        if len(place) < 8:
            continue
        # 3. des mots aux temps : l'accord vaut de son mot jusqu'à l'accord suivant, sans déborder de sa ligne
        sur = [None] * n
        ordre = sorted(place)
        for x, k in enumerate(ordre):
            fin_ligne = max(nus[y][1] for y in par_ligne[nus[k][2]]) + 1.0
            suivant = nus[ordre[x + 1]][1] if x + 1 < len(ordre) else fin_ligne
            a, b = temps_de(nus[k][1]), temps_de(min(suivant, fin_ligne))
            for i in range(a, max(b, a + 1)):
                sur[i] = place[k]
        candidates.append((sur, origine, len(place)))
    return candidates


MINEURES = ("min", "min7", "min6", "minmaj7", "dim", "dim7", "hdim7")


def recouper(candidates, n, entendus=None, seuil=0.45):
    """Transpose chaque grille accrochée dans le ton où elle s'accorde le mieux avec `entendus` (par temps :
    (fondamentale, qualité, …) ou None), l'écarte si elle ne s'y accorde pas, puis fait voter les grilles temps par
    temps (chacune pèse ce qu'elle s'accorde). Sans `entendus`, toutes sont gardées telles quelles.
    Renvoie (par temps : (fondamentale, qualité, basse) ou None, infos)."""
    retenues, infos = [], []
    for sur, origine, nb in candidates:
        couverts = [i for i in range(n) if sur[i] is not None]
        d, accord = 0, None
        if entendus is not None:
            communs = [i for i in couverts if entendus[i] is not None]
            if len(communs) >= 12:
                d, juste = max(((d, sum((sur[i][0] + d) % 12 == entendus[i][0] for i in communs)) for d in range(12)), key=lambda x: x[1])
                accord = juste / len(communs)
        infos.append({"origine": origine, "accords_places": nb, "temps_couverts": len(couverts), "transposition_demi_tons": d,
                      "accord_avec_l_ecoute": None if accord is None else round(accord, 2)})
        if accord is None or accord >= seuil:
            retenues.append(([None if a is None else ((a[0] + d) % 12, a[1], None if a[2] is None else (a[2] + d) % 12) for a in sur],
                             accord or 0.5))
    sortie = [None] * n
    for i in range(n):
        voix = {}
        for sur, poids in retenues:
            if sur[i] is not None:
                voix.setdefault((sur[i][0], sur[i][1] in MINEURES), [0.0, sur[i]])[0] += poids
        if voix:
            sortie[i] = max(voix.values(), key=lambda v: v[0])[1]
    return sortie, {"accrochees_aux_paroles": len(candidates), "retenues": len(retenues),
                    "temps_couverts": sum(a is not None for a in sortie), "detail": infos}


def accords_sur_les_mots(pages, mots, temps, entendus=None, lire=None):
    """Recoupe les grilles trouvées en ligne avec ce que l'analyse sait déjà : où tombe chaque mot (accrocher_aux_mots)
    et quel accord elle entend sur chaque temps (recouper). Renvoie (accords par temps ou None, infos)."""
    sortie, infos = recouper(accrocher_aux_mots(pages, mots, temps, lire), len(temps), entendus)
    return sortie, {"grilles": len(pages), **infos}


def recouper_entre_elles(candidates, n):
    """Avant toute écoute : les grilles se recoupent entre elles. Celle avec laquelle les autres s'accordent le plus sans
    transposition donne le ton (la plupart des pages sont dans le ton du disque ; une version avec capodastre est seule
    dans le sien) ; les autres y sont transposées, et la majorité l'emporte temps par temps.
    Renvoie (accords par temps ou None, infos)."""
    if not candidates:
        return [None] * n, {"accrochees_aux_paroles": 0, "retenues": 0}
    def entente(a, b):
        communs = [i for i in range(n) if a[i] is not None and b[i] is not None]
        return sum(a[i][0] == b[i][0] for i in communs) / len(communs) if len(communs) >= 12 else 0.0
    notes = [sum(entente(a[0], b[0]) for b in candidates if b is not a) for a in candidates]
    k = max(range(len(candidates)), key=lambda x: (round(notes[x], 2), candidates[x][2]))
    sortie, infos = recouper(candidates, n, candidates[k][0], seuil=0.5)
    infos["reference"] = candidates[k][1]
    return sortie, infos


def tonalite_de(accords):
    """Tonalité la plus simple qui explique des accords ((fondamentale, qualité, …) ou None) : la gamme majeure qui en
    contient le plus, majeure ou mineure selon que son premier ou son sixième degré revient le plus. None si trop peu."""
    compte = {}
    for a in accords:
        if a is not None:
            f = (a[0] % 12, a[1] in MINEURES)
            compte[f] = compte.get(f, 0) + 1
    if sum(compte.values()) < 8:
        return None
    gamme = lambda t: [(t, False), ((t + 5) % 12, False), ((t + 7) % 12, False), ((t + 2) % 12, True), ((t + 4) % 12, True), ((t + 9) % 12, True)]
    t = max(range(12), key=lambda t: sum(compte.get(f, 0) for f in gamme(t)) + 0.5 * compte.get((t, False), 0))
    mineur = compte.get(((t + 9) % 12, True), 0) > compte.get((t, False), 0)
    return {"tonique": (t + 9) % 12 if mineur else t, "mode": "mineur" if mineur else "majeur"}


def _simple(s):
    return limace(re.sub(r"\s*[\(\[].*?[\)\]]", "", s) or s, " ")


def pages_cifraclub(artiste, titre, nombre=2):
    """Adresses des pages du morceau sur Cifra Club, d'après le moteur de recherche du site."""
    court = re.sub(r"\s*[\(\[].*?[\)\]]", "", titre).strip() or titre
    u = "https://solr.sscdn.co/cc/h2/?" + urllib.parse.urlencode({"q": f"{artiste} {court}", "limit": 8})
    if not admis(u):
        return []
    brut = _lire(u, 200_000).strip()
    docs = json.loads(brut[brut.index("{"):brut.rindex("}") + 1]).get("response", {}).get("docs", [])
    voulu, mots = _simple(titre), set(_simple(artiste).split())
    sortie = []
    for d in docs:
        if not d.get("d") or not d.get("u"):
            continue
        if _simple(str(d.get("m", ""))) == voulu and mots & set(_simple(str(d.get("a", ""))).split()):
            sortie.append(f"https://www.cifraclub.com/{d['d']}/{d['u']}/")
    return list(dict.fromkeys(sortie))[:nombre]


def _court(titre):
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", titre).strip() or titre


def pages_guitartabs(artiste, titre, nombre=2):
    """Pages d'accords du morceau sur guitartabs.cc, d'après son moteur de recherche (versions « _crd »)."""
    u = "https://www.guitartabs.cc/search.php?" + urllib.parse.urlencode({"tabtype": "chords", "band": artiste, "song": _court(titre)})
    if not admis(u):
        return []
    voulu = limace(_court(titre), "")                                   # lettres seules : « l'aime » s'écrit l_aime ou laime
    liens = re.findall(r'href=["\'](/tabs/[^"\'#?]+_crd(?:_ver_\d+)?\.html)["\']', _lire(u, 300_000))
    bons = [l for l in dict.fromkeys(liens) if limace(l.rsplit("/", 1)[1].split("_crd")[0], "") == voulu]
    return ["https://www.guitartabs.cc" + l for l in bons[:nombre]]


def pages_guitaretab(artiste, titre, nombre=2):
    """Pages d'accords du morceau sur guitaretab.com, d'après son moteur de recherche (liens dont le texte porte le titre)."""
    u = "https://www.guitaretab.com/fetch/?" + urllib.parse.urlencode({"type": "tab", "query": f"{artiste} {_court(titre)}"})
    if not admis(u):
        return []
    voulu, mots = _simple(titre), set(_simple(artiste).split())
    sortie = []
    for lien, texte in re.findall(r'(?is)<a\b[^>]*href=["\'](/[a-z0-9]/[^"\'#?/]+/\d+\.html)["\'][^>]*>(.*?)</a>', _lire(u, 300_000)):
        t = _simple(html.unescape(re.sub(r"(?s)<[^>]+>", " ", texte)))
        if not (mots & set(limace(lien.split("/")[2], " ").split())) or not t.startswith(voulu):
            continue
        reste = t[len(voulu):].split()
        if any(x in reste for x in ("tab", "tabs", "bass", "solo", "intro", "ukulele", "drum", "power")):
            continue                                                    # tablatures : pas de grille d'accords dedans
        sortie.append("https://www.guitaretab.com" + lien)
    return list(dict.fromkeys(sortie))[:nombre]


def pages_acordesweb(artiste, titre, nombre=1):
    """La page du morceau sur acordesweb.com, à son adresse directe (le site range ses pages par artiste et titre)."""
    sans = lambda x: limace(re.sub(r"['’]", "", x))                     # « l'envie d'aimer » : l-envie-d-aimer ou lenvie-daimer
    return list(dict.fromkeys(f"https://acordesweb.com/cancion/{f(artiste)}/{f(_court(titre))}" for f in (limace, sans)))


def _proche(a, b, seuil=0.86):
    import difflib
    a, b = limace(a, ""), limace(b, "")
    return bool(a) and (a == b or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= seuil)


def pages_tabs4acoustic(artiste, titre, nombre=1):
    """La page du morceau sur tabs4acoustic.com (site français) : son moteur trouve l'artiste, dont la page liste les titres."""
    base = "https://www.tabs4acoustic.com/"
    u = base + "recherche.html?" + urllib.parse.urlencode({"FindMe": artiste})
    if not admis(u):
        return []
    fiches = [l for l in dict.fromkeys(re.findall(r'href=["\'](?:https://www\.tabs4acoustic\.com/)?/?(tablatures-partitions/[a-z0-9-]+-tabs-guitare-\d+\.html)["\']', _lire(u, 300_000)))
              if _proche(l.split("/")[1].split("-tabs-guitare")[0], artiste, 0.8)]
    if not fiches or not admis(base + fiches[0]):
        return []
    liens = re.findall(r'href=["\'](?:https://www\.tabs4acoustic\.com/)?/?(tablatures-partitions/[a-z0-9-]+/([a-z0-9-]+?)-tab-[a-z-]*\d+\.html)["\']', _lire(base + fiches[0], 300_000))
    return [base + l for l, nom in dict.fromkeys(liens) if _proche(nom, _court(titre))][:nombre]


def pages_ukuleletabs(artiste, titre, nombre=2):
    """Pages du morceau sur ukulele-tabs.com, d'après son moteur de recherche (les accords sont ceux de la chanson)."""
    base = "https://www.ukulele-tabs.com"
    u = base + "/search-uke-chords.html?" + urllib.parse.urlencode({"find": _court(titre)})
    if not admis(u):
        return []
    mots = set(_simple(artiste).split())
    liens = re.findall(r'href=["\'](/uke-songs/([a-z0-9-]+)/([a-z0-9-]+?)-uke-tab-\d+\.html)["\']', _lire(u, 300_000))
    return [base + l for l, qui, nom in dict.fromkeys(liens) if mots & set(qui.split("-")) and _proche(nom, _court(titre))][:nombre]


def pages_chordsbase(artiste, titre, nombre=1):
    """La page du morceau sur chordsbase.com, à son adresse directe."""
    return [f"https://www.chordsbase.com/guitar-chords/{limace(artiste)}/{limace(_court(titre))}"]


SITES = (("cifraclub", pages_cifraclub), ("guitartabs", pages_guitartabs), ("guitaretab", pages_guitaretab), ("acordesweb", pages_acordesweb),
         ("tabs4acoustic", pages_tabs4acoustic), ("ukuleletabs", pages_ukuleletabs), ("chordsbase", pages_chordsbase))


def _un_site(nom, pages, artiste, titre, fin, veut_paroles):
    """Interroge un site. Renvoie (grilles [(accords, hôte)], grilles accrochées aux mots [(lignes, hôte)], journal)."""
    trouvees, avec, journal = [], [], []
    try:
        adresses = pages(artiste, titre)
    except Exception as e:
        return trouvees, avec, [f"    {nom} : recherche refusée ou impossible ({type(e).__name__} {str(getattr(e, 'code', '') or e)[:60]})"]
    journal.append(f"    {nom} : {len(adresses)} pages proposées")
    for u in adresses:
        hote = urllib.parse.urlparse(u).netloc
        if time.monotonic() > fin:
            break
        try:
            if not admis(u):
                journal.append(f"      {hote} : lecture non admise")
                continue
            page = _lire(u)
            accords = accords_de_la_page(page)
        except urllib.error.HTTPError as e:
            if e.code != 404:                                           # 404 : le site n'a pas ce morceau, rien à signaler
                journal.append(f"      {hote} : illisible (HTTPError {e.code})")
            continue
        except Exception as e:
            journal.append(f"      {hote} : illisible ({type(e).__name__} {getattr(e, 'code', '')})")
            continue
        journal.append(f"      {hote} : {len(accords)} accords lus" + (f" ({len(set(accords))} différents)" if accords else ""))
        if len(accords) >= 8 and len(set(accords)) >= 3:
            trouvees.append((accords, hote))
            if veut_paroles:
                lignes = lignes_avec_accords(page)
                journal.append(f"      {hote} : {len(lignes)} lignes chantées portent leurs accords ({sum(len(a) for _, a in lignes)} accords)")
                if len(lignes) >= 4:
                    avec.append((lignes, hote))
    return trouvees, avec, journal


def chercher_grilles(artiste, titre, en_tout=45, avec_paroles=None):
    """Renvoie [(liste de symboles d'accords, site d'origine)] pour les pages où une grille a pu être lue.
    Les sites retenus (ceux dont les règles l'admettent, voir CHOIX.md) sont interrogés en même temps, chacun à son
    rythme ; l'ensemble est borné à `en_tout` secondes, et la panne ou le refus d'un site n'empêche pas les autres.
    avec_paroles : liste à remplir, si on la donne, des grilles dont les accords sont accrochés aux mots
    ([(lignes_avec_accords, site)], voir accords_sur_les_mots)."""
    from concurrent.futures import ThreadPoolExecutor, wait
    trouvees, fin = [], time.monotonic() + en_tout
    bassin = ThreadPoolExecutor(max_workers=len(SITES))
    taches = [bassin.submit(_un_site, nom, pages, artiste, titre, fin, avec_paroles is not None) for nom, pages in SITES]
    wait(taches, timeout=en_tout + 5)
    for (nom, _), tache in zip(SITES, taches):
        if not tache.done():
            print(f"    {nom} : trop lent, laissé de côté", flush=True)
            continue
        try:
            t, a, journal = tache.result()
        except Exception as e:
            print(f"    {nom} : {type(e).__name__}", flush=True)
            continue
        print("\n".join(journal), flush=True)
        trouvees += t
        if avec_paroles is not None:
            avec_paroles += a
    bassin.shutdown(wait=False, cancel_futures=True)
    return trouvees


if __name__ == "__main__":
    import sys
    for accords, hote in chercher_grilles(sys.argv[1], sys.argv[2]):
        print(hote, " ".join(accords[:60]))
