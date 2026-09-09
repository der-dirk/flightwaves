"""Weboberfläche (Spez. 14).

Nur lesend und nur im lokalen Netz erreichbar, ohne Login – und deshalb nie
ins Internet zu exponieren. Zeiten werden lokal angezeigt, intern bleibt
alles UTC.

Ohne Rahmenwerk: für eine Handvoll lesender Endpunkte reicht die
Standardbibliothek, und der Pi braucht dann nichts installiert zu bekommen.
"""

import csv
import http.server
import io
import json
import logging
import signal
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta

from . import db, noise

LOG = logging.getLogger(__name__)


def current_aircraft(conn, live_seconds, track_seconds):
    """Flugzeuge mit Spur, Prognose und Zeitpunkt der letzten Meldung."""
    seit = (datetime.now(UTC) - timedelta(seconds=track_seconds)).isoformat()
    fluege = {}
    for row in conn.execute(
        "SELECT p.flight_id, p.observed_utc, p.latitude, p.longitude, p.altitude_m,"
        " p.ground_speed_ms, p.track_deg, p.vertical_rate_ms, p.source,"
        " f.icao24, f.callsign, f.aircraft_type,"
        " n.level_dba, n.category, n.slant_distance_m, n.elevation_deg, n.arrival_utc"
        " FROM flight_positions p"
        " JOIN flights f ON f.id = p.flight_id"
        " LEFT JOIN noise_predictions n"
        "   ON n.flight_id = p.flight_id AND n.emitted_utc = p.observed_utc"
        " WHERE p.observed_utc >= ? ORDER BY p.flight_id, p.observed_utc",
        (seit,),
    ):
        flug = fluege.setdefault(row["flight_id"], {"track": []})
        flug["track"].append([row["latitude"], row["longitude"]])
        flug.update(
            {
                "flight_id": row["flight_id"],
                "icao24": row["icao24"],
                "callsign": row["callsign"],
                "aircraft_type": row["aircraft_type"],
                "observed_utc": row["observed_utc"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "altitude_m": row["altitude_m"],
                "ground_speed_ms": row["ground_speed_ms"],
                "track_deg": row["track_deg"],
                "vertical_rate_ms": row["vertical_rate_ms"],
                "source": row["source"],
                "level_dba": row["level_dba"],
                "category": row["category"],
                "slant_distance_m": row["slant_distance_m"],
                "elevation_deg": row["elevation_deg"],
                "arrival_utc": row["arrival_utc"],
            }
        )

    frisch = (datetime.now(UTC) - timedelta(seconds=live_seconds)).isoformat()
    return [flug for flug in fluege.values() if flug["observed_utc"] >= frisch]


def loudest_flights(conn, hours, limit=200):
    """LAmax je Flug – das Maximum der Zeitreihe, keine eigene Größe (Spez. 6)."""
    seit = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM (SELECT f.id AS flight_id, f.icao24, f.callsign, f.aircraft_type,"
            " f.first_seen_utc, f.last_seen_utc, p.level_dba, p.category,"
            " p.slant_distance_m, p.elevation_deg, p.arrival_utc, ROW_NUMBER() OVER"
            " (PARTITION BY p.flight_id ORDER BY p.level_dba DESC) AS rang"
            " FROM noise_predictions p JOIN flights f ON f.id = p.flight_id"
            " WHERE p.arrival_utc >= ?) WHERE rang = 1"
            " ORDER BY level_dba DESC LIMIT ?",
            (seit, limit),
        )
    ]


def export_rows(conn, hours):
    """Je Flug eine Zeile – die Einheit, mit der eine Auswertung rechnet."""
    classes = noise.noise_classes()
    zeilen = []
    for flug in loudest_flights(conn, hours, limit=100000):
        zeilen.append(
            {
                "icao24": flug["icao24"],
                "callsign": flug["callsign"],
                "aircraft_type": flug["aircraft_type"],
                "noise_class": classes.get((flug["aircraft_type"] or "").upper(), "unknown"),
                "first_seen_utc": flug["first_seen_utc"],
                "last_seen_utc": flug["last_seen_utc"],
                "predicted_lamax_dba": round(flug["level_dba"], 1),
                "category": flug["category"],
                "slant_distance_m": round(flug["slant_distance_m"], 1),
                "elevation_deg": round(flug["elevation_deg"], 2),
                "arrival_utc": flug["arrival_utc"],
            }
        )
    return zeilen


def _handler(cfg, conn_factory):
    site = cfg["site"]
    web = cfg["web"]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            pfad = urllib.parse.urlparse(self.path)
            frage = urllib.parse.parse_qs(pfad.query)
            stunden = float(frage.get("hours", ["6"])[0])
            conn = conn_factory()
            try:
                if pfad.path in ("/", "/index.html"):
                    return self._send(PAGE.encode(), "text/html; charset=utf-8")
                if pfad.path == "/api/state":
                    return self._json({
                        "site": site,
                        "model_version": noise.MODEL_VERSION,
                        "generated_utc": datetime.now(UTC).isoformat(),
                        "aircraft": current_aircraft(
                            conn, web["live_seconds"], web["track_seconds"]
                        ),
                    })
                if pfad.path == "/api/flights":
                    return self._json({"flights": loudest_flights(conn, stunden)})
                if pfad.path == "/api/export.json":
                    return self._json(export_rows(conn, stunden))
                if pfad.path == "/api/export.csv":
                    return self._csv(export_rows(conn, stunden))
            finally:
                conn.close()
            self.send_error(404)

        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload):
            self._send(json.dumps(payload).encode(), "application/json")

        def _csv(self, zeilen):
            puffer = io.StringIO()
            if zeilen:
                schreiber = csv.DictWriter(puffer, fieldnames=list(zeilen[0]))
                schreiber.writeheader()
                schreiber.writerows(zeilen)
            self._send(puffer.getvalue().encode(), "text/csv; charset=utf-8")

        def log_message(self, *args):
            pass        # eine Zeile je Abruf im Sekundentakt wäre nur Lärm

    return Handler


def run(cfg):
    """Weboberfläche servieren, bis das Programm beendet wird."""
    web = cfg["web"]

    def conn_factory():
        # Je Anfrage eine Verbindung: der Server bedient nebenläufig, und
        # SQLite-Verbindungen gehören nicht über Fäden hinweg geteilt.
        return db.connect(cfg["database"]["path"])

    server = http.server.ThreadingHTTPServer(
        (web["host"], web["port"]), _handler(cfg, conn_factory)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("Weboberfläche auf http://%s:%d – nur für das lokale Netz",
             web["host"], web["port"])

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    LOG.info("Weboberfläche beendet")


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlightWaves</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --laut:#d62728; --mittel:#e07000; --leise:#2e7d32; --still:#78849a; }
  * { box-sizing: border-box; }
  body { margin:0; height:100vh; display:flex; flex-direction:column;
         font:14px/1.45 system-ui, sans-serif; color:#16202e; background:#f4f6f9; }
  header { padding:.6rem 1rem; background:#16202e; color:#fff; display:flex;
           gap:1.2rem; align-items:baseline; flex-wrap:wrap; }
  header h1 { font-size:1rem; margin:0; font-weight:600; letter-spacing:.02em; }
  header .meta { font-size:.8rem; color:#9fb0c7; }
  main { flex:1; display:flex; min-height:0; }
  #karte { flex:1; min-width:0; }
  aside { width:34rem; max-width:45vw; overflow:auto; background:#fff;
          border-left:1px solid #dde3ec; }
  aside h2 { font-size:.78rem; text-transform:uppercase; letter-spacing:.07em;
             color:#5b6b82; margin:0; padding:.8rem 1rem .4rem; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:.35rem .6rem; border-bottom:1px solid #eef1f6; }
  th { font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
       color:#5b6b82; font-weight:600; position:sticky; top:0; background:#fff; }
  td.zahl { text-align:right; }
  .pegel { font-weight:600; }
  .laut{color:var(--laut)} .mittel{color:var(--mittel)}
  .leise{color:var(--leise)} .still{color:var(--still)}
  .hinweis { padding:.6rem 1rem; color:#5b6b82; font-size:.8rem; }
  .marke { font:600 11px system-ui; color:#16202e; text-shadow:0 0 3px #fff,0 0 3px #fff; }
</style>
</head>
<body>
<header>
  <h1>FlightWaves</h1>
  <span class="meta" id="standort"></span>
  <span class="meta" id="stand"></span>
  <span class="meta">Pegel sind Schätzungen, ohne Nachkommastelle bis zur Kalibrierung</span>
</header>
<main>
  <div id="karte"></div>
  <aside>
    <h2>In Reichweite</h2>
    <table><thead><tr><th>Flug</th><th>Muster</th><th class="zahl">LAmax</th>
      <th>Kategorie</th><th class="zahl">Abstand</th><th class="zahl">Höhe</th></tr></thead>
      <tbody id="live"></tbody></table>
    <h2>Lauteste der letzten 6 Stunden</h2>
    <table><thead><tr><th>Flug</th><th>Muster</th><th class="zahl">LAmax</th>
      <th class="zahl">Abstand</th><th>Ankunft</th></tr></thead>
      <tbody id="verlauf"></tbody></table>
    <p class="hinweis">Export:
      <a href="/api/export.csv?hours=24">CSV</a> ·
      <a href="/api/export.json?hours=24">JSON</a> (24 h)</p>
  </aside>
</main>
<script>
const KLASSEN = {"laut":"laut","mittel":"mittel","leise":"leise","nicht hörbar":"still"};
const FARBEN = {"laut":"#d62728","mittel":"#e07000","leise":"#2e7d32","nicht hörbar":"#78849a"};
let karte, ebene, gesetzt = false, hatKarte = false;

function stil(kategorie) { return KLASSEN[kategorie] || "still"; }
function zeit(iso) {
  return iso ? new Date(iso).toLocaleTimeString("de-DE") : "–";
}
function pegel(wert, kategorie) {
  if (wert === null || wert === undefined) return '<span class="still">–</span>';
  return `<span class="pegel ${stil(kategorie)}">${Math.round(wert)}</span>`;
}

function karteAufbauen(site) {
  // Ohne Leaflet keine Karte – aber die Tabellen müssen weiterlaufen. Das
  // Gerät steht womöglich in einem Netz ohne Internetzugang, und dann ist
  // eine leere Seite das schlechteste Ergebnis.
  if (typeof L === "undefined") {
    document.getElementById("karte").innerHTML =
      '<p class="hinweis">Karte nicht verf&uuml;gbar: Leaflet wurde nicht geladen ' +
      '(kein Internetzugang im Browser). Die Tabellen arbeiten weiter.</p>';
    return false;
  }
  karte = L.map("karte").setView([site.latitude_deg, site.longitude_deg], 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom: 18, attribution: "© OpenStreetMap"}).addTo(karte);
  L.circleMarker([site.latitude_deg, site.longitude_deg],
    {radius: 6, color: "#16202e", fillColor: "#16202e", fillOpacity: 1}).addTo(karte)
    .bindTooltip(site.name || "Standort", {permanent: false});
  // 20 km ist die Grenze der dichten Abtastung, 50 km der Aufzeichnungsradius.
  for (const [r, text] of [[20000, "20 km"], [50000, "50 km"]]) {
    L.circle([site.latitude_deg, site.longitude_deg],
      {radius: r, color: "#5b6b82", weight: 1, opacity: .35, fill: false,
       dashArray: "4 6"}).addTo(karte)
      .bindTooltip(text, {permanent: false});
  }
  ebene = L.layerGroup().addTo(karte);
  return true;
}

async function aktualisieren() {
  const daten = await (await fetch("/api/state")).json();
  if (!gesetzt) { hatKarte = karteAufbauen(daten.site); gesetzt = true;
    document.getElementById("standort").textContent =
      `${daten.site.name} · ${daten.site.latitude_deg.toFixed(4)}, ` +
      `${daten.site.longitude_deg.toFixed(4)} · ${daten.site.elevation_m} m`;
  }
  document.getElementById("stand").textContent =
    `${daten.aircraft.length} in Reichweite · Stand ${zeit(daten.generated_utc)} · ` +
    `Modell ${daten.model_version}`;

  const sortiert = [...daten.aircraft].sort((a, b) => (b.level_dba ?? -99) - (a.level_dba ?? -99));
  if (hatKarte) {
    ebene.clearLayers();
    for (const f of sortiert) {
      const farbe = FARBEN[f.category] || "#78849a";
      if (f.track.length > 1) {
        L.polyline(f.track, {color: farbe, weight: 2, opacity: .55}).addTo(ebene);
      }
      L.circleMarker([f.latitude, f.longitude],
        {radius: 5, color: farbe, fillColor: farbe, fillOpacity: .85, weight: 1})
        .addTo(ebene)
        .bindTooltip(
          `<b>${f.callsign || f.icao24}</b> ${f.aircraft_type || ""}<br>` +
          `${f.level_dba != null ? Math.round(f.level_dba) + " dB(A) · " + f.category
                                 : "keine Prognose"}<br>` +
          `${(f.slant_distance_m/1000).toFixed(1)} km schräg · ` +
          `${Math.round(f.altitude_m)} m MSL`, {direction: "top"});
    }
  }

  document.getElementById("live").innerHTML = sortiert.map(f => `
    <tr><td>${f.callsign || f.icao24}</td><td>${f.aircraft_type || "–"}</td>
    <td class="zahl">${pegel(f.level_dba, f.category)}</td>
    <td class="${stil(f.category)}">${f.category || "keine Prognose"}</td>
    <td class="zahl">${(f.slant_distance_m/1000).toFixed(1)} km</td>
    <td class="zahl">${Math.round(f.altitude_m)} m</td></tr>`).join("")
    || '<tr><td colspan="6" class="hinweis">Nichts in Reichweite.</td></tr>';
}

async function verlaufLaden() {
  const daten = await (await fetch("/api/flights?hours=6")).json();
  document.getElementById("verlauf").innerHTML = daten.flights.slice(0, 40).map(f => `
    <tr><td>${f.callsign || f.icao24}</td><td>${f.aircraft_type || "–"}</td>
    <td class="zahl">${pegel(f.level_dba, f.category)}</td>
    <td class="zahl">${(f.slant_distance_m/1000).toFixed(1)} km</td>
    <td>${zeit(f.arrival_utc)}</td></tr>`).join("")
    || '<tr><td colspan="5" class="hinweis">Noch keine Prognosewerte.</td></tr>';
}

aktualisieren(); verlaufLaden();
setInterval(aktualisieren, 2000);
setInterval(verlaufLaden, 30000);
</script>
</body>
</html>
"""
