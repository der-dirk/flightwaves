"""SQLite-Zugriff. Kein ORM – bei dieser Zahl von Tabellen ist SQL direkter (Spez. 11)."""

import csv
import sqlite3

# Zeitstempel stehen als ISO-8601 mit "+00:00" in der Datenbank: das ist
# datetime.isoformat() ohne eigenes Format, lexikografisch sortierbar und
# trägt die Zeitzone sichtbar am Wert – das Abnahmekriterium der Stufe 1.1
# verlangt, dass sich UTC am gespeicherten Wert prüfen lässt.
#
# ponytail: Schema mit IF NOT EXISTS statt Migrationsframework. Sobald sich
# eine bestehende Spalte ändert (Stufe 1.2 bringt weather/noise_predictions),
# eine Tabelle schema_migrations dazunehmen.
SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id             INTEGER PRIMARY KEY,
    icao24         TEXT NOT NULL,
    callsign       TEXT,
    aircraft_type  TEXT,              -- Typenkürzel; Lärmklasse wird bei Bedarf
                                      -- daraus abgeleitet, das spart Migrationen
    first_seen_utc TEXT NOT NULL,
    last_seen_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flight_positions (
    id               INTEGER PRIMARY KEY,
    flight_id        INTEGER NOT NULL REFERENCES flights (id) ON DELETE CASCADE,
    observed_utc     TEXT NOT NULL,   -- Abstrahlzeit, Empfangszeit auf der Pi-Uhr
    latitude         REAL NOT NULL,
    longitude        REAL NOT NULL,
    altitude_m       REAL NOT NULL,   -- verwendete Höhe über MSL
    altitude_source  TEXT NOT NULL,   -- 'geometric' oder 'barometric', nie leer
    baro_altitude_m  REAL,            -- Druckhöhe wie gemeldet, unkorrigiert
    ground_speed_ms  REAL,
    track_deg        REAL,
    vertical_rate_ms REAL,
    source           TEXT NOT NULL,   -- 'adsb' oder 'opensky'
    CHECK (altitude_source IN ('geometric', 'barometric')),
    CHECK (source IN ('adsb', 'opensky')),
    CHECK (observed_utc LIKE '%+00:00')
);

CREATE TABLE IF NOT EXISTS weather (
    id                  INTEGER PRIMARY KEY,
    observed_utc        TEXT NOT NULL,
    level_m             REAL NOT NULL,   -- 0 = Boden, sonst geopotentielle Höhe
    wind_direction_deg  REAL,            -- meteorologisch: woher der Wind weht
    wind_speed_ms       REAL,
    temperature_c       REAL,
    humidity_pct        REAL,            -- nur am Boden
    pressure_msl_hpa    REAL,            -- nur am Boden; QNH für die Höhenkorrektur
    source              TEXT NOT NULL,
    CHECK (observed_utc LIKE '%+00:00'),
    UNIQUE (observed_utc, level_m)       -- derselbe Abruf zweimal ist kein neuer Wert
);

CREATE INDEX IF NOT EXISTS idx_weather_level_time
    ON weather (level_m, observed_utc);

CREATE INDEX IF NOT EXISTS idx_positions_flight_time
    ON flight_positions (flight_id, observed_utc);
CREATE INDEX IF NOT EXISTS idx_flights_icao24_last_seen
    ON flights (icao24, last_seen_utc);
"""


def connect(path):
    """Verbindung öffnen, Schema sicherstellen.

    WAL, weil Flug-, Wetter- und Audio-Collector nebeneinander schreiben
    (Spez. 10). synchronous=NORMAL: bei Stromausfall fehlt höchstens die
    letzte Transaktion, und Lücken sind nach Spez. 13 zulässig.
    """
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- Stammdaten: icao24 -> Typenkürzel, offline (Spez. 4) -------------------

def aircraft_types(path):
    """Nachschlagefunktion icao24 -> Typenkürzel; gibt None zurück, wenn unbekannt.

    Fehlt die Stammdatenbank, wird nichts aufgelöst statt zu scheitern: die
    Flüge sind auch ohne Typ die wertvollere Aufzeichnung, unbekannte
    Kennungen rechnet Stufe 1.2 mit mittlerem Referenzpegel weiter.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return lambda icao24: None

    cache = {}

    def lookup(icao24):
        if icao24 not in cache:
            row = conn.execute(
                "SELECT type_designator FROM aircraft WHERE icao24 = ?", (icao24,)
            ).fetchone()
            cache[icao24] = row[0] if row else None
        return cache[icao24]

    return lookup


def build_aircraft_db(csv_path, out_path):
    """Stammdatenbank aus einer CSV bauen (OpenSky-Aircraft-Database o. ä.).

    Erwartet Spalten icao24 und typecode; erkannt werden auch die Kopfzeilen
    des Schwesterprojekts (type/type_designator). Gespeichert wird nur das
    Typenkürzel – mehr braucht das Modell nicht.

    Führende Kommentarzeilen (`#`) werden übersprungen: Der mitgelieferte
    Bestand in assets/ trägt dort seinen Herkunftsnachweis, und eine
    ICAO24-Kennung beginnt nie mit einem Rautezeichen.
    """
    conn = sqlite3.connect(out_path)
    conn.execute("DROP TABLE IF EXISTS aircraft")
    conn.execute("CREATE TABLE aircraft (icao24 TEXT PRIMARY KEY, type_designator TEXT NOT NULL)")

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(line for line in handle if not line.startswith("#"))
        names = {n.lower(): n for n in reader.fieldnames or []}
        icao_col = names.get("icao24") or names.get("icao")
        type_col = names.get("typecode") or names.get("type_designator") or names.get("type")
        if not icao_col or not type_col:
            raise SystemExit(f"{csv_path}: Spalten icao24/typecode nicht gefunden")

        rows = (
            (r[icao_col].strip().lower(), r[type_col].strip().upper())
            for r in reader
            if r.get(icao_col) and r.get(type_col) and r[type_col].strip()
        )
        conn.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?)", rows)

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]
    conn.close()
    return count
