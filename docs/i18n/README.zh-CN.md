# Dream Bot · 梦 Bot

**可自托管的个人 Agent：以会话为中心，按需创建容器，空闲后销毁。**

[English](../../README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

梦 Bot 将会话、任务、文件和浏览器 profile 保存在自己的服务器。宿主入口常驻；真正执行工作的 Docker 容器最多一个，用时启动，满足回收条件后空闲 300 秒删除。浏览器查看、人工接管和文件预览直接出现在会话侧栏。

## 能力

- 持久会话与续问，每次执行独立记录，保留审批、检查点与审计。
- 文件读取、资料研究、生成可下载成果；只给工具明确授予的文件权限。
- 真实 Chromium，支持人工接管处理登录与验证，交还后需明确继续任务。
- Chat Completions / Responses 兼容接口；国产标准 API 与符合用途范围的 Token/Coding Plan 分别配置。协议连通不代表套餐授权，不自动切换计费入口。
- 记忆、排程、设备、通知与加密备份；高级选项收起，重要权限确认仍明确显示。

## 完整开源与架构

前端、后端、Go 宿主管理、沙箱、安装脚本和测试均在仓库中，原创代码采用 MIT，可修改、商用和自行部署，没有独立闭源控制面或收费版本。模型服务仍由所选供应商按其条款计费。noVNC、字体与其他依赖保留各自许可证。

宿主网关管理身份、调度和生命周期；受限 helper 独占 Docker 权限。容器内 API、Worker、浏览器、执行器分属不同 UID。API 独占数据库和模型密钥；生成代码使用 Landlock/seccomp，Chromium 保持原生沙箱。

自动轮询、SSE 与只读观看不唤醒、不续命。最后一位观看者断开 30 秒后停 x11vnc，浏览器页面按原有生命周期保留。跨会话创建新页面并移除旧历史，owner Cookie 仍共享；这是单用户应用，不是多租户隔离平台。

## 安装与状态

建议 Linux amd64、systemd/cgroup v2、Docker 和至少 4 GB 内存；必须支持 AppArmor、Landlock、seccomp 和 Chromium 用户命名空间。技术栈为 Go、Python 3.12+、TypeScript/React、SQL/SQLite、CSS、Dockerfile、Shell/Make 与可选 PowerShell。

这是实验性源码发布。已有 amd64 工程验证，但公开源码的全新机器安装、ARM64、完整供应商矩阵、MiniMax 自主浏览器任务及全部故障/回退矩阵仍有待验收。当前应用界面以中文为主；其他语言 README 是文档翻译。

每次安装自行生成一次性初始化凭据，由你选择用户名、密码和 TOTP。仓库不提供默认密码，不含作者服务器凭据、用户记录、截图或生产密钥。不要将这些数据上传 issue。

阅读 [中文安装指南](../INSTALL.zh-CN.md)、[模型与套餐](../MODELS.md)、[详细架构和流程图](../ARCHITECTURE.md)、[接口契约](../CONTRACT.md) 与 [验证边界](../STATUS.md)。

![Dream Bot architecture](../assets/architecture.svg)

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
```

[MIT](../../LICENSE) · [Third-party notices](../../THIRD_PARTY_NOTICES.md) · [Security](../../SECURITY.md)
