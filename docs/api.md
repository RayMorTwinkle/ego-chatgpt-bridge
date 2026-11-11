# 接口与产物交付

MCP `/mcp`、HTTP `POST /api/<tool>`、CLI 和 stdio 共用同一 Service。网络入口默认使用 EasyTier 内 HTTP、Bearer token、Host 白名单和来源 CIDR；TLS 可选。WebUI 使用同一个身份 token 登录后签发的一年 cookie；普通 MCP／HTTP API 不接受 WebUI cookie 替代 Bearer。

## MCP 工具

| 工具 | 必填参数 | 行为 |
| --- | --- | --- |
| create_query | text、request_id | 固定專案新会话；model 只允许 GPT-6，effort=extra_high/high |
| continue_query | query_id、text、request_id | 同一会话按序追加 |
| get_query_status | query_id | 可选 message_id；状态、错误、阻塞前序 |
| get_query_result | query_id | 可选 message_id；文字、链接、产物及交付完整性 |
| wait_query | query_id | 可選 message_id、cursor、timeout_seconds（0–30）；等候狀態變化 |
| list_queries | 无 | status、limit=1..100、offset；身份隔离 |
| health_check | 无 | 实际检查浏览器、空间、账户显示名、專案和模型控件 |
| diagnose | 无 | 可选 query_id/message_id，返回结构化诊断 |
| recover_session | query_id | 对账已提交消息，不重发 |
| retry_query | query_id | 仅允许提交边界前的安全重试 |
| cancel_query | query_id | 排队可确定取消，已发送仅尽力停止 |
| list_messages | query_id | 按序列出消息操作 |
| get_conversation_info | query_id | 空间／專案／会话映射 |
| delete_query | query_id | 删除终止任务的本地记录及缓存，不删除远端会话 |
| list_artifacts | query_id | 可选 message_id；列出产物元数据 |
| collect_artifacts | query_id | 重新下载同一已生成回答的产物，不再提问 |
| get_artifact | artifact_id | 当前身份的单个产物元数据 |
| read_artifact | artifact_id | 可选 inline_image；小图片原生 ImageContent，其他为 ResourceLink |

text 为 1..100000 字符，request_id 为 1..200 字符。默认 soft_timeout=600、hard_timeout=3600 秒，需 `1 <= soft <= hard <= 86400`。每身份最多 100 条等待消息。MCP 和研究提交入口请求体上限为 56 MiB，其他普通 API 为 1 MiB。

Query ID 对应一个 Conversation；Message ID 对应一次输入／响应。幂等键属于调用方：同键同参数复用原操作，异参冲突。发送文本带内部请求标记以便页面对账，这不是网页服务端提供的幂等键。

`create_query` 回傳 `accepted=true`、任務識別碼與 `waiting_guidance`。單次任務可能需要 10 分鐘以上。帶回 `wait_query` 回傳的 cursor 繼續等候原任務。`progress` 提供最近觀測時間、正文長度和來源數，即使 cursor 未變也會回傳。`last_observed_at` 是最近的觀測時間，正文可能與前一次相同。`generation_done` 可能隨網頁狀態回退，請以 `complete=true` 判定交付，再驗收內容品質。

| 超時參數（秒） | 觸發後的行為 |
| --- | --- |
| `wait_query.timeout_seconds`（預設 25，範圍 0–30） | 這次等待結束。`timed_out=true` 表示這次未見新變化，保留原 ID 和 cursor 繼續等待。0 會立即取快照。 |
| `soft_timeout`（預設 600） | `soft_timeout_exceeded=true` 提醒呼叫端，任務繼續。進入提交前從入列起算，之後從提交時間起算。 |
| `hard_timeout`（預設 3600） | 從進入提交起算。若後續觀察發現超時且尚未完成，橋接器停止自動觀察並標記 `NEEDS_ATTENTION`。網頁可能繼續生成。用原 ID 執行 `recover_session` 對帳，再依 `next_action` 取得結果。 |

MCP 或 HTTP API 提交回應的 `submission_trace` 提供服務端入口、工具派送、任務建立及回應就緒時間，皆為 Unix 秒。`trace_id` 可對照服務日誌。用同一個冪等鍵重試時，會有本次呼叫的 trace ID，任務建立時間沿用原操作。

## 研究附件

WebUI 的「新建研究」和「在此会话继续研究」可选本机文件。MCP、HTTP API 和 CLI 的 `create_query` 与 `continue_query` 共用可选的 `attachments` 参数。

```json
{"text":"读取附件并总结。","request_id":"research-with-file-01","attachments":[{"filename":"notes.txt","content_base64":"aGVsbG8K"}]}
```

每轮最多 4 个非空文件，原始字节合计最多 40 MiB。`filename` 是文件名，`content_base64` 是原始字节的标准 Base64 编码。文件类型由 ChatGPT 网页决定。幂等核对包含文件名与 SHA-256，状态响应只回传附件元数据。通过网页上传并核对后才提交本轮消息，网页拒收时保留可安全重试的发送前状态。

附件字节在排队及发送前故障时私有暂存，进入提交或取消排队后删除暂存记录。暂存总量最多 400 MiB。备份可能包含快照当时尚在暂存的附件。ChatGPT 中的附件随远端会话管理。

## 结果

```json
{
  "ok": true,
  "query_id": "q_...",
  "message_id": "msg_...",
  "status": "COMPLETED",
  "generation_complete": true,
  "delivery_complete": true,
  "complete": true,
  "observed": {"model":"GPT-6","effort":"extra_high","verification":"ui_selection","backend_verified":false},
  "result": {
    "content": "可见回答文字，也可能为空而只有图片",
    "sources": [{"title":"来源","url":"https://example.com/"}],
    "artifacts": [{
      "artifact_id":"art_...","filename":"image-1.png","status":"AVAILABLE",
      "mime_type":"image/png","kind":"image","size_bytes":12345,"sha256":"...",
      "download_url":"http://<配置地址>:8766/artifacts/art_.../content",
      "requires_authentication":true
    }],
    "unsupported_outputs":[],
    "artifact_discovery":"rendered_ui_only",
    "delivery_phase":"AVAILABLE"
  }
}
```

`generation_complete` 描述已确认渲染的网页生成信号；`delivery_complete` 描述已发现的产物是否就绪；两者满足且操作完成后 complete 才为 true。部分失败保留文字和错误，不用空文件制造成功。默认读取最新消息，历史结果传 message_id。

产物状态：PENDING → DOWNLOADING → AVAILABLE／FAILED。记录先落库，临时字节验证后原子重命名并 fsync，再发布元数据。中断后的已完成文件可对账，不再次提问。默认单文件最多 50 MiB，超限、空文件、签名或容器不匹配均失败。

常见图片不超过 4 MiB 时，`read_artifact` 返回 MCP ImageContent；`inline_image=false` 强制使用 `ego-artifact://...` ResourceLink。MCP `resources/read` 沿用当前连接认证返回原文件字节，stdio 代理也转发资源模板和读取；资源协议 MIME 为 application/octet-stream，实际 MIME 见产物元数据。HTTP 下载支持 GET、HEAD 和 Range；需同一调用方的 Bearer token，不将 token 放入 URL。signed source URL、内部磁盘路径和浏览器认证头不回传。

普通外链保持为链接，不自动抓取任意地址。可见音视频、Canvas、iframe 或大 SVG 等尚未实现的原始导出会列入 unsupported_outputs，提供需 ChatGPT 登录的查看位置，交付完整性为 false。

## WebUI 边界

`/ui/api/login` 接受 MCP token，签发 HttpOnly、SameSite=Strict cookie（HTTPS 启用时额外设置 Secure），Max-Age=31536000。cookie 绑定 token 指纹，修改 token 并重启会使旧会话失效。UI 的写操作校验同源 Origin，token 不存 localStorage。

`POST /ui/api/mcp-info` 仅在已登录 UI 中返回当前身份的接入配置；用户可以显式复制自己的 token。默认 HTTP 不返回证书信息或显示 CA 下载入口；只有主动开启 HTTPS 时，`GET /ui/api/certificate` 才提供可选的公有证书。

CLI `download artifact_id --output path` 使用同一认证，验证大小及 SHA-256 后原子发布文件，不覆盖已有目标。普通 call 和 download 可用 EGO_BRIDGE_URL／EGO_BRIDGE_TOKEN 环境变量（HTTPS 才需额外的 CA 配置）作为独立 LAN 客户端；不需要服务端数据库。

主 token 的 UI 会话另有 `/ui/api/exports`，输入实际 ChatGPT 对话 URL，异步只读打包为 ZIP。它不受普通查询的固定專案限制，**没有对应 MCP 工具、普通 `/api/` 工具或 CLI 导出命令**。仅持有 Bearer 而没有 UI 登录 cookie 不能直接调用该入口。额外 Agent 身份不能执行账户级调试导出。

## 错误

错误含 error_code、message、stage、query_id、request_id、retryable、safe_to_retry、user_action_required、diagnostic_context、recovery_suggestion。已受理任务的失败由状态与 last_error 表达；参数错误返回 ok=false。MCP 调用方应检查 structuredContent 的 ok／status，不能只检查 isError。

下载与模型消息的重试是两类操作。OUTCOME_UNKNOWN 不允许强制重发；collect_artifacts 只重新取得输出字节。取消不承诺撤回已发送输入。网页只提供 UI 模型核验，不承诺实际后端型号。
