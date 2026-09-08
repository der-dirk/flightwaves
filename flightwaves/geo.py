"""Geometrie und Höhenbezug. Reines Python, ohne Hardware, Netz oder Datenbank (Spez. 3)."""

import math
from typing import NamedTuple

_A = 6378137.0                          # WGS84, große Halbachse
_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
STANDARD_QNH_HPA = 1013.25
METERS_PER_HPA = 8.0                    # Spez. 4


class Relation(NamedTuple):
    horizontal_m: float
    slant_m: float
    elevation_deg: float


def _radii(lat_deg):
    """Krümmungsradien (Meridian, Querkrümmung) auf dieser Breite."""
    s = math.sin(math.radians(lat_deg))
    n = _A / math.sqrt(1 - _E2 * s * s)
    return n * (1 - _E2) / (1 - _E2 * s * s), n


def relate(site_lat, site_lon, site_alt_m, lat, lon, alt_m) -> Relation:
    """Lage des Flugzeugs relativ zum Standort. Höhen in Metern über MSL.

    Lokale Tangentialebene mit den Krümmungsradien auf der mittleren Breite:
    über 50 km bleibt der Fehler im Meterbereich, die Kugelnäherung läge bei
    über 100 m.
    """
    mid = 0.5 * (site_lat + lat)
    m_rad, n_rad = _radii(mid)
    north = math.radians(lat - site_lat) * m_rad
    east = math.radians((lon - site_lon + 180) % 360 - 180) * n_rad * math.cos(math.radians(mid))
    up = alt_m - site_alt_m
    horizontal = math.hypot(east, north)
    elevation = math.degrees(math.atan2(up, horizontal))
    return Relation(horizontal, math.hypot(horizontal, up), elevation)


def bounding_box(lat, lon, radius_m):
    """(lamin, lamax, lomin, lomax) um den Standort – begrenzt nur die OpenSky-Abfrage."""
    m_rad, n_rad = _radii(lat)
    dlat = math.degrees(radius_m / m_rad)
    dlon = math.degrees(radius_m / (n_rad * max(math.cos(math.radians(lat)), 1e-6)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def altitude_msl(geom_alt_m, baro_alt_m, undulation_m, qnh_hpa=None):
    """(Höhe über MSL, Quelle) oder None, wenn keine Höhe vorliegt (Spez. 4).

    Die geometrische ADS-B-Höhe bezieht sich auf das WGS84-Ellipsoid, die
    barometrische ist eine Druckhöhe der Standardatmosphäre. Ohne bekannten
    QNH bleibt die Druckhöhe stehen – eine erfundene Korrektur wäre schlechter
    als keine. Den QNH liefert der Wetter-Collector der Stufe 1.2 nach.
    """
    if geom_alt_m is not None:
        return geom_alt_m - undulation_m, "geometric"
    if baro_alt_m is not None:
        qnh = STANDARD_QNH_HPA if qnh_hpa is None else qnh_hpa
        return baro_alt_m + (qnh - STANDARD_QNH_HPA) * METERS_PER_HPA, "barometric"
    return None
