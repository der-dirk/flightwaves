# FlightWaves Sensor – Spezifikation

Messsystem, das Flugbewegungen in der Umgebung erfasst, daraus den zu
erwartenden Fluglärm prognostiziert und die Prognose gegen eine reale
akustische Messung prüft. Betrieb an einem festen Standort (Vorgabe, siehe
Abschnitt 17.2).

Status: Entwurf, Stand 2026-09-10. Ersetzt `development/specifications_draft.md`.
Das Schwesterprojekt `flightwaves-app-flutter` (Mobile-App, gleiche fachliche
Grundlage) ist in Abschnitt 16 abgegrenzt.

Ausgelegt für einen Standort **im An- und Abflugbereich eines Drehkreuzes**:
viele nahe Vorbeiflüge, 20–40 Flugzeuge gleichzeitig in Reichweite. Ein
ländlicher Standort mit reinen Reiseflug-Überflügen ist darin enthalten.

---

## 1. Ziel und Abgrenzung

### Ziel

Ein kleines Linux-Gerät, das

1. Flugbewegungen im Umkreis des Standorts aufzeichnet,
2. für jeden Überflug einen erwarteten Maximalpegel schätzt,
3. diesen Pegel gegen eine eigene Mikrofonmessung prüft und
4. aus dem Vergleich einen standortspezifischen, gelabelten Datensatz aufbaut.

Der eigentliche Wert des Projekts liegt in Schritt 3 und 4: Prognosemodelle für
Fluglärm gibt es, überprüfte Prognosen für einen konkreten Standort nicht.

### Nicht-Ziele

- **Keine amtliche oder gerichtsfeste Lärmmessung.** Ohne kalibriertes
  Klasse-1-Messmikrofon und normkonforme Aufstellung sind die Werte
  Schätzungen mit bekanntem Offset, nicht mehr.
- **Keine Bewertung von Flügen.** Es wird gemessen und gekennzeichnet, nie als
  "Verstoß" oder "zu laut" bewertet – Ausnahmegenehmigungen sind von außen
  nicht beurteilbar. (Konvention aus dem Schwesterprojekt.)
- **Kein Server, keine Cloud, keine Nutzerkonten.** Alle Daten bleiben auf dem
  Gerät.
- **Keine flächendeckende Erfassung.** Nur der Empfangsbereich des eigenen
  Standorts.
- **Kein Modelltraining auf dem Gerät.** Siehe Abschnitt 9.

---

## 2. Entwicklungsstufen

Verbindliche Nummerierung für das gesamte Dokument. Jede Stufe ist für sich
nutzbar und wird abgenommen (Kriterien in Abschnitt 15), bevor die nächste
beginnt.

| Stufe | Inhalt | Neue Hardware |
| --- | --- | --- |
| **1.1** | Flugtracking, Datenbank | Pi 5, RTL-SDR |
| **1.2** | Wetterdaten, Lärmprognose (LAmax je Überflug), Weboberfläche | – |
| **2.1** | Mikrofon, Pegelmessung, Grundgeräuschpegel | Messmikrofon, Windschutz |
| **2.2** | Vergleich Prognose ↔ Messung je Überflug | – |
| **3.1** | Audio-Klassifizierung mit vortrainiertem Modell (YAMNet), Ereigniszuordnung | – |
| **3.2** | Standortspezifischer Datensatz, angepasstes Modell | – |

Vor Beginn von Stufe 2 sind die offenen Entscheidungen aus Abschnitt 17 zu
treffen. Sie betreffen Datenschutz und Standortkonzept und ändern das
Datenmodell.

---

## 3. Architektur

```text
RTL-SDR ──► dump1090 ──┐
                       ├──► Flight Collector ──► SQLite ◄── Weather Collector ◄── Open-Meteo
OpenSky API ───────────┘         ▲                 │
(Abdeckung, Fallback)            │                 ▼
                       Aircraft-Database      Noise Model ──► noise_predictions
                       (icao24 → Typ, lokal)                       │
                                                                   ▼
Mikrofon ──► Audio Capture ──┬──► Pegelmessung ──► SQLite ──►  Vergleich ──► comparisons
             (Stufe 2)       │                                     ▲
                             └──► Klassifizierung ──► SQLite ──────┘
                                  (Stufe 3, TFLite)
                                                                   │
                                                                   ▼
                                                      Weboberfläche / Export
```

Die Kette Flugdaten → Prognose läuft unabhängig von der Audio-Kette. Fällt eine
aus, arbeitet die andere weiter; der Vergleich überspringt Lücken, statt zu
interpolieren.

Das Schaubild zeigt den Datenfluss, nicht die Prozessgrenzen. Als eigene
Dienste laufen Flug-Collector, Wetter-Collector und Weboberfläche. Das
Lärmmodell ist kein vierter: Es rechnet im Flug-Collector, wo Standort,
Muster und Wetter ohnehin vorliegen, und schreibt `noise_predictions` neben
der Position. Damit ist die Tabelle ohne Nachlauf aktuell und bleibt trotzdem
jederzeit neu ableitbar (Abschnitt 6).

**Trennung von Fachlogik und Gerätezugriff.** Das Lärmmodell, die
Geodatenberechnung und die Laufzeitkorrektur sind reines Python ohne Zugriff
auf Hardware, Netz oder Datenbank und damit ohne Gerät testbar. Diese
Schichtung hat sich im Schwesterprojekt bewährt und wird übernommen.

**Standortkonfiguration.** Latitude, Longitude, **Höhe über Meeresspiegel**
und Geoidundulation des Standorts (für die Umrechnung ellipsoidischer
ADS-B-Höhen, siehe Abschnitt 4), ab Stufe 2 zusätzlich Mikrofonhöhe und
-ausrichtung. Eine Konfigurationsdatei, keine Tabelle – solange 17.2 nicht
anders entscheidet.

---

## 4. Flugdaten

### Primärquelle: eigener ADS-B-Empfänger

- **RTL-SDR-Stick mit 1090-MHz-Antenne**, ausgewertet durch `dump1090`
  (bzw. `readsb`), Anbindung über dessen lokale JSON-Schnittstelle, vom
  Collector einmal pro Sekunde gelesen.
- Auflösung rund **1 Hz**, kein Abfragelimit, kein Internetzugang nötig.
- Begründung: Der Zeitpunkt der größten Annäherung bestimmt den LAmax. Bei
  200 m/s Grundgeschwindigkeit liegen zwischen zwei Abfragen im 20-s-Takt rund
  4 km, im 60-s-Takt 12 km – dieser Punkt würde interpoliert statt gemessen.
  Bei nahen Vorbeiflügen (unter 2 km) macht schon eine 5-s-Lücke bis zu 1 dB
  aus.
- Zeitstempel ist die Empfangszeit auf der Pi-Uhr (aus `now − seen_pos`).

### Sekundärquelle: OpenSky Network

- Ergänzt die **Abdeckung**: Flüge außerhalb des eigenen Empfangsbereichs,
  Lücken bei Abschattung. OpenSky liefert dieselben Felder wie der eigene
  Empfänger – es kommt keine zusätzliche Information je Flugzeug hinzu.
- Endpunkt `/states/all` mit Bounding-Box um den Standort.
- **Abfragetakt clientseitig gedrosselt** und am Credit-Budget ausgerichtet
  (Größenordnung 4.000 Credits/Tag bei registriertem Zugang; vor der
  Implementierung gegen die aktuelle Dokumentation prüfen). Voreinstellung:
  60 s, konfigurierbar.
- **Zusammenführung:** Der lokale Empfänger hat Vorrang. Eine
  OpenSky-Position wird nur übernommen, wenn für dieselbe ICAO24-Kennung seit
  mehr als 30 s keine lokale Meldung vorliegt. Die Quelle wird je Zeile
  gespeichert.
- **Authentifizierung über OAuth2 Client Credentials.** OpenSky hat HTTP Basic
  Auth abgekündigt. *Anmerkung: Das Schwesterprojekt verwendet in
  `opensky_client.dart` noch einen `Basic`-Header – dort separat zu prüfen.*
- Nutzungsbedingungen: nichtkommerzieller Gebrauch. Bei Verwendung ist die
  Quelle zu nennen.
- Ausfälle sind normal und werden nicht als Fehler gemeldet, solange die
  Primärquelle liefert.

### Erfasste Felder

Pro Positionsmeldung: `icao24`, Zeitstempel, Latitude, Longitude, barometrische
Höhe, geometrische Höhe (soweit vorhanden), Grundgeschwindigkeit, Kurs über
Grund, Steig-/Sinkrate, Callsign, Quelle.

- **Einheiten: durchgängig SI** – Meter, Meter pro Sekunde, Grad, Sekunden.
  Was ADS-B in Fuß und Knoten liefert, wird beim Import umgerechnet.
- **Zeit: durchgängig UTC.**
- **Höhenbezug: Meter über Meeresspiegel (MSL).** Die geometrische
  ADS-B-Höhe (`alt_geom`) bezieht sich auf das WGS84-Ellipsoid und liegt in
  Deutschland rund 45–50 m über MSL; sie wird über die konfigurierte
  Geoidundulation korrigiert. Die barometrische Höhe ist eine Druckhöhe
  (Standardatmosphäre) und wird über den aktuellen QNH aus den Wetterdaten
  korrigiert (~8 m je hPa). Verwendet wird die geometrische Höhe, wenn sie
  vorliegt, sonst die korrigierte barometrische; beide werden gespeichert, die
  verwendete Quelle wird vermerkt. Die Differenz beider erreicht mehrere
  hundert Meter und geht direkt in die Entfernung ein.
- **Räumliche Begrenzung:** Nur Flugzeuge innerhalb eines konfigurierbaren
  Radius (Voreinstellung 50 km Schrägentfernung) um den Standort werden
  gespeichert.
- **Flugzeuge am Boden werden nicht gespeichert.** Das Modell überspringt sie
  ohnehin; in Drehkreuznähe sind sie ein erheblicher Teil der empfangenen
  Ziele.
- **Abtastung nach Entfernung:** Innerhalb von 20 km Schrägentfernung wird
  jede Meldung gespeichert (1 Hz), außerhalb nur jede fünfte Sekunde. Die
  Entfernung liegt für den Radiusfilter ohnehin vor; die Regel ist eine
  Zeile im Collector. Ein Flug, dessen größte Annäherung jenseits 20 km liegt,
  hat aus der gröberen Abtastung einen LAmax-Fehler unter 0,01 dB. Kein
  nachträglicher Ausdünnungsjob.

### Flugidentität

Ein Flug (`flights`) ist eine Folge von Meldungen derselben ICAO24-Kennung
ohne Lücke von mehr als **10 Minuten**. Danach beginnt ein neuer Flug, auch
wenn Callsign und Muster gleich sind.

### Flugzeugtyp

Der Live-Datenstrom enthält **keinen Flugzeugtyp**, nur die ICAO24-Kennung.

- Auflösung über eine **lokal gespeicherte Kopie der frei verfügbaren
  OpenSky-Aircraft-Database** (icao24 → Typenkürzel/Kategorie), reduziert auf
  die benötigten Felder. Der Lookup erfolgt offline, ohne zusätzliche
  API-Aufrufe.
- Das Schwesterprojekt liefert diesen Datenbestand bereits aufbereitet mit
  (Ausgabe 2025-08, rund 515.000 Muster, erzeugt durch
  `tool/build_aircraft_database.dart`). **Dieser Bestand wird übernommen**,
  nicht neu gebaut.
- Unbekannte Kennungen liefern die Klasse `unknown` und werden mit einem
  mittleren Referenzpegel weitergerechnet, statt den Flug zu verschweigen.
- Gespeichert wird nur das Typenkürzel. Die Lärmklasse wird bei Bedarf aus
  der Zuordnungstabelle abgeleitet, damit eine geänderte Zuordnung keine
  Migration braucht.

### Lärmklassen je Typenkürzel

`assets/noise_classes.csv` ordnet jedem Typenkürzel eine Lärmklasse zu, erzeugt
durch `tool/build_noise_classes.py` in drei Ebenen:

1. **Kuratiert** – die 168 Verkehrsflugzeuge aus dem Schwesterprojekt. Sie
   gewinnen bei jedem Konflikt, weil Doc 8643 die Gewichtsklasse nicht kennt:
   Ein CRJ steht dort in derselben Kategorie `M` wie ein A320, obwohl er rund
   5 dB leiser ist.
2. **Sammelkürzel** – Kennungen, die kein Muster benennen (`GLID`, `BALL`,
   `PARA`, `ZZZZ`) oder in Doc 8643 fehlen; kurz gehalten und einzeln belegt.
3. **Doc 8643** – alles Übrige mechanisch aus Antriebsart und
   Wirbelschleppenkategorie, statt einer Pflegeliste über tausende Muster.

Die Ebenen 1 und 3 stimmen auf den kuratierten Mustern in 131 von 162 Fällen
überein; die 31 Abweichungen sind systematisch (Regionaljets und die B757, die
Doc 8643 als `M` führt) und genau der Grund für die Reihenfolge.

Abdeckung gegen den mitgelieferten Bestand: **99,9 %** der Registereinträge.
Die verbleibenden 471 Einträge in 144 seltenen Kürzeln bleiben bewusst
unbekannt, statt geraten zu werden.

---

## 5. Wetterdaten

Quelle: **Open-Meteo** (kein API-Schlüssel, liefert auch Winde auf
Druckflächen). Abfragetakt 15 Minuten.

Verpflichtend, Bodenniveau: Windrichtung (meteorologisch, Richtung *aus* der
der Wind weht), Windgeschwindigkeit, Temperatur, relative Luftfeuchte,
Luftdruck auf Meeresniveau (QNH, für die Höhenkorrektur in Abschnitt 4).

Zusätzlich, ab Stufe 1.2: Wind und Temperatur auf den Druckflächen 850, 700
und 500 hPa (grob 1,5 / 3 / 5,5 km), gespeichert mit ihrer geopotentiellen
Höhe.

- **Begründung:** Der akustisch relevante Windeffekt ist nicht die Mitführung
  des Schalls, sondern die **Refraktion am Wind- und Temperaturgradienten** –
  gegen den Wind entsteht eine Schattenzone, mit dem Wind wird Schall zum Boden
  gebeugt. Bodenwind allein bildet das nicht ab.
- Die Höhenwerte gehen in Stufe 1.2 **nicht** in den Pegel ein und werden
  auch nicht zu einer Kennzahl verdichtet. Sie werden nur gespeichert; der
  Zusammenhang mit dem Prognosefehler wird in der Auswertung der Stufe 2.2
  aus den Messdaten gewonnen, statt vorab modelliert zu werden.

---

## 6. Lärmprognose

### Zielgröße

**LAmax je Überflug in dB(A)** – der A-bewertete Maximalpegel, den ein
einzelner Flug am Standort erzeugt, erreicht im Moment der größten Annäherung.

- Die Zeitreihe der Momentanpegel wird gespeichert; der LAmax ist ihr Maximum
  je Flug, keine eigene Größe.
- Ergänzend wird eine Kategorie ausgewiesen, mit denselben Schwellen wie im
  Schwesterprojekt: **nicht hörbar < 35, leise < 50, mittel < 62, laut ≥ 62
  dB(A)**.
- Der Dezibelwert wird bis zur Kalibrierung nach Stufe 2.2 **ohne
  Nachkommastelle und als Schätzung gekennzeichnet** dargestellt.

### Schall-Laufzeit

Was am Standort ankommt, hat das Flugzeug vorher abgestrahlt: bei 10 km rund
30 Sekunden, in denen es mehrere Kilometer weitergeflogen ist. Ohne Korrektur
ist jede Zuordnung zwischen Flug und Geräusch systematisch falsch, und zwar
entfernungsabhängig. Deshalb trägt jeder Prognosewert zwei Zeitstempel:
**Abstrahlzeit** (aus den Flugdaten) und **Ankunftszeit** (was am Mikrofon
eintrifft).

Zwei Richtungen, unterschiedlich schwer:

- **Vorwärts (Prognose, Stufe 1.2):** Zu jeder Position ist die Ankunftszeit
  direkt `Abstrahlzeit + Schrägentfernung / Schallgeschwindigkeit`. Kein
  Löser nötig.
- **Rückwärts (Ereigniszuordnung, Stufe 3.1):** Zu einer Ankunftszeit den
  Zustand des Flugzeugs im Abstrahlmoment finden. Das ist implizit – die
  Laufzeit hängt von der Entfernung ab, die Entfernung von der Position zum
  noch unbekannten Abstrahlzeitpunkt – und wird per **Fixpunktiteration**
  gelöst. Sie konvergiert, weil ein Flugzeug deutlich langsamer ist als der
  Schall; der Fehler schrumpft je Durchlauf um den Faktor `v_radial / c`.

  **Über eine Toleranz abbrechen, nicht über eine feste Zahl von
  Durchläufen**, und bei Nichtkonvergenz keinen Wert liefern. Ein
  Zwischenergebnis, das nach der letzten Runde übrig bleibt, sieht aus wie
  eine Lösung und ist keine. Nachgemessen an geraden Bahnen mit konstanter
  Geschwindigkeit, bis zur Toleranz von 100 ms:

  | Geometrie | Durchläufe | Fehler nach 4 Durchläufen |
  | --- | --- | --- |
  | Anflug, 400 m Schrägentfernung | 5 | 48 m |
  | Abflug, 1 km | 7 | 225 m |
  | Überflug 250 m/s, 3 km Höhe | 16 | 1757 m |
  | Reiseflug frontal, 30 km | 16 | 3717 m |

  Die Fehlerspalte gilt jeweils an der ungünstigsten Stelle oberhalb von
  40 dB(A), also im hörbaren Bereich. Der schlechte Fall ist ein schnelles
  Flugzeug, das radial auf den Standort zuhält – am Drehkreuz die häufige
  Geometrie, nicht die exotische. 1757 m sind bei 250 m/s sieben Sekunden und
  verschieben damit, welche Flüge als gleichzeitig hörbar gelten (Abschnitt 8).

  Referenzimplementierung: `solveEmission()` in
  `flightwaves-app-flutter/lib/core/noise/sound_propagation.dart`.
  **Der Algorithmus wird übernommen, seine Parameter nicht.** Dort stehen vier
  Durchläufe fest und 343 m/s als Schallgeschwindigkeit; für die Anzeige einer
  App ist beides folgenlos, für eine Messung nicht. Ebenfalls nicht übernommen
  wird der dortige Rückfall auf die letzte bekannte Position: Er löst das
  Problem veralteter Live-Daten, das bei einer Auswertung im Nachhinein nicht
  auftritt. Übernommen werden die Extrapolationssperre, die Bereichsprüfung
  nach der Schleife und das Hörfenster je Track.

- Schallgeschwindigkeit `331,3 + 0,606 · T[°C]` m/s. Als Temperatur dient das
  Mittel aus Bodentemperatur und der Temperatur der nächstgelegenen
  Druckfläche zur Flughöhe; liegen keine Höhenwerte vor, die Bodentemperatur.
  (In 10 km Höhe herrschen −40 °C; mit Bodentemperatur allein ist die
  Laufzeit rund 10 % zu kurz – bei 200 m/s 600 m Positionsfehler, etwa
  0,5 dB.) **Eine feste Schallgeschwindigkeit ist nicht zulässig**, auch nicht
  als Vorgabewert: Die Referenzimplementierung rechnet mit 343 m/s, dem Wert
  für 20 °C, und liegt damit über einen Weg aus 10 km Höhe rund 7 % zu kurz –
  systematisch und entfernungsabhängig, also genau die Art Fehler, die eine
  Kalibrierung nicht mehr findet.
- **Es wird nie extrapoliert.** Liegt der gesuchte Abstrahlzeitpunkt außerhalb
  des bekannten Tracks, gibt es keinen Wert.

### Modell

Für jede gespeicherte Position und jedes Flugzeug:

```text
L = Referenzpegel(Lärmklasse)
    − geometrische Ausbreitung
    − Luftabsorption
    − laterale Dämpfung
    + Schubkorrektur
```

| Term | Ansatz |
| --- | --- |
| Referenzpegel | je Lärmklasse, bezogen auf **300 m** Schrägentfernung bei Reiseschub: heavyJet 88, mediumJet 82, regionalJet 77, Turboprop 75, Kolbenflugzeug 68, Hubschrauber 80, unbekannt 80 dB(A) |
| Geometrische Ausbreitung | `20·log₁₀(d/300 m)` – Punktquelle, −6 dB je Entfernungsverdopplung |
| Luftabsorption | `1,5 dB/km` A-bewertet, auf die Strecke oberhalb 300 m |
| Laterale Dämpfung | bis 8 dB, linear ansteigend unterhalb 20° Elevationswinkel (Bodeneffekt und Bebauung bei streifendem Einfall) |
| Schubkorrektur | aus der Steigrate: ≥5 m/s → +6, ≥2 → +4, >−2 → 0, >−6 → −2, sonst −3 dB |

- Zwei Klassen bekommen **keinen** Referenzpegel und damit keine Prognose:
  `unpowered` (Segelflugzeuge, Ballone, Fallschirmspringer, Drohnen – 7,0 %
  des Bestands) und `notAircraft` (Bodenfahrzeuge, Türme, undefinierte
  Kennungen – 1,0 %). Ohne diese Unterscheidung fallen sie auf den
  Ersatzpegel `unbekannt` von 80 dB(A), dem Referenzpegel eines
  Verkehrsflugzeugs, und erzeugen laute Prognosen für stille Objekte.
  `notAircraft` wird zusätzlich aus jeder Auswertung genommen.
- Die **Luftabsorption gehört in die erste Version**, nicht in eine spätere
  Ausbaustufe: Über 9 km trägt sie in derselben Größenordnung bei wie die
  geometrische Ausbreitung. Ein Modell ohne sie liegt bei Überflügen in
  Reiseflughöhe um zweistellige Dezibelbeträge daneben.
- **Distanz ist immer die 3D-Schrägentfernung** zwischen Flugzeug und
  Standort (mit Standorthöhe). Horizontale Entfernungen werden nur zur
  Anzeige berechnet und in der Oberfläche als solche beschriftet.
- Sind mehrere Flugzeuge gleichzeitig in Reichweite, werden die Pegel
  **energetisch summiert**: `10·log₁₀(Σ 10^(Lᵢ/10))`. Zwei gleich laute
  Flugzeuge ergeben 3 dB mehr, nicht das Doppelte. Arithmetische Mittelung von
  Pegeln ist an keiner Stelle zulässig.

### Stellschrauben und Versionierung

Alle Schwellen, Dämpfungsparameter und Referenzpegel liegen in **einem**
Konfigurationsobjekt beisammen, nicht als Konstanten über den Code verteilt.
Jeder gespeicherte Prognosewert trägt **Modellversion und Konfigurations-Hash**.

Prognosen sind aus den Positionen jederzeit neu ableitbar. Deshalb hält
`noise_predictions` **nur die aktuelle Modellversion**; nach einer Änderung
wird die Tabelle neu berechnet. Was eingefroren bleiben muss – der
Prognosewert, gegen den eine Messung verglichen wurde – steht mit
Modellversion und Hash in `comparisons` (Abschnitt 8) und wird nie
überschrieben.

Nicht im Modell und bewusst offen gelassen: Wetter- und Windeinfluss auf die
Ausbreitung, Gelände, Gebäudeabschirmung, tatsächliche Schubsetzung,
Bodenbeschaffenheit. Diese Größen sollen aus den Messdaten der Stufe 2.2
gewonnen werden.

---

## 7. Akustische Messung (Stufe 2.1)

### Hardware

- **USB-Messmikrofon oder Mikrofon mit USB-Audio-Interface** mit bekanntem
  Frequenzgang. Ein beliebiges PC- oder Headset-Mikrofon ist ungeeignet.
- **Windschutz ist Pflicht, nicht optional.** Ohne ihn misst eine
  Außeninstallation das Windgeräusch an der Kapsel – und zwar am stärksten
  genau dann, wenn der Windeinfluss untersucht werden soll.
- Wetterschutz für die dauerhafte Außenaufstellung; Aufstellhöhe und
  -richtung stehen in der Standortkonfiguration, weil sie in die Kalibrierung
  eingehen.

### Verarbeitung

```text
Mikrofon → Audiostream 48 kHz mono → 1-s-Fenster → Pegel + Terzbandanalyse → Speicherung
```

Je Fenster gespeichert: LAeq, **LAFmax** (A-bewertet, Zeitbewertung *Fast*,
125 ms – die Standardgröße für Fluglärm und die Vergleichsgröße zum
prognostizierten LAmax), Terzbandpegel, Zeitstempel (UTC, Ankunftszeit).

- **Zeitstempel ist die Aufnahmezeit der Abtastwerte, nicht die Zeit des
  Verarbeitungsaufrufs.** ALSA puffert 100 ms bis 1 s; ohne Korrektur um die
  Pufferlatenz entsteht ein systematischer Versatz gegen die Flugdaten.
- **Grundgeräuschpegel wird laufend miterfasst** als gleitendes
  L90-Perzentil der 1-s-LAeq-Werte über 5 Minuten. Ohne diese Referenz ist
  nicht entscheidbar, ob ein Überflug überhaupt hörbar war – ein Flug in 8 km
  Höhe liegt an vielen Standorten unter dem Umgebungspegel. Der L90 ist damit
  Voraussetzung für jede Aussage der Stufe 2.2, nicht Zusatzinformation.
- **Kalibrierung:** Der Pegel wird über einen gespeicherten Offset in dB auf
  absolute Werte gebracht. Zwei zulässige Wege: ein akustischer Kalibrator
  (94 dB / 1 kHz; setzt eine ½"-Kapsel voraus, die in den Koppler passt) oder
  die **Kalibrierdatei des Herstellers** (Sensitivität und Frequenzgang, wie
  bei üblichen USB-Messmikrofonen mitgeliefert), ergänzt um eine
  Vergleichsmessung mit einem Schallpegelmesser. Der Offset gilt ab einem
  Zeitpunkt und wird historisiert, damit ältere Messungen nachträglich
  umgerechnet werden können. Ohne gültigen Offset werden Pegel als relativ
  gekennzeichnet.
- Der Mikrofon-Offset wird **ausschließlich** auf diesem Weg bestimmt, nie
  durch Anpassung an Flugereignisse – sonst wäre das Abnahmekriterium der
  Stufe 2.2 zirkulär.

### Rohaudio

Dauerhaft gespeichert werden nur Pegel und Terzbanddaten. Ob und wie lange
Rohaudio bei Ereignissen vorgehalten wird, ist **vor Beginn der Stufe 2.1 zu
entscheiden** – siehe Abschnitt 17.1.

---

## 8. Vergleich und Zuordnung

### Stufe 2.2 – fluggetrieben

In dieser Stufe gibt es noch keinen Klassifikator und keine
Ereigniserkennung. Der Vergleich geht deshalb vom Flug aus, nicht vom Geräusch:

1. Für jeden Flug mit prognostiziertem LAmax über **L90 + 10 dB** wird das
   **Hörfenster** bestimmt: Ankunftszeit des prognostizierten Maximums
   ± 15 s.
2. Ein Flug ist ein **Einzelüberflug**, wenn in seinem Hörfenster kein
   anderer Flug eine Prognose über (eigener LAmax − 10 dB) hat. Nur
   Einzelüberflüge werden verglichen; alle anderen werden als Mehrfachereignis
   gekennzeichnet und übersprungen.
3. Gemessener Wert ist das Maximum der LAFmax-Werte im Hörfenster.
4. Gespeichert wird je Vergleich: prognostizierter LAmax, gemessener LAFmax,
   L90 zum Zeitpunkt, Wetterdatensatz, Modellversion, Konfigurations-Hash.

Ein gleichzeitig vorbeifahrendes Auto verfälscht den gemessenen Wert; das ist
in dieser Stufe unvermeidbar und Teil des Rauschens, das die
Fehlerschranke in Abschnitt 15 abbildet. Stufe 3.1 filtert solche Fälle
nachträglich.

### Stufe 3.1 – ereignisgetrieben

Mit Klassifikator werden zusätzlich Geräuschereignisse erkannt und Flügen
zugeordnet – über das laufzeitkorrigierte Hörfenster jedes Tracks
(Rückwärtsrichtung aus Abschnitt 6), nicht über den rohen
Positionszeitstempel.

- **Waren mehrere Flugzeuge gleichzeitig hörbar, wird keinem einzelnen
  zugeordnet.** Das Ereignis gilt als kombiniertes Geräusch und wird gegen die
  energetische Summe der Prognosen verglichen. Das kostet Aussagekraft für
  einzelne Muster, vermeidet aber Fehlzuordnungen, die den Trainingsdatensatz
  dauerhaft verderben würden. (Übernommen aus dem Schwesterprojekt, dort
  bewusst so festgelegt.)
- Die Beziehung Ereignis ↔ Flug ist **n:m**, nicht 1:1.

Auswertbare Fragen: Wie groß ist der systematische Fehler der Prognose? Hängt
er von Entfernung, Elevationswinkel, Flugzeugklasse, Steigrate oder
Windrichtung ab? Ab welcher Entfernung liegt ein Muster unter dem
Grundgeräusch?

---

## 9. Audio-Klassifizierung (Stufe 3)

- Ausgangspunkt: **YAMNet als TFLite-Modell**. Der Weg über TensorFlow Hub ist
  auf ARM64 mühsam; die TFLite-Variante ist die praktikable Wahl auf dem Pi.
  Eingabe 16 kHz mono, aus dem 48-kHz-Strom heruntergetastet.
- Zunächst nur die Klassen *Aircraft*, *Train*, *Car*, *Other*. Die
  Zuordnung der AudioSet-Klassen (u. a. *Aircraft*, *Fixed-wing aircraft*,
  *Jet engine*, *Helicopter*) auf diese vier ist eine Konfigurationstabelle.
- Klassifiziert wird nur, wenn der LAeq des Fensters über L90 + 6 dB liegt;
  Stille braucht keine Inferenz.
- Klassifikationsergebnisse liegen **ausschließlich** in `audio_events`, nie
  zusätzlich in der Pegeltabelle.
- **Training findet nicht auf dem Gerät statt.** Der Pi führt Inferenz aus. Ein
  angepasstes Modell (Stufe 3.2) wird auf einem Arbeitsrechner aus dem
  exportierten Datensatz trainiert und als fertiges Modell zurückgespielt.

Der Wert des eigenen Datensatzes liegt darin, dass ein allgemeines Modell den
Standort nicht kennt: Entfernungsverteilung, Gebäude, Gelände, Mikrofon,
typische Flugrouten und Hintergrundgeräusche sind hier andere als im
Trainingsmaterial.

---

## 10. Datenmodell

SQLite im **WAL-Modus** – mehrere Prozesse schreiben (Flug-, Wetter-,
Audio-Collector, Prognose); ohne WAL gibt es Sperrkonflikte. Alle Zeitstempel
UTC, alle Größen SI, Einheit im Spaltennamen.

### `flights` – ein Flug als Ganzes

`id`, `icao24`, `callsign`, `aircraft_type`, `first_seen_utc`, `last_seen_utc`

### `flight_positions` – Zeitreihe der Rohpositionen

`id`, `flight_id` → `flights.id`, `observed_utc` (Abstrahlzeit), `latitude`,
`longitude`, `altitude_m` (MSL), `altitude_source` (geometrisch/barometrisch),
`baro_altitude_m`, `ground_speed_ms`, `track_deg`, `vertical_rate_ms`,
`source` (adsb/opensky)

> Die Trennung von `flights` ist wesentlich: Ein Audioereignis von 30 Sekunden
> überdeckt Dutzende Positionsmeldungen. Ein Fremdschlüssel auf eine einzelne
> Positionszeile wäre bedeutungslos.

### `noise_predictions` – abgeleitet, nur aktuelle Modellversion

`id`, `flight_id`, `emitted_utc`, `arrival_utc`, `slant_distance_m`,
`elevation_deg`, `level_dba`, `category`, `model_version`, `config_hash`

> Der LAmax je Überflug ist das Maximum über diese Zeitreihe, keine eigene
> Spalte. Die Tabelle wird nach einer Modelländerung komplett neu berechnet.

### `weather`

`id`, `observed_utc`, `level_m` (0 = Boden, sonst geopotentielle Höhe der
Druckfläche), `wind_direction_deg`, `wind_speed_ms`, `temperature_c`,
`humidity_pct`, `pressure_msl_hpa`, `source`

### `sound_levels` – Stufe 2

`id`, `window_start_utc`, `window_seconds`, `laeq_dba`, `lafmax_dba`,
`l90_dba`, `band_levels_json`, `calibration_id`

### `calibrations`

`id`, `valid_from_utc`, `offset_db`, `method`, `note`

### `comparisons` – Stufe 2.2, die zentrale Tabelle des Projekts

`id`, `flight_id`, `event_id` (NULL bis Stufe 3), `window_start_utc`,
`window_end_utc`, `predicted_lamax_dba`, `measured_lafmax_dba`, `l90_dba`,
`weather_id`, `is_single_flyover`, `model_version`, `config_hash`,
`computed_utc`

> Wird nie überschrieben. Ein neues Modell erzeugt neue Zeilen mit neuer
> Version; der Vergleich der Versionen ist eine Abfrage.

### `audio_events` – Stufe 3

`id`, `start_utc`, `end_utc`, `classification`, `confidence`, `model_version`,
`peak_lafmax_dba`, `embedding_reference`, `audio_reference`

### `event_flight_links` – n:m, Stufe 3

`event_id`, `flight_id`, `match_score`; Primärschlüssel (`event_id`,
`flight_id`)

### Indizes

`flight_positions(flight_id, observed_utc)`, `flights(icao24, last_seen_utc)`,
`noise_predictions(flight_id)`, `noise_predictions(arrival_utc)`,
`sound_levels(window_start_utc)`, `comparisons(flight_id)`,
`audio_events(start_utc)`

### Datenmengen und Aufbewahrung

Mit der Abtastregel aus Abschnitt 4 und ohne Bodenziele: am Drehkreuz
Größenordnung **40 GB/Jahr** für Positionen und Prognosen zusammen, ländlich
unter 10 GB. Pegeldaten: rund 20 MB/Tag. Alles wird unbegrenzt aufbewahrt;
ein Löschjob ist nicht vorgesehen. Rohaudio: siehe Abschnitt 17.1.

Ein Wechsel auf PostgreSQL ist nicht vorgesehen; er würde erst mit mehreren
Standorten in einer gemeinsamen Datenbank sinnvoll.

---

## 11. Software

- **Betriebssystem:** Raspberry Pi OS (Debian, 64 Bit).
- **Sprache:** Python 3.
- **Stufen 1.1 und 1.2: nur die Standardbibliothek.** `sqlite3` für die
  Datenbank, `urllib` für dump1090, OpenSky und Open-Meteo, `http.server`
  für die Weboberfläche, `tomllib` für die Konfiguration. Kein ORM – bei
  dieser Zahl von Tabellen ist SQL direkter als eine Abstraktionsschicht
  darüber.

  Ursprünglich waren `requests`, `numpy` und `Flask` vorgesehen. Keines davon
  trägt bei dieser Größe: Ein GET mit JSON sind in `urllib` drei Zeilen, die
  Pegelrechnung ist Skalarmathematik ohne Felder, und die Oberfläche hat eine
  Handvoll lesender Endpunkte. Der Gewinn ist handfest – unter Raspberry Pi
  OS (Bookworm) verweigert `pip` die Installation ins System, und ohne
  Abhängigkeiten braucht es weder venv noch `apt`-Paketsuche.
- **Karte:** `Leaflet`, aus dem Netz nachgeladen. Fehlt der Internetzugang im
  Browser, entfällt die Karte; die Tabellen der Oberfläche arbeiten weiter.
- **Stufe 2:** ALSA über `sounddevice`, `scipy` für die Terzbandanalyse.
- **Stufe 3:** `tflite-runtime` (nicht das vollständige TensorFlow).

Ab Stufe 2 sind Abhängigkeiten unvermeidbar; bis dahin bleibt das Gerät ohne.
`pandas` wird für den Dauerbetrieb nicht benötigt und nur bei Bedarf für die
Auswertung nachinstalliert.

**Prüfen ohne Hardware.** Ein simulierter Empfänger (`simulate`) erzeugt
Verkehr um den Standort und liefert ihn im Format von dump1090. Er ersetzt
keine Abnahme – die Kriterien in Abschnitt 15 meinen den Betrieb am Gerät –,
macht aber die ganze Kette vom Empfang bis zur Oberfläche ohne Empfänger
prüfbar.

---

## 12. Hardware

### Stufe 1 (Pflicht)

- Raspberry Pi 5, 4 GB
- **offizielles 27-W-Netzteil** – SDR und SSD hängen am USB; mit schwächerer
  Versorgung meldet der Pi Unterspannung und drosselt
- **SSD statt microSD**, mindestens 512 GB, per NVMe-HAT oder USB – der
  Dauerschreibbetrieb der Datenbank verschleißt SD-Karten; das ist keine
  Komfortfrage
- Gehäuse, aktive Kühlung
- Netzwerkanbindung (Ethernet bevorzugt)
- RTL-SDR-Stick mit 1090-MHz-Antenne, möglichst mit freier Sicht; in
  Sendernähe optional ein 1090-MHz-Filter

### Stufe 2

- USB-Messmikrofon oder Mikrofon mit USB-Audio-Interface, mit
  Kalibrierdatei des Herstellers
- Windschutz (Pflicht), Wetterschutz
- akustischer Kalibrator oder leihweise ein Schallpegelmesser für den
  Offset-Abgleich

### Später erwägenswert

- besseres Messmikrofon
- unterbrechungsfreie Stromversorgung

Eine NVIDIA-Jetson-Plattform wird **nicht** benötigt: YAMNet-Inferenz auf
Einsekundenfenstern läuft auf dem Pi 5, und Training findet ohnehin nicht auf
dem Gerät statt. Ein ESP32 kommt allenfalls als zusätzlicher, entfernter
Pegelsensor in Frage, nicht als Zentrale.

Der Anwendungscode bleibt hardwareunabhängig, damit ein Wechsel auf andere
Linux-Hardware möglich bleibt.

---

## 13. Betrieb

- **Eine gemeinsame Uhr.** Flugdaten aus dump1090 und Audio werden beide auf
  der Pi-Uhr gestempelt; zwischen ihnen gibt es keinen Drift, egal wie gut
  die Uhr geht. **NTP** ist trotzdem Pflicht – für die Zusammenführung mit
  OpenSky- und Wetterzeitstempeln und für die Vergleichbarkeit exportierter
  Daten. Der Synchronisationszustand wird protokolliert.
- Alle Sammler laufen als `systemd`-Dienste mit automatischem Neustart.
- **Ein einzelner fehlgeschlagener Abruf wird nicht gemeldet.** Erst wiederholte
  Fehlschläge erzeugen einen Eintrag, mit unterschiedener Ursache (kein Netz,
  Quelle nicht erreichbar, Kontingent erschöpft). Die letzten bekannten Werte
  bleiben mit ihrem Zeitstempel stehen.
- Lücken in der Zeitreihe sind zulässig und werden nie durch Extrapolation
  gefüllt.
- Datenbanksicherung: regelmäßig per `sqlite3 .backup` auf ein separates
  Medium. Bei der erwarteten Datenmenge ist das eine Dateikopie, kein
  Dump-Verfahren.

---

## 14. Weboberfläche (ab Stufe 1.2)

Nur im lokalen Netz erreichbar, ohne Login – und deshalb nie ins Internet
exponiert.

Darstellung: Karte mit aktuellen Flugzeugen und Spuren, Standort,
Schrägentfernung, Höhe, prognostizierter Pegel und Kategorie; ab Stufe 2
zusätzlich gemessener Pegel, Grundgeräuschpegel und erkannte Ereignisse; die
Gegenüberstellung Prognose ↔ Messung als Zeitreihe und als Streudiagramm über
die Entfernung. Zeiten werden lokal angezeigt, intern bleibt alles UTC.

Export der Daten als CSV und JSON, gefiltert nach Zeitraum.

---

## 15. Abnahmekriterien

Definitionen: **Linienflug** = Callsign nach ICAO-Schema (drei Buchstaben
Airline-Kennung + Flugnummer). **Einzelüberflug** = wie in Abschnitt 8.

| Stufe | Kriterium |
| --- | --- |
| 1.1 | 72 h Dauerbetrieb ohne Eingriff. Für ≥ 85 % der Linienflüge im 50-km-Radius liegt ein Flugzeugtyp vor. Keine Zeitstempel außerhalb UTC, keine Höhe ohne vermerkte Quelle, keine Bodenziele in der Datenbank. |
| 1.2 | Das Modell reproduziert die Referenzwerte exakt: mediumJet in 300 m Schrägentfernung, Reiseschub, ≥ 20° Elevation → 82 dB(A). Abstrahl- und Ankunftszeit unterscheiden sich um `d/c`. Jeder Prognosewert trägt Modellversion und Konfigurations-Hash. Die Weboberfläche zeigt Karte, Flüge und Prognose. |
| 2.1 | 24 h Pegelaufzeichnung ohne Lücke > 5 s. Gültiger Kalibrier-Offset hinterlegt, bestimmt ohne Bezug auf Flugereignisse. L90 durchgängig verfügbar. |
| 2.2 | Über ≥ 50 Einzelüberflüge mit prognostiziertem LAmax > L90 + 10 dB: mittlerer Betragsfehler Prognose gegen Messung ≤ 5 dB. Modellparameter dürfen an Daten angepasst werden, **nicht an den 50 Überflügen der Abnahme**. Wird das Kriterium nicht erreicht, wird das Modell nachjustiert, nicht das Kriterium. |
| 3.1 | Bei Überflügen, die Kriterium 2.2 erfüllen, liegt in ≥ 80 % der Fälle im Hörfenster mindestens ein Fenster mit Klasse *Aircraft* und Konfidenz ≥ 0,5. |
| 3.2 | Das angepasste Modell übertrifft YAMNet im F1-Wert für *Aircraft* auf einem zurückgehaltenen Testanteil desselben Standorts. |

Risiko: An einem ruhigen Standort mit reinen Reiseflug-Überflügen kann es
lange dauern, bis 50 Überflüge die Schwelle L90 + 10 dB erreichen. Für den
vorgesehenen Standort im An- und Abflugbereich ist das nicht zu erwarten.

---

## 16. Verhältnis zu `flightwaves-app-flutter`

Gleiche fachliche Grundlage, andere Rolle: Die App schätzt mobil und ohne
Messung, dieses System misst stationär und liefert die Grundlage, um die
Schätzung zu prüfen.

**Wird von dort übernommen** (nicht neu entwickelt): Aircraft-Database und
Erzeugungswerkzeug, die Referenzpegel, das Modell aus Abschnitt 6, die
Laufzeitkorrektur, die energetische Pegelsummierung, die Kategorieschwellen,
die Konvention "keine Bewertung von Flügen".

**Entsteht hier und fließt zurück:** die **Lärmklassentabelle**. Die Tabelle
der App deckt 168 Muster ab – gemessen am mitgelieferten Bestand sind das
40,5 % der Registereinträge; der Rest fällt auf `unbekannt` mit 80 dB(A).
`assets/noise_classes.csv` erreicht 99,9 % und unterscheidet zusätzlich
antriebslose Luftfahrzeuge und Nicht-Luftfahrzeuge. Die App sollte diese
Tabelle übernehmen, statt ihre eigene zu pflegen.

**Gilt hier nicht:** der 10-Sekunden-Abfragetakt (die App fragt nur bei
geöffnetem Bildschirm ab, dieses System läuft durchgehend – daher der eigene
Empfänger) und der Vorrang der Kategorie vor dem Dezibelwert (hier ist der
Dezibelwert die zu prüfende Größe).

**Rückfluss:** Die aus Stufe 2.2 gewonnenen Korrekturen an Referenzpegeln und
Dämpfungsparametern sollen in die App zurückfließen. Deshalb müssen die
Parameter in beiden Projekten dieselbe Bedeutung und denselben Bezugspunkt
(300 m) behalten.

Offene Punkte dort:

- die Authentifizierung gegen OpenSky (Abschnitt 4);
- `solveEmission()` bricht nach vier Durchläufen ab und gibt den letzten
  Zwischenwert zurück, ohne zu melden, dass er keine Lösung ist
  (Abschnitt 6). Für die Anzeige der App bleibt der Fehler bei nahen
  Flugzeugen unter 50 m und ist damit harmlos; die fehlende
  Konvergenzmeldung ist es nicht, sobald der Löser anderswo verwendet wird.

---

## 17. Offene Entscheidungen

Beide sind **vor Beginn der Stufe 2.1** zu treffen; sie ändern das Datenmodell
und lassen sich nachträglich nur mit Datenverlust korrigieren.

### 17.1 Umgang mit Rohaudio

Ein dauerhaft aufzeichnendes Mikrofon im Außenbereich erfasst auch Sprache –
YAMNet führt *Speech* als eigene Klasse. Das ist datenschutzrechtlich relevant,
unabhängig davon, dass die Daten das Gerät nicht verlassen.

Zu entscheiden ist zwischen:

- **nur Pegel, Terzbandmerkmale und YAMNet-Embeddings speichern.** Das
  Embedding (1024 Werte je Fenster) reicht, um in Stufe 3.2 einen eigenen
  Klassifikator zu trainieren, und lässt sich praktisch nicht zu Sprache
  zurückführen. Damit entfällt das Datenschutzproblem, ohne 3.2 zu
  verbauen. Verzichtet wird nur auf das Nachhören einzelner Ereignisse durch
  Menschen;
- **Rohaudio nur bei Ereignissen, mit automatischer Löschfrist** – nötig, falls
  Ereignisse nachgehört oder Verfahren jenseits von YAMNet trainiert werden
  sollen; erfordert dann eine ausformulierte Festlegung zu Frist, Zugriff und
  Aufstellungsort.

Bis zur Entscheidung gilt die erste Variante.

### 17.2 Fester Standort oder mobiler Betrieb

Standortspezifische Kalibrierung (Abschnitt 7) und ein standortspezifisches
Modell (Stufe 3.2) setzen einen **festen** Standort voraus. Ein wechselnder
Aufstellort widerspricht dem. Zu entscheiden ist zwischen einem festen
Standort, mehreren benannten Standortprofilen mit je eigener Kalibrierung, und
echtem mobilem Betrieb mit GPS und standortunabhängigem Modell.

Die Wahl bestimmt, ob der Standort eine Konfigurationsgröße bleibt oder eine
Dimension im Datenmodell wird. Bis zur Entscheidung wird von **einem festen,
transportablen Standort** ausgegangen: Koordinaten und Höhe in der
Konfiguration, Kalibrierung und Modell gelten je Aufstellung.
