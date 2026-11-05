# Agent 使用说明

推荐 Agent 使用 MCP。CLI 适合脚本、状态检查和将原始产物落盘。两者共用同一后台队列、身份和幂等记录。

## 最短调用路径

1. 可先用廉价的 `diagnose` 查看服务/worker。`health_check` 是可选的浏览器控件诊断，可能较慢；失败不意味着服务整体不可用，不要反复调用。每次发送前仍会独立核验账户、专案与模型。
2. `create_query(text, request_id)` 返回 Query ID 和消息操作 ID；请求接受不等于生成完成。
3. 每5秒查询 `get_query_result`，同时保存 Query ID 与消息操作 ID。`result.content` 是截至最近观察的累计快照，可能增长或被网页修订；更新时替换旧快照，不要盲目拼接。`result_is_partial=true` 表示可先读取但仍需等待，不能因为暂时为空就创建第二份请求；遵循 `next_action`。
4. `complete=true` 才表示这一轮的生成及交付均完成。`generation_complete=true` 时正文已生成，文件未齐也可以先使用正文。`generation_complete=true`、`delivery_complete=false` 时使用 `collect_artifacts`，不要重新提问。
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

## 服务端浏览器与诊断证据

这是远程 MCP：`space_identifier`、`runtime_space_identifier` 和 `page_label` 属于**桥接服务器的 Ego 实例**。本机安装了 ego-browser 也不代表能访问该实例；找不到空间8不能推出服务端空间8不存在。不要据此在客户端新建同名空间或重发研究。返回的 `execution_context` 会标明服务端执行环境和管理界面入口。

消息关联失败时，`last_error.diagnostic_context` 给出具体 `reason`、基线/新增轮次数、提示词长度和SHA-256、首个差异位置、请求标记是否存在、已观察消息ID、网页轮次状态及DOM读取统计。不会回传完整提示词、浏览器凭据或私有HTML。`observed_conversation_url` 明确标记未经关联验证，不等同于正式 `conversation_url`。

OUTCOME_UNKNOWN 表示关联尚未确认，先调用 `recover_session`；持续失败再 `diagnose`，把结构化证据交给服务维护者。只要已有发送意图就不能换键重发。网页仍在输出时，状态保持 GENERATING，`progress.answer_characters/source_count/last_observed_at` 表明最近观察到的增量，`complete=false` 不等于失败。

网页引用的外部图片以 `external_images` 和 `outputs[type=link, kind=web_image_reference]` 回传 URL，`delivery=reference_only` 表示未缓存第三方图片字节。搜索过程中的临时缩略图不算交付文件。ChatGPT 原生生成图片和文件继续走 `artifacts`，仅实际取得并校验字节后才标为 AVAILABLE；不要把引用链接当作本地图片。
