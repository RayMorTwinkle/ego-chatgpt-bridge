# 接口与产物交付（v0.5.0）

MCP `/mcp`、REST `POST /api/<tool>`、WebUI `POST /ui/api/tool/<name>` 共用 Service。MCP/REST 使用调用方 Bearer token，WebUI 使用登录 cookie；身份之间的任务和文件隔离。默认 EasyTier HTTP，TLS 可选。

## 默认 MCP 工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| ask | text；query_id=null、effort="extra_high"、attachments=null、request_id=null、wait_seconds=0 | 新建或在原会话追问，顺便等待可省一次调用 |
| wait | query_id；message_id=null、timeout_seconds=50 | 最多等待 300 秒，终止时直接返回最终结果 |
| get | query_id；message_id=null、verbose=false | 精简结果；verbose=true 返回旧版 get_query_result compact=false 默认完整 payload |
| list | status=null、limit=20（1..100） | 列出 query_id、title、status、rounds、updated_at |
| read_artifact | artifact_id；inline_image=true | 小图片直接返回，其余返回 MCP 资源链接 |
| cancel | query_id；message_id=null | 排队时取消，已开始则尽力停止并等待网页确认 |
| diagnose | query_id=null、message_id=null | 仅在 needs_you 且 what_to_do 无法解决时给维护者用 |

`text` 为 1..100000 字符，effort 仅 extra_high/high。wait_seconds 和 timeout_seconds 均为有限数字 0..300。省略 message_id 时固定读取调用开始时的最新轮次，等待不会跳到后来追加的轮次。等待中不返回正文；有 progressToken 时约每 10 秒发送 MCP progress 通知。MCP 使用 SSE 传输通知和最终响应；REST 只在等待结束时返回 JSON。

### 自动去重

省略 request_id：按身份、输入 query_id（新建为空）、原始文本、附件 SHA-256 的排序集合（保留重复数量）比较。未终止的操作或创建不超过 600 秒的操作直接复用；OUTCOME_UNKNOWN 在超过窗口后也继续复用。附件名、顺序、effort 不参与自动去重。匹配和入队之间不让出执行权，复用持久化操作记录，重启后仍有效。显式 request_id 则沿用原来的永久幂等语义，同键异参冲突；可用新键明确要求相同内容的新请求。

### 精简结果

```json
{"ok":true,"query_id":"q_...","message_id":"msg_...","round":1,"status":"running","elapsed_seconds":12.0,"answer_characters":0,"phase":"思考中","next":{"tool":"wait","arguments":{"query_id":"q_...","message_id":"msg_...","timeout_seconds":50}}}
```

```json
{"ok":true,"query_id":"q_...","message_id":"msg_...","round":1,"status":"done","elapsed_seconds":25.0,"answer_characters":4,"answer":"回答正文","files":[{"artifact_id":"art_...","filename":"report.pdf","mime_type":"application/pdf","size_bytes":12345,"download_url":"http://<配置地址>:8766/artifacts/art_.../content"}],"sources":["https://example.com/"]}
```

空 files/sources、非运行时 phase/next、未完成时 answer 均省略。正文为 Markdown。状态为 running/done/needs_you/failed/cancelled；错误或需处理时包含 `problem={code,message,what_to_do}`。参数错误返回 ok=false、status=failed 和 problem；任务状态失败仍可 ok=true，表示状态读取成功。

COMPLETED 且生成与文件完整才映射 done；其余终止但不完整状态映射 needs_you。QUEUED/PREPARING/SUBMITTING/WAITING_FOR_RESPONSE/GENERATING 映射 running。FAILED、CANCELLED 分别映射 failed、cancelled。最终交付不代表内容质量验收。

发送结果不确定且有提交记录和基线时，服务端自动 recover_session；已确认生成完成但文件未齐时自动 collect_artifacts。对外分别显示 running/核对中、running/收集文件中。每轮共 3 次、至少间隔 10 秒，次数存入数据库；执行名额满时延后且不消耗次数。失败耗尽后 needs_you，what_to_do 说明自动处理已停止。不支持的输出直接 needs_you。自动路径绝不重新发送。

MCP 普通工具结果只含 structuredContent 和一个 TextContent：未完成为紧凑 JSON；done 时文本为 Markdown 正文，末尾附 query_id、轮次、文件 artifact_id 和来源，纯文本客户端也能追问和取文件。消费者选一个通道读取。read_artifact 保持原有图片/资源协议，diagnose 保留完整诊断。

## WebUI 会话串

`get_thread(query_id)` 通过 REST、WebUI service dispatch 提供，不注册到 MCP。返回：

```json
{"ok":true,"query_id":"q_...","title":"用户的第一条输入","conversation_url":null,"status":"running","messages":[{"message_id":"msg_...","round":1,"status":"running","phase":"生成中","prompt":"用户原文","attachments":[],"answer":"已观察到的 Markdown 快照","partial":true,"files":[],"sources":[],"problem":null,"created_at":123.0,"updated_at":125.0}]}
```

messages 按 round 升序，包含全部轮次；answer 可以是部分正文，partial 表示生成尚未完整。query 级 status 为最新轮次状态，title 为第一条输入前 160 字符。旧 list_queries 每项新增 rounds，其他字段保留。

## 研究附件

WebUI 的「新建研究」和「在此会话继续研究」可选本机文件。MCP、HTTP API 和 CLI 的 `ask`（也兼容 `create_query` 与 `continue_query`） 共用可选的 `attachments` 参数。

```json
{"text":"读取附件并总结。","request_id":"research-with-file-01","attachments":[{"filename":"notes.txt","content_base64":"aGVsbG8K"}]}
```

每轮最多 4 个非空文件，原始字节合计最多 40 MiB。`filename` 是文件名，`content_base64` 是原始字节的标准 Base64 编码。文件类型由 ChatGPT 网页决定。幂等核对包含文件名与 SHA-256，状态响应只回传附件元数据。通过网页上传并核对后才提交本轮消息，网页拒收时保留可安全重试的发送前状态。

网页上传使用带唯一前缀的文件名，避免 Ego 暂存同名文件时自动改名。任务元数据保留调用方提供的原文件名。重试发送前的附件失败时，只替换包含本操作请求标记且文字完全一致的 Agent 草稿附件。

附件字节在排队及发送前故障时私有暂存，进入提交或取消排队后删除暂存记录。暂存总量最多 400 MiB。备份可能包含快照当时尚在暂存的附件。ChatGPT 中的附件随远端会话管理。

## 兼容工具

config.json 的 `mcp_legacy_tools` 默认 false；true 时额外注册旧工具（与 read_artifact、diagnose 共享同名工具）。REST、WebUI、CLI 不受目录开关影响，始终支持新旧名字。CLI 旧工具的 `--wait` 及退出码不变。

| 旧工具 | 行为 |
| --- | --- |
| create_query / continue_query | text、request_id 必填；continue_query 另需 query_id |
| get_query_status / get_query_result | query_id，可指定 message_id；完整状态与结果 |
| wait_query | 0..30 秒，默认 25；携带 cursor 等待变化，默认不返回正文 |
| list_queries | 旧状态筛选、limit/offset 分页，新增 rounds |
| health_check | 浏览器诊断 |
| recover_session / retry_query | 恢复原消息；只有未发送的任务可重试发送 |
| cancel_query / delete_query | 尽力停止／删除本地终止记录 |
| list_messages / get_conversation_info | 消息列表／会话映射 |
| list_artifacts / collect_artifacts / get_artifact | 文件列表／重收集／元数据 |
| read_artifact / diagnose | 文件读取／维护者诊断 |

旧提交仍支持 model="GPT-6"、context_mode="project" 或 "request_only_best_effort"、soft_timeout=600、hard_timeout=3600，要求 1 <= soft <= hard <= 86400。后者是提示词约束，不能保证项目记忆隔离。旧 get_query_result 保留 include_content、include_outputs、compact、content_format；REST 默认完整纯文本，旧 MCP 默认 compact=true、Markdown、不重复 outputs。wait_query 返回 cursor 和累计快照，正文应替换，不能拼接。

旧结果含 generation_complete、delivery_complete、complete、result_is_partial、result 等字段；COMPLETED 且 complete=true 才算交付。旧错误沿用 error_code、message、stage 等结构。REST read_artifact 返回文件元数据；MCP read_artifact 保持原生图片或 ResourceLink。

每身份最多 100 条等待消息，研究提交和 MCP 请求体上限 56 MiB，其他普通 API 上限 1 MiB。文件可用后才发布下载地址；默认单文件上限 50 MiB。资源通过同一 MCP 认证读取，HTTP 下载支持 HEAD/Range。外链不自动下载，音视频等不支持的原始输出会标记未完整交付。

## WebUI 边界

`/ui/api/login` 接受 MCP token，签发 HttpOnly、SameSite=Strict cookie（HTTPS 启用时额外设置 Secure），Max-Age=31536000。cookie 绑定 token 指纹，修改 token 并重启会使旧会话失效。UI 的写操作校验同源 Origin，token 不存 localStorage。

`POST /ui/api/mcp-info` 仅在已登录 UI 中返回当前身份的接入配置；用户可以显式复制自己的 token。默认 HTTP 不返回证书信息或显示 CA 下载入口；只有主动开启 HTTPS 时，`GET /ui/api/certificate` 才提供可选的公有证书。

CLI `download artifact_id --output path` 使用同一认证，验证大小及 SHA-256 后原子发布文件，不覆盖已有目标。普通 call 和 download 可用 EGO_BRIDGE_URL／EGO_BRIDGE_TOKEN 环境变量（HTTPS 才需额外的 CA 配置）作为独立 LAN 客户端；不需要服务端数据库。

主 token 的 UI 会话另有 `/ui/api/exports`，输入实际 ChatGPT 对话 URL，异步只读打包为 ZIP。它不受普通查询的固定專案限制，**没有对应 MCP 工具、普通 `/api/` 工具或 CLI 导出命令**。仅持有 Bearer 而没有 UI 登录 cookie 不能直接调用该入口。额外 Agent 身份不能执行账户级调试导出。
