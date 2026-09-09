"""Prüfungen der Regeln, die nicht offensichtlich sind: python3 -m pytest"""

import csv
import math
import random
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from flightwaves import db
from flightwaves.__main__ import temperature_for, weather_series
from flightwaves.collector import Tracker, store_weather
from flightwaves.geo import altitude_msl, relate
from flightwaves.noise import (
    Model,
    arrival_utc,
    combine_dba,
    noise_classes,
    path_temperature_c,
    speed_of_sound_ms,
    travel_time_s,
)
from flightwaves.simulate import (
    CEILING_M,
    FLOOR_M,
    ground_traffic,
    register_by_type,
    scenario,
    snapshot,
)
from flightwaves.sources import (
    Position,
    parse_dump1090,
    parse_open_meteo,
    parse_states,
)

SITE = {"latitude_deg": 50.0, "longitude_deg": 8.0, "elevation_m": 100.0,
        "geoid_undulation_m": 47.0}
CFG = {
    "site": SITE,
    "tracking": {"radius_m": 50000, "fine_sampling_radius_m": 20000,
                 "coarse_sampling_seconds": 5, "flight_gap_seconds": 600},
    "opensky": {"local_grace_seconds": 30},
    "weather": {"max_qnh_age_seconds": 10800},
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


# --- Simulierter Empfänger -------------------------------------------------

def test_simulator_liefert_genau_das_was_der_parser_erwartet():
    flights, ground = scenario(1, 600, 50000), ground_traffic(1)
    payload = snapshot(SITE, flights, ground, 300, 1_000_000.0, random.Random(1))
    reports = parse_dump1090(payload)

    assert len(reports) > 10                                   # Verkehr wie am Drehkreuz
    assert any(r.on_ground for r in reports)                   # Rollverkehr
    assert any(r.geom_altitude_m is None for r in reports)     # Transponder ohne alt_geom
    assert all(-90 <= r.latitude <= 90 for r in reports)
    assert len(payload["aircraft"]) > len(reports)             # Ziele ohne Position


def test_fluege_verschwinden_statt_auf_bodenhoehe_zu_kleben():
    # Ein Anflug landet, ein Abflug erreicht seine Reisehöhe – ohne das
    # Höhenfenster klebte ein Anflug minutenlang auf der Klemmhöhe.
    for flight in scenario(3, 1800, 50000):
        for moment in (flight.start_s + 1, flight.cpa_s, flight.end_s - 1):
            state = flight.state(moment)
            if state:
                assert FLOOR_M - 1 <= state[2] <= CEILING_M + 1


def test_simulierter_verkehr_laeuft_durch_bis_in_die_datenbank():
    track = tracker()
    flights, ground = scenario(2, 600, 50000), ground_traffic(2)
    jitter, now = random.Random(2), time.time()
    for step in range(20):
        payload = snapshot(SITE, flights, ground, 300 + step, now + step, jitter)
        track.ingest(parse_dump1090(payload))

    zeilen = stored(track)
    assert len(zeilen) > 50
    assert track.dropped_ground == 20 * len(ground)
    quellen = {row[0] for row in track.conn.execute(
        "SELECT DISTINCT altitude_source FROM flight_positions")}
    assert quellen == {"geometric", "barometric"}   # beide Höhenpfade kommen vor


def test_simulator_nimmt_echte_kennungen_aus_dem_bestand():
    """Erfundene Kennungen würden die Typauflösung im Testlauf überspringen."""
    register = register_by_type()
    assert {"A320", "B738", "C172", "EC35"} <= set(register)

    for flight in scenario(4, 600, 50000, register)[:20]:
        assert flight.icao24 in register[flight.aircraft_type]


# --- Lärmmodell ------------------------------------------------------------

def test_abnahmekriterium_der_stufe_1_2():
    """mediumJet, 300 m Schrägentfernung, Reiseschub, >= 20 Grad -> 82 dB(A).

    Der Referenzwert der Spezifikation, exakt und ohne Toleranz: er definiert
    den Bezugspunkt, den beide Projekte teilen müssen, damit die in Stufe 2.2
    gewonnenen Korrekturen übertragbar bleiben.
    """
    model = Model()
    assert model.level_dba("mediumJet", 300.0, 20.0, 0.0) == 82.0
    assert model.level_dba("mediumJet", 300.0, 90.0, 0.0) == 82.0
    assert len(model.config_hash()) == 12


def test_die_fuenf_terme_des_modells():
    model = Model()
    ohne_daempfung = dict(elevation_deg=90.0, vertical_rate_ms=0.0)

    # Verdopplung der Entfernung: -6 dB Ausbreitung, dazu die Absorption.
    nah = model.level_dba("mediumJet", 300.0, **ohne_daempfung)
    fern = model.level_dba("mediumJet", 600.0, **ohne_daempfung)
    verdopplung = 20 * math.log10(2)              # 6,0206 dB, nicht glatt 6
    assert abs((nah - fern) - (verdopplung + 1.5 * 0.3)) < 1e-9

    # Laterale Dämpfung: null ab 20 Grad, voll bei streifendem Einfall.
    assert model.lateral_attenuation_db(20.0) == 0.0
    assert model.lateral_attenuation_db(10.0) == 4.0
    assert model.lateral_attenuation_db(0.0) == 8.0
    assert model.lateral_attenuation_db(-5.0) == 8.0

    # Schubkorrektur an den Schwellen der Spezifikation.
    korrektur = model.thrust_correction_db
    assert [korrektur(v) for v in (6, 5, 3, 2, 0, -2, -5, -6, -9)] == [
        6.0, 6.0, 4.0, 4.0, 0.0, -2.0, -2.0, -3.0, -3.0
    ]


def test_stille_klassen_bekommen_keinen_ersatzpegel():
    """Ein Segelflugzeug auf 80 dB(A) fallen zu lassen, wäre schlimmer als nichts."""
    model = Model()
    assert model.level_dba("unpowered", 500.0, 45.0) is None
    assert model.level_dba("notAircraft", 500.0, 45.0) is None
    assert model.level_dba(None, 300.0, 90.0, 0.0) == 80.0        # unbekannt: Ersatzpegel
    assert model.category(None) is None


def test_pegel_werden_energetisch_summiert_nie_gemittelt():
    assert abs(combine_dba([60.0, 60.0]) - 63.0103) < 1e-4        # nicht 60, nicht 120
    assert abs(combine_dba([60.0, 40.0]) - 60.0432) < 1e-4        # das leise geht unter
    assert combine_dba([70.0]) == 70.0
    assert combine_dba([None, None]) is None


def test_ankunftszeit_liegt_um_d_durch_c_nach_der_abstrahlung():
    emittiert = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    assert abs(speed_of_sound_ms(15.0) - 340.39) < 0.01
    assert abs(travel_time_s(10000, 15.0) - 29.38) < 0.01          # rund 30 s bei 10 km

    angekommen = arrival_utc(emittiert, 10000, 15.0)
    # timedelta rechnet in Mikrosekunden – feiner geht die Zusicherung nicht,
    # und feiner braucht es auch niemand: die Laufzeit selbst ist auf Sekunden genau.
    assert abs((angekommen - emittiert).total_seconds() - travel_time_s(10000, 15.0)) < 1e-6

    # Kältere Luft trägt den Schall langsamer: in 10 km Höhe herrschen -40 Grad.
    assert travel_time_s(10000, -40.0) > travel_time_s(10000, 15.0) * 1.09


def test_kategorien_und_klassentabelle():
    model = Model()
    assert [model.category(x) for x in (34.9, 49.9, 61.9, 62.0)] == [
        "nicht hörbar", "leise", "mittel", "laut"
    ]
    klassen = noise_classes()
    assert klassen["A320"] == "mediumJet" and klassen["B77W"] == "heavyJet"
    assert klassen["GLID"] == "unpowered" and klassen["C172"] == "lightPiston"


# --- Wetter ----------------------------------------------------------------

OPEN_METEO = {
    "current": {
        "time": "2026-09-09T19:30",
        "temperature_2m": 17.4,
        "relative_humidity_2m": 71,
        "pressure_msl": 1019.3,
        "wind_speed_10m": 3.6,
        "wind_direction_10m": 245,
    },
    "hourly": {
        "time": ["2026-09-09T18:00", "2026-09-09T19:00", "2026-09-09T20:00"],
        "temperature_850hPa": [8.1, 7.9, 7.6],
        "wind_speed_850hPa": [11.2, 11.8, 12.0],
        "wind_direction_850hPa": [250, 252, 255],
        "geopotential_height_850hPa": [1490.0, 1488.0, 1486.0],
        "temperature_700hPa": [-2.4, -2.6, -2.9],
        "wind_speed_700hPa": [15.0, 15.4, 15.9],
        "wind_direction_700hPa": [262, 264, 266],
        "geopotential_height_700hPa": [3080.0, 3078.0, 3075.0],
        "temperature_500hPa": [-19.8, -20.1, -20.4],
        "wind_speed_500hPa": [24.1, 24.6, 25.0],
        "wind_direction_500hPa": [271, 272, 274],
        "geopotential_height_500hPa": [5620.0, 5617.0, 5613.0],
    },
}


def test_open_meteo_liefert_boden_und_die_drei_druckflaechen():
    jetzt = datetime(2026, 9, 9, 19, 40, tzinfo=UTC)
    zeilen = parse_open_meteo(OPEN_METEO, jetzt)

    boden = zeilen[0]
    assert boden.level_m == 0.0
    assert (boden.temperature_c, boden.pressure_msl_hpa) == (17.4, 1019.3)
    assert (boden.wind_speed_ms, boden.wind_direction_deg) == (3.6, 245)
    assert boden.observed.tzinfo is UTC          # Open-Meteo nennt keine Zone

    # Die Druckflächen kommen stündlich; genommen wird die nächstliegende
    # Stunde, nicht die zuletzt vergangene.
    assert [z.level_m for z in zeilen[1:]] == [1486.0, 3075.0, 5613.0]      # 20 Uhr
    frueher = parse_open_meteo(OPEN_METEO, datetime(2026, 9, 9, 19, 20, tzinfo=UTC))
    assert [z.level_m for z in frueher[1:]] == [1488.0, 3078.0, 5617.0]     # 19 Uhr
    assert [z.temperature_c for z in frueher[1:]] == [7.9, -2.6, -20.1]
    # Feuchte und Luftdruck gibt es nur am Boden.
    assert all(z.humidity_pct is None and z.pressure_msl_hpa is None for z in zeilen[1:])


def test_druckflaeche_ohne_hoehe_wird_uebersprungen():
    """Ohne geopotentielle Höhe hat die Druckfläche keinen Ort."""
    payload = {"current": {}, "hourly": dict(OPEN_METEO["hourly"])}
    payload["hourly"]["geopotential_height_700hPa"] = [None, None, None]
    hoehen = [z.level_m for z in parse_open_meteo(payload, datetime(2026, 9, 9, 19, 0, tzinfo=UTC))]
    assert hoehen == [1488.0, 5617.0]


def test_derselbe_abruf_zweimal_erzeugt_keine_zweite_zeile():
    conn = db.connect(":memory:")
    zeilen = parse_open_meteo(OPEN_METEO, datetime(2026, 9, 9, 19, 40, tzinfo=UTC))
    assert store_weather(conn, zeilen) == 4
    store_weather(conn, zeilen)
    assert conn.execute("SELECT COUNT(*) FROM weather").fetchone()[0] == 4


def test_wegtemperatur_mittelt_boden_und_naechste_druckflaeche():
    flaechen = [(1488.0, 7.9), (3078.0, -2.6), (5617.0, -20.1)]
    assert path_temperature_c(17.4, flaechen, 1400) == (17.4 + 7.9) / 2
    assert path_temperature_c(17.4, flaechen, 5000) == (17.4 - 20.1) / 2
    assert path_temperature_c(17.4, [], 5000) == 17.4          # ohne Höhenwerte
    assert path_temperature_c(None, flaechen, 3000) == -2.6


def test_qnh_korrigiert_die_barometrische_hoehe_nur_solange_er_frisch_ist():
    track = tracker()
    frisch = datetime.now(UTC).isoformat()
    track.conn.execute(
        "INSERT INTO weather (observed_utc, level_m, pressure_msl_hpa, source)"
        " VALUES (?, 0, 1023.25, 'test')", (frisch,)
    )
    track.refresh_qnh()
    assert track.qnh_hpa == 1023.25

    # Nur barometrisch gemeldet: 10 hPa über Normal sind rund 80 m mehr.
    track.ingest([at(0)._replace(geom_altitude_m=None, baro_altitude_m=1000.0)])
    (zeile,) = stored(track)
    assert abs(zeile["altitude_m"] - 1080.0) < 1e-6

    track.conn.execute("UPDATE weather SET observed_utc = ?",
                       ((datetime.now(UTC) - timedelta(days=2)).isoformat(),))
    track.refresh_qnh()
    assert track.qnh_hpa is None      # veraltet ist schlechter als keiner


def test_bodenwerte_und_druckflaechen_finden_zusammen():
    """Sie tragen verschiedene Zeitstempel und dürfen trotzdem nicht getrennt bleiben.

    Open-Meteo liefert Bodenwerte zum Abrufzeitpunkt, Druckflächen stündlich.
    Nach Zeitstempel gruppiert käme die Wegtemperatur nie zustande – sie ist
    gerade das Mittel aus beiden.
    """
    conn = db.connect(":memory:")
    zeilen = parse_open_meteo(OPEN_METEO, datetime(2026, 9, 9, 19, 40, tzinfo=UTC))
    store_weather(conn, zeilen)
    boden, flaechen = weather_series(conn)
    assert len(boden) == 1 and len(flaechen) == 1

    moment = datetime(2026, 9, 9, 19, 35, tzinfo=UTC)
    # In 1,5 km Höhe: Mittel aus 17,4 °C am Boden und 7,6 °C auf 850 hPa.
    assert temperature_for(boden, flaechen, moment, 1500) == (17.4 + 7.6) / 2
    # In Reiseflughöhe zählt die 500-hPa-Fläche.
    assert temperature_for(boden, flaechen, moment, 5600) == (17.4 - 20.4) / 2
    # Ohne alles der sichtbare Platzhalter.
    assert temperature_for([], [], moment, 5600) == 15.0
