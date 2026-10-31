# Dream Bot · 梦 Bot

**Um agente pessoal auto-hospedado, com conversas persistentes e ambiente de execução descartável.**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

O Dream Bot mantém conversas, arquivos e o perfil do navegador no seu servidor. Um pequeno gateway permanece ativo; no máximo um contêiner Docker é criado para trabalhar e excluído após 300 segundos de inatividade segura. O navegador, o controle manual e as prévias de arquivos aparecem dentro da conversa.

## Recursos

- Conversas e execuções persistentes, aprovações explícitas e conferência de efeitos externos incertos.
- Chromium real, arquivos, memórias confirmadas, agendamento e backups criptografados.
- APIs compatíveis com Chat Completions ou Responses, incluindo provedores chineses configuráveis. Token/Coding Plans exigem uso autorizado pelo provedor; compatibilidade de protocolo não comprova esse direito.
- Leituras automáticas, SSE e visualização somente leitura não iniciam nem mantêm o contêiner ativo. Dados persistentes não equivalem a uma imagem da memória.

Todo o código próprio —interface, backend, gestão do host, sandbox, implantação e testes— está sob MIT. Não há um plano de controle proprietário separado. Os modelos podem cobrar pelo uso e as dependências mantêm suas licenças.

## Instalação e estado

Alvo atual: Linux amd64, systemd/cgroup v2, Docker, AppArmor, Landlock, seccomp e namespaces do Chromium. Recomenda-se ao menos 4 GB de RAM. Stack: Go, Python, TypeScript/React e SQLite.

Versão experimental de código-fonte. Instalação limpa independente, ARM64 e as matrizes completas de provedores, falhas e restauração ainda precisam de validação. A interface é principalmente chinesa; as traduções são da documentação. Não há senhas iniciais compartilhadas nem credenciais de produção.

[Instalação](../INSTALL.md) · [Arquitetura](../ARCHITECTURE.md) · [Modelos](../MODELS.md) · [Validação](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
