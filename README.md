# deploy-delivery：最小发布通知 API

独立、可复用的 Go 服务：接收 GitHub Release Webhook，验签并持久化最新发布消息，再通过本机 HTTP/SSE 通知客户端。适用于现有 2 核 2 GiB Ubuntu 节点，不需要额外服务器、数据库、Redis、容器运行时或 root 常驻权限。

本仓库已改为通知组件，删除旧 SSH 交付脚本、安装器和历史兼容入口。**服务不执行命令、不下载安装包、不持有 SSH 私钥，也不直接触发升级。** 更新包发现、完整性核验和受保护安装仍由 deploy-platform 的 cloudctl 负责；平台 SSE 消费者见 [deploy-platform PR #5](https://github.com/stewardful/deploy-platform/pull/5)，两边合并部署后即可接入；通知服务本身仍不触发升级。

## 流程与最小配置

```text
GitHub 发布固定 dev-RUN_ID 或 vX.Y.Z
    → HTTPS /hooks/github/releases
    → HMAC 验签、固定仓库、去重和版本排序
    → 本地私有 state.json
    → 本机查询或 SSE snapshot
    → 消费者重新核对 GitHub 发布信息，按本机策略提示或升级
```

默认源仓库 `stewardful/deploy-platform`，默认监听 `127.0.0.1:8090`。唯一必需凭据是一份 Webhook 随机密钥，分别保存在服务器私有文件与 GitHub **仓库 Settings → Webhooks → Secret** 中；无需 GitHub Actions 自定义 Secret、PAT、GitHub App 或服务器 SSH 凭据。域名和 DNS 是公开接入需要的部署配置。

服务只支持字面量 loopback IP 监听，拒绝公网地址和主机名。Caddy 仅转发一个 POST 路径，查询和 SSE 留在本机。若放在另一台机器，消费者需要额外的安全网络/认证设计；当前版本面向同机集成，不提供公网订阅 API。

## 构建与本地验证

开发机需要 Go 1.25+；只用标准库，没有第三方 Go 依赖：

```bash
make check test smoke
./bin/release-notify --help
```

`make test` 开启 race detector；`make smoke` 以当前非 root 用户启动实际二进制，验证 HTTP、SSE、重启与持久化去重，不连接外部服务器。CI 使用最低 Go 1.25 和 stable，测试通过后构建 amd64/arm64 Linux 单文件程序和 SHA256，存放在对应 Actions 的 Artifacts，保留 14 天；CI 没有发布或服务器部署任务。私有仓库的 CI 产物下载需要登录 GitHub。

在本地临时目录生成测试密钥并运行（不要提交或发送密钥）：

```bash
mkdir -m 700 -p state
(umask 077; openssl rand -hex 32 > state/webhook.secret)
./bin/release-notify --secret-file ./state/webhook.secret --state-dir ./state
```

另一个本地终端：

```bash
curl --fail http://127.0.0.1:8090/healthz
curl --fail http://127.0.0.1:8090/v1/releases/latest
curl --no-buffer http://127.0.0.1:8090/v1/events
```

健康检查返回 `{"status":"ok"}`；初始快照 `revision=0`、`latest={}`。SSE 连接立即返回快照并保持连接，后续收到消息时推送新快照，每 15 秒发送心跳。断线后重新连接，总会收到最新完整快照，不依赖无限事件历史，也不使用定时查版本。

## Ubuntu 部署步骤（手动执行，CI 不连接服务器）

准备匹配架构的 `release-notify` 二进制和可信渠道取得的 SHA256，自行校验后安装到 `/usr/local/bin/release-notify`。目标服务器需要 systemd；反向代理可复用现有 Caddy，不要覆盖 deploy-platform 自动管理的配置。

```bash
sudo install -m 0755 release-notify /usr/local/bin/release-notify
sudo install -d -m 0700 /etc/deploy-delivery
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/deploy-delivery/webhook.secret'
sudo install -m 0644 deploy/release-notify.service /etc/systemd/system/release-notify.service
sudo systemctl daemon-reload
sudo systemctl enable --now release-notify
curl --fail http://127.0.0.1:8090/healthz
```

生成密钥的命令只用于首次初始化，不要在重装时覆盖已有密钥。GitHub Webhook 设置时，在自己可信终端读取该文件，将值填入 Secret，不要放到仓库、命令行参数或日志。systemd 通过 LoadCredential 提供只读密钥副本；仅在管理器提供的凭据目录中接受这种 0440 文件，普通密钥仍要求 0600/0400。服务使用 DynamicUser 运行；状态目录由 systemd 创建，权限 0700。无 sudo、Docker Socket 或平台升级目录权限。示例限制 64 MiB 内存、25% 单核 CPU 和 16 个 SSE 订阅；这是隔离上限，不是已测量的实际占用。

将 [Caddy 示例](deploy/Caddyfile.example) 中域名替换成你的通知域名，解析到现有服务器，并把路由合并到合适的代理配置。公网只需正常 HTTPS 443；8090 不对公网开放。DNS/ACME 与端口要求沿用你的 Caddy 部署方式。

GitHub 源仓库添加 Webhook：

- Payload URL：`https://通知域名/hooks/github/releases`。
- Content type：`application/json`。
- Secret：与服务器文件内容相同的随机密钥。
- Enable SSL verification，事件仅选择 **Releases**。

签名正确的 ping 返回 204；符合条件的发布返回 `{"accepted":true}`，重复或旧发布返回 `{"accepted":false}`。GitHub 的 Recent Deliveries 可查看响应并手动 Redeliver。

## API 契约

| 端点 | 用途 | 对公网 |
| --- | --- | --- |
| `POST /hooks/github/releases` | GitHub 原始 JSON 与 HMAC-SHA256 签名 | 通过 HTTPS 代理 |
| `GET /healthz` | 进程可用性 | 否 |
| `GET /v1/releases/latest` | 当前 revision 和 dev/stable 最新消息 | 否 |
| `GET /v1/events` | SSE，event 名 snapshot、id 为持久化 revision | 否 |

只接受配置仓库的 `release/published`，并校验仓库 ID 非零、Release ID 与发布时间。预发布必须是 `dev-正整数`，正式版必须是 `vX.Y.Z`；忽略草稿、其他动作和滚动 `dev-latest`。dev 按构建编号、stable 按数值版本排序，重复签名正文即使换投递 ID 也不会重复推进版本；相同标签重建不会被当作新版本。

事件只保留 repository、channel、tag、release_id、published_at，不采用请求里的脚本、URL、路径、命令或安装参数。签名证明来源，不证明代码安全；消费者仍须重新验证受信发布清单和包摘要，不能直接执行通知内容。

请求正文最多 256 KiB、同时处理最多 8 个 Webhook，最多 16 个 SSE 订阅；慢消费者合并为最新快照，写入有期限。HTTP 头部/正文读取有超时；不记录原始正文、签名或密钥。持久化最多两个通道及 256 个有效投递 ID，状态文件最多 128 KiB，权限 0600，原子替换并 fsync，单进程锁阻止两实例写同一目录。状态格式错误明确失败，没有历史迁移。

## 平台接入

Go 控制端使用本机 `/v1/events`，不依赖本私有仓库的 SDK 或 GitHub 凭据。首次连接和重连发送完整快照，平台重新核对固定公开发布清单，不执行消息中的字段。平台 dev 自动开关默认关闭，开启命令为 `sudo cloudctl update policy --dev-auto-upgrade on`；正式版始终只提示手动确认。设置关闭用 `off`，不需要 GitHub Actions 开关或 SSH Secrets。

API 和平台通过运行时 HTTP/SSE 契约协作，两个仓库 CI 独立，不互相触发。源码见关联 PR，尚未合并或部署；本通知服务的运行权限和原状态契约不因平台接入而扩大。

## 重启、遗漏与排错

状态先落盘再唤醒消费者。重启后保留去重和最新消息；消费者 SSE 重连会拿到完整快照。**仅靠 Webhook 不能发现服务停机期间从未送达的版本**：GitHub 失败投递需要重投，平台消费者应在启动/重连或管理员检查时主动核对公开发布清单。该 API 不增加后台版本轮询。

```bash
sudo systemctl status release-notify
sudo journalctl -u release-notify -n 100 --no-pager
```

401：签名错误或密钥不一致；413：正文超限；422：仓库/标签/发布元数据不符合契约；503：并发、订阅数或状态写入异常。失败不会调用 cloudctl 或改变业务项目。

本版本未部署到用户服务器，也未配置真实 GitHub Webhook。测试使用本地签名事件和 HTTP/SSE，不能替代公网 HTTPS、真实 GitHub 投递及平台消费者联调。
