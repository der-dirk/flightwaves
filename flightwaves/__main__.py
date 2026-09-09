"""Kommandozeile: python3 -m flightwaves <collect|check|aircraft-db|simulate>."""

import argparse
import logging
from datetime import datetime

from . import collector, config, db, noise, simulate
from .geo import relate

# Linienflug: ICAO-Schema, drei Buchstaben Airline-Kennung + Flugnummer (Spez. 15).
SCHEDULED = "callsign GLOB '[A-Z][A-Z][A-Z][0-9]*'"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="flightwaves", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="Konfigurationsdatei")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Flugdaten dauerhaft aufzeichnen")
    sub.add_parser("check", help="Abnahmekriterien der Stufe 1.1 prüfen")
    sub.add_parser("predict", help="LAmax je Flug aus den Positionen rechnen")
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
    elif args.command == "check":
        return check(cfg)
    elif args.command == "predict":
        return predict(cfg)
    elif args.command == "simulate":
        simulate.run(cfg, args.duration, args.seed)
    else:
        count = db.build_aircraft_db(args.csv, cfg["database"]["aircraft_path"])
        print(f"{count} Muster nach {cfg['database']['aircraft_path']} geschrieben")
    return 0


def predict(cfg):
    """LAmax je Flug aus den gespeicherten Positionen (Spez. 6).

    Prognosen sind aus den Positionen jederzeit neu ableitbar; deshalb wird
    hier gerechnet und nicht nachgeschlagen. Der Dezibelwert steht bis zur
    Kalibrierung nach Stufe 2.2 ohne Nachkommastelle und als Schätzung.
    """
    conn = db.connect(cfg["database"]["path"])
    model, classes, site = noise.Model(), noise.noise_classes(), cfg["site"]

    lauteste = {}
    for row in conn.execute(
        "SELECT f.id, f.callsign, f.aircraft_type, p.observed_utc, p.latitude,"
        " p.longitude, p.altitude_m, p.vertical_rate_ms"
        " FROM flights f JOIN flight_positions p ON p.flight_id = f.id"
    ):
        lage = relate(
            site["latitude_deg"], site["longitude_deg"], site["elevation_m"],
            row["latitude"], row["longitude"], row["altitude_m"],
        )
        klasse = classes.get((row["aircraft_type"] or "").upper(), "unknown")
        pegel = model.level_dba(
            klasse, lage.slant_m, lage.elevation_deg, row["vertical_rate_ms"] or 0.0
        )
        if pegel is None:
            continue        # unpowered, notAircraft: keine Prognose
        bisher = lauteste.get(row["id"])
        if bisher is None or pegel > bisher[0]:
            lauteste[row["id"]] = (
                pegel, klasse, row["callsign"], row["aircraft_type"], lage.slant_m,
                noise.arrival_utc(datetime.fromisoformat(row["observed_utc"]), lage.slant_m),
            )

    if not lauteste:
        print("Keine Positionen mit Prognose.")
        return 1

    print(f"Modell {noise.MODEL_VERSION}, Konfiguration {model.config_hash()}")
    print(f"{len(lauteste)} Flüge, lauteste zuerst\n")
    print("  Rufzeichen  Muster Klasse       LAmax  Kategorie     Abstand  Ankunft")
    for pegel, klasse, rufzeichen, muster, abstand, ankunft in sorted(
        lauteste.values(), reverse=True
    )[:15]:
        print(f"  {str(rufzeichen):<11} {str(muster):<6} {klasse:<12} "
              f"{pegel:4.0f}   {model.category(pegel):<12} "
              f"{abstand/1000:5.1f} km  {ankunft:%H:%M:%S}")

    verteilung = {}
    for eintrag in lauteste.values():
        name = model.category(eintrag[0])
        verteilung[name] = verteilung.get(name, 0) + 1
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
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
