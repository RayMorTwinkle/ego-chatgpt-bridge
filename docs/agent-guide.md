# Agent 使用说明

推荐 Agent 使用 MCP。CLI 适合脚本、状态检查和将原始产物落盘。两者共用同一后台队列、身份和幂等记录。

## 最短调用路径

1. `health_check` 核对账户、專案、GPT-6 和思考强度。
2. `create_query(text, request_id)` 返回 Query ID 和消息操作 ID；请求接受不等于生成完成。
3. 以 2–5 秒间隔查询 `get_query_result`，同时保存 Query ID 与消息操作 ID。
4. `complete=true` 才表示这一轮的生成及交付均完成。`generation_complete=true`、`delivery_complete=false` 时使用 `collect_artifacts`，不要重新提问。
5. 用原 Query ID 调用 `continue_query` 保留上下文，每轮使用新的 request_id。

request_id 是调用身份范围内的幂等键。网络超时后查询原操作，或用**相同内容、相同键**获取原提交结果；不要换键重复发送。新建与追问的键不能混用。

## 文件和图片

结果的 `artifacts` 列出 artifact_id、文件名、MIME、字节数、SHA-256、认证 HTTP 下载地址和 `mcp_resource_uri`。

- `read_artifact(artifact_id)`：不超过 4 MiB 的 PNG/JPEG/GIF/WebP 返回原生 MCP ImageContent，Agent 可直接查看。
- `read_artifact(artifact_id, inline_image=false)`：返回 `ego-artifact://...` ResourceLink。
- 对 ResourceLink 使用 MCP `resources/read`：返回 base64 编码的原文件字节，认证沿用当前 MCP 连接。HTTP MCP 和 stdio 代理均支持。
- 资源内容的协议 MIME 为 application/octet-stream，实际文件 MIME、文件名及校验值以产物元数据为准。
- 所有已缓存产物也可以用认证 HTTP GET 下载；支持 HEAD／Range。认证不要放进 URL。

CLI 直接下载并校验，不会覆盖已有文件：

```sh
APP="$HOME/apps/ego-chatgpt-bridge"
"$APP/.venv/bin/ego-bridge" call create_query --json '{"text":"分析方案并生成 PDF。","request_id":"my-task-001"}' --wait
"$APP/.venv/bin/ego-bridge" call get_query_result --json '{"query_id":"q_..."}'
"$APP/.venv/bin/ego-bridge" download art_... --output ./report.pdf
```

## LAN 客户端

WebUI 的 MCP 接入提供 HTTP 配置和当前身份 token。默认 EasyTier 内 HTTP 无需 CA 证书，复制配置即可。不要复制服务端数据库或浏览器身份信息。

远程 CLI 无需服务端运行目录，配置环境变量即可：

```sh
export EGO_BRIDGE_URL=http://10.126.126.11:8766
# EGO_BRIDGE_TOKEN 由 Agent 的私密环境配置注入，勿写进任务正文或日志。
ego-bridge call list_queries
ego-bridge download art_... --output ./report.pdf
```

本机 stdio 命令是 `~/apps/ego-chatgpt-bridge/.venv/bin/ego-bridge stdio`，读取本机 .env 并代理后台服务，不再创建执行器。CLI 普通 call 与 download 的 token 可通过环境或本机 .env 提供。

只有主动启用 HTTPS 时，客户端才需配置证书信任，可用 EGO_BRIDGE_CA 或 --ca-file 指定证书；HTTP 默认流程不需要这些参数。

## 错误和退出码

| 情况 | 应做的操作 |
| --- | --- |
| QUEUED／WAITING_FOR_RESPONSE／GENERATING | 等待并轮询，不重发 |
| OUTCOME_UNKNOWN | recover_session 对账；不能 retry_query |
| NEEDS_ATTENTION 且尚未进入发送边界 | 查看原因，修复后 retry_query |
| 已生成但文件尚未回传 | collect_artifacts，只收文件 |
| 登录失效／模型不可验证 | 保留任务并处理实际网页问题 |
| 临时 local-chatgpt 地址 | 等待正式地址；服务不会把临时地址当稳定映射 |

CLI `--wait` 的退出码：0=这一轮完成，1=请求或连接失败，2=任务终止但未成功／需处理，3=客户端等待超时。退出码 3 不取消后台任务，JSON 同时给出任务标识和 background_task_continues。

调试 ZIP 不是 Agent 工具：仅主身份 WebUI 登录提供，允许只读导出固定專案以外的自己的对话，不注册为 MCP 工具。

## 使用判断

该接口适合需要持久化上下文、异步等待、明确恢复边界的研究 Agent。文件／图片交付已与文字生成分离，不需要把二进制内容塞进回答文字。它仍受网页 DOM、登录、模型控件和实际生成能力影响，不能提供官方推理 API 的稳定性保证。具体已验证与未验证范围见 [验证记录](verification.md)。
