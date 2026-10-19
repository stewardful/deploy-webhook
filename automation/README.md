# 自动更新平台组件 PR

`main` 的 push 或手动运行 `Delivery components` 后，先执行交付组件测试、自动更新器测试和脚本检查；全部通过才尝试创建平台 PR。PR 事件只运行测试，不读取跨仓库凭据。辅助仓库其他分支不会更新平台。

更新器读取通过本次测试的完整提交 SHA，检查它仍是辅助仓库最新 main；在 `stewardful/deploy-platform` 的 `dev` 上执行平台自己的同步工具，重新验证快照并运行快照内的组件测试，只提交 `distribution/vendor/deploy-delivery/` 与相邻 lock 文件。分支为 `codex/deploy-delivery-完整SHA`，目标固定 `dev`。每个提交最多创建一个 PR，重跑复用原 PR，不 force push；已关闭的 PR 也不会被自动重新创建。不同提交可以各有一个 PR，请关闭被后续版本替代的旧 PR。

没有自动合并、发布 Release、SSH 或服务器操作。平台 PR 自行执行完整 CI；只有之后人工合并到 dev 才触发现有测试服务器部署。正常平台构建无需访问辅助私有仓库。

## 一次性配置（在 deploy-delivery 仓库）

1. 在 stewardful 创建一个专用 GitHub App。关闭 webhook；Repository permissions 只开启 **Contents: Read and write**、**Pull requests: Read and write**（Metadata 自动只读）。无需 Actions、Administration 或组织权限。
2. 将 App 安装到 stewardful，选择 **Only select repositories → deploy-platform**。不要选择所有仓库。辅助仓库 checkout 使用它自己的 GITHUB_TOKEN，无需给 App 读取辅助仓库权限。
3. 生成 App 私钥，将完整 PEM 直接填入 deploy-delivery 的 **Settings → Secrets and variables → Actions → Secrets**：`DELIVERY_APP_PRIVATE_KEY`。这是新 GitHub App 的私钥，**不是服务器 SSH 私钥**。
4. 在相同页面的 **Variables** 新增 `DELIVERY_APP_ID`，填 App 设置页面的数字 App ID。它是普通变量，不是 Secret，也不是 Installation ID。
5. 检查组织策略允许该 App 的机器人创建 PR，且 Actions 可以使用 `actions/create-github-app-token`。平台分支保护保持人工审核与必需检查，不给予绕过权限。
6. 先合并平台 PR #4 以使 dev 拥有同步协议（本次任务不合并）。然后在辅助仓库 Actions → Delivery components → Run workflow，选择 main。之后推送 main 会自动联动。

工作流固定官方 token action 提交，只请求平台仓库 Contents/PR 写权限。临时 installation token 在作业结束后撤销；它创建的 push/PR 可以触发平台 CI，避免使用 GITHUB_TOKEN 产生的递归事件限制。不要把本机 gh 登录令牌存为自动化凭据。

缺少任一配置时测试照常执行，更新作业输出 warning 和 Summary **not enabled**，不推送分支。不具备同步协议时输出 NOT READY，不创建 PR。配置齐全后鉴权或平台操作失败会使更新作业失败，不会伪装为已创建 PR；修好权限后可以重跑。旧 workflow 重跑时，如果不是最新 main 会跳过；平台已锁定相同提交时无需更新；平台 pin 不是新提交的祖先时拒绝回退。

## 验收

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s automation/tests -v
bash -n scripts/*.sh
shellcheck scripts/*.sh
```

自动更新器测试使用临时 Git 仓库和本地 bare 远端，执行真实同步、测试、提交、推送与重跑；仅模拟 GitHub PR API，不需要云凭据。`automation/tests/vendor-delivery.fixture` 是 consumer 同步协议的测试夹具，来自平台 `distribution/ci/vendor-delivery.py`；平台协议变更时同步更新夹具。自动化代码位于 automation/，不进入平台服务器交付快照。

完成配置并手动运行后：辅助工作流 test 与 update-platform 成功，日志出现平台 PR URL；平台 PR base 为 dev，只有快照和 lock 变更，CI 自动启动，auto-merge 未启用。**创建 PR 不等于已经部署。** 如果配置未提供，本地及无凭据 CI 不能证明实际跨仓库授权成功。

参考：[官方 GitHub App token action](https://github.com/actions/create-github-app-token/tree/fee1f7d63c2ff003460e3d139729b119787bc349)、[GitHub token 事件触发限制](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow#triggering-a-workflow-from-a-workflow)。
