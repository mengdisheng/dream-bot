# Dream Bot · 梦 Bot

**대화를 중심으로 사용하는 자체 호스팅 개인 에이전트. 필요할 때 실행 환경을 만들고 유휴 상태에서 삭제합니다.**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

Dream Bot은 대화, 파일, 브라우저 프로필을 사용자 서버에 보관합니다. 작은 호스트 게이트웨이는 상주하며 작업용 Docker 컨테이너는 최대 하나만 생성합니다. 안전하게 회수할 수 있는 상태에서 300초 동안 유휴하면 실제로 삭제합니다. 브라우저 보기·직접 제어와 파일 미리보기는 대화 안의 패널에서 열립니다.

## 기능

- 지속되는 대화와 실행 기록, 명시적인 승인, 불확실한 외부 작업 결과 확인.
- Chromium, 수동 인계, 파일 처리, 메모리, 일정 및 암호화 백업.
- 설정 가능한 Chat Completions / Responses 호환 API. Token/Coding Plan은 실제 제공자의 허용 범위 안에서만 사용합니다. 프로토콜 호환성이 요금제 사용 권한을 보장하지 않습니다.
- 자동 조회, SSE, 읽기 전용 보기는 컨테이너를 깨우거나 수명을 연장하지 않습니다. 데이터 보존은 메모리 스냅샷 복원이 아닙니다.

자체 작성한 프런트엔드, 백엔드, 호스트 관리, 샌드박스, 배포 및 테스트는 모두 MIT로 공개됩니다. 별도의 비공개 제어 서비스는 없습니다. 모델 서비스 요금과 타사 라이선스는 별도로 적용됩니다.

## 설치와 상태

Linux amd64, systemd/cgroup v2, Docker 및 AppArmor·Landlock·seccomp·Chromium 사용자 네임스페이스를 지원하는 커널이 필요합니다. 메모리는 4 GB 이상을 권장합니다. Go, Python, TypeScript/React, SQLite를 사용합니다.

실험적 소스 릴리스입니다. 독립적인 새 설치, ARM64, 전체 제공자 및 장애·복구 검증은 아직 완료되지 않았습니다. 앱 UI는 주로 중국어이며 이 문서는 번역된 README입니다. 공용 초기 비밀번호나 운영 인증 정보는 포함하지 않습니다.

[설치](../INSTALL.md) · [아키텍처](../ARCHITECTURE.md) · [모델](../MODELS.md) · [검증 범위](../STATUS.md)

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
