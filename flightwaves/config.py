"""Konfiguration: eine TOML-Datei, keine Tabelle (Spez. 3, offen bis 17.2)."""

import tomllib

DEFAULTS = {
    "site": {"name": "Standort", "geoid_undulation_m": 47.0},
    "tracking": {
        "radius_m": 50000,
        "fine_sampling_radius_m": 20000,
        "coarse_sampling_seconds": 5,
        "flight_gap_seconds": 600,
        "poll_interval_seconds": 1.0,
    },
    "dump1090": {
        "enabled": True,
        "url": "http://127.0.0.1:8080/data/aircraft.json",
        "timeout_seconds": 2.0,
    },
    "opensky": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "token_url": "https://auth.opensky-network.org/auth/realms/opensky-network"
        "/protocol/openid-connect/token",
        "states_url": "https://opensky-network.org/api/states/all",
        "poll_interval_seconds": 60,
        "local_grace_seconds": 30,
        "timeout_seconds": 15.0,
    },
    "weather": {
        "enabled": True,
        "poll_interval_seconds": 900,
        "timeout_seconds": 15.0,
        # Ein veralteter QNH ist schlechter als keiner: 30 hPa Abweichung sind
        # rund 240 m Höhenfehler, mehr als die Standardatmosphäre danebenliegt.
        "max_qnh_age_seconds": 10800,
    },
    "database": {"path": "data/flightwaves.db", "aircraft_path": "data/aircraft_database.sqlite"},
    "logging": {"level": "INFO"},
}

REQUIRED_SITE_KEYS = ("latitude_deg", "longitude_deg", "elevation_m")


def load(path):
    """Konfiguration lesen, über die Voreinstellungen legen und prüfen."""
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise SystemExit(
            f"{path} nicht gefunden. Anlegen mit:\n"
            f"  cp config.example.toml {path}\n"
            "und den Standort eintragen."
        ) from None
    except tomllib.TOMLDecodeError as fehler:
        raise SystemExit(f"{path} ist kein gültiges TOML: {fehler}") from None

    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    for section, values in raw.items():
        cfg.setdefault(section, {}).update(values)

    missing = [k for k in REQUIRED_SITE_KEYS if k not in cfg.get("site", {})]
    if missing:
        raise SystemExit(f"{path}: [site] fehlt: {', '.join(missing)}")
    if cfg["tracking"]["fine_sampling_radius_m"] > cfg["tracking"]["radius_m"]:
        raise SystemExit(f"{path}: fine_sampling_radius_m ist größer als radius_m")
    if not (cfg["dump1090"]["enabled"] or cfg["opensky"]["enabled"]):
        raise SystemExit(f"{path}: keine Flugdatenquelle aktiviert")
    if cfg["opensky"]["enabled"] and not cfg["opensky"]["client_secret"]:
        raise SystemExit(f"{path}: opensky.enabled=true ohne client_id/client_secret")
    return cfg
