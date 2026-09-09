"""Flug-Collector: Meldungen filtern, Flügen zuordnen, speichern (Spez. 4)."""

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import db
from .geo import altitude_msl, bounding_box, relate
from .sources import OpenSky, read_dump1090, read_open_meteo

LOG = logging.getLogger(__name__)
FAILURE_STREAK = 5      # Erst wiederholte Fehlschläge werden gemeldet (Spez. 13)


@dataclass
class _State:
    """Was der Collector je ICAO24-Kennung im Gedächtnis behält."""

    flight_id: int
    last_seen: datetime         # letzte Meldung im Radius, auch ungespeicherte
    last_stored: datetime | None = None
    last_local: datetime | None = None
    callsign: str | None = None


class Tracker:
    """Entscheidet je Meldung: verwerfen, verdichten oder speichern.

    Keine Netz- und keine Hardwarezugriffe – die Regeln sind ohne Gerät
    testbar, die Verbindung kommt von außen.
    """

    def __init__(self, conn, cfg, lookup_type=lambda icao24: None):
        self.conn = conn
        self.site = cfg["site"]
        self.tracking = cfg["tracking"]
        self.grace_seconds = cfg["opensky"]["local_grace_seconds"]
        self.max_qnh_age_s = cfg["weather"]["max_qnh_age_seconds"]
        self.qnh_hpa = None
        self.lookup_type = lookup_type
        self.state: dict[str, _State] = {}
        self.stored = 0
        self.dropped_ground = 0
        self._recover_open_flights()
        self.refresh_qnh()

    def _recover_open_flights(self):
        """Laufende Flüge nach einem Neustart weiterführen, statt sie zu zerschneiden.

        Ohne das erzeugt jeder Dienstneustart mitten im Überflug zwei Flüge –
        und damit zwei halbe Maxima statt eines LAmax.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=self.tracking["flight_gap_seconds"])
        rows = self.conn.execute(
            "SELECT id, icao24, callsign, MAX(last_seen_utc) AS last_seen_utc FROM flights "
            "WHERE last_seen_utc > ? GROUP BY icao24",
            (cutoff.isoformat(),),
        ).fetchall()
        for row in rows:
            last_seen = datetime.fromisoformat(row["last_seen_utc"])
            self.state[row["icao24"]] = _State(
                flight_id=row["id"],
                last_seen=last_seen,
                last_stored=last_seen,
                callsign=row["callsign"],
            )
        if rows:
            LOG.info("%d laufende Flüge übernommen", len(rows))

    def refresh_qnh(self):
        """Jüngsten Bodendruck übernehmen, sofern er nicht zu alt ist (Spez. 4).

        Er korrigiert die barometrische Höhe, die eine Druckhöhe der
        Standardatmosphäre ist. Ein veralteter Wert ist schlechter als keiner:
        30 hPa Abweichung sind rund 240 m Höhenfehler.
        """
        row = self.conn.execute(
            "SELECT observed_utc, pressure_msl_hpa FROM weather"
            " WHERE level_m = 0 AND pressure_msl_hpa IS NOT NULL"
            " ORDER BY observed_utc DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self.qnh_hpa = None
            return
        alter = (datetime.now(UTC) - datetime.fromisoformat(row["observed_utc"])).total_seconds()
        self.qnh_hpa = row["pressure_msl_hpa"] if alter <= self.max_qnh_age_s else None

    def ingest(self, positions):
        """Meldungen verarbeiten. Gibt die Zahl gespeicherter Positionen zurück."""
        rows, touched, stored = [], {}, 0

        for pos in sorted(positions, key=lambda p: p.observed):
            # Bodenziele überspringt das Modell ohnehin und sind in
            # Drehkreuznähe ein erheblicher Teil der empfangenen Ziele.
            if pos.on_ground:
                self.dropped_ground += 1
                continue

            altitude = altitude_msl(
                pos.geom_altitude_m,
                pos.baro_altitude_m,
                self.site["geoid_undulation_m"],
                self.qnh_hpa,
            )
            if altitude is None:
                continue        # ohne Höhe keine Schrägentfernung
            altitude_m, altitude_source = altitude

            slant_m = relate(
                self.site["latitude_deg"],
                self.site["longitude_deg"],
                self.site["elevation_m"],
                pos.latitude,
                pos.longitude,
                altitude_m,
            ).slant_m
            if slant_m > self.tracking["radius_m"]:
                continue

            state = self.state.get(pos.icao24)

            # Der lokale Empfänger hat Vorrang; OpenSky ergänzt nur Lücken.
            if (
                pos.source == "opensky"
                and state
                and state.last_local
                and (pos.observed - state.last_local).total_seconds() <= self.grace_seconds
            ):
                continue

            if state and (
                pos.observed - state.last_seen
            ).total_seconds() > self.tracking["flight_gap_seconds"]:
                state = None    # Lücke zu groß: das ist ein neuer Flug

            if state is None:
                state = self._open_flight(pos)
                self.state[pos.icao24] = state

            state.last_seen = max(state.last_seen, pos.observed)
            if pos.source == "adsb":
                state.last_local = pos.observed
            if pos.callsign and not state.callsign:
                state.callsign = pos.callsign
                self.conn.execute(
                    "UPDATE flights SET callsign = ? WHERE id = ?",
                    (pos.callsign, state.flight_id),
                )

            if not self._should_store(state, pos, slant_m):
                continue

            state.last_stored = pos.observed
            touched[state.flight_id] = pos.observed.isoformat()
            rows.append(
                (
                    state.flight_id,
                    pos.observed.isoformat(),
                    pos.latitude,
                    pos.longitude,
                    altitude_m,
                    altitude_source,
                    pos.baro_altitude_m,
                    pos.ground_speed_ms,
                    pos.track_deg,
                    pos.vertical_rate_ms,
                    pos.source,
                )
            )
            stored += 1

        if rows:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO flight_positions (flight_id, observed_utc, latitude, longitude,"
                " altitude_m, altitude_source, baro_altitude_m, ground_speed_ms, track_deg,"
                " vertical_rate_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.executemany(
                "UPDATE flights SET last_seen_utc = ? WHERE id = ?",
                [(seen, flight_id) for flight_id, seen in touched.items()],
            )
            self.conn.execute("COMMIT")

        self.stored += stored
        return stored

    def _should_store(self, state, pos, slant_m):
        """Abtastung nach Entfernung (Spez. 4) und Schutz gegen Wiederholungen.

        dump1090 meldet dieselbe Position, solange keine neue eintrifft; ohne
        den Vergleich der Abstrahlzeit stünde sie sekündlich erneut in der
        Datenbank.
        """
        if state.last_stored is None:
            return True
        seconds = (pos.observed - state.last_stored).total_seconds()
        if seconds <= 0:
            return False
        if slant_m > self.tracking["fine_sampling_radius_m"]:
            return seconds >= self.tracking["coarse_sampling_seconds"]
        return True

    def _open_flight(self, pos):
        cursor = self.conn.execute(
            "INSERT INTO flights (icao24, callsign, aircraft_type, first_seen_utc, last_seen_utc)"
            " VALUES (?,?,?,?,?)",
            (
                pos.icao24,
                pos.callsign,
                self.lookup_type(pos.icao24),
                pos.observed.isoformat(),
                pos.observed.isoformat(),
            ),
        )
        return _State(
            flight_id=cursor.lastrowid,
            last_seen=pos.observed,
            last_local=pos.observed if pos.source == "adsb" else None,
            callsign=pos.callsign,
        )

    def forget_stale(self):
        """Kennungen vergessen, deren Flug abgeschlossen ist – sonst wächst der Speicher."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.tracking["flight_gap_seconds"])
        for icao24 in [k for k, s in self.state.items() if s.last_seen < cutoff]:
            del self.state[icao24]


def _opensky_worker(cfg, site, radius_m, stop, sink):
    """OpenSky in einem eigenen Faden: ein 15-s-Timeout darf den 1-Hz-Takt nicht anhalten."""
    client = OpenSky(cfg)
    box = bounding_box(site["latitude_deg"], site["longitude_deg"], radius_m)
    failures = 0
    while not stop.wait(cfg["poll_interval_seconds"]):
        try:
            sink.put(client.states(box))
            if failures >= FAILURE_STREAK:
                LOG.info("OpenSky wieder erreichbar")
            failures = 0
        except Exception as error:
            failures += 1
            if failures == FAILURE_STREAK:
                LOG.warning("OpenSky seit %d Versuchen nicht erreichbar: %s", failures, error)


def run(cfg):
    """Dauerbetrieb: eigener Empfänger im 1-Hz-Takt, OpenSky nebenher."""
    conn = db.connect(cfg["database"]["path"])
    tracker = Tracker(conn, cfg, db.aircraft_types(cfg["database"]["aircraft_path"]))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    sink = queue.Queue()
    if cfg["opensky"]["enabled"]:
        # Die Box ist großzügiger als der Radius; gefiltert wird über die Schrägentfernung.
        threading.Thread(
            target=_opensky_worker,
            args=(cfg["opensky"], cfg["site"], cfg["tracking"]["radius_m"] * 1.2, stop, sink),
            daemon=True,
        ).start()

    interval = cfg["tracking"]["poll_interval_seconds"]
    failures = 0
    next_report = time.monotonic() + 60
    LOG.info("Collector läuft, Radius %.0f km", cfg["tracking"]["radius_m"] / 1000)

    while not stop.is_set():
        started = time.monotonic()
        positions = []

        if cfg["dump1090"]["enabled"]:
            try:
                positions = read_dump1090(
                    cfg["dump1090"]["url"], cfg["dump1090"]["timeout_seconds"]
                )
                if failures >= FAILURE_STREAK:
                    LOG.info("Empfänger wieder erreichbar")
                failures = 0
            except Exception as error:
                failures += 1
                if failures == FAILURE_STREAK:
                    LOG.warning("dump1090 seit %d Abrufen nicht erreichbar: %s", failures, error)

        while True:
            try:
                positions += sink.get_nowait()
            except queue.Empty:
                break

        if positions:
            tracker.ingest(positions)

        if time.monotonic() >= next_report:
            tracker.forget_stale()
            tracker.refresh_qnh()
            LOG.info(
                "%d Positionen gespeichert, %d Flüge im Blick, %d Bodenziele verworfen",
                tracker.stored,
                len(tracker.state),
                tracker.dropped_ground,
            )
            next_report += 60

        stop.wait(max(0.0, interval - (time.monotonic() - started)))

    conn.close()
    LOG.info("Collector beendet, %d Positionen in dieser Sitzung", tracker.stored)


def store_weather(conn, rows):
    """Wetterzeilen ablegen. Derselbe Abruf zweimal ist kein neuer Wert."""
    cursor = conn.executemany(
        "INSERT OR IGNORE INTO weather (observed_utc, level_m, wind_direction_deg,"
        " wind_speed_ms, temperature_c, humidity_pct, pressure_msl_hpa, source)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                row.observed.isoformat(),
                row.level_m,
                row.wind_direction_deg,
                row.wind_speed_ms,
                row.temperature_c,
                row.humidity_pct,
                row.pressure_msl_hpa,
                row.source,
            )
            for row in rows
        ],
    )
    return cursor.rowcount


def _zahl(wert, einheit):
    return "     –" if wert is None else f"{wert:6.1f} {einheit}"


def run_weather(cfg, once=False):
    """Wetterdaten im 15-Minuten-Takt sammeln (Spez. 5).

    Eigener Dienst neben dem Flug-Collector: Ein Abruf mit 15 s Zeitlimit darf
    den 1-Hz-Takt der Flugdaten nicht anhalten.
    """
    conn = db.connect(cfg["database"]["path"])
    site, weather = cfg["site"], cfg["weather"]

    stop = threading.Event()
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())

    failures = 0
    while True:
        try:
            rows = read_open_meteo(
                site["latitude_deg"], site["longitude_deg"], weather["timeout_seconds"]
            )
            gespeichert = store_weather(conn, rows)
            if failures >= FAILURE_STREAK:
                LOG.info("Wetterdienst wieder erreichbar")
            failures = 0
            LOG.info("%d Niveaus abgerufen, %d neu gespeichert", len(rows), gespeichert)
            if once:
                # Einmallauf ist auch der Prüflauf gegen die echte Schnittstelle:
                # zeigt, ob jedes Feld ankommt oder nur die Zeile.
                for row in rows:
                    print(
                        f"  {row.level_m:>7.0f} m  {row.observed:%Y-%m-%d %H:%M}Z  "
                        f"{_zahl(row.temperature_c, '°C')}  "
                        f"{_zahl(row.wind_speed_ms, 'm/s')}  "
                        f"{_zahl(row.wind_direction_deg, '°')}  "
                        f"{_zahl(row.humidity_pct, '%')}  "
                        f"{_zahl(row.pressure_msl_hpa, 'hPa')}"
                    )
                if not any(row.level_m == 0 for row in rows):
                    LOG.warning("Keine Bodenwerte – ohne sie gibt es keinen QNH")
        except Exception as error:
            failures += 1
            if failures == FAILURE_STREAK:
                LOG.warning("Wetterdienst seit %d Abrufen gestört: %s", failures, error)
            elif once:
                raise
        if once or stop.wait(weather["poll_interval_seconds"]):
            break

    conn.close()
