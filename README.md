# DownloadSorter

Windows-Tool (Python + tkinter), das den Downloads-Ordner scannt, Dateien nach Art
kategorisiert und beim Aufräumen hilft. Keine Abhängigkeiten außer Python 3.10+ (getestet mit 3.13).

**Sicherheitsprinzip:** Nichts wird verschoben oder gelöscht, ohne dass vorher eine Vorschau
bestätigt wird. Löschen geht nur in den Windows-Papierkorb, nie endgültig. Jede Verschiebung
wird protokolliert und lässt sich rückgängig machen.

## Starten

```bash
python DownloadSorter.pyw
```

oder per Doppelklick auf `DownloadSorter.pyw`. Optional einen Ordner mitgeben:
`python -m downloadsorter D:\Irgendwo`.

## Funktionen

- **Scan** von `Downloads` (echter Speicherort per Windows-API, auch wenn der Ordner verschoben wurde)
  oder einem beliebigen Ordner, optional mit Unterordnern.
- **Kategorien:** Installer, Archive, Dokumente, Bilder, Videos, Musik, Torrents, Code, Sonstiges.
  Erkennung per Endung **und** Dateiinhalt (Magic Bytes): Dateien ohne Endung werden erkannt,
  falsche Endungen (z. B. ein PDF als `.txt`) werden markiert. Zip-basierte Formate (docx, jar, apk …)
  werden anhand ihres Inhalts unterschieden.
- **Übersicht:** Anzahl, Größe, Anteil und Alter je Kategorie; Altersverteilung.
- **Dateien:** filter- und sortierbare Liste mit Details. **Herkunft** (`HostUrl`/`ReferrerUrl`
  aus dem Alternate Data Stream `Zone.Identifier`), sofern Windows sie gespeichert hat.
- **Aufräumen-Vorschläge:**
  - Installer älter als N Tage (einstellbar, Standard 30)
  - abgebrochene Downloads (`.crdownload`, `.part`, `.tmp`, …), die seit über einer Stunde unverändert sind
  - Archive, deren Inhalt schon entpackt daneben liegt (Zip: ≥ 90 % der Dateien vorhanden;
    andere Archive: nicht leerer Ordner mit gleichem Namen)
  - Duplikate (SHA-256; die älteste Kopie bleibt)
- **Einsortieren** in Kategorie-Unterordner oder feste Ziele (unter *Einstellungen* je Kategorie
  festlegen), oder „Markierte verschieben nach…“. Namenskonflikte werden durch `Name (1).ext`
  gelöst, vorhandene Dateien werden nie überschrieben.
- **Protokoll / Rückgängig:** jede Verschiebe-Aktion einzeln zurücknehmbar (mit Vorschau).
  Ist am Ursprungsort inzwischen eine andere Datei, wird diese Datei übersprungen statt überschrieben.
- **Eigene Regeln** (Tab *Regeln & Automatik*): Bedingungen Kategorie, Endungen, Dateiname
  (Platzhalter wie `*forge*.jar`) und Herkunft (z. B. `forgecdn.net`) → Ziel (Unterordner oder
  fester Pfad). Die erste passende Regel gewinnt. „Regeln jetzt anwenden“ zeigt erst eine Vorschau.
- **Automatik (Hintergrundmodus):** sortiert neue Downloads selbstständig nach den Regeln
  (optional den Rest nach Kategorie).
  - Nur fertige Downloads: keine `.crdownload`/`.part`/…, einige Sekunden unverändert, von keinem
    Programm mehr gesperrt.
  - Standardmäßig nur Dateien, die nach dem Start neu kommen; vorhandene bleiben liegen.
  - Jede Verschiebung steht im Protokoll und ist rückgängig zu machen. Eine zurückgeholte Datei
    wird nicht sofort wieder wegsortiert. Gelöscht oder überschrieben wird nie.
  - Läuft im Fenster; beim Schließen kann sie ohne Fenster weiterlaufen. Optional per Verknüpfung
    im Windows-Autostart (`DownloadSorter.pyw --hintergrund`). Log: `%LOCALAPPDATA%\DownloadSorter\hintergrund.log`.
  - Es läuft immer nur eine Automatik gleichzeitig (benannter Mutex).
- **Papierkorb:** über `SHFileOperationW` mit `FOF_ALLOWUNDO`. Kann eine Datei nicht in den
  Papierkorb (z. B. Netzlaufwerk), fragt Windows nach, statt still endgültig zu löschen.

## Claude-Schnittstelle

Claude kann DownloadSorter über **ToolHub** (MCP-Server) bedienen. Grundlage sind
`cli.py` (Kommandozeile) und `toolhub.json` (Beschreibung der Befehle für ToolHub).

- Jeder Befehl gibt **JSON auf stdout** aus. Fehler: Meldung auf stderr, Exit-Code ≠ 0.
- Ohne `--folder` wird der Downloads-Ordner des Nutzers verwendet.
- Über die Schnittstelle wird **nie gelöscht**; Papierkorb nur in der Oberfläche.

| Befehl | readonly | Zweck |
|---|---|---|
| `scan` | ja | Übersicht pro Kategorie, Alter, Anzahl Duplikate/Vorschläge |
| `files` | ja | Dateien auflisten (`--category`, `--name`, `--sort`, `--limit`) |
| `suggestions` | ja | Aufräum-Vorschläge mit Begründung |
| `duplicates` | ja | Duplikat-Gruppen (SHA-256), älteste Kopie = `keep` |
| `origin` | ja | Herkunft einer Datei (`--file`): HostUrl, ReferrerUrl, Zone |
| `history` | ja | Protokoll der Verschiebungen (IDs für `undo`) |
| `rules` | ja | Eigene Regeln, Kategorie-Ziele, Zustand der Automatik |
| `sort` | nein | Einsortieren nach Kategorie, Regeln (`--mode rules`) oder in `--target`; muss mit `--category`, `--file`, `--name` oder `--all` eingegrenzt werden; `--dry-run` = Vorschau |
| `undo` | nein | Verschiebung zurücknehmen (`--id`, sonst neueste offene); `--dry-run` = Vorschau |

Beispiele:

```bash
python cli.py scan --json
```

```bash
python cli.py sort --category Installer --dry-run
```

ToolHub führt schreibende Befehle ohne Bestätigung nur mit `--dry-run` aus. Verschiebungen über
die Schnittstelle landen im selben Protokoll wie die der Oberfläche (Label „Claude: …“) und lassen
sich auch dort rückgängig machen.

## Daten

Einstellungen und Protokoll liegen in `%LOCALAPPDATA%\DownloadSorter\`
(`config.json`, `protokoll.json`). Über die Umgebungsvariable `DOWNLOADSORTER_HOME`
lässt sich der Ort ändern (nutzen die Tests).

## Tests

```bash
python -m unittest -v
```

Die Tests arbeiten ausschließlich in temporären Testordnern (Kategorisierung, Herkunft, Duplikate,
Vorschläge, Sortieren + Rückgängig, kein Überschreiben, Regeln, Automatik mit wachsenden und
gesperrten Dateien, Einzelinstanz, jeder Befehl aus `toolhub.json` liefert gültiges JSON). Der Papierkorb-Test legt eine 3-Byte-Testdatei
`unbekannt.xyz` in den echten Papierkorb.

## Aufbau

| Datei | Inhalt |
|---|---|
| `downloadsorter/categories.py` | Kategorien, Endungen, Magic-Byte-Erkennung |
| `downloadsorter/scanner.py` | Scan, Zone.Identifier, Duplikate, Aufräum-Vorschläge |
| `downloadsorter/actions.py` | Einstellungen, Verschiebe-Vorschau, Protokoll/Rückgängig, Papierkorb |
| `downloadsorter/rules.py` | Eigene Sortierregeln |
| `downloadsorter/watcher.py` | Automatik/Hintergrundmodus, Einzelinstanz, Autostart |
| `downloadsorter/gui.py` | tkinter-Oberfläche |
| `cli.py`, `toolhub.json` | Claude-Schnittstelle (JSON-Kommandozeile für ToolHub) |

## Ideen für später

- Tray-Symbol für den Hintergrundmodus (bräuchte eine Zusatzbibliothek wie `pystray`).
- Windows-Benachrichtigung, wenn die Automatik etwas einsortiert hat.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
