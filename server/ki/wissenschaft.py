#!/usr/bin/env python3
"""
wissenschaft.py - Suche in Lehrbuechern und Fachliteratur statt im offenen Netz.

Die allgemeine Websuche liefert bei Fachfragen zunehmend nachgeplapperte
Zusammenfassungen. Hier wird stattdessen dort gesucht, wo die Urtexte liegen.
Sechs Quellen, alle ohne Schluessel, alle am 28.08.2026 von heissa.de aus
geprueft:

  K10plus     Verbundkatalog deutscher Bibliotheken - hier stehen die Lehrbuecher
  Crossref    Metadaten praktisch aller Fachverlage ueber die DOI
  OpenAlex    Nachfolger des Microsoft Academic Graph, mit Volltextsuche
  EuropePMC   Medizin und Biologie, mit Abstracts
  arXiv       Preprints aus Physik, Mathematik, Informatik (nur ueber https)
  DOAJ        Open-Access-Zeitschriften, Volltext frei

Die Quellen werden nebenlaeufig gefragt. Faellt eine aus, fehlt nur deren
Block - die Suche laeuft weiter.

    python3 wissenschaft.py "Springerproblem Heuristik"
    python3 wissenschaft.py --art buecher "Algorithmen Lehrbuch"
"""

import argparse
import concurrent.futures
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

POST = "gh@heissa.de"          # hoefliche Kennung, verlangen Crossref und OpenAlex
UA = f"heissa-analyzer/1.0 (mailto:{POST})"
ZEIT = 20
JE_QUELLE = 4

ARTEN = {
    "alles":    ["k10plus", "crossref", "openalex", "europepmc", "arxiv", "doaj"],
    "buecher":  ["k10plus", "openalex"],
    "studien":  ["crossref", "openalex", "doaj"],
    "medizin":  ["europepmc", "crossref"],
    "preprints": ["arxiv", "openalex"],
}


def _holen(url: str, kopf: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(kopf or {})})
    with urllib.request.urlopen(req, timeout=ZEIT) as r:
        return r.read(1_500_000)


def _text(roh: str, laenge: int = 260) -> str:
    """Abstracts kommen mal als JATS-XML, mal als HTML - beides entschaerfen."""
    roh = re.sub(r"<[^>]+>", " ", roh or "")
    roh = html.unescape(roh)
    return re.sub(r"\s+", " ", roh).strip()[:laenge]


def _eintrag(titel, autoren, jahr, wo, url, kurz="") -> dict:
    return {"titel": (titel or "").strip()[:200],
            "autoren": (autoren or "").strip()[:120],
            "jahr": str(jahr or "")[:4],
            "wo": (wo or "").strip()[:110],
            "url": (url or "").strip(),
            "kurz": kurz}


# --- die einzelnen Quellen --------------------------------------------------

def crossref(q: str) -> list[dict]:
    d = json.loads(_holen("https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query": q, "rows": JE_QUELLE, "mailto": POST, "select":
         "title,author,issued,container-title,DOI,abstract"})))
    aus = []
    for w in d.get("message", {}).get("items", []):
        autoren = ", ".join(
            f"{a.get('family','')}".strip() for a in (w.get("author") or [])[:3])
        jahr = (w.get("issued", {}).get("date-parts") or [[None]])[0][0]
        aus.append(_eintrag(
            (w.get("title") or [""])[0], autoren, jahr,
            (w.get("container-title") or [""])[0],
            "https://doi.org/" + w["DOI"] if w.get("DOI") else "",
            _text(w.get("abstract", ""))))
    return aus


def openalex(q: str) -> list[dict]:
    d = json.loads(_holen("https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"search": q, "per-page": JE_QUELLE, "mailto": POST})))
    aus = []
    for w in d.get("results", []):
        autoren = ", ".join(a.get("author", {}).get("display_name", "")
                            for a in (w.get("authorships") or [])[:3])
        wo = ((w.get("primary_location") or {}).get("source") or {}).get(
            "display_name", "")
        # OpenAlex speichert das Abstract als Wortindex und muss zurueckgebaut werden
        kurz = ""
        idx = w.get("abstract_inverted_index")
        if idx:
            worte = {}
            for wort, stellen in idx.items():
                for s in stellen:
                    worte[s] = wort
            kurz = _text(" ".join(worte[k] for k in sorted(worte)))
        aus.append(_eintrag(w.get("display_name"), autoren,
                            w.get("publication_year"), wo, w.get("doi") or "", kurz))
    return aus


def k10plus(q: str) -> list[dict]:
    roh = _holen("https://sru.k10plus.de/gvk?" + urllib.parse.urlencode({
        "version": "1.1", "operation": "searchRetrieve",
        "query": f"pica.all={q}", "maximumRecords": JE_QUELLE,
        "recordSchema": "dc"}))
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    aus = []
    for satz in ET.fromstring(roh).iter():
        if not satz.tag.endswith("}dc") and not satz.tag.endswith("dc"):
            continue
        hol = lambda t: [e.text or "" for e in satz.findall(f"dc:{t}", ns)]
        titel = " ".join(hol("title"))
        if not titel:
            continue
        aus.append(_eintrag(titel, ", ".join(hol("creator")[:3]),
                            (hol("date") or [""])[0][:4],
                            " ".join(hol("publisher"))[:80],
                            (hol("identifier") or [""])[0],
                            " ".join(hol("description"))[:260]))
        if len(aus) >= JE_QUELLE:
            break
    return aus


def europepmc(q: str) -> list[dict]:
    d = json.loads(_holen(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        + urllib.parse.urlencode({"query": q, "format": "json",
                                  "pageSize": JE_QUELLE, "resultType": "core"})))
    aus = []
    for w in d.get("resultList", {}).get("result", []):
        aus.append(_eintrag(
            w.get("title"), w.get("authorString"), w.get("pubYear"),
            w.get("journalTitle"),
            "https://doi.org/" + w["doi"] if w.get("doi") else
            f"https://europepmc.org/article/{w.get('source','MED')}/{w.get('id','')}",
            _text(w.get("abstractText", ""))))
    return aus


def arxiv(q: str) -> list[dict]:
    roh = _holen("https://export.arxiv.org/api/query?"
                 + urllib.parse.urlencode({"search_query": f"all:{q}",
                                           "max_results": JE_QUELLE}))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    aus = []
    for e in ET.fromstring(roh).findall("a:entry", ns):
        hol = lambda t: (e.findtext(f"a:{t}", "", ns) or "").strip()
        autoren = ", ".join((a.findtext("a:name", "", ns) or "")
                            for a in e.findall("a:author", ns)[:3])
        aus.append(_eintrag(hol("title"), autoren, hol("published")[:4],
                            "arXiv", hol("id"), _text(hol("summary"))))
    return aus


def doaj(q: str) -> list[dict]:
    d = json.loads(_holen("https://doaj.org/api/search/articles/"
                          + urllib.parse.quote(q)
                          + f"?pageSize={JE_QUELLE}"))
    aus = []
    for w in d.get("results", []):
        b = w.get("bibjson", {})
        verweis = ""
        for l in b.get("link", []):
            if l.get("url"):
                verweis = l["url"]
                break
        aus.append(_eintrag(
            b.get("title"),
            ", ".join(a.get("name", "") for a in (b.get("author") or [])[:3]),
            b.get("year"), (b.get("journal") or {}).get("title", ""),
            verweis, _text(b.get("abstract", ""))))
    return aus


QUELLEN = {"crossref": ("Crossref", crossref), "openalex": ("OpenAlex", openalex),
           "k10plus": ("K10plus (Bibliotheken)", k10plus),
           "europepmc": ("Europe PMC", europepmc), "arxiv": ("arXiv", arxiv),
           "doaj": ("DOAJ", doaj)}


# --- deutsche Begriffe uebersetzen ------------------------------------------
# Fachliteratur ist englisch. "Springerproblem" findet bei Crossref eine
# Handvoll Treffer, "knight's tour" ueber achthunderttausend. Darauf zu hoffen,
# dass das Modell von sich aus uebersetzt, reicht nicht - es passiert hier,
# bevor ueberhaupt gesucht wird.

DEUTSCH_ZEICHEN = set("äöüßÄÖÜ")
DEUTSCH_WORTE = {
    "der", "die", "das", "und", "oder", "von", "mit", "fuer", "für", "bei",
    "nach", "ueber", "über", "unter", "gegen", "ohne", "durch", "beim", "zum",
    "zur", "des", "dem", "den", "ein", "eine", "einer", "eines", "ist", "sind",
    "wie", "was", "warum", "welche", "verfahren", "loesung", "lösung",
    "berechnung", "untersuchung", "wirkung", "einfluss", "vergleich",
    "grundlagen", "lehrbuch", "studie", "auswirkungen", "entwicklung",
}
# typische deutsche Wortausgaenge, die im Englischen so nicht vorkommen
DEUTSCH_ENDUNGEN = ("ung", "ungen", "heit", "keit", "schaft", "lich", "isch",
                    "problem", "verfahren", "gesetz", "kunde")


def ist_deutsch(begriff: str) -> bool:
    if any(z in begriff for z in DEUTSCH_ZEICHEN):
        return True
    worte = [w.strip(".,;:!?\"'()").lower() for w in begriff.split()]
    if any(w in DEUTSCH_WORTE for w in worte):
        return True
    return any(len(w) > 8 and w.endswith(DEUTSCH_ENDUNGEN) for w in worte)


def ins_englische(begriff: str) -> str:
    """Uebersetzt ueber dasselbe lokale Modell. Schlaegt das fehl, wird der
    Begriff unveraendert gesucht - lieber ein schwaches Ergebnis als keins."""
    try:
        import netz_werkzeug as nw
        ziel = nw.LLM_URL
    except Exception:
        ziel = "http://10.9.0.6:8081/v1/chat/completions"
    auftrag = ("Uebersetze diesen deutschen Suchbegriff in den englischen "
               "Fachbegriff, wie er in wissenschaftlicher Literatur benutzt "
               "wird. Antworte NUR mit dem englischen Begriff, ohne Anfuehrungs"
               f"zeichen und ohne Erklaerung.\n\n{begriff}")
    body = json.dumps({"messages": [{"role": "user", "content": auftrag}],
                       "max_tokens": 30, "temperature": 0.0}).encode()
    try:
        req = urllib.request.Request(ziel, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            roh = json.load(r)["choices"][0]["message"]["content"]
    except Exception:
        return begriff
    # Das Modell haengt gelegentlich einen Satz an - die erste Zeile zaehlt
    erste = (roh or "").strip().splitlines()[0] if (roh or "").strip() else ""
    erste = erste.strip(' "\'.:;').strip()
    return erste if 2 < len(erste) < 120 else begriff


def wissenschaft_suchen(begriff: str, art: str = "alles",
                        uebersetzen: bool = True) -> str:
    """Sucht in Fachliteratur statt im offenen Netz.

    art: alles | buecher | studien | medizin | preprints
    Deutsche Begriffe werden vorher ins Englische uebersetzt."""
    begriff = (begriff or "").strip()
    if not begriff:
        return "kein Suchbegriff"

    original = begriff
    if uebersetzen and ist_deutsch(begriff):
        begriff = ins_englische(begriff)

    namen = ARTEN.get((art or "alles").lower(), ARTEN["alles"])

    treffer: dict[str, list] = {}
    fehler: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(namen)) as pool:
        auftraege = {pool.submit(QUELLEN[n][1], begriff): n for n in namen}
        for fertig in concurrent.futures.as_completed(auftraege, timeout=ZEIT + 10):
            n = auftraege[fertig]
            try:
                treffer[n] = fertig.result()
            except Exception as e:
                fehler.append(f"{QUELLEN[n][0]}: {type(e).__name__}")

    zeilen = []
    for n in namen:                      # feste Reihenfolge, nicht die des Eintreffens
        for e in treffer.get(n, []):
            if not e["titel"]:
                continue
            kopf = f"- {e['titel']}"
            teile = [t for t in (e["autoren"], e["jahr"], e["wo"]) if t]
            if teile:
                kopf += "\n  " + " · ".join(teile)
            if e["url"]:
                kopf += f"\n  {e['url']}"
            if e["kurz"]:
                kopf += f"\n  {e['kurz']}"
            zeilen.append(kopf)
        if treffer.get(n):
            zeilen.insert(len(zeilen) - len(treffer[n]), f"\n[{QUELLEN[n][0]}]")

    uebersetzt = (f"[gesucht als: {begriff}  (aus dem deutschen "
                  f"{original!r} uebersetzt, Fachliteratur ist englisch)]\n"
                  if begriff != original else "")

    if not zeilen:
        return (uebersetzt + f"keine Fundstellen zu {begriff!r}"
                + (f" (nicht erreichbar: {', '.join(fehler)})" if fehler else "")
                + ". Versuche andere Stichworte oder die allgemeine Websuche.")
    kopf = uebersetzt
    if fehler:
        kopf += f"[nicht erreichbar: {', '.join(fehler)}]\n"
    return kopf + "\n".join(zeilen) + (
        "\n\nDie Adressen fuehren zur Originalarbeit. Brauchst du mehr als den "
        "Abriss, lies sie mit hole_seite - viele DOI-Verweise landen allerdings "
        "hinter einer Bezahlschranke.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("begriff", nargs="+")
    ap.add_argument("--art", default="alles", choices=list(ARTEN))
    args = ap.parse_args()
    print(wissenschaft_suchen(" ".join(args.begriff), args.art))
    return 0


if __name__ == "__main__":
    sys.exit(main())
