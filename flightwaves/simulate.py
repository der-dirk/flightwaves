"""Simulierter ADS-B-Empfänger.

Erzeugt Verkehr um den Standort und serviert ihn im Format von dump1090,
damit die ganze Kette ohne Hardware läuft. Der Collector sieht keinen
Unterschied zum echten Empfänger: dieselbe JSON-Schnittstelle, dieselben
Einheiten (Fuß, Knoten, Fuß pro Minute), derselbe 1-Hz-Takt.

Ausgelegt auf einen Standort im An- und Abflugbereich eines Drehkreuzes
(Spez., Vorbemerkung): 20–40 Flugzeuge gleichzeitig in Reichweite.
"""

import csv
import http.server
import json
import logging
import math
import random
import signal
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .geo import offset

LOG = logging.getLogger(__name__)

FEET = 0.3048
KNOTS = 1852 / 3600

# Verkehrsarten mit ihren Kennwerten am Punkt der größten Annäherung.
# (Name, Anteil, Höhe m, Geschwindigkeit m/s, Steigrate m/s, Vorbeiabstand m, Muster)
PROFILES = [
    ("arrival", 30, (450, 2500), (72, 130), (-9, -4), (400, 9000),
     ("A320", "B738", "A20N", "E190", "A321", "B739")),
    ("departure", 28, (600, 4000), (90, 175), (5, 13), (400, 9000),
     ("A320", "B738", "A20N", "CRJ9", "E195", "A21N")),
    ("overflight", 27, (9000, 11800), (215, 265), (-1, 1), (0, 45000),
     ("B77W", "A359", "B789", "A333", "B38M", "A388")),
    ("general", 10, (300, 1800), (40, 75), (-2, 2), (1000, 40000),
     ("C172", "PA28", "SR22", "DA42")),
    ("helicopter", 5, (150, 800), (30, 65), (-2, 2), (300, 25000),
     ("EC35", "H145", "R44", "B06")),
]

FLOOR_M = 120.0        # darunter ist der Flug gelandet und verschwindet
CEILING_M = 12500.0    # darüber steigt kein Verkehrsflugzeug

# Am Repository verankert, nicht am Arbeitsverzeichnis.
REGISTER_CSV = Path(__file__).resolve().parent.parent / "assets" / "aircraft_types.csv"

AIRLINES = ("DLH", "BER", "EWG", "CFG", "AUA", "SWR", "KLM", "AFR", "BAW",
            "RYR", "EZY", "UAE", "SIA", "ACA", "THY", "IBE")


def register_by_type(path=REGISTER_CSV):
    """Echte ICAO24-Kennungen aus dem mitgelieferten Bestand, nach Muster.

    Der Simulator erfindet keine Kennungen: Nimmt er sie aus dem Register,
    prüft der Testlauf dieselbe Typauflösung, die später am Gerät läuft –
    einschließlich der Muster, die dort fehlen.
    """
    wanted = {code for profile in PROFILES for code in profile[6]}
    by_type = {code: [] for code in wanted}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(line for line in handle if not line.startswith("#")):
            code = row["typecode"].strip().upper()
            if code in by_type:
                by_type[code].append(row["icao24"].strip().lower())
    return {code: found for code, found in by_type.items() if found}


@dataclass(frozen=True, slots=True)
class Flight:
    """Ein Flug als Gerade am Standort vorbei.

    Beschrieben über den Punkt der größten Annäherung: dort ist der
    Vorbeiabstand definiert, und von dort aus wird in beide Richtungen
    gekoppelt. Das ist die Größe, die den späteren LAmax bestimmt.
    """

    icao24: str
    callsign: str
    aircraft_type: str
    kind: str
    start_s: float
    end_s: float
    cpa_s: float
    cpa_east_m: float
    cpa_north_m: float
    cpa_altitude_m: float
    heading_deg: float
    speed_ms: float
    climb_ms: float
    has_geometric: bool
    blind_from_s: float
    blind_to_s: float

    def state(self, t):
        """(Ost m, Nord m, Höhe m, Steigrate m/s) oder None, wenn nicht empfangen."""
        if not self.start_s <= t <= self.end_s:
            return None
        if self.blind_from_s <= t <= self.blind_to_s:
            return None     # Abschattung: der Empfänger sieht das Flugzeug nicht
        run = self.speed_ms * (t - self.cpa_s)
        heading = math.radians(self.heading_deg)
        return (
            self.cpa_east_m + math.sin(heading) * run,
            self.cpa_north_m + math.cos(heading) * run,
            self.cpa_altitude_m + self.climb_ms * (t - self.cpa_s),
            self.climb_ms,
        )


def scenario(seed, duration_s, radius_m, register=None, concurrent=30):
    """Flüge erzeugen, die über ``duration_s`` am Standort vorbeikommen."""
    rng = random.Random(seed)
    register = register or {}
    reach_m = 1.3 * radius_m            # etwas über den Radius hinaus, damit
    mean_visible_s = 2 * reach_m / 150  # Flüge auch von außen hereinkommen
    count = max(1, round(concurrent * (duration_s + mean_visible_s) / mean_visible_s))

    kinds = [name for name, share, *_ in PROFILES for _ in range(share)]
    flights = []
    for _ in range(count):
        kind = rng.choice(kinds)
        _, _, alt_range, speed_range, climb_range, miss_range, types = next(
            p for p in PROFILES if p[0] == kind
        )
        speed = rng.uniform(*speed_range)
        climb = rng.uniform(*climb_range)
        altitude = rng.uniform(*alt_range)
        visible = 2 * reach_m / speed
        start = rng.uniform(-mean_visible_s, duration_s)
        end = start + visible
        cpa_s = start + visible / 2

        # Ein steigender Flug war vorher am Boden, ein sinkender landet: das
        # Höhenfenster begrenzt die Sichtbarkeit. Ohne das klebt ein Anflug
        # minutenlang auf der Bodenhöhe, statt zu verschwinden.
        if abs(climb) > 1e-6:
            low, high = sorted(((FLOOR_M - altitude) / climb, (CEILING_M - altitude) / climb))
            start = max(start, cpa_s + low)
            end = min(end, cpa_s + high)

        # Größte Annäherung: ein Punkt im Abstand `miss` unter zufälliger
        # Peilung, überflogen im rechten Winkel dazu – so entsteht ein echter
        # Vorbeiflug statt einer beliebigen Geraden.
        miss = rng.uniform(*miss_range)
        bearing = rng.uniform(0, 360)
        heading = (bearing + rng.choice((90, -90))) % 360

        # Rund jeder zehnte Flug hat eine Empfangslücke (Abschattung, Gelände).
        if rng.random() < 0.1:
            blind_from = start + rng.uniform(0.1, 0.7) * (end - start)
            blind_to = blind_from + rng.uniform(30, 900)
        else:
            blind_from = blind_to = math.inf

        airline = rng.choice(AIRLINES)
        aircraft_type = rng.choice(types)
        known = register.get(aircraft_type)
        flights.append(
            Flight(
                icao24=rng.choice(known) if known else f"{rng.randrange(16**6):06x}",
                callsign=(
                    f"{airline}{rng.randrange(1, 9999)}"
                    if kind in ("arrival", "departure", "overflight")
                    else "D-"
                    + ("H" if kind == "helicopter" else "E")
                    + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3))
                ),
                aircraft_type=aircraft_type,
                kind=kind,
                start_s=start,
                end_s=end,
                cpa_s=cpa_s,
                cpa_east_m=miss * math.sin(math.radians(bearing)),
                cpa_north_m=miss * math.cos(math.radians(bearing)),
                cpa_altitude_m=altitude,
                heading_deg=heading,
                speed_ms=speed,
                climb_ms=climb,
                # Ältere Transponder melden keine geometrische Höhe; dann muss
                # die barometrische herhalten und die Quelle stimmen.
                has_geometric=rng.random() > 0.15,
                blind_from_s=blind_from,
                blind_to_s=blind_to,
            )
        )
    return flights


def ground_traffic(seed, register=None, count=8):
    """Rollverkehr: wird vom Collector verworfen, muss aber vorkommen."""
    rng = random.Random(seed + 1)
    register = register or {}

    def rolling():
        aircraft_type = rng.choice(("A320", "B738", "E190"))
        known = register.get(aircraft_type)
        return (rng.choice(known) if known else f"{rng.randrange(16**6):06x}", aircraft_type)

    fleet = [rolling() for _ in range(count)]
    return [
        Flight(
            icao24=icao24,
            callsign=f"{rng.choice(AIRLINES)}{rng.randrange(1, 9999)}",
            aircraft_type=aircraft_type,
            kind="ground",
            start_s=-math.inf,
            end_s=math.inf,
            cpa_s=0.0,
            cpa_east_m=rng.uniform(-4000, 4000),
            cpa_north_m=rng.uniform(-4000, 4000),
            cpa_altitude_m=0.0,
            heading_deg=rng.uniform(0, 360),
            speed_ms=rng.uniform(0, 15),
            climb_ms=0.0,
            has_geometric=True,
            blind_from_s=math.inf,
            blind_to_s=math.inf,
        )
        for icao24, aircraft_type in fleet
    ]


def snapshot(site, flights, ground, elapsed_s, now, jitter):
    """Ein aircraft.json, wie dump1090 es liefert."""
    aircraft = []
    for flight in flights:
        state = flight.state(elapsed_s)
        if state is None:
            continue
        east_m, north_m, altitude_m, climb_ms = state
        latitude, longitude = offset(
            site["latitude_deg"], site["longitude_deg"], east_m, north_m
        )
        entry = {
            "hex": flight.icao24,
            "flight": flight.callsign.ljust(8),
            "lat": round(latitude, 6),
            "lon": round(longitude, 6),
            # dump1090 meldet die Höhe in 25-Fuß-, die Steigrate in 64-fpm-Schritten.
            "alt_baro": round(altitude_m / FEET / 25) * 25,
            "gs": round(flight.speed_ms / KNOTS, 1),
            "track": round(flight.heading_deg, 1),
            "baro_rate": round(climb_ms / FEET * 60 / 64) * 64,
            "squawk": f"{jitter.randrange(1000, 7777):04d}",
            "messages": jitter.randrange(50, 5000),
            "rssi": round(jitter.uniform(-28, -3), 1),
            "seen": round(jitter.uniform(0.0, 0.4), 1),
            "seen_pos": round(jitter.uniform(0.1, 1.4), 1),
        }
        if flight.has_geometric:
            # Die geometrische Höhe ist ellipsoidisch und liegt hier rund 47 m
            # über der Meereshöhe – genau die Korrektur, die der Collector macht.
            entry["alt_geom"] = round((altitude_m + 47.0) / FEET / 25) * 25
        aircraft.append(entry)

    for flight in ground:
        latitude, longitude = offset(
            site["latitude_deg"], site["longitude_deg"], flight.cpa_east_m, flight.cpa_north_m
        )
        aircraft.append({
            "hex": flight.icao24,
            "flight": flight.callsign.ljust(8),
            "lat": round(latitude, 6),
            "lon": round(longitude, 6),
            "alt_baro": "ground",
            "gs": round(flight.speed_ms / KNOTS, 1),
            "seen_pos": round(jitter.uniform(0.1, 1.4), 1),
        })

    # Ziele ohne Position: Mode-S-Antworten ohne ADS-B. Der Collector muss sie
    # überspringen, ohne zu stolpern.
    for index in range(3):
        aircraft.append({"hex": f"ffff0{index}", "seen": 12.4, "messages": 40})

    return {"now": now, "messages": jitter.randrange(10**6, 10**7), "aircraft": aircraft}


def run(cfg, duration_s, seed, register_csv=REGISTER_CSV):
    """Den simulierten Empfänger auf dem Port aus der Konfiguration servieren."""
    site = cfg["site"]
    register = register_by_type(register_csv)
    flights = scenario(seed, duration_s, cfg["tracking"]["radius_m"], register)
    ground = ground_traffic(seed, register)
    jitter = random.Random(seed)

    started = time.time()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            now = time.time()
            body = json.dumps(
                snapshot(site, flights, ground, now - started, now, jitter)
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass        # eine Zeile je Sekunde wäre nur Lärm

    port = urllib.parse.urlparse(cfg["dump1090"]["url"]).port or 80
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    counts = {}
    for flight in flights:
        counts[flight.kind] = counts.get(flight.kind, 0) + 1
    LOG.info(
        "Simulierter Empfänger auf Port %d: %d Flüge über %d s (%s), %d Bodenziele",
        port, len(flights), duration_s,
        ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), len(ground),
    )

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait(duration_s)
    server.shutdown()
    LOG.info("Simulierter Empfänger beendet")
