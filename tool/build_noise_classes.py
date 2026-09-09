"""Erzeugt assets/noise_classes.csv: ICAO-Typenkürzel -> Lärmklasse.

Aufruf:
    python3 tool/build_noise_classes.py <doc8643.csv> [ausgabe.csv]

Die Quelldatei ist eine Doc-8643-Ableitung mit den Spalten type_designator,
description, engine_type, engine_count und wtc, zum Beispiel
https://github.com/ColtJD45/icao-aircraft-designator-list (icao_aircraft_data.csv).
Sie wird nicht mitgeliefert, weil sie fremdes Material ist; die daraus
abgeleitete Zuordnung schon.

Drei Ebenen, in dieser Reihenfolge:

1. KURIERT  – die 168 Verkehrsflugzeuge aus dem Schwesterprojekt. Sie gewinnen,
   weil Doc 8643 die Gewichtsklasse nicht kennt: Ein CRJ ist dort dieselbe
   Kategorie M wie ein A320, obwohl er rund 5 dB leiser ist.
2. SAMMELKÜRZEL – Kürzel, die kein Flugzeugmuster benennen (GLID, BALL, ZZZZ)
   oder in Doc 8643 fehlen. Kurz gehalten und einzeln belegt.
3. DOC 8643 – alles Übrige mechanisch aus Antriebsart und Wirbelschleppen-
   kategorie.

Geprüft: Auf den 168 kuratierten Mustern stimmt Ebene 3 in 131 Fällen mit der
Handarbeit überein. Die 31 Abweichungen sind systematisch (Regionaljets und die
B757, die Doc 8643 als M führt) – genau deshalb steht Ebene 1 obenauf.
"""

import csv
import sys

# --- Ebene 1: kuratiert, aus flightwaves-app-flutter ---------------------
CURATED = {
    "heavyJet": """A124 A306 A310 A332 A333 A337 A338 A339 A342 A343 A345 A346
        A359 A35K A388 B742 B743 B744 B748 B74F B752 B753 B762 B763 B764 B772
        B773 B778 B779 B77L B77W B788 B789 B78X IL96 MD11""",
    "mediumJet": """A19N A20N A21N A318 A319 A320 A321 B37M B38M B39M B3XM B733
        B734 B735 B736 B737 B738 B739 BCS1 BCS3 E190 E195 E290 E295 F100 F70
        MD82 MD83 MD87 MD88""",
    "regionalJet": """C25A C25B C25C C525 C56X C68A C750 CL35 CL60 CRJ1 CRJ2
        CRJ7 CRJ9 CRJX E135 E145 E170 E175 E50P E55P E75L E75S F2TH FA7X FA8X
        GL5T GLF4 GLF5 GLF6 H25B LJ45 LJ60 PRM1 RJ1H RJ85""",
    "turboprop": """A400 AT43 AT45 AT46 AT72 AT75 AT76 B350 BE20 C130 C208 D228
        D328 DH8A DH8B DH8C DH8D F50 JS32 JS41 PC12 SB20 SF34 TBM9""",
    "lightPiston": """AC11 BE33 BE36 BE58 C152 C172 C182 C206 C210 DA40 DA42
        DA62 P28A P28R PA28 PA34 PA46 RV7 SR20 SR22 ULAC""",
    "helicopter": """A109 A139 AS50 AS55 AS65 B06 B407 B429 EC20 EC30 EC35 EC45
        EC55 EC75 H135 H145 H60 R22 R44 R66 S76 S92""",
}

# --- Ebene 2: Sammelkürzel und Lücken in Doc 8643 ------------------------
OVERRIDES = {
    # Kein Triebwerk oder so klein, dass am Boden nichts ankommt. Ohne diese
    # Zeile landen 33.000 Registereinträge auf dem Referenzpegel eines
    # Verkehrsflugzeugs und erzeugen laute Prognosen für stille Luftfahrzeuge.
    "unpowered": """GLID BALL PARA DRON UAV DISC VENT DUOD NIMB JANU ARCU G102
        G103 G104 LS4 LS8""",
    # Keine Luftfahrzeuge: Bodenfahrzeuge, Türme, Undefiniertes. Bekommen nie
    # einen Pegel und gehören aus jeder Auswertung heraus.
    "notAircraft": "ZZZZ UNDEFINED SERV GRND GND TWR SHIP TEST 0000",
    # In Doc 8643 nicht enthalten, aber im Bestand vorhanden.
    "helicopter": "UHEL KA32 M171 B47D",
    "turboprop": "KODI",
    # Sammelkürzel für Tragschrauber; die Begründung steht in derive().
    "lightPiston": "GYRO",
    "regionalJet": "G450 G550 G650 GL6T CL61 CL64 CL65 F2EX F2LX F5EX F9EX F9LX",
    # Kampfflugzeuge sind lauter als jeder Verkehrsjet; eine eigene Klasse
    # lohnt bei gut hundert Registereinträgen nicht. mediumJet unterschätzt
    # sie, liegt aber weit näher als der Kolbenflugzeug-Pegel.
    "mediumJet": "F18",
}


def _spread(groups):
    return {code: cls for cls, codes in groups.items() for code in codes.split()}


def derive(row):
    """Lärmklasse aus den Doc-8643-Merkmalen einer Zeile."""
    desc = row["description"].strip()
    engine = row["engine_type"].strip()
    wtc = row["wtc"].strip().upper()

    if desc in ("Helicopter", "Tiltrotor"):
        return "helicopter"
    if desc == "Gyrocopter":
        # Freilaufender Rotor und kleiner Kolbenmotor – akustisch ein
        # Leichtflugzeug, nicht der Blattschlag eines Hubschraubers.
        return "lightPiston"
    if engine in ("None", "Electric"):
        return "unpowered"
    if engine == "Jet":
        return {"J": "heavyJet", "H": "heavyJet", "M": "mediumJet"}.get(wtc, "regionalJet")
    if engine.startswith("Turbo"):
        return "turboprop"
    if engine == "Piston":
        return "lightPiston"
    return None


def build(source_path):
    classes = _spread(CURATED)
    for code, cls in _spread(OVERRIDES).items():
        classes.setdefault(code, cls)
    curated_and_overrides = set(classes)

    with open(source_path, newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            code = (row["type_designator"] or "").strip().upper()
            if not code or code in curated_and_overrides:
                continue
            cls = derive(row)
            if cls:
                classes.setdefault(code, cls)
    return classes


def main(argv):
    if not argv:
        raise SystemExit(__doc__.strip().splitlines()[2].strip())
    source = argv[0]
    out = argv[1] if len(argv) > 1 else "assets/noise_classes.csv"
    classes = build(source)

    with open(out, "w", newline="", encoding="utf-8") as handle:
        handle.write(
            "# FlightWaves – Lärmklassen (ICAO-Typenkürzel -> Klasse)\n"
            "#\n"
            "# Erzeugt mit tool/build_noise_classes.py.\n"
            f"# Ebene 1 kuratiert: {len(_spread(CURATED))} Verkehrsflugzeuge "
            "aus flightwaves-app-flutter\n"
            f"# Ebene 2 Sammelkürzel: {len(_spread(OVERRIDES))}\n"
            f"# Ebene 3 Doc 8643 ({source}): der Rest\n"
            f"# Einträge: {len(classes)}\n"
        )
        writer = csv.writer(handle)
        writer.writerow(["typecode", "noise_class"])
        writer.writerows(sorted(classes.items()))
    print(f"{len(classes)} Kürzel nach {out} geschrieben")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
