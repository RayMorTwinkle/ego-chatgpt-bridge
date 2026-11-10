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

源码留在检出目录；应用以非 editable 方式安装到运行目录，后台启动不依赖源码路径。虚拟环境、数据库、产物、日志和凭据位于：

```text
~/apps/ego-chatgpt-bridge/
├── .env                  # MCP token、额外调用方 token、WebUI 签名密钥，0600
├── config.json           # 空间、專案、账户与网络配置
├── .venv/
├── bridge.sqlite3
├── artifacts/
├── exports/
├── tls-cert.pem          # 仅主动开启 HTTPS 时使用
├── tls-key.pem           # 可选 TLS 私钥
└── service.log
```

将 [config.example.json](config.example.json) 中的空间 ID、專案 ID 和账户显示名替换为实际核验结果。普通查询使用固定專案。专用空间按独占模式自动接管；丢失后可重建并记录迁移，再按精确对话地址及消息核验恢复，不把空空间称为原空间恢复。请勿将日常工作放进这个专用空间。

EasyTier 示例只绑定 `10.126.126.11`，来源仅允许 `10.126.126.0/24`。按实际虚拟网卡地址配置后启动：

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/python" scripts/manage.py start
"$APP/.venv/bin/ego-bridge" call health_check
```

UI：`http://10.126.126.11:8766/ui/`。默认通过 EasyTier 内 HTTP 提供服务，**无需导入 CA 证书**。登录使用 `.env` 的同一个 MCP token，浏览器使用一年有效的 HttpOnly、SameSite cookie；不会把 token 写入 localStorage。修改 token 并重启会使旧登录失效。

`EGO_BRIDGE_RUNTIME` 可显式更改运行目录。初次 init 默认仅监听回环；EasyTier 部署需配置具体地址，不会自动放宽到所有接口。

## WebUI

- 创建、追问、查看研究任务、结果、图片和文件。
- 查看错误、核对网页、仅重试安全操作或重新收集产物。
- 并列展示 EasyTier 直连和公网两套 MCP 地址及配置，优先直连；保留传统 Token、凭据文件路径和 OAuth 授权。
- **仅 WebUI、主 token 登录可用的调试导出**：输入任意自己的 ChatGPT 对话 URL，包括固定專案之外的对话，只读生成 ZIP。

ZIP 包含 Markdown／JSON 对话、可取得的原始产物和完整性清单。缺失、未完成或不支持的内容会写入 manifest。长对话的服务端历史分页尚未完全验证，归档明确标记采集范围。导出不发送消息，不更改模型，不创建普通 Query，也不注册为 MCP 工具。

## Agent 与程序调用

MCP Streamable HTTP：`http://<已配置地址>:8766/mcp`。HTTP 工具：`POST /api/<tool_name>`。需要 `Authorization: Bearer <token>`。在 WebUI 的“MCP 接入”复制配置即可，无需 CA 导入。

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/ego-bridge" call create_query --json '{"text":"比较两个候选方案并注明来源。","request_id":"research-001"}' --wait
"$APP/.venv/bin/ego-bridge" call continue_query --json '{"query_id":"q_...","text":"继续比较成本。","request_id":"research-002"}' --wait
"$APP/.venv/bin/ego-bridge" call get_query_result --json '{"query_id":"q_..."}'
```

CLI 原始文件下载（不会覆盖已有文件，校验大小和 SHA-256）：

```sh
"$APP/.venv/bin/ego-bridge" download art_... --output ./report.pdf
```

远程 CLI 也可只配置 `EGO_BRIDGE_URL` 和 `EGO_BRIDGE_TOKEN`，无需复制服务端数据库或运行目录。`--wait` 的退出码为 0=完成、1=调用失败、2=任务需处理／未成功、3=客户端等待超时；超时不取消后台任务。

stdio 客户端使用 `~/apps/ego-chatgpt-bridge/.venv/bin/ego-bridge stdio`，共用后台服务，不创建第二个浏览器执行器。

新建研究和追问可在 WebUI 选择附件，也可通过 `create_query` / `continue_query` 的 `attachments` 参数提交。每轮最多 4 个文件，合计 5 MiB。格式见 [研究附件](docs/api.md#研究附件)。

`--wait` 只是等待；退出客户端不取消后台任务。每个调用身份只能访问自己的任务和产物。主身份可通过 WebUI 执行账户级调试导出，额外 Agent 身份没有该权限。

## ChatGPT OAuth 接入

可启用独立公网 HTTPS MCP／OAuth 入口，同时将 WebUI 保持在 EasyTier 内。支持 PKCE、独立凭证、权限限制、刷新和撤销；授权确认在私网工作台完成。公网不接受管理 token。部署及限制见 [OAuth 文档](docs/oauth.md)。

## 产物交付

结果分开返回 `generation_complete`、`delivery_complete` 和 `complete`。文字结束不等于文件可下载。

- 文字与普通网页链接保持为结构化输出。
- 文件及图片取得真实字节后才标记 AVAILABLE，返回 MIME、大小、SHA-256 和认证下载地址。
- 小于等于 4 MiB 的常见位图可通过 `read_artifact` 返回原生 MCP ImageContent；其他文件返回 `ego-artifact://...` ResourceLink。
- MCP `resources/read` 使用同一认证连接读取原始文件字节；HTTP 和 stdio 都支持，不必另行拼接认证下载请求。
- 不支持导出的音视频、交互预览等会明确列为未完成输出，不会悄悄丢失。
- 下载失败可单独重收产物，不重新提问、不自动重新生成。

## macOS 登录自启与保活

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/python" scripts/manage.py stop
"$APP/.venv/bin/ego-bridge" autostart install
"$APP/.venv/bin/ego-bridge" autostart status
```

LaunchAgent 随当前用户登录启动，`launchd` 保持 supervisor 存活；supervisor 负责服务崩溃重启、连续 HTTP 探测失败后的重启、等待配置的网络地址就绪。默认每 10 秒探测、连续 3 次失败后重启。Ego 未运行时在后台启动已安装的 Ego 应用，每 30 秒检查一次；不安装额外浏览器。

这需要用户的桌面登录会话，不是登录前的系统服务。未模拟重启整台电脑；可用 `autostart restart` 重启受管任务，`autostart uninstall` 移除自启动并保留数据。`scripts/manage.py start/stop` 识别受管模式，停止不会被保活立即拉起。

## 检查与备份

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/python" -m pytest -q
"$APP/.venv/bin/python" scripts/verify_stdio.py
"$APP/.venv/bin/ego-bridge" backup "$APP/backup.zip" --include-artifacts
"$APP/.venv/bin/python" scripts/manage.py stop
```

`--include-artifacts` 包含一致的 SQLite 快照和已校验产物字节；不包含 `.env`、TLS 私钥或可重新生成的调试 ZIP。普通 backup 只备份数据库。凭据需另行安全保管。

- [Agent 使用说明](docs/agent-guide.md)
- [接口](docs/api.md)
- [架构与恢复](docs/operations.md)
- [验证范围](docs/verification.md)
- [参考项目审查](docs/reference-review.md)

本项目不会读取或导出浏览器 Cookie、Session Token 和认证请求头，也不会绕过登录、验证或访问限制。请在适用授权和服务条款允许的范围内使用。

HTTPS 是可选部署方式：主动设置 `tls_enabled=true` 后运行 `renew-tls` 并配置证书信任；默认 EasyTier 部署不启用。切换 HTTP／HTTPS 使用不同 cookie 名称，切换后需登录一次。

## 联网研究与分段输出

支持长提示词与联网研究的逐段正文读取。创建请求后使用 `wait_query`（最长30秒、携带上次 cursor）等待变化，再用 `get_query_result` 读取 `result.content` 的累计快照并遵循 `next_action`；`result_is_partial=true` 时继续等待，`complete=true` 才是完整交付。`health_check` 为可选浏览器诊断，发送前会独立核验。空间ID属于服务器，客户端不必也不应据此启动本机Ego。

## 日志与文件保留

图片及文件在成功采集后保留14天；调试 ZIP 和工作文件同样保留14天。服务启动后、此后每小时清理一次，运行中的任务和文件下载会延后物理清理。到期后下载立即停用，API 返回 `EXPIRED`／HTTP410，文字、对话映射和文件校验元数据保留；可用 `collect_artifacts` 尝试从原网页重新采集，不会重新提问，远端文件不保证仍可用。

`service.log`、`supervisor.log`、`backup.log` 按UTC日期或10MiB触发压缩轮换，监督器每分钟检查，每类最多5份归档、最长14天。适用于持有文件描述符的 launchd 子进程；采用copy/truncate，存在极短的并发写入窗口，不承诺取证级无损日志。

运行目录 `backups/` 内的应用备份最长14天，且不晚于包内最早产物到期时间；到期删除整包，避免旧备份额外保留图片。旧版备份按其数据库里的采集时间推算。只管理本应用运行目录；手动复制到其他位置的文件由调用方管理。`verification/`、`evidence/` 内的临时验收文件也保留14天。

策略可在 `config.json` 调整，修改后重启；现有文件的到期时间固定记录，新采集采用新配置。`diagnose` 返回保留策略和最近清理结果，运行目录 `retention.json` 保存清理回执。`.env`、TLS密钥和对话文字不会因文件轮换而删除。


v0.4.3：完成判定要求整轮 `complete`、无停止生成按钮、产物渲染就绪，并跨至少两次观察保持内容/产物描述不变（默认至少4秒）。`generation_done` 只说明网页标记结束，`extraction_stable` 表示已通过上述确认；`complete` 仍不是研究质量验收，`semantic_validation=not_performed`。正文提取提供 Markdown 表格/内联链接及 DOM 字数、块 ID、anchor 数和覆盖范围证据。仅覆盖当前可见回答 DOM，不保证未渲染/虚拟分页内容不存在。

`get_query_result` MCP 默认 Markdown、精简元数据、不重复 `outputs`；REST 为兼容保留旧默认，可传 `content_format="markdown", compact=true, include_outputs=false`。`include_content=false` 可只取状态。`wait_query` 返回变更 cursor，未变不重复正文；变化正文是替换快照，非 append delta。MCP 的 JSON 文本与 structuredContent 为兼容均保留，客户端只消费其中一种。

`context_mode="request_only_best_effort"` 可用于创建/追加，附加仅依据本条请求和核验来源的范围约束。固定专案指令和记忆仍可能参与，不能保证真正隔离。健康检查使用独立临时页，`health` 分别返回 probe、worker、submission_blocked_by_probe 和最近核验页面；不再复用旧的卡死专案页或活动研究页。
