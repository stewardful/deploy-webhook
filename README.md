# deploy-webhook

轻量 GitHub Release Webhook 执行器：验签、持久化、去重，然后由无 HTTP 入口的后台 worker 固定调用服务器上的 cloudctl。

**本仓库不提供 cloudctl，不下载或安装平台包，也不实现升级/回退。** cloudctl 是 [deploy-platform monorepo](https://github.com/stewardful/deploy-platform) 的独立工具。平台 Web/Go/Rust Agent 管理项目；本服务只负责“收到已就绪的发布 → 触发固定安装”。

```text
合并 dev → CI 测试、构建并发布固定 dev-RUN_ID
  → GitHub release/published Webhook
  → DynamicUser HTTP 验签、原始通知落盘并 fsync
  → systemd.path 唤醒 root oneshot，再验签与去重
  → /usr/local/bin/cloudctl install --channel dev --repository REPO --run-id ID --commit SHA --automatic
  → cloudctl 自己校验、首次安装 / 同构建跳过 / 保护升级或回退
```

不在刚收到 push 时拉 dev-latest：那时新包尚未构建。只接收已发布的固定 dev 构建，携带完整 target_commitish SHA；CLI 再核对发布清单与实际安装包提交，避免装到上一次构建。稳定发布只记录待人工处理，绝不自动执行安装。

## 最小配置

- 默认源仓库 stewardful/deploy-platform，HTTP 为 127.0.0.1:8090。
- 唯一凭据：GitHub 仓库 Webhooks 与 `/etc/deploy-webhook/webhook.secret` 中相同的 HMAC 密钥。
- 需要独立可维护的 HTTPS 入口，只代理 POST `/hooks/github/releases`。示例见 [Caddyfile](deploy/Caddyfile.example)，也可用云 HTTPS 网关或已有代理；不要覆盖平台自动生成的业务代理配置。本版不自动集成平台动态代理或申请公网证书。
- 无 SSH、PAT、GitHub App、Actions 自定义 Secret、跨仓库触发或私有 SDK。CI 从不连接服务器。

GitHub Webhook 选择 Releases、application/json、启用 SSL verification。使用完整通知域名 URL。dev-latest、草稿、非 published 事件不入执行队列；伪造签名返回 401，错误来源/元数据返回 422，正文超过 256 KiB 返回 413，持久化失败返回 503。

## 首次准备（操作者在服务器执行）

先从 deploy-platform 的已发布入口独立安装 cloudctl（该版 CLI 改动合并发布后可用）：

```bash
bash -o pipefail -c 'curl -fsSL --connect-timeout 20 --max-time 120 https://github.com/stewardful/deploy-platform/releases/download/dev-latest/install.sh | sudo bash -s -- --cli-only'
sudo cloudctl version
# 可先手动安装；无需启动本 Webhook 服务
sudo cloudctl install --channel dev
```

cloudctl 自身位于 /opt/cloudctl/releases，不从平台 current 加载；平台尚未安装或控制端故障时，安装/更新任务查询仍能工作。应用管理命令需要 Rust Agent。

本工具使用 Go 1.25+、标准库，无第三方依赖。在开发机构建：

```bash
make check test smoke build
```

CI 在最低 Go/stable 上测试，构建 Linux amd64/arm64 二进制和 SHA256，存入 Actions Artifacts；私有仓库取产物需要 GitHub 登录，用户应核对可信摘要。无需在 2 核 2 GiB 服务器编译。

取得匹配架构的二进制和 deploy/ 目录后，在服务器安装本服务：

```bash
sudo bash deploy/install.sh ./deploy-webhook
curl --fail http://127.0.0.1:8090/healthz
sudo deploy-webhook --status
```

安装脚本只部署本服务自身、生成首次密钥和安装 systemd 单元，不执行平台安装。已有密钥不覆盖。将密钥值在自己的可信终端读出并填入 GitHub Webhook Secret，不发到聊天、仓库或日志。

自动安装由 **cloudctl 的本地开关** 控制，默认关闭：

```bash
sudo cloudctl update policy
sudo cloudctl update policy --dev-auto-upgrade on
sudo cloudctl update policy --dev-auto-upgrade off
```

开关关闭时保存 blocked；开启后下一次新发布生效，已跳过构建可由管理员手动 cloudctl install。开启不能由 Webhook 正文决定。自动请求仅允许固定 dev run + SHA；正式版在平台网页手动确认，或 `cloudctl install --channel stable --version X.Y.Z --yes`。

## 权限与可靠性

HTTP 服务使用 DynamicUser、只读系统、零 capabilities 和 64 MiB 上限，不能访问平台目录、sudo、Docker Socket 或执行命令。它只写私有 `/var/lib/deploy-webhook/inbox.json`；最多保存 dev/stable 两条原始通知，总量有界。没有 SSE、HTTP 转发、任意命令参数、轮询或重试部署的后台循环。

root worker 由 systemd.path 和启动恢复触发，没有监听端口。它重新验签、校验来源/标签/提交，执行账本位于 HTTP 无权修改的 `/var/lib/deploy-webhook-worker/status.json`。attempted 先可靠落盘；重复、旧通知、失败或进程重启不重复执行同一构建。执行中收到更高构建会合并处理，不并发安装。

CLI stdout 有上限、执行有期限，命令使用固定路径和 argv，绝不调用 shell 或执行正文的 URL/脚本。cloudctl 继续核对本地授权、公开固定清单、包摘要与来源，并复用兼容性检查、任务排空、备份、业务验证和失败回退。HTTP 200 仅表示通知可靠保存，不表示安装成功。

查询与排错：

```bash
sudo deploy-webhook --status
sudo cloudctl update status
sudo cloudctl system-logs install
sudo journalctl -u deploy-webhook -u deploy-webhook-worker -n 100 --no-pager
```

有效发布去重以构建编号为主，不靠可以更换的 delivery ID。失败不自动重试，排错后由管理员手动安装；不要删除 root 账本强制重试。服务停机漏送需 GitHub Recent Deliveries 重投，已保存通知由启动 worker 恢复。轮换 HMAC 密钥时须停服务并处理旧签名收件箱，不能保留无法验签的数据继续执行。

本仓库只维护当前命令、状态和 API 契约。部署后需单独验证公网 HTTPS、GitHub Recent Deliveries 和本机任务结果；源码测试与隔离 Ubuntu 联调不能替代真实公网验收。
