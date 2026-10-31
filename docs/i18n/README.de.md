# Dream Bot · 梦 Bot

**Ein selbst gehosteter persönlicher Agent mit dauerhaft gespeicherten Gesprächen und einer kurzlebigen Laufzeit.**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

Dream Bot speichert Gespräche, Dateien und das Browserprofil auf deinem Server. Ein kleines Host-Gateway bleibt aktiv. Für Aufgaben wird höchstens ein Docker-Container erstellt und nach 300 Sekunden sicherem Leerlauf gelöscht. Browseransicht, manuelle Übernahme und Dateivorschau öffnen sich direkt im Gespräch.

## Funktionen

- Dauerhafte Gespräche und Ausführungsprotokolle, ausdrückliche Freigaben und Klärung ungewisser externer Wirkungen.
- Echter Chromium-Browser, Dateien, bestätigte Erinnerungen, Zeitpläne und verschlüsselte Sicherungen.
- Konfigurierbare Chat-Completions-/Responses-kompatible APIs einschließlich chinesischer Anbieter. Token-/Coding-Pläne erfordern die passende Nutzungserlaubnis; Protokollkompatibilität beweist keine Berechtigung.
- Automatische Lesezugriffe, SSE und reine Browseransicht wecken die Laufzeit nicht auf und halten sie nicht am Leben. Persistente Daten sind kein Arbeitsspeicher-Snapshot.

Der gesamte eigene Code für Oberfläche, Backend, Host-Verwaltung, Sandbox, Bereitstellung und Tests steht unter MIT. Es gibt keine separate proprietäre Steuerungsebene. Modellanbieter können Gebühren verlangen; Fremdkomponenten behalten ihre Lizenzen.

## Installation und Stand

Zielplattform: Linux amd64, systemd/cgroup v2, Docker sowie AppArmor, Landlock, seccomp und Chromium-Benutzernamensräume. Mindestens 4 GB RAM werden empfohlen. Stack: Go, Python, TypeScript/React und SQLite.

Experimentelle Quellcodeveröffentlichung. Eine unabhängige Neuinstallation, ARM64 und vollständige Anbieter-, Fehler- und Wiederherstellungsmatrizen sind noch offen. Die Oberfläche ist hauptsächlich chinesisch; die Übersetzungen betreffen die Dokumentation. Keine gemeinsamen Startpasswörter oder Produktionszugangsdaten sind enthalten.

[Installation](../INSTALL.md) · [Architektur](../ARCHITECTURE.md) · [Modelle](../MODELS.md) · [Validierungsumfang](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
