# Ego Research Bridge

把已登录的 ChatGPT 网页会话连接到 Agent：固定專案、异步任务、可恢复对话、原始图片与文件回传，以及 EasyTier 内的管理界面。

浏览器执行环境为已安装的 **Ego Browser**。使用 **GPT-6**，只允许极高或高思考。网页选择器核验会明确标记为 UI 验证，不能证明实际推理后端模型。

## 安装与运行

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Ego Browser CLI，以及在 Ego 中已登录的 ChatGPT 账户。

```sh
git clone https://github.com/RayMorTwinkle/ego-chatgpt-bridge.git
cd ego-chatgpt-bridge
./scripts/setup.sh
```

源码留在检出目录；虚拟环境、数据库、产物、日志和凭据位于：

```text
~/apps/ego-chatgpt-bridge/
├── .env                  # MCP token、额外调用方 token、WebUI 签名密钥，0600
├── config.json           # 空间、專案、账户与网络配置
├── .venv/
├── bridge.sqlite3
├── artifacts/
├── exports/
├── tls-cert.pem
├── tls-key.pem
└── service.log
```

将 [config.example.json](config.example.json) 中的空间 ID、專案 ID 和账户显示名替换为实际核验结果。普通查询必须使用固定專案；初始化不会替你接管日常空间或重新创建丢失空间。

EasyTier 示例只绑定 `10.126.126.11`，来源仅允许 `10.126.126.0/24`。按实际虚拟网卡地址配置后，生成包含对应地址的证书：

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/ego-bridge" renew-tls
"$APP/.venv/bin/python" scripts/manage.py start
"$APP/.venv/bin/ego-bridge" call health_check
```

UI：`https://10.126.126.11:8766/ui/`。首次访问需信任本机生成的证书。登录使用 `.env` 的同一个 MCP token，浏览器使用一年有效的 HttpOnly、Secure、SameSite cookie；不会把 token 写入 localStorage。修改 token 并重启会使旧登录失效。

`EGO_BRIDGE_RUNTIME` 可显式更改运行目录。初次 init 默认仅监听回环；EasyTier 部署需配置具体地址，不会自动放宽到所有接口。

## WebUI

- 创建、追问、查看研究任务、结果、图片和文件。
- 查看错误、核对网页、仅重试安全操作或重新收集产物。
- 复制 MCP 地址、当前身份的 token 和接入 JSON。
- **仅 WebUI、主 token 登录可用的调试导出**：输入任意自己的 ChatGPT 对话 URL，包括固定專案之外的对话，只读生成 ZIP。

ZIP 包含 Markdown／JSON 对话、可取得的原始产物和完整性清单。缺失、未完成或不支持的内容会写入 manifest。长对话的服务端历史分页尚未完全验证，归档明确标记采集范围。导出不发送消息，不更改模型，不创建普通 Query，也不注册为 MCP 工具。

## Agent 与程序调用

MCP Streamable HTTP：`https://<已配置地址>:8766/mcp`。HTTP 工具：`POST /api/<tool_name>`。需要 `Authorization: Bearer <token>`，客户端信任 `tls-cert.pem`。在 WebUI 的“MCP 接入”复制配置，并下载公有 CA 证书供远程客户端信任。

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/ego-bridge" call create_query --json '{"text":"比较两个候选方案并注明来源。","request_id":"research-001"}' --wait
"$APP/.venv/bin/ego-bridge" call continue_query --json '{"query_id":"q_...","text":"继续比较成本。","request_id":"research-002"}' --wait
"$APP/.venv/bin/ego-bridge" call get_query_result --json '{"query_id":"q_..."}'
```

stdio 客户端使用 `~/apps/ego-chatgpt-bridge/.venv/bin/ego-bridge stdio`，共用后台服务，不创建第二个浏览器执行器。

`--wait` 只是等待；退出客户端不取消后台任务。每个调用身份只能访问自己的任务和产物。主身份可通过 WebUI 执行账户级调试导出，额外 Agent 身份没有该权限。

## 产物交付

结果分开返回 `generation_complete`、`delivery_complete` 和 `complete`。文字结束不等于文件可下载。

- 文字与普通网页链接保持为结构化输出。
- 文件及图片取得真实字节后才标记 AVAILABLE，返回 MIME、大小、SHA-256 和认证下载地址。
- 小于等于 4 MiB 的常见位图可通过 `read_artifact` 返回原生 MCP ImageContent；其他文件返回 ResourceLink。
- 不支持导出的音视频、交互预览等会明确列为未完成输出，不会悄悄丢失。
- 下载失败可单独重收产物，不重新提问、不自动重新生成。

## 检查与备份

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/python" -m pytest -q
"$APP/.venv/bin/python" scripts/verify_stdio.py
"$APP/.venv/bin/ego-bridge" backup "$APP/backup.zip" --include-artifacts
"$APP/.venv/bin/python" scripts/manage.py stop
```

`--include-artifacts` 包含一致的 SQLite 快照和已校验产物字节；不包含 `.env`、TLS 私钥或可重新生成的调试 ZIP。普通 backup 只备份数据库。凭据需另行安全保管。

- [接口](docs/api.md)
- [架构与恢复](docs/operations.md)
- [验证范围](docs/verification.md)
- [参考项目审查](docs/reference-review.md)

本项目不会读取或导出浏览器 Cookie、Session Token 和认证请求头，也不会绕过登录、验证或访问限制。请在适用授权和服务条款允许的范围内使用。
