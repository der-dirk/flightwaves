"""Kommandozeile: python3 -m flightwaves <collect|check|aircraft-db|simulate>."""

import argparse
import logging

from . import collector, config, db, simulate

# Linienflug: ICAO-Schema, drei Buchstaben Airline-Kennung + Flugnummer (Spez. 15).
SCHEDULED = "callsign GLOB '[A-Z][A-Z][A-Z][0-9]*'"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="flightwaves", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="Konfigurationsdatei")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Flugdaten dauerhaft aufzeichnen")
    sub.add_parser("check", help="Abnahmekriterien der Stufe 1.1 prüfen")
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
    elif args.command == "simulate":
        simulate.run(cfg, args.duration, args.seed)
    else:
        count = db.build_aircraft_db(args.csv, cfg["database"]["aircraft_path"])
        print(f"{count} Muster nach {cfg['database']['aircraft_path']} geschrieben")
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
