# Linux amd64 安装指南

这是实验性源码版本。运行中的 amd64 实现已有工程验证，但公开安装流程仍需独立的全新机器验收；不要把已有实例的成功视为任意服务器都能一键部署。你不需要作者的服务器账号、密码或模型密钥。

## 环境

- Linux amd64、systemd、cgroup v2、Docker Engine、AppArmor，内核支持 Landlock、seccomp 和 Chromium 非特权用户命名空间。已验证的主机系列为 Ubuntu 24.04。
- 建议至少 4 GB 内存，并为宿主与其他业务保留空间。源码构建可能比空闲运行消耗更多内存；重型构建和备份维护应在项目临时容器停止时进行。
- Git、Make、Python 3.12+、Node.js 24 LTS、npm、Go 1.26+、OpenSSL，以及依赖源和所选模型接口的正常网络连接。
- ARM64 尚未验证。执行器使用 x86_64 系统调用规则，不能只改 `GOARCH` 就宣称支持。

## 构建

当前安装器使用固定前缀 `/opt/personal-bot`。以下命令在目标 Linux 主机执行：

```sh
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /opt/personal-bot/src
git clone https://github.com/mengdisheng/dream-bot.git /opt/personal-bot/src
cd /opt/personal-bot/src
make build-host
make build-web
docker build --pull=false -t dream-bot:local -f deploy/Dockerfile .
```

Dockerfile 下载开源依赖、Chromium，并编译校验过摘要的 SQLite。不要把模型密钥、SSH 凭据或现有用户数据库放入构建上下文。Python 依赖目前使用版本范围，不是完整的可复现锁文件；长期维护自己的分支时应审查并固定最终依赖。前端 lockfile 和 Go sums 已提供。本次发布不提供预构建镜像。

## 安装与启动

先阅读 `deploy/install-host.py`、`host/*.example.json`、`deploy/systemd/` 与沙箱策略。安装器会创建项目专属账号、目录、网络和服务，不负责重启你的其他业务；已有项目网关运行时会拒绝覆盖配置。

```sh
sudo install -d -m 0755 /opt/personal-bot/build
sudo install -m 0755 build/personal-bot-helper build/personal-bot-gateway \
  build/personal-botctl build/personal-bot-tls /opt/personal-bot/build/
IMAGE_ID="$(docker image inspect dream-bot:local --format '{{.Id}}')"
sudo python3 deploy/install-host.py --image "$IMAGE_ID"
sudo -u personal-bot-gateway /opt/personal-bot/bin/personal-botctl init
sudo systemctl enable --now personal-bot-helper.service personal-bot-gateway.service
sudo /opt/personal-bot/bin/personal-botctl doctor
sudo /opt/personal-bot/bin/personal-botctl status
```

`init` 返回本机一次性初始化凭据的文件路径。请仅在自己的服务器上私下读取，用于首次注册；没有默认用户名、密码或仓库通用凭据。

紧凑模板为临时容器 1 CPU / 2 GiB / 256 PIDs，整个项目 1.5 CPU / 2432 MiB，保守预留宿主 768 MiB。运行环境只能使用固定目录和镜像；网页不能提交任意 Docker 配置。

## SSH 隧道与 HTTPS

在你自己的电脑上，使用已核验 SSH 主机公钥的别名：

```sh
ssh -N -L 127.0.0.1:18443:127.0.0.1:8443 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o StrictHostKeyChecking=yes YOUR_VERIFIED_SSH_ALIAS
```

Windows 可运行 `deploy/start-ssh-test.ps1 -HostAlias YOUR_VERIFIED_SSH_ALIAS`。然后打开 `https://localhost:18443`。localhost 是本地隧道入口，程序实际运行在服务器。

安装器默认生成测试证书。请通过已核验的 SSH 连接核对指纹，为自己的测试站点配置信任，或换成正确签发的证书；不要全局关闭 TLS 校验。网关 `origin` 必须与浏览器地址一致。默认入口只监听回环地址，不要直接暴露内部 API。

用本机一次性凭据注册你自己的 owner 账号，设置密码、TOTP，离线保存恢复码。初始化后凭据不可重复注册。登录 Cookie、TLS 私钥、模型密钥、浏览器 profile 和数据库均属于私有数据。

## 模型与日常使用

在「设置 → 模型」添加连接，分别填写供应商、区域、模型、协议与计费入口。保存不调用模型；点击验证可能消耗套餐/API 额度。API Key 只提交到自己的设置页，不要写入聊天、源码或 GitHub issue。详见 [模型与套餐](MODELS.md)。

首页是持久会话；有真实浏览器活动时，从会话卡片打开浏览器侧栏。文件成果也在会话中预览。排程、预算、备份与诊断收在高级设置中。

主动工作按需创建最多一个临时容器。无可执行任务、人工接管等阻止条件且控制 outbox 已交接后，空闲 300 秒删除容器，独立数据保留。自动轮询、SSE 和只读观看不唤醒、不续命。删除重建不恢复运行中的 DOM 或内存。

维护前使用 `personal-botctl suspend`，检查完成后明确 `resume`。备份需要有效的 age 公钥接收方；私钥离线保管。恢复会改变权限 epoch、暂停旧排程并要求核对未结束任务。已有新用户活动后，不应把旧数据库恢复当成普通应用回退。

## 验证范围

[英文完整安装指南](INSTALL.md) 提供测试命令与隔离验收说明。[当前状态](STATUS.md) 列出尚未覆盖的门槛。工程 probe 只能按模块说明用于独立验收实例，不能直接当作生产冒烟测试。

隔离能力失败必须保持关闭。不要通过 privileged、SYS_ADMIN、unconfined、Chromium `--no-sandbox` 或普通 subprocess 回退绕过安全要求。
