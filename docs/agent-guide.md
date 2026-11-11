# Agent 使用说明

推荐使用 MCP `/mcp`，默认只提供 `ask`、`wait`、`get`、`list`、`read_artifact`、`cancel`、`diagnose` 七个工具。服务端在固定项目的 ChatGPT 会话中执行 GPT-6 研究，支持 `extra_high`（默认）和 `high`。

## 最短调用路径

```json
{"tool":"ask","arguments":{"text":"比较两个候选方案并注明来源。","wait_seconds":50}}
```

返回 `status=running` 时，照着 `next.tool` 和 `next.arguments` 继续调用 `wait`。默认每次等 50 秒，最多 300 秒；一次研究可能超过 10 分钟。等待期间不返回正文，带 MCP progressToken 的请求约每 10 秒收到进度通知。超时只结束本次等待，后台任务继续。

`status=done` 时直接读取 `answer`（Markdown）和 `files`，不需要再调用 `get`。`get(query_id)` 可随时查看精简状态，`get(query_id, verbose=true)` 返回旧版完整记录。需要继续追问时：

```json
{"tool":"ask","arguments":{"query_id":"q_...","text":"继续比较成本。","wait_seconds":50}}
```

保存返回的 `query_id` 和 `message_id`；指定 `message_id` 可查看或等待某一轮，省略时在调用开始时选定最新一轮。

## 去重与恢复

`request_id` 可省略：同一调用身份、同一输入 query_id（新建时为空）、完全相同文本和相同附件 SHA-256 集合，若已有未终止操作，或操作创建尚未超过 10 分钟，就复用原操作。新建与追问分别去重；附件顺序、文件名和 effort 不参与自动去重。未确认发送结果的操作持续复用，不因超过 10 分钟而重发。需要明确创建内容相同的新一轮时，提供新的 `request_id`。

显式 `request_id` 是身份范围内永久幂等键：同键同参数复用，异参报错。网络断开后继续等待原任务，或按原参数调用 `ask`；不要为了处理故障更换键发送。

服务端自动核对发送结果和补收文件，显示为“核对中”或“收集文件中”。每轮最多自动处理 3 次，两次之间至少 10 秒；次数持久化，服务重启不会重置。自动恢复只读取原消息和收集文件，不重新发送。无有效提交记录、消息基线或不支持导出的内容会直接要求处理。

五种状态：`running` 等待；`done` 读取答案；`needs_you` 按 `problem.what_to_do` 处理；`failed` 查看原因；`cancelled` 已取消。`diagnose` 仅在 needs_you 且 what_to_do 无法解决时给维护者用。浏览器属于服务端，不需要在 Agent 本机寻找对应空间。

## 文件和图片

`ask` 支持 `attachments=[{filename, content_base64}]`，每轮最多 4 个文件、原始大小合计 40 MiB。状态只返回附件元数据，不返回上传的 Base64。

`files` 仅包含实际可下载文件的 artifact_id、filename、mime_type、size_bytes、download_url。HTTP 下载需要当前身份的 Bearer token，不能把 token 放进 URL。

- `read_artifact(artifact_id)`：不超过 4 MiB 的 PNG/JPEG/GIF/WebP 直接返回 MCP 图片。
- `read_artifact(artifact_id, inline_image=false)`：返回资源链接，沿用当前连接调用 MCP `resources/read` 读取原始字节。
- CLI：`ego-bridge download art_... --output ./report.pdf`，校验大小与 SHA-256，不覆盖已有文件。

文件默认保留 14 天；正文与会话记录保留。`done` 表示生成和文件交付完成，回答质量、来源可信度和是否满足要求仍需调用方核验。模型仅通过网页选择器核验，不能证明实际推理后端；项目指令及记忆可能参与回答。

## 连接与兼容

WebUI 的“MCP 接入”提供当前身份配置；默认 EasyTier HTTP 无需 CA。每个身份只能访问自己的会话和文件。远程 CLI 可用 `EGO_BRIDGE_URL`、`EGO_BRIDGE_TOKEN`，不需要服务端数据库。

REST `POST /api/<tool>` 和 WebUI `POST /ui/api/tool/<name>` 同时支持新旧工具。`get_thread(query_id)` 是 WebUI 服务端接口，一次返回全部轮次，不注册为 MCP 工具。

旧工具仍可通过 REST/CLI 调用。配置 `mcp_legacy_tools=true` 后才额外显示在 MCP 目录中，默认 false。旧流程 `create_query` / `continue_query` 仍要求 request_id，`wait_query` 仍为 0..30 秒及 cursor 语义，`get_query_result` 参数与结果保持兼容。

```sh
ego-bridge call create_query --json '{"text":"分析方案并生成 PDF。","request_id":"my-task-001"}' --wait
```

CLI `--wait` 行为及退出码保持不变：0 完成，1 调用失败，2 任务需处理或未成功，3 客户端等待超时（不取消后台任务）。主身份 WebUI 的调试 ZIP 导出不是 Agent 工具。
