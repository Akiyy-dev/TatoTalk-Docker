# TatoTalk Docker Android 验证环境

在国内服务器运行 Docker Android 模拟器，验证 TatoTalk 的安装、手工登录和新文本消息
接收。此仓库独立于 TG-forwarder，不读取其配置，也不需要 Redis、PostgreSQL、Telegram
会话或 Bot Token。

**当前未在目标服务器实际运行容器。** Windows 开发机没有 Docker 运行环境，现阶段可
完成文件和脚本检查，模拟器启动、APK 安装、登录与收消息必须到 Linux 服务器分别验收。
本仓库尚未实现通知自动提取或转发；应用界面收到消息不能记为“已监听消息”。

## 1. 文件与默认配置

本地仓库为 `D:\TatoTalk-Docker`。可上传仓库文件，或上传 `release/TatoTalk-Docker.zip` 后解压，进入 `TatoTalk-Docker` 目录。后续 Linux
命令均在上传后的仓库根目录执行。

| 文件 | 用途 |
| --- | --- |
| `compose.yaml` | 独立 Compose 项目，服务名 `android` |
| `Dockerfile` | 基于固定 digest 的上游 Android 模拟器镜像构建 |
| `.env.example` | APK 地址、可选 SHA-256、VNC 密码与资源配置模板 |
| `scripts/probe.py` | Python 标准库脚本，通过容器内 ADB 执行检查、安装及状态查询 |
| `scripts/start.sh` | 启动前校验 VNC 密码，然后启动上游模拟器进程 |
| `tests/` | Python 标准库 unittest；启动校验测试另外调用 POSIX shell |

默认 Android guest 内存为 2048 MB、CPU 为 2 核；Docker 容器内存上限为 6 GB、CPU 上限
为 2。guest 内存不是容器总占用，模拟器和图形环境还有额外开销；容器上限也不是已占用
内存。对于 16 GB 服务器，先按此预算做 PoC，并结合服务器上的其他进程观察实际占用。

上游压缩镜像层约 3 GB，下载、解压、构建与 Android 数据还会占用更多空间。建议先给
验证环境预留 **20 GB 以上可用磁盘**；这是规划估算，应以实际镜像和数据增长为准。

noVNC 默认绑定宿主 `127.0.0.1:6090`，映射到容器 `6080`。Android 数据通过 named
volume 挂载整个 `/home/androidusr`，用于跨重启保留模拟器及账号数据。

## 2. 逐条检查服务器条件

以下命令每次只复制执行一条，先看输出再继续。

先看内存：

```bash
free -h
```

再看磁盘：

```bash
df -h
```

确认宿主架构：

```bash
uname -m
```

本方案要求输出 `x86_64`。继续确认本机 KVM 设备：

```bash
ls -l /dev/kvm
```

必须有可用的 `/dev/kvm` 字符设备。云服务器即使是 x86_64，也不一定允许嵌套虚拟化；
设备不存在时，应先确认服务器是否支持 KVM，不能直接跳过继续部署。

检查 Docker Engine：

```bash
docker version
```

检查 Compose 插件：

```bash
docker compose version
```

需要 Docker Engine 和 `docker compose` 插件。若当前用户没有 Docker 权限，按服务器
已有管理方式处理；需要 sudo 时，对后续 Docker 命令统一使用。

此仓库面向 Debian 13 做部署验证。上游快速开始以 Ubuntu 宿主为支持示例，Debian 13
的实际运行结果仍须在目标服务器验收。

## 3. 配置独立环境文件

在服务器的本仓库根目录操作。首次部署且 `.env` 尚不存在时复制：

```bash
cp .env.example .env
```

限制文件权限：

```bash
chmod 600 .env
```

编辑：

```bash
nano .env
```

必须设置 `VNC_PASSWORD`，使用**正好 8 位英文字母或数字**。这仅是 noVNC/VNC 访问密码，
不是 TatoTalk 账号密码。`scripts/start.sh` 会在启动上游进程前强制校验长度和字符范围，
不满足则拒绝启动；上游会将该值插入 shell 命令，因此不能使用标点或其他特殊字符。
已有 `.env` 时直接编辑，不要再次复制覆盖。

默认 APK 下载地址为
[TatoTalk-26100306.apk](https://downtato.ffvjei.com/android/TatoTalk-26100306.apk)。
APK URL 对应 `.env` 中的 `TATOTALK_APK_URL`；可选校验值为 `TATOTALK_APK_SHA256`。
若填写 SHA-256，应使用独立取得的可信校验值，校验不一致时安装脚本会失败。
`TATOTALK_BOOT_TIMEOUT` 默认 300 秒；`TATOTALK_EXPECTED_ABI` 默认 `arm64-v8a`，不要为了
绕过当前 APK 的架构限制而改成 x86 ABI。

检查 Compose 展开结果：

```bash
docker compose config --quiet
```

命令成功退出且没有输出，表示 Compose 配置可解析、必填变量已提供。`--quiet` 不输出
展开后的 VNC 密码；启动后用下一节的 `docker compose ps` 确认容器状态。

## 4. 构建与启动

国内服务器需要分别能访问 Docker Hub 上的上游镜像、Dockerfile 中的 Ubuntu apt 软件源，
以及 APK 下载域名 `downtato.ffvjei.com`。镜像拉取与 apt 安装发生在构建阶段；APK 只在
执行 install 时下载。任一阶段失败，应按日志确认是网络、DNS、证书还是其他错误，
不要推荐或直接替换为未经核验的镜像源。需要调整镜像来源时，先核验来源与固定 digest
对应的内容，再修改 `ANDROID_BASE_IMAGE`。

```bash
docker compose up -d --build android
```

首次构建需要拉取较大的上游镜像，所需时间取决于服务器网络。默认上游镜像通过 digest
固定；更换镜像版本后，应重新执行完整兼容性和登录持久化验收。

查看容器：

```bash
docker compose ps
```

查看近期启动日志：

```bash
docker compose logs --tail=100 android
```

检查 Android 启动状态：

```bash
docker compose exec android python3 /opt/tatotalk/probe.py check
```

脚本默认最多等待 300 秒，检查 KVM 加速、单个在线 ADB 设备、Android 启动、SDK 和
设备声明的 ABI；check 还要求默认的 `arm64-v8a`。native-bridge 属性存在本身不能证明
ARM 转译可用，设备必须声明支持所需 ABI。容器运行或 Compose 健康状态只证明部分启动
条件，不代表 check 已通过。**仅在 check 成功后继续安装**；失败时先检查日志、资源、
KVM 和 ABI 条件。

## 5. 安装 APK 与兼容性闸门

```bash
docker compose exec android python3 /opt/tatotalk/probe.py install
```

安装命令才会下载 APK，并使用 aapt 读取 APK manifest 中的实际包名与 minSDK，校验
设备兼容性后安装。包名不能假定为 `org.im.ttalk`，应以脚本确认的实际身份为准。

当前默认 APK 仅包含 `arm64-v8a`，minSDK 为 24。x86_64 宿主机和 KVM 可用，只说明
模拟器运行条件满足；Android guest 还必须具有可用的 ARM64 执行或转译能力。
check 会先按默认 ARM64 需求检查设备，install 再根据下载 APK 的实际 ABI 与 minSDK
重新核对。**check 通过仍不等于该 APK 已通过实际运行验收。** install 若报告兼容性
不满足，必须先解决该问题，不能跳过检查强行安装。

install 成功安装后会自动启动实际包名对应的应用，并在约 3 秒后检查运行进程 PID。
这只证明应用初始进程存在，不验证账号登录、稳定运行或收消息。

查看安装结果及设备状态：

```bash
docker compose exec android python3 /opt/tatotalk/probe.py status
```

status 输出设备 serial、启动状态、实际包名、是否已安装及是否有运行进程，不输出
应用版本。记录这些结果，并在界面确认应用能正常操作、不会持续崩溃；账号登录仍须
手工验收。

## 6. SSH 隧道与手工登录

在自己的电脑上执行以下命令，将 `your_user` 和 `your_server` 换成服务器 SSH
用户与地址，并保持该终端运行：

```bash
ssh -N -L 6090:127.0.0.1:6090 your_user@your_server
```

本地浏览器打开 [http://127.0.0.1:6090](http://127.0.0.1:6090)，输入配置的 VNC 密码。
SSH 隧道将本地连接送到服务器的 noVNC，无需将服务器端口改为公网监听。

如果本地 6090 已被占用，只修改隧道本地端口：

```bash
ssh -N -L 16090:127.0.0.1:6090 your_user@your_server
```

此时打开 [http://127.0.0.1:16090](http://127.0.0.1:16090)。

在 Android 界面打开 TatoTalk，使用普通成员账号手工登录，确认目标群或会话可见，
并按应用提示设置通知权限。账号验证码与登录步骤由用户在界面完成。

## 7. 分阶段验收

| 阶段 | 验收内容 | 通过条件 |
| --- | --- | --- |
| 部署 | check、noVNC、Android 界面 | 启动检查通过，界面可操作 |
| 安装 | install、status、打开应用 | 实际包名已确认，应用可启动且不持续崩溃 |
| 登录 | 普通成员账号与目标会话 | 可登录，能看到目标会话 |
| 新消息 | 启动后到达的测试文本 | 应用界面收到新消息，逐项核对内容 |
| 后台通知 | 应用退到后台后的 Android 通知 | 核对有无通知、正文是否完整或被合并 |
| 持久化 | 容器重启后的登录和会话 | 无需重新登录，仍能收到新的测试消息 |

至少验收以下三种新文本；同时观察前台内容与后台通知：

1. 一条短文本：来源和正文是否一致。
2. 一条长文本：展开通知后正文是否仍被截断，是否只有“收到新消息”等摘要。
3. 连续多条不同文本：是否分别到达，后台通知是否合并或覆盖。

然后验证重启保留登录：

```bash
docker compose restart android
```

重新确认系统启动：

```bash
docker compose exec android python3 /opt/tatotalk/probe.py check
```

再查看状态：

```bash
docker compose exec android python3 /opt/tatotalk/probe.py status
```

在界面确认账号仍登录，重复一次后台新消息测试。应分别记录“界面收到新文本”和
“后台通知提供完整正文”的结果。本仓库没有实现自动 capture、通知正文提取或跨服务器
转发；后续采集功能需单独开发，并以这里的实测结果为依据。

可在运行中观察容器实际资源占用：

```bash
docker stats --no-stream
```

## 8. 停止与数据保留

临时停止：

```bash
docker compose stop android
```

移除此项目的容器和网络，同时保留 named volume：

```bash
docker compose down
```

以后在同一项目目录执行 `docker compose up -d android`，会复用已保留的模拟器数据。
保持 Compose 项目名一致，避免误连到一个新的空 volume。

日常停止不要附加 `-v`。**down -v 会删除 named volume 和其中的 Android、账号数据**；
只有明确要清空环境且确认数据可丢弃时才考虑执行。

## 9. 脚本本地测试

Windows 开发机已完成 36 项测试（包括 Git Bash 的启动校验）和 Compose v5.6.0 配置解析，未运行 Docker 容器。

测试不需要 Docker 或下载 APK，使用 Python 标准库 unittest，不需要第三方 Python 库。
其中 4 项启动校验测试还需 POSIX shell；Linux 通常提供 `/bin/sh`，Windows 没有可用的
`sh` 时这 4 项会自动跳过。可通过 `TATOTALK_TEST_SHELL` 指定已有的 POSIX shell 路径。

```bash
python3 -m unittest discover -s tests -v
```

脚本测试通过不能代替 Linux 上的 Docker、Android、ARM64 兼容性与 TatoTalk 登录验收。

## 10. 主源文档

- [Docker-Android 上游仓库与运行说明](https://github.com/budtmo/docker-android)
- [Docker-Android 持久化说明](https://github.com/budtmo/docker-android#persisting-data)
- [Android 11 模拟器的 ARM 应用支持说明](https://android-developers.googleblog.com/2020/03/run-arm-apps-on-android-emulator.html)
- [默认 TatoTalk 官方 APK 下载地址](https://downtato.ffvjei.com/android/TatoTalk-26100306.apk)
