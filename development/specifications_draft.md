# Mobiles System zur Flugrouten- und Fluglärmprognose

## 1. Ziel

Ein kleines, portables System soll kontinuierlich Flugbewegungen in der Umgebung erfassen und daraus zunächst den zu erwartenden Fluglärm abschätzen.

Die Entwicklung erfolgt in drei Stufen:

1. **Flugtracking + Lärmprognose**
   - Flugposition, Höhe, Geschwindigkeit und Flugzeugtyp erfassen
   - Entfernung zum Messstandort berechnen
   - Wind und weitere relevante Einflussgrößen berücksichtigen
   - daraus einen geschätzten Lärmpegel bzw. eine Lärmwahrscheinlichkeit ableiten

2. **Reale akustische Messung**
   - Mikrofon an einen kleinen Linux-Rechner anschließen
   - tatsächlichen Schalldruckpegel bzw. Audiopegel erfassen
   - Messwerte zeitlich mit den Flugbewegungen abgleichen
   - Prognosemodell anhand realer Messungen kalibrieren

3. **Akustische Klassifizierung**
   - Audio zusätzlich automatisch klassifizieren
   - beispielsweise Flugzeug, Zug, Auto, sonstiges
   - erkannte Geräusche mit den gleichzeitig vorhandenen Flugtracking-Daten korrelieren
   - langfristig eigenes Trainings-/Validierungsset für den konkreten Standort aufbauen

---

## 2. Empfohlene Plattform

### Raspberry Pi 5

Der Raspberry Pi 5 ist die bevorzugte Basis für die erste und zweite Entwicklungsstufe.

Vorteile:

- vollständiges Linux-System
- gute Python-Unterstützung
- USB-Anschlüsse für Audio-Hardware
- Ethernet und WLAN
- ausreichend CPU-Leistung für Flugtracking, Datenbank und einfache Audioanalyse
- kompakt und portabel
- große Community und viele fertige Projekte
- später auch lokale KI-Inferenz möglich

Empfohlene Ausstattung:

- Raspberry Pi 5
- zunächst 4 GB RAM
- zuverlässige USB-C-Stromversorgung
- microSD-Karte oder besser SSD
- Gehäuse
- optional aktive Kühlung
- WLAN oder Ethernet
- USB-Mikrofon bzw. USB-Audio-Interface

Der eigentliche Anwendungscode sollte möglichst hardwareunabhängig geschrieben werden. Dadurch bleibt später ein Wechsel auf andere Linux-Hardware möglich.

---

## 3. Alternative Hardware

### NVIDIA Jetson

Eine NVIDIA-Jetson-Plattform ist interessant, wenn später deutlich mehr lokale KI-Rechenleistung benötigt wird.

Vorteile:

- deutlich stärkere GPU
- gute Unterstützung für Machine Learning
- geeignet für rechenintensivere Audio-Klassifizierung

Nachteile:

- teurer
- höherer Stromverbrauch
- für die erste Version unnötig

Daher zunächst nicht erforderlich.

### AudioMoth / ähnliche Audiorecorder

Spezialisierte Audiorecorder sind für reine Audioaufzeichnung interessant.

Für dieses Projekt sind sie zunächst weniger geeignet, weil neben Audio gleichzeitig:

- Flugtracking
- API-Abfragen
- Datenbank
- Geodatenberechnung
- Wetterdaten
- Lärmprognose

laufen sollen.

Ein Raspberry Pi vereint diese Aufgaben einfacher auf einem Gerät.

### ESP32

Ein ESP32 kann später als günstiger, stromsparender Sensor für reine Audio-/Pegel-Messungen interessant sein.

Für die zentrale Anwendung ist ein Linux-Rechner jedoch wesentlich flexibler.

---

## 4. Flugtracking

Als erste Datenquelle kommt insbesondere **OpenSky Network** infrage.

Benötigte Informationen können je nach API und verfügbarem Datensatz unter anderem sein:

- ICAO24
- Position
- Latitude / Longitude
- Höhe
- Geschwindigkeit
- Steig-/Sinkrate
- Flugrichtung
- Callsign
- Zeitstempel
- Flugzeug-/Fluginformationen

Wichtig ist die Trennung zwischen:

- aktuellen Positionsdaten
- Fluginformationen/Historie
- Flugzeugtyp und technischen Eigenschaften

Für das Projekt muss nicht jeder Flug weltweit verarbeitet werden. Sinnvoll ist eine räumliche Begrenzung auf einen Bereich um den Messstandort.

---

## 5. Relevante Flugzeugdaten

Für eine erste Lärmabschätzung sind insbesondere interessant:

- Flugzeugtyp
- Triebwerks-/Flugzeugklasse
- Größe bzw. Masseklasse
- aktuelle Höhe
- horizontale Entfernung
- 3D-Entfernung zum Messstandort
- Geschwindigkeit
- Flugrichtung
- Steigflug / Reiseflug / Sinkflug
- Position relativ zum Messstandort

Später können zusätzliche Informationen wie Triebwerkstyp oder konkrete Flugzeugvariante einbezogen werden.

---

## 6. Wetterdaten

Wind ist ein wichtiger zusätzlicher Faktor.

Mindestens erfassen:

- Windrichtung
- Windgeschwindigkeit

Optional:

- Temperatur
- Luftdruck
- Luftfeuchtigkeit
- Niederschlag
- Bewölkung

Die Windrichtung sollte korrekt als meteorologische Windrichtung interpretiert werden. Für die Modellierung ist außerdem die relative Beziehung zwischen Windrichtung, Flugrichtung und Messstandort relevant.

---

## 7. Erste Lärmprognose

Die erste Version benötigt noch kein Machine-Learning-Modell.

Ein physikalisch motiviertes Modell kann beispielsweise berücksichtigen:

### Entfernung

Grundsätzlich nimmt der Pegel mit zunehmender Entfernung ab.

Für eine einfache Näherung kann die geometrische Ausbreitung als Ausgangspunkt verwendet werden.

### Flughöhe

Die Höhe beeinflusst die Entfernung zum Messpunkt und damit den erwarteten Pegel.

### Flugzeugtyp

Unterschiedliche Flugzeugklassen erzeugen unterschiedliche Schallpegel.

### Flugphase

Start, Steigflug, Reiseflug und Sinkflug können unterschiedliche Geräuschcharakteristika besitzen.

### Geschwindigkeit

Die Geschwindigkeit kann zusammen mit Flugzeugtyp und Flugphase als zusätzlicher Einflussfaktor verwendet werden.

### Wind

Wind kann die Schallausbreitung zwischen Flugzeug und Messpunkt beeinflussen.

### Weitere Faktoren

Später können berücksichtigt werden:

- Temperatur
- Luftfeuchtigkeit
- Luftdruck
- Gelände
- Gebäude
- Bodenbeschaffenheit
- Abschirmungen
- atmosphärische Schallabsorption

Die erste Version sollte bewusst einfacher bleiben.

---

## 8. Datenmodell

Als lokale Datenbank reicht zunächst **SQLite**.

Mögliche Tabellen:

### flights

- id
- timestamp
- icao24
- callsign
- aircraft_type
- latitude
- longitude
- altitude
- velocity
- heading
- vertical_rate
- distance_to_sensor
- estimated_noise
- noise_estimation_timestamp

### weather

- timestamp
- wind_direction
- wind_speed
- temperature
- humidity
- pressure

### measurements

- timestamp
- sound_level
- frequency_data
- recording_reference
- detected_event

### audio_events

- start_time
- end_time
- classification
- confidence
- estimated_source
- related_flight_id

Später kann bei Bedarf auf PostgreSQL gewechselt werden.

---

## 9. Software

### Betriebssystem

Raspberry Pi OS / Debian Linux.

### Programmiersprache

**Python**

Geeignet für:

- API-Zugriff
- Geodatenberechnung
- Datenbank
- Wetterdaten
- Fluglärm-Modell
- Audioverarbeitung
- Machine Learning

### Sinnvolle Python-Komponenten

- `requests` oder `httpx` für APIs
- `sqlite3` bzw. SQLAlchemy für Datenbankzugriff
- `numpy`
- `scipy`
- `pandas`
- Geodaten-/Geometriebibliotheken nach Bedarf

Die Anwendung sollte aus mehreren klar getrennten Komponenten bestehen:

```text
Flight API
    ↓
Flight Collector
    ↓
Flight Database
    ↓
Noise Model ← Weather API
    ↓
Noise Prediction

Microphone
    ↓
Audio Capture
    ↓
Sound Level Measurement
    ↓
Measurement Database

Audio Capture
    ↓
Audio Classifier
    ↓
Classification
    ↓
Correlation with Flights
```

---

## 10. Audio-Hardware

Für die erste echte Messstufe ist ein **USB-Mikrofon** oder ein **USB-Audio-Interface mit geeignetem Mikrofon** sinnvoll.

Vorteile:

- Plug-and-play unter Linux
- keine zusätzliche Mikrocontroller-Hardware nötig
- einfache Aufnahme mit ALSA
- später auch für KI-Klassifizierung verwendbar

Für ernsthafte Pegelmessungen sollte nicht einfach irgendein PC-Mikrofon verwendet werden.

Interessanter sind:

- kalibrierbare Messmikrofone
- geeignete USB-Messmikrofone
- Mikrofon + Audio-Interface mit bekanntem Frequenzgang

Für wissenschaftlich belastbare dB(A)-Messungen wäre eine echte Kalibrierung erforderlich.

---

## 11. Audio-Software

Unter Linux stehen insbesondere **ALSA** und darauf aufbauende Audio-Software zur Verfügung.

Für die Anwendung kann Audio zunächst als kurze Zeitfenster verarbeitet werden, beispielsweise:

```text
Mikrofon
   ↓
Audio Stream
   ↓
1–5-Sekunden-Fenster
   ↓
Pegelberechnung
   ↓
Speicherung
```

Nicht zwingend muss dauerhaft Roh-Audio gespeichert werden.

Es kann sinnvoll sein:

- Pegel dauerhaft zu speichern
- Roh-Audio nur bei relevanten Ereignissen zu speichern
- Audio nach einer bestimmten Zeit automatisch zu löschen

---

## 12. Audio-Klassifizierung

Für die dritte Entwicklungsstufe kann beispielsweise **YAMNet** als Ausgangspunkt verwendet werden.

YAMNet ist ein vortrainiertes Audio-Klassifizierungsmodell und kann verschiedene Geräuschklassen erkennen.

Beispielhafte Kategorien können sein:

- Aircraft
- Train
- Car
- Engine
- Vehicle
- Speech
- andere Umgebungsgeräusche

Das Modell kann zunächst ohne eigenes Training getestet werden.

---

## 13. Warum eigenes Training später sinnvoll sein kann

Ein allgemeines Modell wurde nicht speziell für den eigenen Messstandort trainiert.

Die tatsächliche akustische Situation kann sich unterscheiden durch:

- Entfernung
- Gebäude
- Gelände
- Mikrofon
- Wind
- Wetter
- Flugrichtung
- Flugzeugtyp
- Hintergrundgeräusche

Deshalb kann später ein eigener Datensatz entstehen:

```text
Flugtracking
     +
Mikrofonaufnahme
     +
bekannter Flugzeugtyp
     +
Position / Höhe / Entfernung
     ↓
gelabelter Datensatz
```

Damit kann ein eigenes Modell trainiert oder ein bestehendes Modell angepasst werden.

---

## 14. Kombination aus Tracking und Audio

Der interessanteste Teil des Projekts ist die zeitliche Korrelation.

Beispiel:

```text
14:32:10  Flugzeug A  8.000 m Höhe  12 km entfernt
14:32:15  Flugzeug A  7.900 m Höhe  11 km entfernt
14:32:20  Mikrofon     52 dB
14:32:25  Mikrofon     58 dB
14:32:30  Flugzeug A  7.700 m Höhe   9 km entfernt
14:32:35  Audio-KI     Aircraft 0.91
```

Damit kann das System später untersuchen:

- Welcher Flug verursacht welchen Pegel?
- Wie gut stimmt die Prognose mit der Messung überein?
- Wie stark beeinflusst der Wind die Messung?
- Welche Flugzeugtypen sind besonders deutlich hörbar?
- Ab welcher Entfernung sind bestimmte Flugzeuge noch erkennbar?

---

## 15. Geografische Darstellung

Eine spätere Weboberfläche kann darstellen:

- aktuelle Flugzeuge
- Flugspuren
- Messstandort
- Entfernung zum Flugzeug
- Flughöhe
- geschätzten Lärmpegel
- tatsächlich gemessenen Pegel
- erkannte Audioereignisse

Beispiel:

```text
                  Flugzeug
                     ✈
                     |
                     |  8 km
                     |
             ~~~~~~~~~~~~~~~~~
                    Sensor
                     🎙
```

Zusätzlich kann eine Karte mit Flugrouten und Lärmwerten verwendet werden.

---

## 16. Architektur

Empfohlene erste Gesamtarchitektur:

```text
                 ┌─────────────────┐
                 │   OpenSky API   │
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │ Flight Collector│
                 └────────┬────────┘
                          │
                          ▼
                    ┌───────────┐
                    │  SQLite   │
                    └─────┬─────┘
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
      ┌──────────────┐         ┌──────────────┐
      │ Noise Model  │         │ Web Interface│
      └──────┬───────┘         └──────────────┘
             │
             ▼
       Noise Prediction


      ┌──────────────┐
      │ USB Mikrofon │
      └──────┬───────┘
             │
             ▼
      ┌──────────────┐
      │ Audio Capture│
      └──────┬───────┘
             │
        ┌────┴─────┐
        ▼          ▼
   Pegelmessung  Audio-KI
        │          │
        └────┬─────┘
             ▼
          SQLite
```

---

## 17. Entwicklungsphasen

### Phase 1 – Flugtracking

Zunächst nur:

- Raspberry Pi bzw. Entwicklungs-PC
- OpenSky-Daten
- lokale Datenbank
- Standortkoordinaten
- Entfernung zum Messstandort
- Flughöhe
- Geschwindigkeit
- Flugzeugtyp
- einfache Karte bzw. Weboberfläche

Noch kein Mikrofon erforderlich.

### Phase 2 – Lärmprognose

Ergänzen:

- Wetterdaten
- Wind
- Flugphase
- Flugzeugklasse
- physikalisch motiviertes Lärm-Modell

Ausgabe beispielsweise:

```text
Flugzeug: A320
Höhe: 7.200 m
Entfernung: 9,4 km
Wind: 240° / 18 km/h

geschätzter Pegel: 48 dB
```

Die absolute Genauigkeit sollte anfangs ausdrücklich als Schätzung betrachtet werden.

### Phase 3 – Mikrofon

Ergänzen:

- USB-Mikrofon
- Audioaufnahme
- Pegelberechnung
- Zeitstempel
- Speicherung der Messwerte

Jetzt können Prognose und Realität verglichen werden.

### Phase 4 – Korrelation

Automatisch bestimmen:

```text
Flugzeug → erwartetes Geräusch → tatsächliche Messung
```

Damit entsteht der eigene Datensatz.

### Phase 5 – Audio-KI

YAMNet oder ein vergleichbares vortrainiertes Modell einsetzen.

Zunächst nur:

```text
Aircraft
Train
Car
Other
```

### Phase 6 – eigenes Modell

Mit den gesammelten Standortdaten kann später ein eigenes Modell entstehen, das speziell auf:

- den Messstandort
- das Mikrofon
- typische Flugrouten
- Flugzeugtypen
- Wetterbedingungen

angepasst ist.

---

## 18. Empfohlene Hardware für den Start

### Pflicht

- Raspberry Pi 5, 4 GB
- Netzteil
- microSD oder SSD
- Gehäuse
- Netzwerkverbindung

### Für Phase 2

- noch keine zusätzliche Hardware notwendig

### Für Phase 3

- geeignetes USB-Mikrofon oder USB-Audio-Interface
- möglichst kalibrierbare Messhardware
- optional Windschutz

### Später

- wetterfestes Gehäuse
- externe Stromversorgung / Powerbank / Akku
- ggf. GPS
- ggf. besseres Messmikrofon
- optional NVIDIA Jetson für umfangreichere lokale KI

---

## 19. Zielbild

Das langfristige System soll ungefähr folgendes leisten:

```text
                    FLUGDATEN
                       │
                       ▼
             ┌──────────────────┐
             │ Flugzeug + Route │
             └────────┬─────────┘
                      │
                      ▼
             ┌──────────────────┐
             │ Lärmprognose     │
             └────────┬─────────┘
                      │
                      ▼
                 erwarteter
                   Pegel
                      │
                      ▼
              ┌───────────────┐
              │   Mikrofon    │
              └───────┬───────┘
                      │
                      ▼
                gemessener
                   Pegel
                      │
                      ▼
             ┌──────────────────┐
             │ Audio-Klassifier │
             └────────┬─────────┘
                      │
                      ▼
             erkannter Schall
                      │
                      ▼
             ┌──────────────────┐
             │   Korrelation    │
             │ Flug ↔ Geräusch  │
             └────────┬─────────┘
                      │
                      ▼
             ┌──────────────────┐
             │ eigener Datensatz│
             └────────┬─────────┘
                      │
                      ▼
             besseres Prognose-
                  modell
```

## 20. Grundentscheidung

Für den Projektstart ist die sinnvollste Kombination:

**Raspberry Pi 5 + Python + OpenSky Network + Wetterdaten + SQLite**

Zunächst ohne Mikrofon und ohne KI.

Danach:

**USB-Mikrofon + Audioaufnahme + Pegelmessung**

und erst anschließend:

**YAMNet bzw. vergleichbares Audio-Modell + eigene Trainingsdaten.**

Damit bleibt das Projekt von Anfang an klein und portabel, kann aber schrittweise bis zu einem lokal arbeitenden Fluglärm-Erkennungssystem ausgebaut werden.
