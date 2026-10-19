# deploy-delivery

独立安装在服务器上的交付工具：项目 CI 只通过 SSH 调用固定命令，服务器通过 HTTPS 拉取已构建的安装包。Python 3 标准库与 Bash 实现，无常驻守护进程、无平台轮询、无需 GitHub App、Docker Engine 或服务器编译环境。当前适配 deploy-platform 的 `.run --check` / `.run --upgrade` 安装协议。

## 两个仓库的职责

- deploy-delivery：独立测试、构建体积很小的服务器工具离线包，人工首次安装或更新工具。
- deploy-platform：测试、构建平台更新包；dev CI 经 SSH 调用服务器已安装的工具，等待部署状态。两个仓库不互相触发、不自动创建 PR。
- 服务器工具：绑定允许的项目仓库与架构，验证请求和下载内容，启动独立 systemd worker，调用平台安装器。平台安装器继续负责兼容性检查、任务排空、备份、迁移、业务验证和失败回退。交付成功只在安装器成功提交后报告。

## 首次安装（不由项目 CI 自动执行）

先决条件：Ubuntu 24.04 / 26.04 LTS、amd64 / arm64、systemd、cgroup v2、Python 3、系统 CA 证书、运维账号可非交互 sudo。部署 preflight 要求 `/opt` 所在文件系统至少有 2 GiB 可用空间，这是磁盘空间，不是要求新增 2 GiB 内存。SSH 账号是有部署权限的运维账号；本工具不自动配置 sudoers。

**本地电脑**：从辅助仓库某个成功的 main CI 下载 `deploy-delivery-server` 产物。将 RUN_ID 替换为 Actions 中成功运行的编号；本机 gh 需有读取该私有仓库的权限。

```bash
gh run download RUN_ID --repo stewardful/deploy-delivery --name deploy-delivery-server --dir ./delivery-package
```

也可以从同一个已确认提交在本地构建：

```bash
python3 -B scripts/package.py --output dist
```

产物为 `deploy-delivery-0.1.0.tar.gz` 和 `.tar.gz.sha256`，amd64/arm64 共用，不包含平台安装包或基础运行时。首次安装只需上传这两个小文件；不要上传服务器 SSH 私钥或 GitHub Token。以下示例在**本地电脑**执行，替换服务器地址与 SSH 端口：

```bash
ssh -p 22 lighthouse@SERVER 'install -d -m 700 ~/deploy-delivery-install'
scp -P 22 ./delivery-package/deploy-delivery-0.1.0.tar.gz ./delivery-package/deploy-delivery-0.1.0.tar.gz.sha256 lighthouse@SERVER:deploy-delivery-install/
```

**服务器**：校验同一可信 CI 运行下载的摘要，再解包安装。x86_64 使用 amd64，aarch64 使用 arm64。检查值用于完整性校验，首次包的来源仍需信任。

```bash
cd ~/deploy-delivery-install
sha256sum -c deploy-delivery-0.1.0.tar.gz.sha256
tar -xzf deploy-delivery-0.1.0.tar.gz
sudo /usr/bin/python3 -I -B deploy-delivery-0.1.0/scripts/install.py --repository stewardful/deploy-platform --arch amd64
sudo /usr/local/bin/deploy-delivery version
sudo /usr/local/bin/deploy-delivery preflight amd64
```

成功信号：摘要检查 OK；安装器输出 PASS；version 为 `{"protocol": 1, "version": "0.1.0"}`；preflight 返回 `phase=ready`、预期 repository 和 arch。安装不部署任何平台或业务应用，不修改平台数据、containerd、Caddy 或应用卷。

## 项目 CI 接入

保留原有五个 SSH 配置和项目仓库自身的 `GITHUB_TOKEN`（contents/actions read）。运行 CI 端客户端：

```bash
bash scripts/deploy-test.sh
```

客户端固定调用 `/usr/local/bin/deploy-delivery`：

1. `preflight ARCH`：校验安装情况、协议、仓库策略、架构、系统和磁盘；失败时不签发下载 URL，也不会远程自动安装工具。
2. `start`：仅从 stdin 接受 `{protocol, request, ticket}` JSON（上限 24 KiB）。不允许上传脚本、任意命令、工作目录或安装参数。启动任务后立即返回。
3. `status RUN_ID ATTEMPT`：返回过滤后的 JSON 状态；`logs RUN_ID ATTEMPT`：返回私有安装日志最后 100 行。
4. `refresh`：仅在下载等待授权时，使用原请求与递增 revision 刷新临时 URL；最多三次刷新，不改变产物身份。

GitHub API Token 仅留在 runner。发送到服务器的只有固定身份元数据和短时 HTTPS 下载授权，不在命令参数或控制台输出授权。下载具有大小、时间、HTTPS 域名、摘要、封闭 ZIP 文件列表校验，支持 Range 续传。systemd worker 使用接受任务时的绝对版本路径，独立持锁，SSH 断开不取消下载和升级。runner 已断开且 URL 过期时无法刷新，会在期限内失败。

`protocol=1` 是客户端与服务器的兼容边界，工具无需每次随项目更新；不兼容或未安装时项目 CI 明确失败。平台可以保留经审查、按完整 SHA 固定的 CI 客户端/测试快照，但部署请求不会从快照向服务器发送执行代码，也不会更新工具自身。

## 目录、升级与排错

- `/usr/local/bin/deploy-delivery`：固定入口，使用隔离模式 Python，忽略调用者 PYTHONPATH。
- `/opt/deploy-delivery/releases/版本-内容摘要/scripts/`：root 拥有的不可变服务端代码；`current` 原子切换版本。
- `/etc/deploy-delivery/config.json`：root 私有，绑定 protocol、repository、arch。重复安装不得静默更换绑定。
- `/var/lib/deploy-delivery/jobs/RUN_ID-ATTEMPT/`：root 私有请求、进度和诊断。临时下载授权在成功、失败、启动失败或并发拒绝后删除；进程被强制终止/服务器断电可能留下文件，需要运维检查。
- `deploy-platform-ci-RUN_ID-ATTEMPT.service`：一次性任务；没有工具常驻服务。沿用全局 worker 锁，拒绝并行部署。

工具自身更新：取得新的可信工具包，执行同一套校验和安装命令。先验证候选模块、协议和完整性，成功后原子切换 `current`；候选失败保留旧入口。旧版本和任务目录保留，正在运行的任务继续使用旧版本代码；重装不会删除任务、改写项目配置或更新平台。目录只允许 root 修改，拒绝符号链接或不安全权限。

查看一次任务（服务器执行）：

```bash
sudo /usr/local/bin/deploy-delivery status RUN_ID ATTEMPT
sudo /usr/local/bin/deploy-delivery logs RUN_ID ATTEMPT
sudo journalctl -u deploy-platform-ci-RUN_ID-ATTEMPT --no-pager
```

任务重试使用新的 ATTEMPT；同一编号不覆盖已有任务。服务器重启不是断线：瞬态 worker 不会在重启后自行重试，须先检查平台安装事务与任务结果，再发新请求。不要把失败误报为已回退成功，具体回退状态由平台安装器诊断确认。

工具不自动清理旧版本或失败产物。只在任务已终止、确认无需诊断/恢复后清理对应 job，清理旧工具版本前确认无运行任务引用它；不要删除活动目录或 `/var/lib/deploy-platform`、应用卷。私有日志可能包含业务运维信息，只提供给有部署权限的人员。

## 测试

```bash
python3 -B -m unittest discover -s tests -v
bash -n scripts/*.sh
shellcheck scripts/*.sh
python3 -B scripts/package.py --output dist
```

单元测试不需要云凭据；覆盖 JSON-only 协议、仓库/架构策略、旧请求拒绝、凭据清理、固定 worker 路径、包校验和 SSH 传输边界。完整隔离 Ubuntu/systemd/真实 SSH + TLS 验证由 deploy-platform 负责，包括独立安装、重复安装、坏候选保留旧工具、运行中更新工具、断线续跑、并发拒绝、损坏归档和 URL 过期续传。不会连接用户服务器。
