#!/usr/bin/env python3
"""
Täglicher Marktbriefing-Agent (08:00 Europe/Berlin)

Datenquellen (alle kostenlos):
  - ECB Data Portal  (€STR, Euribor)            kein Key nötig
  - FRED             (SOFR, Fed Funds, UST, OAS) FRED_API_KEY nötig (gratis)
  - Stooq            (Bund/OAT/BTP-Renditen)     kein Key nötig
  - Yahoo Finance    (Indizes, FX, Rohstoffe)    via yfinance

Interpretation: Claude API (ANTHROPIC_API_KEY). Ohne Key wird nur die Tabelle gesendet.
Versand: SMTP (SMTP_HOST/PORT/USER/PASS, MAIL_TO). Ohne SMTP: Ausgabe in der Konsole.

Jeder Abruf schlägt "weich" fehl: Ein fehlender Wert wird als n/a angezeigt.
"""
import csv
import io
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

FRED_KEY = os.getenv("FRED_API_KEY")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
TIMEOUT = 20

# ----------------------------------------------------------------- Datenabruf
def fred(series):
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series, "api_key": FRED_KEY, "file_type": "json",
                "sort_order": "desc", "limit": 10},
        timeout=TIMEOUT)
    r.raise_for_status()
    vals = [float(o["value"]) for o in r.json()["observations"] if o["value"] != "."]
    return vals[0], vals[1]


def ecb(key):
    r = requests.get(f"https://data-api.ecb.europa.eu/service/data/{key}",
                     params={"lastNObservations": 2, "format": "csvdata"}, timeout=TIMEOUT)
    r.raise_for_status()
    rows = sorted(csv.DictReader(io.StringIO(r.text)), key=lambda x: x["TIME_PERIOD"])
    vals = [float(x["OBS_VALUE"]) for x in rows]
    return vals[-1], vals[-2]


def stooq(symbol):
    r = requests.get("https://stooq.com/q/d/l/", params={"s": symbol, "i": "d"}, timeout=TIMEOUT)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return float(rows[-1]["Close"]), float(rows[-2]["Close"])


def yahoo(symbol):
    import yfinance as yf
    closes = yf.Ticker(symbol).history(period="7d")["Close"].dropna()
    return float(closes.iloc[-1]), float(closes.iloc[-2])


FETCH = {"fred": fred, "ecb": ecb, "stooq": stooq, "yahoo": yahoo}

# kind: "rate" -> Änderung in bp | "px" -> Änderung in %
ITEMS = [
    ("Zinsen & Geldmarkt", "€STR", "ecb", "EST/B.EU000A2X2A25.WT", "rate"),
    ("Zinsen & Geldmarkt", "Euribor 3M", "ecb", "FM/D.U2.EUR.RT.MM.EURIBOR3MD_.HSTA", "rate"),
    ("Zinsen & Geldmarkt", "SOFR", "fred", "SOFR", "rate"),
    ("Zinsen & Geldmarkt", "Fed Funds (eff.)", "fred", "DFF", "rate"),
    ("Staatsanleihen-Renditen", "Bund 2J", "stooq", "2dey.b", "rate"),
    ("Staatsanleihen-Renditen", "Bund 10J", "stooq", "10dey.b", "rate"),
    ("Staatsanleihen-Renditen", "OAT 10J", "stooq", "10fry.b", "rate"),
    ("Staatsanleihen-Renditen", "BTP 10J", "stooq", "10ity.b", "rate"),
    ("Staatsanleihen-Renditen", "UST 2J", "fred", "DGS2", "rate"),
    ("Staatsanleihen-Renditen", "UST 10J", "fred", "DGS10", "rate"),
    ("Staatsanleihen-Renditen", "UST 30J", "fred", "DGS30", "rate"),
    ("Kreditspreads", "US IG OAS", "fred", "BAMLC0A0CM", "rate"),
    ("Kreditspreads", "US HY OAS", "fred", "BAMLH0A0HYM2", "rate"),
    ("Aktienindizes", "DAX", "yahoo", "^GDAXI", "px"),
    ("Aktienindizes", "Euro Stoxx 50", "yahoo", "^STOXX50E", "px"),
    ("Aktienindizes", "S&P 500", "yahoo", "^GSPC", "px"),
    ("Aktienindizes", "Nasdaq Composite", "yahoo", "^IXIC", "px"),
    ("Aktienindizes", "Nikkei 225", "yahoo", "^N225", "px"),
    ("Aktienindizes", "VIX", "yahoo", "^VIX", "px"),
    ("FX", "EUR/USD", "yahoo", "EURUSD=X", "px"),
    ("FX", "EUR/CHF", "yahoo", "EURCHF=X", "px"),
    ("FX", "USD/JPY", "yahoo", "JPY=X", "px"),
    ("Rohstoffe", "Brent", "yahoo", "BZ=F", "px"),
    ("Rohstoffe", "Gold", "yahoo", "GC=F", "px"),
    ("Rohstoffe", "TTF Gas", "yahoo", "TTF=F", "px"),
]

# Berechnete Spreads: (Name, Minuend, Subtrahend), Ergebnis in bp (Yields in % -> *100)
SPREADS = [
    ("OAT-Bund 10J", "OAT 10J", "Bund 10J"),
    ("BTP-Bund 10J", "BTP 10J", "Bund 10J"),
    ("UST 2s10s", "UST 10J", "UST 2J"),
    ("UST 10s30s", "UST 30J", "UST 10J"),
    ("Bund 2s10s", "Bund 10J", "Bund 2J"),
]


def collect():
    data = {}  # name -> (cat, last, prev, kind) | None
    for cat, name, src, key, kind in ITEMS:
        try:
            if src == "fred" and not FRED_KEY:
                raise RuntimeError("FRED_API_KEY fehlt")
            last, prev = FETCH[src](key)
            data[name] = (cat, last, prev, kind)
        except Exception as e:  # weiches Scheitern
            print(f"[warn] {name}: {e}", file=sys.stderr)
            data[name] = (cat, None, None, kind)
    for name, a, b in SPREADS:
        da, db = data.get(a), data.get(b)
        if da and db and None not in (da[1], da[2], db[1], db[2]):
            data[name] = ("Spreads", (da[1] - db[1]) * 100, (da[2] - db[2]) * 100, "bp")
        else:
            data[name] = ("Spreads", None, None, "bp")
    # OAS aus FRED liegen in % vor -> Anzeige ebenfalls in bp
    for n in ("US IG OAS", "US HY OAS"):
        cat, last, prev, _ = data[n]
        if last is not None:
            data[n] = ("Spreads", last * 100, prev * 100, "bp")
        else:
            data[n] = ("Spreads", None, None, "bp")
    return data


# ----------------------------------------------------------------- Darstellung
CAT_ORDER = ["Zinsen & Geldmarkt", "Staatsanleihen-Renditen", "Spreads",
             "Aktienindizes", "FX", "Rohstoffe"]


def fmt_row(name, last, prev, kind):
    if last is None:
        return f"  {name:<18} n/a"
    if kind == "rate":
        return f"  {name:<18} {last:>9.3f} %   {(last - prev) * 100:>+7.1f} bp"
    if kind == "bp":
        return f"  {name:<18} {last:>9.1f} bp  {last - prev:>+7.1f} bp"
    return f"  {name:<18} {last:>9,.2f}      {(last / prev - 1) * 100:>+7.2f} %"


def table(data):
    out = []
    for cat in CAT_ORDER:
        out.append(f"\n{cat.upper()}")
        for name, (c, last, prev, kind) in data.items():
            if c == cat:
                out.append(fmt_row(name, last, prev, kind))
    return "\n".join(out)


LEGEND = """LEGENDE
Geldmarkt
  €STR   Euro Short-Term Rate: unbesicherter Tagesgeldsatz im Euroraum (EZB), Referenz für Overnight-Zinsen.
  Euribor 3M  Zins, zu dem Banken einander Euro für 3 Monate leihen; Basis vieler variabler Kredite.
  SOFR   Secured Overnight Financing Rate: besicherter US-Tagesgeldsatz, löst den LIBOR ab.
  Fed Funds  Effektiver Leitzins der US-Notenbank (Tagesgeld zwischen US-Banken).
Anleihen
  Bund   Deutsche Bundesanleihe, Euro-Benchmark für "risikolos". J = Restlaufzeit in Jahren.
  OAT    Obligation Assimilable du Trésor: französische Staatsanleihe.
  BTP    Buono del Tesoro Poliennale: italienische Staatsanleihe.
  UST    US Treasury: US-Staatsanleihe, globale Benchmark.
  Yield/Rendite  Effektivverzinsung bis Laufzeitende; steigt, wenn der Kurs fällt.
Spreads
  bp     Basispunkt = 0,01 Prozentpunkte (100 bp = 1 %).
  OAT-Bund / BTP-Bund  Renditeabstand zu Bunds; Maß für Länder- und Fragmentierungsrisiko im Euroraum.
  2s10s / 10s30s  Differenz zwischen langer und kurzer Rendite = Steilheit der Zinskurve.
  OAS    Option-Adjusted Spread: Risikoaufschlag von Unternehmensanleihen ggü. Staatsanleihen.
  IG / HY  Investment Grade (gute Bonität) / High Yield (schwächere Bonität).
  iTraxx / CDX  CDS-Indizes für Europa/USA. Nicht frei verfügbar; US IG/HY OAS dienen als Näherung.
Märkte
  VIX    Erwartete S&P-500-Schwankung der nächsten 30 Tage ("Angstbarometer").
  Euro Stoxx 50 / DAX / S&P 500 / Nasdaq / Nikkei  Leitindizes Euroraum, Deutschland, USA, Tech-USA, Japan.
  Brent / TTF  Nordseeöl-Benchmark / europäischer Erdgas-Preis (Dutch Title Transfer Facility).
  EUR/USD, EUR/CHF, USD/JPY  Wechselkurse (Preis der erstgenannten Währung in der zweiten).
Hinweis: Stand = letzter verfügbarer Schlusskurs; Änderung ggü. vorherigem Handelstag."""

SYSTEM_PROMPT = """Du bist Marktstratege und schreibst ein Morgenbriefing auf Deutsch für einen Profi.
Du bekommst eine Datentabelle. Würdige und interpretiere JEDE Kategorie in 2-3 Sätzen:
Zinsen & Geldmarkt, Staatsanleihen-Renditen, Spreads, Aktienindizes, FX, Rohstoffe.
Ordne ein: Richtung, Größenordnung der Bewegung, Zusammenhänge zwischen den Kategorien
(z. B. Renditen vs. Aktien, Öl vs. Inflationserwartung, Kurvensteilheit, Länderspreads).
Regeln: Nutze ausschließlich Zahlen aus der Tabelle. Erfinde keine Nachrichten oder Ursachen;
formuliere Ursachen als Hypothese. Werte mit n/a erwähnst du kurz als fehlend.
Schließe mit "Fazit" (3 Sätze: was ist heute marktrelevant). Kein Markdown, nur Fließtext
mit Kategorie-Überschriften in GROSSBUCHSTABEN."""


def interpret(tbl):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "(Keine Interpretation: ANTHROPIC_API_KEY nicht gesetzt.)"
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 1500, "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": tbl}]},
        timeout=90)
    r.raise_for_status()
    return "".join(b["text"] for b in r.json()["content"] if b["type"] == "text")


# ----------------------------------------------------------------- Versand
def send_mail(subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        print(body)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")),
                          context=ssl.create_default_context()) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def write_site(now, data, text):
    """Schreibt docs/index.html (GitHub Pages) + docs/data.json."""
    import html as h
    import json
    cards = []
    for cat in CAT_ORDER:
        rows = []
        for name, (c, last, prev, kind) in data.items():
            if c != cat:
                continue
            if last is None:
                rows.append(f"<tr><td>{h.escape(name)}</td><td class=n>n/a</td><td></td></tr>")
                continue
            if kind == "px":
                val, d, unit = f"{last:,.2f}", (last / prev - 1) * 100, "%"
            elif kind == "rate":
                val, d, unit = f"{last:.3f} %", (last - prev) * 100, "bp"
            else:
                val, d, unit = f"{last:.1f} bp", last - prev, "bp"
            cls = "up" if d > 0 else "dn" if d < 0 else ""
            rows.append(f"<tr><td>{h.escape(name)}</td><td class=n>{val}</td>"
                        f"<td class='n {cls}'>{d:+.2f} {unit}</td></tr>")
        cards.append(f"<section><h2>{h.escape(cat)}</h2><table>{''.join(rows)}</table></section>")
    paras = "".join(f"<p>{h.escape(p.strip())}</p>" for p in text.split("\n\n") if p.strip())
    page = f"""<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Marktbriefing {now:%d.%m.%Y}</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#667;--card:#f5f6f8;--up:#0a7d3b;--dn:#c0262d}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111316;--fg:#e8e8e8;--mut:#99a;--card:#1b1e23;--up:#4cc380;--dn:#ff6b72}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:auto}}
h1{{font-size:20px;margin:0}}.sub{{color:var(--mut);font-size:13px;margin:2px 0 16px}}
section,.box{{background:var(--card);border-radius:12px;padding:12px 14px;margin:0 0 12px}}
h2{{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:0 0 6px}}
table{{width:100%;border-collapse:collapse}}td{{padding:4px 0}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.up{{color:var(--up)}}.dn{{color:var(--dn)}}pre{{white-space:pre-wrap;font-size:12px;color:var(--mut)}}summary{{cursor:pointer;font-weight:600}}
</style></head><body>
<h1>Marktbriefing</h1><div class=sub>Stand {now:%d.%m.%Y, %H:%M} Uhr (Berlin) · Schlusskurse, Änderung ggü. Vortag</div>
{''.join(cards)}
<div class=box><h2>Einordnung</h2>{paras}</div>
<div class=box><details><summary>Legende</summary><pre>{h.escape(LEGEND)}</pre></details></div>
</body></html>"""
    os.makedirs("docs", exist_ok=True)
    open("docs/index.html", "w", encoding="utf-8").write(page)
    json.dump({"updated": now.isoformat(),
               "data": {k: {"last": v[1], "prev": v[2]} for k, v in data.items()}},
              open("docs/data.json", "w"), indent=1)


def main():
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    # GitHub-Cron läuft in UTC (Sommer-/Winterzeit): nur um 8 Uhr Berliner Zeit senden
    if os.getenv("CI") and not os.getenv("FORCE") and now.hour != 8:
        return
    if now.weekday() >= 5 and not os.getenv("FORCE"):
        return
    data = collect()
    tbl = table(data)
    text = interpret(tbl)
    write_site(now, data, text)  # Web-App (GitHub Pages)
    body = (f"MARKTBRIEFING {now:%d.%m.%Y, %H:%M} Uhr\n{tbl}\n\n"
            f"EINORDNUNG\n{text}\n\n{LEGEND}\n")
    if os.getenv("SMTP_HOST"):  # Mail ist optional
        send_mail(f"Marktbriefing {now:%d.%m.%Y}", body)
    elif not os.getenv("CI"):
        print(body)


if __name__ == "__main__":
    main()
