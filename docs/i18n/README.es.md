# Dream Bot · 梦 Bot

**Un agente personal autohospedado, con conversaciones persistentes y un entorno de ejecución desechable.**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

Dream Bot guarda conversaciones, archivos y el perfil del navegador en tu servidor. Una pasarela pequeña permanece activa; se crea como máximo un contenedor Docker para trabajar y se elimina tras 300 segundos de inactividad segura. El navegador, la toma de control manual y las vistas previas se abren dentro de la conversación.

## Funciones

- Conversaciones y ejecuciones persistentes, aprobaciones explícitas y conciliación de efectos externos inciertos.
- Chromium real, archivos, memoria confirmada, tareas programadas y copias cifradas.
- API compatibles con Chat Completions o Responses, incluidos proveedores chinos configurables. Los Token/Coding Plans requieren permiso real del proveedor; la compatibilidad del protocolo no acredita ese derecho.
- Las consultas automáticas, SSE y la visualización de solo lectura no despiertan el contenedor ni prolongan su vida. Los datos persistentes no son una instantánea de memoria.

Todo el código propio —interfaz, servidor, gestión del host, aislamiento, despliegue y pruebas— se publica con MIT. No hay un plano de control propietario separado. Los modelos pueden cobrar y las dependencias conservan sus licencias.

## Instalación y estado

Objetivo actual: Linux amd64, systemd/cgroup v2, Docker y soporte de AppArmor, Landlock, seccomp y espacios de nombres de Chromium. Se recomiendan al menos 4 GB de RAM. Tecnologías: Go, Python, TypeScript/React y SQLite.

Versión experimental de código fuente. Faltan una instalación limpia independiente, ARM64 y la matriz completa de proveedores, fallos y restauración. La interfaz está principalmente en chino; estas traducciones son documentación. No se incluyen contraseñas iniciales compartidas ni credenciales de producción.

[Instalación](../INSTALL.md) · [Arquitectura](../ARCHITECTURE.md) · [Modelos](../MODELS.md) · [Alcance de validación](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
