# 安全部署与访问边界

forecast-loop 默认是单操作员、私有部署的研究系统，不是面向公网的多租户服务。
API 和 Web 服务应继续只监听 loopback；上游只读数据、handoff、checkpoint、
SQLite 与 User Judgment Wiki 也都留在受控的运行节点。

## 应用层访问规则

Demo 模式用于本地开发，保持无需 token 即可操作。只要
`VERICOUNCIL_EXECUTION_PROVIDER` 不是 `demo`，以下路由就必须携带 operator
Bearer token：

- `POST /api/user-judgments`
- `POST /api/runs`
- `POST /api/evaluations/run`
- `GET /api/user-judgments/targets`
- `GET /api/user-judgments`
- `GET /api/user-judgments/{judgment_id}`
- `GET /api/user-judgments/{judgment_id}/wiki`

Demo 的免 token 仅适用于 Demo 数据：User Judgment 的目标、列表、详情、Wiki
和写入都会绑定 `mode=demo`，混合数据库中的 Live ID 按不存在处理。Demo 也
完全禁用 `POST /api/evaluations/run`，因为该接口只允许评价 Live Forecast。
因此切换到 Demo 不能成为读取或修改 Live 记录的旁路。

健康检查、预测、公开 Wiki 和 benchmark 等只读 GET 在应用层保持公开，便于
本机状态页与只读展示使用。若整个站点对远程用户可达，应在同源反向代理再保护
整个 origin，不能把“公开 GET”理解为适合直接暴露到互联网。

`VERICOUNCIL_USER_JUDGMENT_ACTOR_ID` 只表示记录归属，不是身份认证。Bearer
token 只建立单操作员边界，也不提供不同用户之间的权限隔离。

## Token 配置

在非 Demo 服务的私有环境文件中设置：

```dotenv
FORECAST_LOOP_OPERATOR_TOKEN=<至少 32 个无空白字符的随机值>
```

建议用密码管理器生成至少 32 个随机字节（例如 64 位十六进制），并将环境文件
权限限制给运行服务的本机用户。示例文件故意留空；空值在非 Demo 模式下会让受
保护路由返回 `503` 并保持 fail closed。配置了 token 后，缺失、格式不对或不
匹配的凭证统一返回 `401` 与 `WWW-Authenticate: Bearer`，响应不会回显 token。

调用示例：

```bash
curl \
  -H "Authorization: Bearer ${FORECAST_LOOP_OPERATOR_TOKEN}" \
  http://127.0.0.1:8000/api/user-judgments
```

不要通过 `?token=`、`?operator_token=` 或其他 URL 参数传 token；URL 经常被
访问日志、浏览器历史和监控系统记录。也不要把 token 放进：

- `VITE_*`、前端源码、静态 JSON 或构建产物；
- `localStorage`、`sessionStorage` 或可由浏览器脚本读取的配置；
- Git、Issue、日志、截图、命令示例中的实际值。

轮换时先为服务更新私有环境，再原子重启 API。旧 token 应立即失效；确认健康
检查和带新 token 的受保护请求都正常后，再删除密码管理器中的旧值。

## 推荐访问方式

### 方式一：SSH tunnel

这是单节点私有运行时的首选方式。API 与 Web 继续绑定 `127.0.0.1`，操作员
从受控工作站通过 SSH 通道转发端口。SSH 负责链路加密，不需要修改应用监听
地址，也不会把数据库或 handoff 目录变成共享写路径。

### 方式二：同源 TLS 反向代理

确需从受控网络访问时，只让反向代理监听 TLS 入口：

- `/` 代理到 loopback 上的前端；
- `/api/` 代理到 `127.0.0.1:8000`；
- 不直接开放 FastAPI 的 8000 端口；
- 只信任该代理写入的 forwarded headers，并限制请求体、超时和访问日志；
- 在最外层清除客户端提供的 `Forwarded` / `X-Forwarded-For`，再以实际
  连接地址重建；不要把未经验证的转发链用于鉴权或限流；
- 在最外层代理按操作员身份或来源地址限流；容器内置 Web 代理仅提供
  1 MiB 请求体上限，不替代公网入口的速率限制；
- 在代理层完成用户认证，然后由代理在服务端为受保护 API 注入 operator
  `Authorization` header，浏览器永远看不到 operator token；
- 清理客户端传入的 `Authorization`，避免把外部 header 原样转发成内部
  operator 身份。

同源代理可以避免为前端扩大 CORS。CORS 只控制浏览器跨域读取，不是认证或网络
访问控制；不能用宽泛的 CORS 配置代替 TLS、代理认证、防火墙或受控网络入口。
本地开发 CORS 只接受 `localhost:5173` 与 `127.0.0.1:5173`，只放行
`GET`/`POST` 及 `Authorization`、`Content-Type`、`Idempotency-Key`，
并关闭 credentialed CORS。被允许的 Origin 仍必须通过 Bearer 认证。

当前前端不应内嵌 operator token。没有可信同源代理时，浏览器只使用公开 GET，
写操作通过本机 CLI、SSH 后的受控调用或其他不暴露 token 的 operator 工作流
完成。

容器内置 Web 服务会返回 CSP、`nosniff`、拒绝 framing、referrer 和
permissions 等基础安全响应头。若改用其他静态托管或反向代理，必须在该入口
提供等价策略，并在运行时检查实际响应头。

## 日志与数据边界

- API 不记录 `Authorization` header；反向代理也应屏蔽该 header，并对查询
  参数做脱敏。
- `data/wiki`、`data/handoffs`、`data/user-wiki`、SQLite、checkpoint 和上游只读副本不
  作为静态目录提供。
- 生产数据库迁移与备份由本机 operator 执行；Web 请求不能获得文件系统写入
  handoff、Wiki 发布区或上游交易数据库的权限。
- 服务仍应使用最小权限的专用本机账号，并保持所有上游数据源只读。
