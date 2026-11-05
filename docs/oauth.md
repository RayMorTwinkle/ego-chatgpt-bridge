# OAuth 与公网 MCP

可选启用 OAuth 2.1，使 ChatGPT 的自定义 MCP 应用和支持 OAuth 的 Agent 接入。推理仍由 Ego 操作 ChatGPT 网页完成，不使用 OpenAI 推理 API。

运行目录 `config.json` 增加：

```json
{
  "oauth_issuer": "https://research.example.com",
  "public_gateway_port": 14103,
  "allowed_hosts": ["10.126.126.11", "research.example.com"],
  "oauth_redirect_hosts": ["chatgpt.com"]
}
```

`oauth_issuer` 必须是无路径的 HTTPS origin。将可信 HTTPS 反代或 frp 指向 **127.0.0.1:14103**。私有主监听（例如 EasyTier 的8766）不得直接公开。公网地址是 `https://research.example.com/mcp`；WebUI 的 MCP 接入窗口也会显示它。

独立网关仅开放：

- `/mcp`
- `/.well-known/oauth-protected-resource` 及 `/mcp` 后缀
- `/.well-known/oauth-authorization-server`
- `/authorize`、`/token`、`/register`、`/revoke`

公网网关拒绝管理 token；只接受 OAuth 独立 access token。`/ui`、私网 REST API、普通文件地址及调试ZIP均不开放。授权浏览器跳转到私网 WebUI 的确认页，因此该浏览器需要连通 EasyTier；ChatGPT 云端只需连通公网的 OAuth/MCP。已有同身份登录可复用；否则在私网确认页输入 `.env` 中的管理 token。只读/写入权限、客户端名称和回调地址均显示后由用户确认。工作台可逐项撤销授权。

支持 DCR（不是 CIMD）、授权码+PKCE S256、public/confidential clients、refresh token 轮换及 RFC8707 resource audience。默认回调允许 `chatgpt.com` HTTPS 及 loopback HTTP（Codex）；其他 HTTPS host 需由管理员显式加入配置。仅精确注册的回调可用于授权；服务器不抓取任意客户端元数据 URL。

凭证保存在0600的 SQLite：access/refresh/code 存 SHA-256，客户端 secret 依 MCP SDK 接口要求存于私有数据库。access 有效一小时，refresh 最长30天并每次换新，授权最长一年。刷新重放会撤销整个授权。撤销或管理 token 轮换后旧凭证失效（管理 token 变更后需重启载入）。SQLite 备份含 OAuth 客户端凭据，必须作为私密归档保管。

权限：`research:read` 读取当前身份的研究及产物，`research:write` 创建/追问/停止/恢复/清理与浏览器健康检查。默认申请两者；只读连接不能执行写操作。OAuth 不授予 WebUI 调试导出权限。

文件经已认证 MCP 的 `resources/read` 返回字节；`read_artifact` 可返回原生图片。公网不开放匿名下载链接。不同客户端是否自动展示 ResourceLink、图片或附件需分别实测；协议成功不等于 ChatGPT 已展示附件。查询仍使用异步创建/轮询，以避开反代单次请求超时。

部署完应从外网验证可信 TLS、discovery、401 challenge、DCR/PKCE、MCP工具/资源及撤销，并确认 UI/exports/原管理 token 在公网不可用。监督器同时检查 worker 与独立网关；任一失效会重启子进程。

依据：[OpenAI MCP OAuth](https://developers.openai.com/plugins/build/auth)、[Cloudflare Origin Rules](https://developers.cloudflare.com/rules/origin-rules/create-api/)。

公网 OAuth 请求设有独立速率和16 KiB正文上限；DCR最多1000个客户端，pending请求会清理过期记录并限制总数。达到容量时返回明确失败，管理员可检查私有数据库，不会自动删除仍在使用的客户端。

公网入口只提供 MCP。当前基于 REST 的 `ego-bridge call/download` 继续走 EasyTier；Codex 等 Agent 可以通过公网 OAuth MCP 使用工具和资源。

WebUI 的“MCP 接入”并列展示 EasyTier Token 配置与公网 OAuth 配置，地址和配置分别复制。直连优先，速度取决于实际链路；复制公网 OAuth JSON 不会附带长期管理 Token。传统 Token 和 `.env` 凭据路径继续保留。
