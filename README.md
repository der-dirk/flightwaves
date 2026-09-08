# FlightWaves Sensor

Messsystem für einen festen Standort, das Flugbewegungen in der Umgebung
aufzeichnet, den zu erwartenden Fluglärm prognostiziert und die Prognose gegen
eine eigene Mikrofonmessung prüft.

Ein Raspberry Pi mit ADS-B-Empfänger und Messmikrofon. Alles läuft lokal: kein
Server, keine Cloud, keine Nutzerkonten.

**Stand: Stufe 1.1 (Flugtracking, Datenbank) implementiert.** Die fachliche
Grundlage steht vollständig in [`SPECIFICATION.md`](SPECIFICATION.md),
inklusive Datenmodell, Lärmmodell mit allen Parametern und Abnahmekriterien
je Stufe.

## Betrieb

Nur Python 3.11 und die Standardbibliothek, kein `pip install`:

```sh
cp config.example.toml config.local.toml     # Standort eintragen
python3 -m flightwaves --config config.local.toml aircraft-db aircraft.csv
python3 -m flightwaves --config config.local.toml collect
python3 -m flightwaves --config config.local.toml check    # Abnahme Stufe 1.1
```

Im Dauerbetrieb übernimmt das die Unit in [`systemd/`](systemd/).

## Entwicklung

Tests und Linter laufen im Container, damit lokal nichts installiert werden
muss und die Python-Version dieselbe ist wie auf dem Pi:

```sh
docker compose run --rm dev          # Tests und ruff
docker compose run --rm dev bash     # Shell im Container
```

Wer Python 3.11 ohnehin hat, braucht den Container nicht: `python3 -m pytest`.

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

[`flightwaves-app-android`](https://github.com/der-dirk/flightwaves-app-android)
ist eine Mobile-App auf derselben fachlichen Grundlage: Sie schätzt mobil und
ohne Messung. Lärmmodell, Referenzpegel, Stammdaten und Laufzeitkorrektur
werden von dort übernommen; die hier gemessenen Korrekturen sollen dorthin
zurückfließen.
