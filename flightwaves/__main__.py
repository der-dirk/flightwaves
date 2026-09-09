"""Kommandozeile: python3 -m flightwaves <collect|weather|check|predict|...>."""

import argparse
import bisect
import logging
from datetime import datetime

from . import collector, config, db, noise, simulate, web
from .geo import relate

# Linienflug: ICAO-Schema, drei Buchstaben Airline-Kennung + Flugnummer (Spez. 15).
SCHEDULED = "callsign GLOB '[A-Z][A-Z][A-Z][0-9]*'"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="flightwaves", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="Konfigurationsdatei")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Flugdaten dauerhaft aufzeichnen")
    wetter = sub.add_parser("weather", help="Wetterdaten dauerhaft aufzeichnen")
    wetter.add_argument("--once", action="store_true", help="einmal abrufen und beenden")
    sub.add_parser("web", help="Weboberfläche im lokalen Netz servieren")
    sub.add_parser("check", help="Abnahmekriterien der Stufe 1.1 prüfen")
    vorhersage = sub.add_parser("predict", help="LAmax je Flug aus der Prognosetabelle")
    vorhersage.add_argument("--recompute", action="store_true",
                            help="Tabelle vollständig neu berechnen")
    build = sub.add_parser("aircraft-db", help="Stammdatenbank aus einer CSV bauen")
    build.add_argument("csv", help="CSV mit den Spalten icao24 und typecode")
    sim = sub.add_parser("simulate", help="Empfänger simulieren, ohne Hardware")
    sim.add_argument("--duration", type=float, default=3600, help="Spieldauer in Sekunden")
    sim.add_argument("--seed", type=int, default=1, help="gleicher Wert = gleicher Verkehr")

    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    logging.basicConfig(
        level=cfg["logging"]["level"],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "collect":
        collector.run(cfg)
    elif args.command == "web":
        web.run(cfg)
    elif args.command == "weather":
        collector.run_weather(cfg, once=args.once)
    elif args.command == "check":
        return check(cfg)
    elif args.command == "predict":
        return predict(cfg, args.recompute)
    elif args.command == "simulate":
        simulate.run(cfg, args.duration, args.seed)
    else:
        count = db.build_aircraft_db(args.csv, cfg["database"]["aircraft_path"])
        print(f"{count} Muster nach {cfg['database']['aircraft_path']} geschrieben")
    return 0


def weather_series(conn):
    """Wetterwerte als zwei Zeitreihen: Boden und Druckflächen.

    Getrennt, weil sie verschiedene Zeitstempel tragen: Open-Meteo liefert
    Bodenwerte zum Abrufzeitpunkt und Druckflächen stündlich. Nach
    Zeitstempel gruppiert kämen beide nie zusammen – und die Wegtemperatur
    ist gerade das Mittel aus ihnen (Spez. 6).
    """
    boden, flaechen = [], {}
    for row in conn.execute(
        "SELECT observed_utc, level_m, temperature_c FROM weather ORDER BY observed_utc"
    ):
        moment = datetime.fromisoformat(row["observed_utc"])
        if row["level_m"] == 0:
            boden.append((moment, row["temperature_c"]))
        else:
            flaechen.setdefault(moment, []).append((row["level_m"], row["temperature_c"]))
    return boden, sorted(flaechen.items())


def nearest(entries, moment):
    """Der zeitlich nächste Eintrag, oder None."""
    if not entries:
        return None
    index = bisect.bisect_left(entries, moment, key=lambda eintrag: eintrag[0])
    nachbarn = [entries[i] for i in (index - 1, index) if 0 <= i < len(entries)]
    return min(nachbarn, key=lambda eintrag: abs((eintrag[0] - moment).total_seconds()))[1]


def temperature_for(boden, flaechen, moment, altitude_m):
    """Wegtemperatur aus den zeitlich nächsten Werten.

    Ohne jede Wetterdaten bleibt der Platzhalter – sichtbar, statt als
    stiller Vorgabewert (Spez. 6).
    """
    bodenwert = nearest(boden, moment)
    hoehenwerte = nearest(flaechen, moment) or []
    if bodenwert is None and not hoehenwerte:
        return noise.PLACEHOLDER_TEMPERATURE_C
    return noise.path_temperature_c(bodenwert, hoehenwerte, altitude_m)


def recompute(conn, cfg, model):
    """Die Prognosetabelle aus den Positionen neu aufbauen (Spez. 6).

    Sie ist abgeleitet und hält nur die aktuelle Modellversion; was
    eingefroren bleiben muss, steht ab Stufe 2.2 in `comparisons`.
    """
    classes, site = noise.noise_classes(), cfg["site"]
    boden, flaechen = weather_series(conn)
    conn.execute("DELETE FROM noise_predictions")

    stapel, gesamt = [], 0
    for row in conn.execute(
        "SELECT p.flight_id, p.observed_utc, p.latitude, p.longitude, p.altitude_m,"
        " p.vertical_rate_ms, f.aircraft_type FROM flight_positions p"
        " JOIN flights f ON f.id = p.flight_id"
    ):
        lage = relate(
            site["latitude_deg"], site["longitude_deg"], site["elevation_m"],
            row["latitude"], row["longitude"], row["altitude_m"],
        )
        abgestrahlt = datetime.fromisoformat(row["observed_utc"])
        vorhersage = noise.predict(
            model,
            classes.get((row["aircraft_type"] or "").upper(), "unknown"),
            lage.slant_m,
            lage.elevation_deg,
            row["vertical_rate_ms"] or 0.0,
            abgestrahlt,
            temperature_for(boden, flaechen, abgestrahlt, row["altitude_m"]),
        )
        if vorhersage is None:
            continue        # stille Klasse: keine Prognose, kein Ersatzwert
        stapel.append((
            row["flight_id"], row["observed_utc"], vorhersage.arrival_utc.isoformat(),
            lage.slant_m, lage.elevation_deg, vorhersage.level_dba, vorhersage.category,
            noise.MODEL_VERSION, model.config_hash(),
        ))
        # Stapelweise schreiben: eine Jahresdatenbank passt nicht in den
        # Arbeitsspeicher eines Pi.
        if len(stapel) >= 10000:
            gesamt += _flush(conn, stapel)
            stapel = []
    gesamt += _flush(conn, stapel)
    return gesamt


def _flush(conn, stapel):
    if not stapel:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO noise_predictions (flight_id, emitted_utc, arrival_utc,"
        " slant_distance_m, elevation_deg, level_dba, category, model_version, config_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        stapel,
    )
    return len(stapel)


def predict(cfg, force=False):
    """LAmax je Flug aus der Prognosetabelle (Spez. 6).

    Der Collector schreibt die Werte laufend mit. Trägt die Tabelle eine
    fremde Modellversion, wird sie neu berechnet – sie ist abgeleitet und
    hält nur die aktuelle.
    """
    conn = db.connect(cfg["database"]["path"])
    model = noise.Model()

    fremd = conn.execute(
        "SELECT COUNT(*) FROM noise_predictions WHERE model_version != ? OR config_hash != ?",
        (noise.MODEL_VERSION, model.config_hash()),
    ).fetchone()[0]
    if fremd or force:
        if fremd:
            print(f"{fremd} Werte einer anderen Modellversion – Tabelle wird neu berechnet.")
        print(f"{recompute(conn, cfg, model)} Prognosewerte berechnet.\n")

    lauteste = conn.execute(
        "SELECT * FROM (SELECT f.callsign, f.aircraft_type, p.level_dba, p.category,"
        " p.slant_distance_m, p.arrival_utc, ROW_NUMBER() OVER"
        " (PARTITION BY p.flight_id ORDER BY p.level_dba DESC) AS rang"
        " FROM noise_predictions p JOIN flights f ON f.id = p.flight_id) WHERE rang = 1"
        " ORDER BY level_dba DESC"
    ).fetchall()
    if not lauteste:
        print("Keine Prognosewerte. Läuft der Collector?")
        return 1

    boden, flaechen = weather_series(conn)
    classes = noise.noise_classes()
    print(f"Modell {noise.MODEL_VERSION}, Konfiguration {model.config_hash()}")
    print(
        f"Wetter: {len(boden)} Bodenwerte, {len(flaechen)} Höhenprofile"
        if boden or flaechen
        else f"Keine Wetterdaten – Laufzeit mit {noise.PLACEHOLDER_TEMPERATURE_C:.0f} °C gerechnet"
    )
    print(f"{len(lauteste)} Flüge, lauteste zuerst\n")
    print("  Rufzeichen  Muster Klasse       LAmax  Kategorie     Abstand  Ankunft")
    for row in lauteste[:15]:
        klasse = classes.get((row["aircraft_type"] or "").upper(), "unknown")
        print(f"  {str(row['callsign']):<11} {str(row['aircraft_type']):<6} "
              f"{klasse:<12} {row['level_dba']:4.0f}   {row['category']:<12} "
              f"{row['slant_distance_m']/1000:5.1f} km  {row['arrival_utc'][11:19]}")

    verteilung = {}
    for row in lauteste:
        verteilung[row["category"]] = verteilung.get(row["category"], 0) + 1
    print("\n  " + "   ".join(f"{name}: {zahl}" for name, zahl in verteilung.items()))
    return 0


def check(cfg):
    """Die Abnahmekriterien der Stufe 1.1 gegen die Datenbank prüfen (Spez. 15)."""
    conn = db.connect(cfg["database"]["path"])

    def one(sql):
        return conn.execute(sql).fetchone()[0]

    positions = one("SELECT COUNT(*) FROM flight_positions")
    if not positions:
        print("Keine Positionen aufgezeichnet.")
        return 1

    first, last = conn.execute(
        "SELECT MIN(observed_utc), MAX(observed_utc) FROM flight_positions"
    ).fetchone()
    hours_covered = one("SELECT COUNT(DISTINCT substr(observed_utc, 1, 13)) FROM flight_positions")
    scheduled = one(f"SELECT COUNT(*) FROM flights WHERE {SCHEDULED}")
    typed = one(f"SELECT COUNT(*) FROM flights WHERE {SCHEDULED} AND aircraft_type IS NOT NULL")
    coverage = 100 * typed / scheduled if scheduled else 0.0
    bad_time = one("SELECT COUNT(*) FROM flight_positions WHERE observed_utc NOT LIKE '%+00:00'")
    bad_altitude = one(
        "SELECT COUNT(*) FROM flight_positions WHERE altitude_source NOT IN"
        " ('geometric', 'barometric')"
    )

    print(f"Zeitraum       {first} .. {last}")
    print(f"Positionen     {positions} in {hours_covered} h")
    print(f"Flüge          {one('SELECT COUNT(*) FROM flights')} ({scheduled} Linienflüge)")
    print()

    results = [
        (f"Typabdeckung Linienflüge {coverage:.1f} % (>= 85 %)", coverage >= 85 and scheduled > 0),
        (f"Zeitstempel ohne UTC-Kennung: {bad_time}", bad_time == 0),
        (f"Höhen ohne vermerkte Quelle: {bad_altitude}", bad_altitude == 0),
        (f"Abdeckung {hours_covered} h (Dauerbetrieb >= 72 h)", hours_covered >= 72),
    ]
    for label, passed in results:
        print(f"  [{'ok' if passed else '  '}] {label}")

    # Bodenziele werden beim Import verworfen und können hier nicht gezählt
    # werden – das Protokoll des Collectors weist sie aus.
    results += check_stage_1_2(conn)
    return 0 if all(passed for _, passed in results) else 1


def check_stage_1_2(conn):
    """Die aus der Datenbank prüfbaren Kriterien der Stufe 1.2 (Spez. 15).

    Der Referenzwert und die Kategorieschwellen stehen als Test in
    test_flightwaves.py; hier zählt, was die gespeicherten Werte tragen.
    """
    model = noise.Model()
    vorhersagen = conn.execute("SELECT COUNT(*) FROM noise_predictions").fetchone()[0]
    if not vorhersagen:
        print("\n  [  ] Stufe 1.2: keine Prognosewerte in der Datenbank")
        return [("Prognosewerte vorhanden", False)]

    fremd = conn.execute(
        "SELECT COUNT(*) FROM noise_predictions WHERE model_version != ? OR config_hash != ?",
        (noise.MODEL_VERSION, model.config_hash()),
    ).fetchone()[0]

    # Ankunft minus Abstrahlung muss der Laufzeit entsprechen. Die
    # Schallgeschwindigkeit hängt von der Temperatur ab; zwischen -40 und
    # +40 Grad liegt sie zwischen 307 und 356 m/s.
    unplausibel = conn.execute(
        "SELECT COUNT(*) FROM noise_predictions WHERE"
        " (julianday(arrival_utc) - julianday(emitted_utc)) * 86400.0"
        " NOT BETWEEN slant_distance_m / 356.0 AND slant_distance_m / 307.0"
    ).fetchone()[0]

    referenz = model.level_dba("mediumJet", 300.0, 20.0, 0.0)
    ergebnisse = [
        (f"Referenzwert mediumJet/300 m/20 Grad: {referenz} dB(A)", referenz == 82.0),
        (f"Prognosewerte fremder Modellversion: {fremd}", fremd == 0),
        (f"Laufzeiten außerhalb d/c: {unplausibel} von {vorhersagen}", unplausibel == 0),
    ]
    print("\n  Stufe 1.2")
    for label, passed in ergebnisse:
        print(f"  [{'ok' if passed else '  '}] {label}")
    return ergebnisse


if __name__ == "__main__":
    raise SystemExit(main())
