"""Flugdatenquellen: eigener Empfänger (Primär) und OpenSky (Abdeckung), Spez. 4.

Einheiten werden hier umgerechnet, nicht später: intern gilt durchgängig SI,
Zeitstempel sind zeitzonenbewusstes UTC.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import NamedTuple

FEET = 0.3048
KNOTS = 1852 / 3600
FEET_PER_MINUTE = FEET / 60

LOG = logging.getLogger(__name__)


class Position(NamedTuple):
    icao24: str
    observed: datetime          # Abstrahlzeit
    latitude: float
    longitude: float
    geom_altitude_m: float | None   # ellipsoidisch (WGS84)
    baro_altitude_m: float | None   # Druckhöhe
    ground_speed_ms: float | None
    track_deg: float | None
    vertical_rate_ms: float | None
    callsign: str | None
    on_ground: bool
    source: str


def _get_json(url, timeout, headers=None, data=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# --- dump1090 --------------------------------------------------------------

def parse_dump1090(payload):
    """aircraft.json in Positionsmeldungen übersetzen."""
    now = payload.get("now") or time.time()
    out = []
    for entry in payload.get("aircraft", []):
        if entry.get("lat") is None or entry.get("lon") is None:
            continue
        baro = entry.get("alt_baro")
        on_ground = baro == "ground"
        vrate = entry.get("geom_rate")
        if vrate is None:
            vrate = entry.get("baro_rate")
        callsign = (entry.get("flight") or "").strip() or None
        out.append(
            Position(
                icao24=entry["hex"].strip().lower(),
                observed=datetime.fromtimestamp(now - entry.get("seen_pos", 0.0), UTC),
                latitude=entry["lat"],
                longitude=entry["lon"],
                geom_altitude_m=_scale(entry.get("alt_geom"), FEET),
                baro_altitude_m=None if on_ground else _scale(baro, FEET),
                ground_speed_ms=_scale(entry.get("gs"), KNOTS),
                track_deg=entry.get("track"),
                vertical_rate_ms=_scale(vrate, FEET_PER_MINUTE),
                callsign=callsign,
                on_ground=on_ground,
                source="adsb",
            )
        )
    return out


def read_dump1090(url, timeout):
    return parse_dump1090(_get_json(url, timeout))


def _scale(value, factor):
    return None if not isinstance(value, (int, float)) else value * factor


# --- Wetter: Open-Meteo (Spez. 5) ------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

GROUND_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
)

PRESSURE_LEVELS_HPA = (850, 700, 500)
"""Grob 1,5 / 3 / 5,5 km. Sie gehen in Stufe 1.2 nicht in den Pegel ein und
werden auch nicht zu einer Kennzahl verdichtet – nur gespeichert. Der
Zusammenhang mit dem Prognosefehler wird in Stufe 2.2 aus den Messdaten
gewonnen, statt vorab modelliert zu werden."""

LEVEL_FIELDS = ("temperature", "wind_speed", "wind_direction", "geopotential_height")


class Weather(NamedTuple):
    observed: datetime
    level_m: float              # 0 = Boden, sonst geopotentielle Höhe
    wind_direction_deg: float | None
    wind_speed_ms: float | None
    temperature_c: float | None
    humidity_pct: float | None          # nur am Boden
    pressure_msl_hpa: float | None      # nur am Boden
    source: str = "open-meteo"


def open_meteo_query(latitude, longitude):
    """Abfrage für Bodenwerte und die drei Druckflächen."""
    return urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(GROUND_FIELDS),
            "hourly": ",".join(
                f"{field}_{level}hPa"
                for level in PRESSURE_LEVELS_HPA
                for field in LEVEL_FIELDS
            ),
            "wind_speed_unit": "ms",        # sonst km/h
            "timezone": "UTC",
            "forecast_days": 1,
        }
    )


def parse_open_meteo(payload, now):
    """Antwort in Wetterzeilen übersetzen, eine je Niveau.

    Die Druckflächen liefert Open-Meteo nur stündlich; genommen wird der
    Zeitpunkt, der ``now`` am nächsten liegt. Fehlende Werte bleiben leer,
    statt ersetzt zu werden – eine Lücke ist zulässig, ein erfundener Wert
    nicht (Spez. 13).
    """
    rows = []
    current = payload.get("current") or {}
    if current.get("time"):
        rows.append(
            Weather(
                observed=_utc(current["time"]),
                level_m=0.0,
                wind_direction_deg=current.get("wind_direction_10m"),
                wind_speed_ms=current.get("wind_speed_10m"),
                temperature_c=current.get("temperature_2m"),
                humidity_pct=current.get("relative_humidity_2m"),
                pressure_msl_hpa=current.get("pressure_msl"),
            )
        )

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return rows

    index = min(range(len(times)), key=lambda i: abs((_utc(times[i]) - now).total_seconds()))
    observed = _utc(times[index])
    for level in PRESSURE_LEVELS_HPA:
        height = _at(hourly, f"geopotential_height_{level}hPa", index)
        if height is None:
            continue        # ohne Höhe hat die Druckfläche keinen Ort
        rows.append(
            Weather(
                observed=observed,
                level_m=height,
                wind_direction_deg=_at(hourly, f"wind_direction_{level}hPa", index),
                wind_speed_ms=_at(hourly, f"wind_speed_{level}hPa", index),
                temperature_c=_at(hourly, f"temperature_{level}hPa", index),
                humidity_pct=None,
                pressure_msl_hpa=None,
            )
        )
    return rows


def read_open_meteo(latitude, longitude, timeout):
    payload = _get_json(f"{OPEN_METEO_URL}?{open_meteo_query(latitude, longitude)}", timeout)
    return parse_open_meteo(payload, datetime.now(UTC))


def _utc(stamp):
    """Open-Meteo liefert bei timezone=UTC Zeiten ohne Zonenangabe."""
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _at(hourly, name, index):
    werte = hourly.get(name)
    return werte[index] if werte and index < len(werte) else None


# --- OpenSky ---------------------------------------------------------------

class OpenSky:
    """Client für /states/all mit OAuth2 Client Credentials.

    HTTP Basic Auth ist von OpenSky abgekündigt (Spez. 4). Ausfälle sind
    normal und werden nicht als Fehler behandelt, solange die Primärquelle
    liefert – der Aufrufer bekommt eine leere Liste.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._token = None
        self._token_expiry = 0.0

    def _access_token(self):
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
            }
        ).encode()
        payload = _get_json(
            self.cfg["token_url"],
            self.cfg["timeout_seconds"],
            {"Content-Type": "application/x-www-form-urlencoded"},
            data,
        )
        self._token = payload["access_token"]
        # 60 s Sicherheitsabstand, damit kein Aufruf mit gerade abgelaufenem Token geht.
        self._token_expiry = time.monotonic() + payload.get("expires_in", 1800) - 60
        return self._token

    def states(self, box):
        """Positionsmeldungen in der Bounding Box; bei Störung eine leere Liste."""
        lamin, lamax, lomin, lomax = box
        query = urllib.parse.urlencode(
            {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}
        )
        try:
            payload = _get_json(
                f"{self.cfg['states_url']}?{query}",
                self.cfg["timeout_seconds"],
                {"Authorization": f"Bearer {self._access_token()}"},
            )
        except Exception as error:      # Netz, Kontingent, Wartung – alles gleich behandelt
            LOG.debug("OpenSky nicht erreichbar: %s", error)
            self._token = None
            raise
        return parse_states(payload)


def parse_states(payload):
    """OpenSky-Zustandsvektoren in Positionsmeldungen. Einheiten sind bereits SI."""
    out = []
    for state in payload.get("states") or []:
        icao24, callsign, _, time_position, last_contact, lon, lat = state[:7]
        if lat is None or lon is None:
            continue
        out.append(
            Position(
                icao24=icao24.strip().lower(),
                observed=datetime.fromtimestamp(time_position or last_contact, UTC),
                latitude=lat,
                longitude=lon,
                geom_altitude_m=state[13],
                baro_altitude_m=state[7],
                ground_speed_ms=state[9],
                track_deg=state[10],
                vertical_rate_ms=state[11],
                callsign=(callsign or "").strip() or None,
                on_ground=bool(state[8]),
                source="opensky",
            )
        )
    return out
