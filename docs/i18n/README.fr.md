# Dream Bot · 梦 Bot

**Un agent personnel auto-hébergé, organisé autour des conversations et d’un environnement d’exécution jetable.**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

Dream Bot conserve conversations, fichiers et profil du navigateur sur votre serveur. Une petite passerelle reste active ; un seul conteneur Docker au maximum est créé pour travailler, puis supprimé après 300 secondes d’inactivité sûre. Le navigateur, la prise en main et l’aperçu des fichiers s’ouvrent dans la conversation.

## Fonctions

- Conversations et exécutions persistantes, autorisations explicites et rapprochement des effets externes incertains.
- Chromium réel, fichiers, mémoire confirmée, planification et sauvegardes chiffrées.
- API compatibles Chat Completions ou Responses, avec fournisseurs chinois configurables. Les offres Token/Coding Plan exigent un usage réellement autorisé ; la compatibilité du protocole ne prouve aucun droit contractuel.
- Les lectures automatiques, SSE et la consultation seule ne réveillent pas le conteneur et ne prolongent pas sa durée de vie. La persistance des données n’est pas une restauration de mémoire vive.

Tout le code propre au projet —interface, serveur, gestion de l’hôte, isolation, déploiement et tests— est publié sous MIT. Aucun plan de contrôle propriétaire distinct n’est nécessaire. Les services de modèles peuvent être payants et les dépendances conservent leurs licences.

## Installation et état

Cible actuelle : Linux amd64, systemd/cgroup v2, Docker, AppArmor, Landlock, seccomp et espaces de noms Chromium. Prévoir au moins 4 Go de RAM. Technologies : Go, Python, TypeScript/React et SQLite.

Publication expérimentale des sources. L’installation neuve indépendante, ARM64 et les matrices complètes de fournisseurs, pannes et restauration restent à valider. L’interface est principalement chinoise ; ces traductions concernent la documentation. Aucun mot de passe initial partagé ni identifiant de production n’est fourni.

[Installation](../INSTALL.md) · [Architecture](../ARCHITECTURE.md) · [Modèles](../MODELS.md) · [Validation](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
