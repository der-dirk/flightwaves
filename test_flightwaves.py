"""Prüfungen der Regeln, die nicht offensichtlich sind: python3 -m pytest"""

import csv
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from flightwaves import db
from flightwaves.collector import Tracker
from flightwaves.geo import altitude_msl, relate
from flightwaves.sources import Position, parse_dump1090, parse_states

SITE = {"latitude_deg": 50.0, "longitude_deg": 8.0, "elevation_m": 100.0,
        "geoid_undulation_m": 47.0}
CFG = {
    "site": SITE,
    "tracking": {"radius_m": 50000, "fine_sampling_radius_m": 20000,
                 "coarse_sampling_seconds": 5, "flight_gap_seconds": 600},
    "opensky": {"local_grace_seconds": 30},
}
# Nahe der Wanduhr, weil der Collector laufende Flüge gegen "jetzt" wiederaufnimmt.
T0 = datetime.now(UTC).replace(microsecond=0)


def at(seconds, lat=50.0, lon=8.0, alt_ft_m=1147.0, icao="3c6444", source="adsb"):
    """Eine Meldung senkrecht über dem Standort, sofern nichts anderes gesagt ist."""
    return Position(icao, T0 + timedelta(seconds=seconds), lat, lon, alt_ft_m, None,
                    200.0, 90.0, 0.0, "DLH123", False, source)


# --- Geometrie -------------------------------------------------------------

def test_relate_kennt_die_wahre_entfernung():
    # 0,1 Grad Breite sind auf 50 Grad Nord 11123 m, nicht die 11132 m der Kugel.
    north = relate(50.0, 8.0, 100, 50.1, 8.0, 100)
    assert abs(north.horizontal_m - 11123) < 2
    assert north.elevation_deg == 0

    overhead = relate(50.0, 8.0, 100, 50.0, 8.0, 1100)
    assert overhead.slant_m == 1000 and overhead.elevation_deg == 90


def test_altitude_bevorzugt_geometrisch_und_vermerkt_die_quelle():
    assert altitude_msl(1000, 900, 47) == (953, "geometric")
    assert altitude_msl(None, 1000, 47) == (1000, "barometric")
    assert altitude_msl(None, 1000, 47, qnh_hpa=1023.25) == (1080, "barometric")
    assert altitude_msl(None, None, 47) is None


# --- Quellen ---------------------------------------------------------------

def test_dump1090_rechnet_um_und_erkennt_bodenziele():
    reports = parse_dump1090({"now": 1000.0, "aircraft": [
        {"hex": "3C6444", "flight": "DLH123 ", "lat": 50.1, "lon": 8.6, "alt_baro": 10000,
         "alt_geom": 10150, "gs": 300, "baro_rate": 1200, "seen_pos": 2.0},
        {"hex": "aaaaaa", "lat": 50.0, "lon": 8.0, "alt_baro": "ground", "seen_pos": 0.0},
        {"hex": "bbbbbb", "alt_baro": 3000},
    ]})
    assert [r.icao24 for r in reports] == ["3c6444", "aaaaaa"]   # ohne Position kein Eintrag
    first = reports[0]
    assert first.observed == datetime.fromtimestamp(998.0, UTC)  # now - seen_pos
    assert abs(first.baro_altitude_m - 3048) < 0.1               # Fuß -> Meter
    assert abs(first.ground_speed_ms - 154.33) < 0.01            # Knoten -> m/s
    assert abs(first.vertical_rate_ms - 6.096) < 0.001           # ft/min -> m/s
    assert reports[1].on_ground and reports[1].baro_altitude_m is None


def test_opensky_liefert_bereits_si():
    state = ["abc123", "DLH9  ", "DE", 1000, 1001, 8.6, 50.1, 9500.0, False,
             230.0, 270.0, 5.0, None, 9700.0, None, False, 0]
    (report,) = parse_states({"states": [state]})
    assert (report.baro_altitude_m, report.geom_altitude_m) == (9500.0, 9700.0)
    assert report.observed == datetime.fromtimestamp(1000, UTC) and report.source == "opensky"


# --- Collector-Regeln ------------------------------------------------------

def tracker():
    return Tracker(db.connect(":memory:"), CFG)


def stored(track):
    return track.conn.execute(
        "SELECT observed_utc, altitude_m, source FROM flight_positions ORDER BY id"
    ).fetchall()


def test_bodenziele_und_hoehenlose_meldungen_kommen_nicht_in_die_datenbank():
    track = tracker()
    track.ingest([at(0)._replace(on_ground=True), at(1)._replace(geom_altitude_m=None)])
    assert stored(track) == [] and track.dropped_ground == 1


def test_radius_begrenzt_die_aufzeichnung():
    track = tracker()
    track.ingest([at(0, lat=50.0, lon=8.0), at(1, lat=51.0, lon=8.0)])   # 111 km entfernt
    assert len(stored(track)) == 1


def test_ferne_flugzeuge_werden_ausgeduennt_nahe_nicht():
    track = tracker()
    nah = [at(s) for s in range(4)]
    fern = [at(s, lat=50.25, icao="abcdef") for s in range(12)]          # rund 28 km
    track.ingest(nah + fern)
    zeilen = stored(track)
    assert len(zeilen) == 4 + 3          # 1 Hz nah, alle 5 s fern (0, 5, 10)


def test_wiederholte_meldung_wird_nicht_doppelt_gespeichert():
    track = tracker()
    track.ingest([at(0), at(0), at(0)])
    assert len(stored(track)) == 1


def test_lange_luecke_beginnt_einen_neuen_flug():
    track = tracker()
    track.ingest([at(0), at(300), at(1200)])
    fluege = track.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    assert fluege == 2


def test_opensky_ergaenzt_nur_wenn_der_eigene_empfaenger_schweigt():
    track = tracker()
    track.ingest([at(0), at(10, source="opensky"), at(60, source="opensky")])
    quellen = [row["source"] for row in stored(track)]
    assert quellen == ["adsb", "opensky"]      # die Meldung nach 10 s wird verworfen


def test_neustart_setzt_den_laufenden_flug_fort():
    track = tracker()
    track.ingest([at(0)])
    fortsetzung = Tracker(track.conn, CFG)
    fortsetzung.ingest([at(5)])
    assert track.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1


# --- Datenbank -------------------------------------------------------------

def test_schema_weist_zeitstempel_ohne_zeitzone_ab():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO flights (icao24, first_seen_utc, last_seen_utc) VALUES ('a','b','c')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO flight_positions (flight_id, observed_utc, latitude, longitude,"
            " altitude_m, altitude_source, source) VALUES (1,'2026-09-08 12:00:00',1,1,1,"
            "'geometric','adsb')"
        )


def test_stammdaten_werden_aus_csv_gebaut_und_offline_gelesen(tmp_path):
    quelle = tmp_path / "aircraft.csv"
    quelle.write_text("icao24,registration,typecode\n3c6444,D-AIZZ,A320\nffffff,X,\n")
    ziel = tmp_path / "aircraft.sqlite"
    assert db.build_aircraft_db(quelle, ziel) == 1        # Zeile ohne Typ zählt nicht
    lookup = db.aircraft_types(ziel)
    assert lookup("3c6444") == "A320" and lookup("000000") is None
    assert db.aircraft_types(tmp_path / "fehlt.sqlite")("3c6444") is None


def test_laermklassen_decken_den_bestand_ab():
    """Die mitgelieferte Zuordnung muss den mitgelieferten Bestand auflösen.

    Der Test schlägt an, wenn eine neu erzeugte Tabelle Muster verliert – die
    stille Verschlechterung, die man einer CSV sonst nicht ansieht.
    """
    klassen = {}
    with open("assets/noise_classes.csv", encoding="utf-8") as handle:
        for row in csv.DictReader(li for li in handle if not li.startswith("#")):
            klassen[row["typecode"]] = row["noise_class"]

    erlaubt = {"heavyJet", "mediumJet", "regionalJet", "turboprop",
               "lightPiston", "helicopter", "unpowered", "notAircraft"}
    assert set(klassen.values()) <= erlaubt
    assert klassen["A320"] == "mediumJet" and klassen["CRJ9"] == "regionalJet"
    assert klassen["GLID"] == "unpowered" and klassen["ZZZZ"] == "notAircraft"

    gesamt = offen = 0
    with open("assets/aircraft_types.csv", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(li for li in handle if not li.startswith("#")):
            gesamt += 1
            offen += row["typecode"].strip().upper() not in klassen
    assert offen / gesamt < 0.005, f"{offen} von {gesamt} Einträgen ohne Lärmklasse"


def test_stammdaten_mit_herkunftskopf_werden_gelesen(tmp_path):
    """Der Bestand in assets/ trägt einen Kommentarblock vor der Kopfzeile."""
    quelle = tmp_path / "aircraft.csv"
    quelle.write_text(
        "# FlightWaves – Flugzeug-Stammdaten\n"
        "# Quelle: aircraft-database-complete-2025-08.csv\n"
        "icao24,typecode\n3c6444,A320\n"
    )
    ziel = tmp_path / "aircraft.sqlite"
    assert db.build_aircraft_db(quelle, ziel) == 1
    assert db.aircraft_types(ziel)("3c6444") == "A320"
