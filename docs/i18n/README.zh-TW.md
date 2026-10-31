# Dream Bot · 梦 Bot

**可自行託管的個人 Agent：以對話為中心，按需建立容器，閒置後刪除。**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

夢 Bot 將對話、檔案與瀏覽器設定檔保存在你的伺服器。小型宿主入口常駐；最多一個臨時 Docker 容器處理工作，安全閒置 300 秒後實際刪除。瀏覽器與檔案預覽在對話側欄開啟。

## 功能與邊界

- 持久對話、個別執行紀錄、明確的工具批准與不確定結果核對。
- 真實 Chromium、人工接管、檔案處理、記憶、排程及加密備份。
- 支援可配置的 Chat Completions／Responses 相容介面與符合供應商用途條款的國產 Token／Coding Plan。連線成功不代表具備方案權益，也不會自動切換計費入口。
- 自動讀取、SSE 與唯讀觀看不喚醒或延長容器壽命；資料獨立保存，並非記憶體快照。

原創前端、後端、宿主管理、沙箱、部署與測試均以 MIT 開源，沒有另行封閉的控制面。模型服務可能收費；第三方元件維持各自授權。

## 安裝與狀態

使用 Linux amd64、systemd／cgroup v2、Docker，以及具備 AppArmor、Landlock、seccomp 與 Chromium 使用者命名空間的核心；建議至少 4 GB 記憶體。技術棧包括 Go、Python、TypeScript／React、SQLite、CSS 與 Shell。

這是實驗性原始碼版本。獨立全新安裝、ARM64、完整模型供應商矩陣及部分故障／還原驗收仍待完成。應用介面目前以中文為主；其他 README 為文件翻譯。每次安裝都自行建立初始化憑據與帳戶；不提供通用密碼，不包含私人資料。

閱讀 [安裝指南](../INSTALL.md)、[架構](../ARCHITECTURE.md)、[模型](../MODELS.md) 與 [驗證範圍](../STATUS.md)。

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
