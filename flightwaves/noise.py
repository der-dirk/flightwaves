"""Lärmmodell (Spez. 6).

Reines Python ohne Zugriff auf Hardware, Netz oder Datenbank – damit ohne
Gerät testbar und in beiden Projekten gleich. Die Parameter behalten
dieselbe Bedeutung und denselben Bezugspunkt (300 m) wie im Schwesterprojekt,
damit die aus Stufe 2.2 gewonnenen Korrekturen dorthin zurückfließen können.
"""

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

MODEL_VERSION = "1.0"
NOISE_CLASSES_CSV = Path(__file__).resolve().parent.parent / "assets" / "noise_classes.csv"

DEFAULT_TEMPERATURE_C = 15.0
"""Standardatmosphäre. Der Wetter-Collector der Stufe 1.2 liefert den echten Wert."""

# Referenzpegel je Lärmklasse in 300 m Schrägentfernung bei Reiseschub.
# unpowered und notAircraft stehen bewusst nicht darin: ohne diese Lücke
# fielen Segelflugzeuge und Bodenfahrzeuge auf 80 dB(A) zurück, den
# Referenzpegel eines Verkehrsflugzeugs.
REFERENCE_LEVELS_DBA = {
    "heavyJet": 88.0,
    "mediumJet": 82.0,
    "regionalJet": 77.0,
    "turboprop": 75.0,
    "lightPiston": 68.0,
    "helicopter": 80.0,
    "unknown": 80.0,
}


@dataclass(frozen=True)
class Model:
    """Alle Stellschrauben an einem Ort, nicht über den Code verteilt (Spez. 6).

    Jeder Prognosewert trägt Modellversion und Konfigurations-Hash, damit
    später nachvollziehbar bleibt, gegen welches Modell eine Messung
    verglichen wurde.
    """

    reference_levels_dba: dict = field(default_factory=lambda: dict(REFERENCE_LEVELS_DBA))
    reference_distance_m: float = 300.0
    air_absorption_db_per_km: float = 1.5
    lateral_max_db: float = 8.0
    lateral_elevation_deg: float = 20.0
    # Schubkorrektur aus der Steigrate: (Schwelle m/s, Zuschlag dB).
    # Die beiden Steigstufen gelten ab der Schwelle, die Sinkstufen darüber.
    thrust_steps: tuple = ((5.0, 6.0), (2.0, 4.0), (-2.0, 0.0), (-6.0, -2.0))
    thrust_floor_db: float = -3.0
    # Kategorieschwellen wie im Schwesterprojekt.
    category_steps: tuple = ((35.0, "nicht hörbar"), (50.0, "leise"), (62.0, "mittel"))
    category_above: str = "laut"

    def config_hash(self):
        """Kurzer Hash über alle Parameter; steht an jedem Prognosewert."""
        canonical = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def level_dba(self, noise_class, slant_distance_m, elevation_deg, vertical_rate_ms=0.0):
        """Momentanpegel am Standort in dB(A).

        Gibt None zurück, wenn die Klasse keinen Referenzpegel hat – für ein
        Segelflugzeug oder ein Bodenfahrzeug gibt es keine Prognose, keinen
        Ersatzwert.
        """
        reference = self.reference_levels_dba.get(noise_class or "unknown")
        if reference is None:
            return None

        distance_m = max(slant_distance_m, 1.0)     # kein Logarithmus von null
        spreading = 20 * math.log10(distance_m / self.reference_distance_m)
        excess_m = max(0.0, distance_m - self.reference_distance_m)
        absorption = self.air_absorption_db_per_km * excess_m / 1000

        return (
            reference
            - spreading
            - absorption
            - self.lateral_attenuation_db(elevation_deg)
            + self.thrust_correction_db(vertical_rate_ms)
        )

    def lateral_attenuation_db(self, elevation_deg):
        """Bodeneffekt und Bebauung bei streifendem Schalleinfall."""
        share = (self.lateral_elevation_deg - elevation_deg) / self.lateral_elevation_deg
        return self.lateral_max_db * min(1.0, max(0.0, share))

    def thrust_correction_db(self, vertical_rate_ms):
        """Schubsetzung, aus der Steigrate geschätzt."""
        (fast, fast_db), (climb, climb_db), (level, level_db), (sink, sink_db) = self.thrust_steps
        if vertical_rate_ms >= fast:
            return fast_db
        if vertical_rate_ms >= climb:
            return climb_db
        if vertical_rate_ms > level:
            return level_db
        if vertical_rate_ms > sink:
            return sink_db
        return self.thrust_floor_db

    def category(self, level_dba):
        """Grobe Einordnung neben dem Dezibelwert."""
        if level_dba is None:
            return None
        for threshold, name in self.category_steps:
            if level_dba < threshold:
                return name
        return self.category_above


def combine_dba(levels):
    """Energetische Summe mehrerer gleichzeitig hörbarer Flugzeuge.

    Zwei gleich laute Flugzeuge ergeben 3 dB mehr, nicht das Doppelte.
    Arithmetische Mittelung von Pegeln ist an keiner Stelle zulässig (Spez. 6).
    """
    energy = sum(10 ** (level / 10) for level in levels if level is not None)
    return 10 * math.log10(energy) if energy else None


def speed_of_sound_ms(temperature_c=DEFAULT_TEMPERATURE_C):
    """Schallgeschwindigkeit in Luft."""
    return 331.3 + 0.606 * temperature_c


def travel_time_s(slant_distance_m, temperature_c=DEFAULT_TEMPERATURE_C):
    """Laufzeit vom Flugzeug zum Standort."""
    return slant_distance_m / speed_of_sound_ms(temperature_c)


def arrival_utc(emitted_utc, slant_distance_m, temperature_c=DEFAULT_TEMPERATURE_C):
    """Ankunftszeit am Standort zu einer Abstrahlzeit.

    Die Vorwärtsrichtung braucht keinen Löser (Spez. 6). Bei 10 km sind das
    rund 30 Sekunden, in denen das Flugzeug mehrere Kilometer weiterfliegt –
    ohne diese Trennung wäre jede Zuordnung zwischen Flug und Geräusch
    systematisch falsch.
    """
    return emitted_utc + timedelta(seconds=travel_time_s(slant_distance_m, temperature_c))


def noise_classes(path=NOISE_CLASSES_CSV):
    """Typenkürzel -> Lärmklasse aus dem mitgelieferten Bestand."""
    with open(path, encoding="utf-8") as handle:
        return {
            row["typecode"].strip().upper(): row["noise_class"].strip()
            for row in csv.DictReader(line for line in handle if not line.startswith("#"))
        }
