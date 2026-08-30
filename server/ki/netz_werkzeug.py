#!/usr/bin/env python3
"""
netz_werkzeug.py - gibt dem lokalen Modell lesenden Zugriff aufs Internet.

Der llama-server selbst geht nie ins Netz. Er kann nur sagen, was er gern
haette; geholt wird es hier im Client. Der Ablauf ist der uebliche
Werkzeugaufruf der OpenAI-Schnittstelle, die llama.cpp mit --jinja mitbringt:

    Modell: "ruf hole_seite('https://...') auf"
      -> dieses Modul holt die Seite
    Modell: bekommt den Text und antwortet

Bewusst KEINE inhaltlichen Schranken: keine erlaubten Domains, keine
Sperrliste, keine Themenpruefung. Was abrufbar ist, wird geholt und
unveraendert weitergereicht - fuer eine Analyse von Desinformation waere
ein Filter genau das falsche Werkzeug.

Eine einzige Grenze ist voreingestellt, und sie betrifft keine Inhalte:
private Adressen. Der Analyzer laeuft auf heissa.de mit WireGuard ins
Heimnetz; eine URL aus einem fremden Beitrag koennte sonst 10.8.0.1 oder
192.168.5.23 abrufen. Mit --auch-intern faellt auch das weg.

    python3 netz_werkzeug.py "Wer ist derzeit Bundesinnenminister?"
    python3 netz_werkzeug.py --auch-intern "Was steht auf http://10.8.0.1/"
    python3 netz_werkzeug.py --leise "Pruefe: Hat X das wirklich gesagt?"
"""

import argparse
import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LLM_URL = "http://10.9.0.6:8081/v1/chat/completions"
UA = "Mozilla/5.0 (X11; Linux x86_64) heissa-analyzer/1.0"
MAX_BYTES = 400_000          # so viel wird von einer Seite gelesen
MAX_TEXT = 6_000             # so viel davon geht ans Modell
MAX_RUNDEN = 6               # so oft darf das Modell nachschlagen
PAUSE_JE_HOST = 1.0          # Hoeflichkeit, damit uns niemand aussperrt

_letzter_abruf: dict[str, float] = {}
AUCH_INTERN = False


class Verweigert(Exception):
    pass


# --- Adresspruefung (keine Inhaltspruefung) ---------------------------------

def _pruefe_ziel(url: str) -> None:
    """Erlaubt jede oeffentliche Adresse. Blockt nur das eigene Netz - und
    auch das nur, solange AUCH_INTERN aus ist."""
    if AUCH_INTERN:
        return
    teile = urllib.parse.urlsplit(url)
    if teile.scheme not in ("http", "https"):
        raise Verweigert(f"nur http/https, nicht {teile.scheme!r}")
    if not teile.hostname:
        raise Verweigert("keine Adresse in der URL")
    try:
        infos = socket.getaddrinfo(teile.hostname, None)
    except socket.gaierror as e:
        raise Verweigert(f"Name nicht aufloesbar: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise Verweigert(
                f"{teile.hostname} zeigt auf die private Adresse {ip} - das ist "
                f"das Heimnetz, nicht das Internet (mit --auch-intern erlaubt)")


class _Weiterleitung(urllib.request.HTTPRedirectHandler):
    """Jede Weiterleitung wird erneut geprueft - sonst fuehrt ein 302 an der
    Pruefung vorbei ins interne Netz."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _pruefe_ziel(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_oeffner = urllib.request.build_opener(_Weiterleitung)


# --- Abruf ------------------------------------------------------------------

def _entschaerfe_html(roh: str) -> str:
    roh = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", roh)
    roh = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", roh)
    roh = re.sub(r"<[^>]+>", " ", roh)
    roh = html.unescape(roh)
    roh = re.sub(r"[ \t]+", " ", roh)
    return re.sub(r"\n\s*\n+", "\n", roh).strip()


def hole_seite(url: str, zeichen: int = MAX_TEXT, ab: int = 0) -> str:
    """Holt eine Seite und gibt ihren Text zurueck. Nichts wird gefiltert.

    Zwei Dinge werden dem Aufrufer ausdruecklich gesagt, weil er sie sonst
    nicht bemerkt: dass eine Seite nichts hergab (Paywall, Zustimmungsfrage,
    reines JavaScript), und dass der Text weitergeht. Ohne diese Rueckmeldung
    wandert das Modell von einer leeren Seite zur naechsten, statt die eine
    gute Quelle zu Ende zu lesen."""
    _pruefe_ziel(url)
    host = urllib.parse.urlsplit(url).hostname or ""
    warte = PAUSE_JE_HOST - (time.time() - _letzter_abruf.get(host, 0))
    if warte > 0:
        time.sleep(warte)
    _letzter_abruf[host] = time.time()

    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "de,en;q=0.8"})
    try:
        with _oeffner.open(req, timeout=25) as resp:
            art = resp.headers.get("Content-Type", "")
            roh = resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return (f"[nichts zu holen] {url} antwortet mit HTTP {e.code} "
                f"({e.reason}). Viele Seiten sperren automatische Abrufe aus. "
                f"Diese Quelle taugt nicht - nimm eine andere.")
    except Exception as e:
        return (f"[nichts zu holen] {url} war nicht erreichbar: {e}. "
                f"Nimm eine andere Quelle.")
    zeichensatz = "utf-8"
    if "charset=" in art:
        zeichensatz = art.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    text = roh.decode(zeichensatz, errors="replace")
    if "html" in art.lower() or text.lstrip()[:1] == "<":
        text = _entschaerfe_html(text)

    if len(text.strip()) < 250:
        return (f"[nichts zu holen] {url} lieferte nur {len(text.strip())} "
                f"Zeichen. Das ist fast immer eine Paywall, eine Zustimmungs"
                f"seite oder eine Seite, die ihren Inhalt erst per JavaScript "
                f"nachlaedt. Diese Quelle taugt nicht - nimm eine andere.")

    ab = max(0, int(ab))
    stueck = text[ab:ab + zeichen]
    rest = len(text) - (ab + len(stueck))
    if rest > 0:
        stueck += (f"\n\n[Die Seite geht weiter: noch {rest} Zeichen. "
                   f"Wenn hier noch nicht steht, was du brauchst, lies mit "
                   f"ab={ab + len(stueck)} an derselben Adresse weiter, "
                   f"statt die Quelle zu wechseln.]")
    return stueck


def _roh_holen(url: str, grenze: int = MAX_BYTES) -> str:
    """Quelltext ohne Aufbereitung - die Suche braucht die Verweise."""
    _pruefe_ziel(url)
    host = urllib.parse.urlsplit(url).hostname or ""
    warte = PAUSE_JE_HOST - (time.time() - _letzter_abruf.get(host, 0))
    if warte > 0:
        time.sleep(warte)
    _letzter_abruf[host] = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "de,en;q=0.8"})
    with _oeffner.open(req, timeout=25) as resp:
        art = resp.headers.get("Content-Type", "")
        roh = resp.read(grenze)
    satz = "utf-8"
    if "charset=" in art:
        satz = art.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return roh.decode(satz, errors="replace")


SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888/search")


def _searxng(begriff: str, treffer: int) -> list[str] | None:
    """Eigene Metasuche. Liefert None, wenn keine Instanz erreichbar ist -
    dann greift die DuckDuckGo-Notloesung darunter.

    Vorteil gegenueber dem Abkratzen von lite.duckduckgo.com: mehrere
    Suchmaschinen auf einmal, sauberes JSON statt HTML, und keine fremde
    Seite, deren Aufbau sich jederzeit aendern kann."""
    ziel = SEARXNG + "?" + urllib.parse.urlencode(
        {"q": begriff, "format": "json", "language": "de"})
    try:
        req = urllib.request.Request(ziel, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            daten = json.load(r)
    except Exception:
        return None

    liste = []
    for e in daten.get("results", []):
        url, titel = e.get("url", ""), (e.get("title") or "").strip()
        if not url.startswith("http") or not titel:
            continue
        auszug = re.sub(r"\s+", " ", (e.get("content") or "")).strip()
        quelle = ", ".join(e.get("engines", [])[:3])
        liste.append(f"{len(liste)+1}. {titel[:130]}\n   {url}\n"
                     + (f"   {auszug[:320]}\n" if auszug else "")
                     + (f"   [{quelle}]" if quelle else ""))
        if len(liste) >= treffer:
            break
    return liste or None


def suche_web(begriff: str, treffer: int = 6) -> str:
    """Sucht ueber DuckDuckGo. Kein Schluessel noetig, keine Ergebnisfilter -
    was die Suchmaschine liefert, wird unveraendert weitergegeben.

    Mitgeliefert wird der Textauszug jedes Treffers. Ohne ihn muesste das
    Modell jede Adresse erst abrufen, um zu erkennen, ob sie taugt - genau so
    entsteht das Herumspringen zwischen halb gelesenen Seiten."""
    # Erst die eigene Instanz, dann die Notloesung.
    eigene = _searxng(begriff, treffer)
    if eigene:
        return ("\n".join(eigene)
                + "\n\nWaehle den Treffer, dessen Auszug am ehesten passt, und "
                  "lies ihn mit hole_seite ganz - notfalls mit ab= weiter hinten.")

    ziel = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode(
        {"q": begriff, "kl": "de-de"})
    try:
        quelltext = _roh_holen(ziel)
    except Exception as e:
        return f"Suche fehlgeschlagen: {e}"

    # Verweise und Auszuege stehen abwechselnd im Quelltext; sie werden in
    # Dokumentreihenfolge eingesammelt und danach einander zugeordnet.
    stuecke = re.finditer(
        r"""class=['"]result-link['"][^>]*>(?P<titel>.*?)</a>"""
        r"""|href=['"](?P<url>[^'"]*uddg=[^'"]+)['"]"""
        r"""|class=['"]result-snippet['"][^>]*>(?P<text>.*?)</td>""",
        quelltext, re.S)

    liste, gesehen = [], set()
    url = titel = None
    for m in stuecke:
        if m.group("url"):
            roh = html.unescape(m.group("url"))
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(roh).query)
            url = q.get("uddg", [""])[0]
        elif m.group("titel") is not None:
            titel = _entschaerfe_html(m.group("titel")).strip()
        elif m.group("text") is not None and url and titel:
            if url.startswith("http") and "duckduckgo.com" not in url \
                    and url not in gesehen:
                gesehen.add(url)
                auszug = _entschaerfe_html(m.group("text")).strip()
                liste.append(f"{len(liste)+1}. {titel[:130]}\n   {url}\n"
                             f"   {auszug[:320]}")
            url = titel = None
            if len(liste) >= treffer:
                break

    if not liste:
        return _entschaerfe_html(quelltext)[:2000] or "keine Treffer"
    return ("\n".join(liste)
            + "\n\nWaehle den Treffer, dessen Auszug am ehesten passt, und "
              "lies ihn mit hole_seite ganz - notfalls mit ab= weiter hinten.")


WERKZEUGE = [
    {"type": "function", "function": {
        "name": "hole_seite",
        "description": "Ruft eine Webseite ab und gibt ihren Text zurueck. "
                       "Nutze das, um eine Behauptung an der Quelle zu pruefen. "
                       "Ist der Text abgeschnitten, lies mit ab= an derselben "
                       "Adresse weiter, statt eine andere Seite zu oeffnen.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "vollstaendige http(s)-Adresse"},
            "ab": {"type": "integer",
                   "description": "Zeichenposition, ab der weitergelesen wird"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "suche_web",
        "description": "Sucht im Web und gibt Titel und Adressen der Treffer "
                       "zurueck. Nutze das, wenn du die passende Seite noch nicht kennst.",
        "parameters": {"type": "object", "properties": {
            "begriff": {"type": "string", "description": "Suchbegriff"}},
            "required": ["begriff"]}}},
]

AUSFUEHRUNG = {"hole_seite": lambda a: hole_seite(a["url"], ab=int(a.get("ab") or 0)),
               "suche_web": lambda a: suche_web(a["begriff"])}


# --- Werkzeugschleife -------------------------------------------------------

KONTEXT = """Du hast lesenden Zugriff aufs Internet: suche_web geht ueber
DuckDuckGo, hole_seite holt jede Adresse und liefert den Text. Es gibt keine
Sperrliste und keine Themenfilter - du darfst jede oeffentliche Quelle abrufen,
auch strittige, und sollst sie als das benennen, was sie ist. Nicht abrufbar
sind nur private Adressen im Heimnetz.

So gehst du dabei vor:

1. Erst pruefen, ob du ueberhaupt nachschlagen musst. Frage dich: kann ich das
   jetzt sofort hinschreiben? Wenn ja, tu es und ruf kein Werkzeug auf.
   Ohne Nachschlagen zu beantworten sind: Algorithmen und ihre Umsetzung in
   einer Programmiersprache, Mathematik, Grammatik, Definitionen, alles
   Lehrbuchwissen. Wer nach "Springerproblem in Perl" fragt, will Code sehen,
   keine Quellenliste - schreib den Code hin.
   Nachschlagen lohnt nur bei Tagesaktuellem, bei konkreten Zahlen, bei
   Ortsbezug und dort, wo du dir wirklich unsicher bist.

2. In die Tiefe, nicht in die Breite. Die Suche liefert zu jedem Treffer einen
   Auszug - waehle danach die eine Quelle, die am ehesten passt, und lies sie
   ganz. Ist der Text abgeschnitten, lies mit ab= an derselben Adresse weiter.
   Wechsle die Quelle erst, wenn sie wirklich nichts hergibt.

3. Meldet hole_seite "[nichts zu holen]", war die Seite leer (Paywall,
   Zustimmungsfrage, JavaScript). Sie zaehlt nicht als geprueft, und du sollst
   sie auch nicht als Beleg nennen.

4. Hoechstens zwei Suchlaeufe. Bringt der zweite nichts Brauchbares, antworte
   mit dem, was du hast, und sage offen, was offen blieb.

Nachschlagen ist ein Mittel, kein Selbstzweck: die beste Antwort ist oft die
ohne einen einzigen Abruf. Antworte auf Deutsch. Nur wenn du tatsaechlich
nachgeschlagen hast, nenne am Ende die Adressen, auf die du dich stuetzt."""


def frage_mit_netz(auftrag: str, system: str | None = None, leise: bool = False,
                   max_tokens: int = 700, runden: int = MAX_RUNDEN,
                   verlauf: list | None = None, melder=None,
                   wahl: str = "auto") -> str:
    """Stellt eine Frage und laesst das Modell selbst entscheiden, ob und was
    es nachschlaegt. Gibt die Endantwort zurueck.

    verlauf: [{"frage": ..., "antwort": ...}] vorangegangener Runden
    melder:  wird bei jedem Werkzeugaufruf gerufen - die Konsole zeigt damit
             live an, wo das Modell gerade nachsieht
    """
    nachrichten = []
    if system:
        nachrichten.append({"role": "system", "content": system})
    for runde in (verlauf or [])[-6:]:
        nachrichten.append({"role": "user", "content": str(runde.get("frage", ""))})
        nachrichten.append({"role": "assistant",
                            "content": str(runde.get("antwort", ""))})
    nachrichten.append({"role": "user", "content": auftrag})

    def sagen(text: str) -> None:
        if melder:
            melder(text)
        if not leise:
            print(f"  {text}", file=sys.stderr, flush=True)

    for runde in range(runden):
        antwort = _an_modell(nachrichten, max_tokens, wahl)
        if antwort is None:
            return "(Modell nicht erreichbar)"
        aufrufe = antwort.get("tool_calls") or []
        if not aufrufe:
            return (antwort.get("content") or "").strip()

        nachrichten.append({k: v for k, v in antwort.items()
                            if k in ("role", "content", "tool_calls")})
        for aufruf in aufrufe:
            name = aufruf["function"]["name"]
            try:
                args = json.loads(aufruf["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            sagen(f"[{runde+1}] {name}("
                  + ", ".join(f"{k}={str(v)[:90]}" for k, v in args.items()) + ")")
            try:
                ergebnis = AUSFUEHRUNG[name](args)
            except Verweigert as e:
                ergebnis = f"Abruf nicht ausgefuehrt: {e}"
            except KeyError:
                ergebnis = f"unbekanntes Werkzeug {name}"
            except Exception as e:
                ergebnis = f"Abruf fehlgeschlagen: {e}"
            sagen(f"    {len(str(ergebnis))} Zeichen")
            nachrichten.append({"role": "tool", "tool_call_id": aufruf.get("id", name),
                                "name": name, "content": str(ergebnis)[:MAX_TEXT]})
        # "required" gilt nur fuer den ersten Zug, sonst schlaegt das Modell
        # endlos nach, statt irgendwann zu antworten.
        wahl = "auto"

    # Runden aufgebraucht: nicht mit leeren Haenden abbrechen, sondern die
    # Werkzeuge wegnehmen und aus dem Gesammelten antworten lassen.
    sagen("Rundenende - Antwort aus dem bisher Gelesenen")
    nachrichten.append({"role": "user", "content":
        "Genug nachgeschlagen. Antworte jetzt aus dem, was du gelesen hast, "
        "und sage offen, was offen geblieben ist."})
    letzte = _an_modell(nachrichten, max_tokens, "none")
    if letzte and (letzte.get("content") or "").strip():
        return letzte["content"].strip()
    return (f"(Keine Antwort: das Modell hat {runden} Runden lang "
            f"nachgeschlagen und danach nichts formuliert.)")


def _an_modell(nachrichten: list, max_tokens: int,
               wahl: str = "auto") -> dict | None:
    """wahl steuert, ob nachgeschlagen werden darf:
       auto     das Modell entscheidet selbst (Vorgabe)
       required es muss mindestens einmal nachschlagen
       none     es muss aus dem eigenen Wissen antworten"""
    body = {"messages": nachrichten, "max_tokens": max_tokens,
            "temperature": 0.2}
    if wahl != "none":
        body["tools"] = WERKZEUGE
        body["tool_choice"] = wahl
    body = json.dumps(body).encode()
    req = urllib.request.Request(LLM_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        # Grosszuegig: gruendliches Lesen laesst den Kontext auf mehrere
        # tausend Token wachsen, und die CPU braucht dafuer Minuten. Ein
        # knappes Limit bestraft genau das Verhalten, das erwuenscht ist.
        with urllib.request.urlopen(req, timeout=1200) as resp:
            return json.load(resp)["choices"][0]["message"]
    except Exception as e:
        print(f"LLM: {e}", file=sys.stderr)
        return None


def main() -> int:
    global AUCH_INTERN
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frage", nargs="+")
    ap.add_argument("--auch-intern", action="store_true",
                    help="auch private Adressen abrufen (Heimnetz, WireGuard)")
    ap.add_argument("--leise", action="store_true", help="ohne Werkzeugprotokoll")
    ap.add_argument("--runden", type=int, default=MAX_RUNDEN)
    ap.add_argument("--holen", metavar="URL",
                    help="nur diese Seite holen und ausgeben, ohne Modell")
    args = ap.parse_args()
    AUCH_INTERN = args.auch_intern

    if args.holen:
        print(hole_seite(args.holen))
        return 0

    system = ("Du hast lesenden Zugriff aufs Internet. Nutze die Werkzeuge, wenn "
              "du etwas nachschlagen musst, und antworte danach auf Deutsch. "
              "Nenne die Adressen, auf die du dich stuetzt.")
    print(frage_mit_netz(" ".join(args.frage), system=system, leise=args.leise,
                         runden=args.runden))
    return 0


if __name__ == "__main__":
    sys.exit(main())
