#!/usr/bin/env python3
"""Cherche en ligne une grille d'accords du morceau, pour corroborer ce que l'analyse entend.
Les sites de grilles publient leurs accords en texte : on interroge le moteur de recherche d'un tel site, on lit la
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


def chercher_grilles(artiste, titre):
    """Renvoie [(liste de symboles d'accords, site d'origine)] pour les pages où une grille a pu être lue."""
    trouvees = []
    try:
        adresses = pages_cifraclub(artiste, titre)
    except Exception as e:
        print(f"    recherche refusée ou impossible : {type(e).__name__} {str(e)[:120]}", flush=True)
        return trouvees
    print(f"    {len(adresses)} pages proposées", flush=True)
    for u in adresses:
        hote = urllib.parse.urlparse(u).netloc
        try:
            if not admis(u):
                print(f"      {hote} : lecture non admise", flush=True)
                continue
            accords = accords_de_la_page(_lire(u))
        except Exception as e:
            print(f"      {hote} : illisible ({type(e).__name__} {getattr(e, 'code', '')})", flush=True)
            continue
        print(f"      {hote} : {len(accords)} accords lus" + (f" ({len(set(accords))} différents)" if accords else ""), flush=True)
        if len(accords) >= 8 and len(set(accords)) >= 3:
            trouvees.append((accords, hote))
    return trouvees


if __name__ == "__main__":
    import sys
    for accords, hote in chercher_grilles(sys.argv[1], sys.argv[2]):
        print(hote, " ".join(accords[:60]))
