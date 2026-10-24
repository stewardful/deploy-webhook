# deploy-delivery：可选的 Webhook 验签转发器

独立 Go 标准库工具：验证 GitHub Release Webhook 后，将**原始已签名请求**提交给一个固定的本机 HTTP 任务接口。无 SSH、脚本执行、安装器、数据库、消息订阅或后台版本轮询。

**deploy-platform 已直接接收 Webhook，不需要安装本工具。** 平台改动见 [PR #5](https://github.com/stewardful/deploy-platform/pull/5)。最简单的部署是 GitHub → 平台 Webhook → 现有升级任务。本仓库适合其他需要独立接入层的项目；不把它加到平台安装包或运行依赖中。

## 行为

```text
GitHub release/published
  → HTTPS /hooks/github/releases
  → HMAC 验签、固定源仓库、发布格式校验
  → POST 原始正文和签名至固定 loopback Webhook 接口
  → 目标可靠入队后返回 200
```

工具默认监听 `127.0.0.1:8090`，目标默认为 `http://127.0.0.1:8088/hooks/github/releases`。只接受字面量 loopback IP 和固定 Webhook 路径，禁止跳转、代理、查询参数及请求中给定的目标。目标接口应再次验证签名，并承担本地权限策略、持久入队和去重；本工具不保存通知状态，不重试升级，也不把 HTTP 成功当成安装成功。

源仓库默认 `stewardful/deploy-platform`。唯一凭据是 GitHub Webhook HMAC 密钥：GitHub、转发器和目标接口使用同一个值，不需要 PAT、GitHub App、Actions 自定义 Secret 或服务器 SSH 私钥。若目标是平台，建议省去本转发器，直接代理平台已有接口。

## 构建与测试

```bash
make check test smoke
./bin/release-notify --help
```

需要 Go 1.25+，零第三方 Go 依赖。测试包含 race detector、正文验签、来源/标签/大小限制、固定目标校验、上游拒绝不返回成功及实际非 root 进程转发。CI 测试后构建 Linux amd64/arm64 二进制与 SHA256，保留 Actions Artifacts 14 天；私有仓库下载需要登录，CI 不部署服务器。

## 可选部署（不是平台必需步骤）

目标 HTTP 接口必须已安装并运行。本工具只监听本机，公网使用 Caddy 代理：

```bash
sudo install -m 0755 release-notify /usr/local/bin/release-notify
sudo install -d -m 0700 /etc/deploy-delivery
# 仅首次生成；已有密钥不要覆盖。将相同值配置在 GitHub 与目标接口。
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/deploy-delivery/webhook.secret'
sudo install -m 0644 deploy/release-notify.service /etc/systemd/system/release-notify.service
sudo systemctl daemon-reload
sudo systemctl enable --now release-notify
```

使用可信渠道获得二进制和摘要并核对。systemd 使用 DynamicUser、LoadCredential，无 sudo、Docker Socket 或平台状态目录权限，不需要可写状态目录。普通密钥文件要求 0600/0400；仅在 systemd 管理器提供的凭据目录接受只读 0440 文件。示例限制 64 MiB 内存、25% 单核 CPU，不代表生产容量测试。

[Caddy 示例](deploy/Caddyfile.example) 仅转发 HTTPS POST `/hooks/github/releases` 到本机 8090；管理和任务 API 不公开。若平台直接接收，将反向代理目标改为 8088，并跳过本工具的安装。

GitHub 仓库 Settings → Webhooks：

- Payload URL：`https://通知域名/hooks/github/releases`。
- Content type：application/json。
- Secret：同一份私有随机密钥；启用 SSL verification。
- 事件只选择 Releases。

只有 `/hooks/github/releases` 路由，方法只允许 POST。原始正文 HMAC-SHA256 常量时间验签；只接受配置仓库的 release/published，dev 使用固定 `dev-正整数` 预发布，正式版使用 `vX.Y.Z`。忽略草稿、其他动作及滚动 dev-latest，不采用命令、脚本地址或安装路径。正文最多 256 KiB、并发最多 8，目标超时 5 秒。密钥和原始正文不写日志。

成功转交返回 200；签名正确的 ping/无关事件返回 204；401 表示验签失败；413 表示正文超限；422 表示来源/发布元数据不合规；503 表示忙碌、配置或目标提交失败。目标返回非 200 均不宣称成功。

Webhook 可能重复或漏送：目标必须持久去重；失败投递可在 GitHub Recent Deliveries 手动 Redeliver。这里没有后台重投、版本查询、SSE 或旧接口兼容。实际升级下载、检查、备份、验证与回退完全由目标现有流程负责。平台 dev 默认关闭自动升级，正式版需要手动确认。

本改动未合并、未部署用户服务器、未配置真实公网 Webhook。本地签名请求与目标模拟不能代替公网 HTTPS、GitHub 实际投递验证。
