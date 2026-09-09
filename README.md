# FlightWaves Sensor

Messsystem für einen festen Standort, das Flugbewegungen in der Umgebung
aufzeichnet, den zu erwartenden Fluglärm prognostiziert und die Prognose gegen
eine eigene Mikrofonmessung prüft.

Ein Raspberry Pi mit ADS-B-Empfänger und Messmikrofon. Alles läuft lokal: kein
Server, keine Cloud, keine Nutzerkonten.

**Stand: Stufen 1.1 und 1.2 implementiert.** Flugtracking, Datenbank,
Wetterdaten, Lärmmodell mit Laufzeitkorrektur, Prognosetabelle und
Weboberfläche stehen. Was aussteht, ist die Abnahme am Gerät: 72 Stunden
Dauerbetrieb mit echtem Empfang.

Die fachliche Grundlage steht vollständig in
[`SPECIFICATION.md`](SPECIFICATION.md), inklusive Datenmodell, Lärmmodell mit
allen Parametern und Abnahmekriterien je Stufe.

## Betrieb

Nur Python 3.11 und die Standardbibliothek, kein `pip install`:

```sh
cp config.example.toml config.local.toml     # Standort eintragen
python3 -m flightwaves --config config.local.toml aircraft-db assets/aircraft_types.csv
python3 -m flightwaves --config config.local.toml collect
python3 -m flightwaves --config config.local.toml weather  # eigener Dienst
python3 -m flightwaves --config config.local.toml web      # http://<pi>:8090
python3 -m flightwaves --config config.local.toml check    # Abnahme 1.1 und 1.2
python3 -m flightwaves --config config.local.toml predict  # LAmax je Flug
```

Im Dauerbetrieb übernehmen das die Units in [`systemd/`](systemd/) – Flug-,
Wetter-Collector und Weboberfläche laufen getrennt, damit ein Abruf mit
Zeitlimit den 1-Hz-Takt der Flugdaten nicht anhält.

Die Weboberfläche zeigt Karte, Flüge und Prognose und exportiert nach CSV und
JSON. Sie ist **nur für das lokale Netz** gedacht: kein Login, keine
Rechteprüfung, also kein Portfreigeben im Router.

Die Wetterdaten sind keine Beigabe: Der Luftdruck korrigiert die
barometrische Höhe, und die Temperatur auf dem Weg bestimmt die
Schall-Laufzeit. Ohne sie rechnet `predict` mit einem sichtbar
gekennzeichneten Platzhalter.

## Flugzeug-Stammdaten

ADS-B überträgt kein Flugzeugmuster, nur die ICAO24-Kennung. Den Typ löst
[`assets/aircraft_types.csv`](assets/aircraft_types.csv) auf: 515.354 Muster,
übernommen aus dem Schwesterprojekt, das sie aus der frei verfügbaren
[OpenSky-Aircraft-Database](https://opensky-network.org/datasets/metadata/)
(Ausgabe 2025-08) auf die beiden gebrauchten Spalten reduziert hat.

Der Bestand wird nicht neu gebaut, sondern übernommen – beide Projekte sollen
dieselben Muster auflösen. Der `aircraft-db`-Aufruf oben macht daraus eine
SQLite-Datei; ohne sie bleibt `aircraft_type` leer und die Aufzeichnung läuft
trotzdem weiter.

Welchen Lärm ein Muster macht, steht in
[`assets/noise_classes.csv`](assets/noise_classes.csv) – 2.730 Typenkürzel,
99,9 % des Bestands. Erzeugt aus drei Ebenen (Abschnitt 4 der Spezifikation):
kuratierte Verkehrsflugzeuge, eine kurze Liste für Sammelkürzel wie `GLID` oder
`ZZZZ`, und für alles Übrige die ICAO-Doc-8643-Merkmale. Neu erzeugen:

```sh
python3 tool/build_noise_classes.py <doc8643.csv>
```

## Entwicklung

Tests und Linter laufen im Container, damit lokal nichts installiert werden
muss und die Python-Version dieselbe ist wie auf dem Pi:

```sh
docker compose run --rm dev          # Tests und ruff
docker compose run --rm dev bash     # Shell im Container
docker compose run --rm --service-ports dev bash   # zusätzlich Port 8090
```

`--service-ports` braucht es nur für die Weboberfläche: `docker compose run`
veröffentlicht sonst keine Ports, und der Browser käme nicht an den
Container.

Wer Python 3.11 ohnehin hat, braucht den Container nicht: `python3 -m pytest`.

### Ohne Hardware testen

Ein simulierter Empfänger erzeugt Verkehr um den konfigurierten Standort und
serviert ihn im Format von dump1090 – der Collector merkt keinen Unterschied.
Der Port kommt aus `dump1090.url`, es ist also nichts umzustellen:

```sh
cp config.example.toml config.local.toml        # einmalig, Standort eintragen
python3 -m flightwaves aircraft-db assets/aircraft_types.csv   # einmalig
python3 -m flightwaves simulate --duration 600 &
python3 -m flightwaves collect &                # schreibt Positionen und Prognosen
python3 -m flightwaves web                      # http://localhost:8090
```

Ohne `--config` sucht jeder Aufruf `config.toml`; die Beispiele oben setzen
voraus, dass du `config.local.toml` angelegt und `--config config.local.toml`
angehängt hast.

Der Verkehr entspricht einem Standort im An- und Abflugbereich eines
Drehkreuzes: 20–40 Flugzeuge gleichzeitig, Anflüge, Abflüge, Überflüge in
Reiseflughöhe, Kleinflugzeuge, Hubschrauber, Rollverkehr am Boden,
Empfangslücken durch Abschattung, Transponder ohne geometrische Höhe und
Mode-S-Ziele ohne Position. Gleicher `--seed` erzeugt denselben Verkehr.

Die Kennungen stammen aus dem mitgelieferten Bestand in `assets/`, sind also
echte Registrierungen mit passendem Muster. Damit prüft der Testlauf dieselbe
Typauflösung, die später am Gerät läuft – einschließlich der Muster, die dort
fehlen.

## Worum es geht

Prognosemodelle für Fluglärm gibt es. Was es nicht gibt, sind *überprüfte*
Prognosen für einen konkreten Standort – mit seinem Gelände, seiner Bebauung,
seinen Flugrouten und seinem Grundgeräuschpegel.

Genau das ist die Aufgabe: Für jeden Überflug wird ein Maximalpegel geschätzt,
und derselbe Überflug wird gemessen. Aus der Differenz über viele Flüge
hinweg entsteht ein standortspezifischer, gelabelter Datensatz – und daraus
ein besseres Modell.

## Wie der Lärm geschätzt wird

Aus den ADS-B-Daten ergeben sich Position, Höhe und Steigrate; das
Flugzeugmuster kommt aus einer lokal gespeicherten Stammdatenkopie. Daraus
rechnet das Modell den Pegel am Standort:

1. **Referenzpegel** der Lärmklasse (Großraumflugzeug, Mittelstrecken-Jet,
   Regionaljet, Turboprop, Kleinflugzeug, Hubschrauber) in 300 m Entfernung.
2. **Geometrische Ausbreitung** – 6 dB weniger je Verdopplung der Entfernung.
3. **Luftabsorption** über die Zusatzstrecke.
4. **Laterale Dämpfung** bei flachem Schalleinfall.
5. **Schubkorrektur** aus der Steigrate.

Mehrere gleichzeitig hörbare Flugzeuge werden energetisch summiert, nie
arithmetisch gemittelt.

**Die Schall-Laufzeit wird durchgehend mitgeführt.** Was am Standort ankommt,
hat das Flugzeug vorher abgestrahlt – bei 10 km rund 30 Sekunden, in denen es
mehrere Kilometer weitergeflogen ist. Ohne diese Trennung von Abstrahl- und
Ankunftszeit wäre jede Zuordnung zwischen Flug und Geräusch systematisch
falsch.

## Warum ein eigener Empfänger

Ein RTL-SDR-Stick mit 1090-MHz-Antenne liefert Positionen im Sekundentakt,
ohne Abfragelimit und ohne Internetverbindung. Das
[OpenSky Network](https://opensky-network.org/) ergänzt nur die Abdeckung dort,
wo der eigene Empfang durch Abschattung ausfällt.

Der Grund ist der Messzweck: Den Maximalpegel bestimmt der Moment der größten
Annäherung. Bei 200 m/s liegen zwischen zwei API-Abfragen im Minutentakt zwölf
Kilometer – dieser Punkt wäre interpoliert statt gemessen.

## Entwicklungsstufen

| Stufe | Inhalt |
| --- | --- |
| 1.1 | Flugtracking, Datenbank |
| 1.2 | Wetterdaten, Lärmprognose, Weboberfläche |
| 2.1 | Mikrofon, Pegelmessung, Grundgeräuschpegel |
| 2.2 | Vergleich Prognose ↔ Messung je Überflug |
| 3.1 | Audio-Klassifizierung (YAMNet), Ereigniszuordnung |
| 3.2 | Standortspezifischer Datensatz, angepasstes Modell |

Jede Stufe wird gegen messbare Kriterien abgenommen, bevor die nächste
beginnt. Der Kern ist Stufe 2.2: mittlerer Fehler zwischen Prognose und
Messung höchstens 5 dB über 50 Einzelüberflüge.

## Hardware

Für den Start: Raspberry Pi 5 (4 GB), 27-W-Netzteil, SSD statt microSD,
RTL-SDR mit 1090-MHz-Antenne.

Ab Stufe 2 zusätzlich ein USB-Messmikrofon mit Kalibrierdatei – und ein
Windschutz, ohne den eine Außeninstallation vor allem das Windgeräusch an der
Kapsel misst.

## Was das System nicht ist

Keine amtliche oder gerichtsfeste Lärmmessung – ohne Klasse-1-Messmikrofon und
normkonforme Aufstellung sind die Werte Schätzungen mit bekanntem Offset.

Flüge werden gekennzeichnet, nie bewertet. Ob eine nächtliche Landung eine
Ausnahmegenehmigung hatte, kann das System nicht wissen, und deshalb nennt es
nichts einen Verstoß.

## Schwesterprojekt

[`flightwaves-app-flutter`](https://github.com/der-dirk/flightwaves-app-flutter)
ist eine Mobile-App auf derselben fachlichen Grundlage: Sie schätzt mobil und
ohne Messung. Lärmmodell, Referenzpegel, Stammdaten und Laufzeitkorrektur
werden von dort übernommen; die hier gemessenen Korrekturen sollen dorthin
zurückfließen.
