# qmt 开源信号中转服务端 (Python)

用 Python 复刻官方未开源的 Go 二进制服务端（`qmt-auto-order-server-*`），
让你无需依赖闭源二进制即可自建/私有部署信号中转服务。

## 它做什么

在「聚宽回测/模拟策略端」与「本机 QMT 客户端」之间转发交易信号：

```
聚宽策略 (转换后的代码)
   │  HTTP POST /send_message   (Authorization: Bearer <JWT>)
   ▼
本服务端  ── 按 JWT 里的 unique_id 路由 ──►  QMT 客户端 (WebSocket /ws)
   ▲                                              │
   └──────────────  ack 确认 + 重发  ◄────────────┘
```

- **鉴权**：客户端与策略端都用同一个「加密盐」(`salt`) 以 JWT(HS256) 签名，服务端用同一个 salt 校验。
- **路由**：JWT 的 payload 里有 `u`（机器 unique_id）。服务端把某策略的信号转发给同一 `u` 下在线的 QMT 客户端。
- **可靠性**：消息带 `id`，客户端回 `ack`；未在超时内收到 ack 会自动重发（对应官方的“重发机制”）。

> 仅实现 **salt 鉴权模式**（客户端设置里 `run_model_type=1`，即“本地/私有服务器”模式）。
> 官方的账号登录/注册模式（`run_model_type=2`）依赖官方账户体系，不在自建范围内。

## 协议细节（与客户端代码对齐）

| 方向 | 端点 | 说明 |
|---|---|---|
| QMT 客户端 → 服务端 | `GET /ws` | WS 升级，请求头 `Authorization: Bearer <JWT{u}>`；之后只回 `ack` |
| 策略端 → 服务端 | `POST /send_message` | body 为信号 JSON，头 `Authorization: Bearer <JWT{u,p}>` |
| 服务端 → 客户端 | (WS 下行) | 信封 `{"id": "...", "content": "<内层JSON字符串>"}`，`content` 为**二次编码字符串** |
| 客户端 → 服务端 | (WS 上行) | `{"type":"ack","id":"...","timestamp":<ms>}` |

- 健康检查：`GET /health` → `{"code":200,"online_clients":N}`
- 信号 `state` 取值：`begin` / `run` / `end` / `dividends`（服务端透明转发，不解析业务）

## 部署

### 1. 安装依赖

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，**`security.token_salt` 必须与客户端“加密盐”完全一致**：

```yaml
server:
  host: 0.0.0.0
  port: 8080
security:
  token_salt: "你的随机长字符串-与客户端一致"
forward:
  ack_timeout: 5
  max_retries: 3
```

也可用环境变量覆盖：`HOST`、`PORT`、`TOKEN_SALT`。

### 3. 运行

```bash
python app.py -c config.yaml
# 后台运行:
nohup python app.py -c config.yaml > server.log 2>&1 &
```

## 客户端如何对接

在客户端「设置」里：

1. 运行模式选择 **本地/私有服务器**（salt 模式）。
2. **加密盐** 填写与服务端 `token_salt` 相同的值。
3. **服务器地址** 填 `ws://<服务器IP>:8080/ws`（与官方客户端 `.example.env` 中 `VITE_WS_URL` 一致）。
4. 策略端：在客户端用“一键转换代码”把聚宽策略转换后粘贴到聚宽运行——转换器会自动把
   `http://<服务器IP>:8080/send_message` 和 token 写进策略代码。

> 公网部署强烈建议在前面挂一层 TLS 反代（nginx/caddy），把 `ws://`/`http://` 换成
> `wss://`/`https://`，避免 token 与下单信号明文传输。

## 测试

无需真实客户端，内置端到端测试会模拟 WS 客户端 + 策略 POST：

```bash
python test_e2e.py
# ✅ 全部通过
```

覆盖：login 下行、begin/run 信号转发、content 二次编码、ack 确认、错误 salt 拒绝(401)、离线 uid 处理。
