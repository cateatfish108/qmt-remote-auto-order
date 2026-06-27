# 整套系统部署指南

本文档覆盖 **qmt-remote-auto-order** 全链路的部署：

```
┌─────────────────┐   HTTP POST /send_message   ┌──────────────────┐   WebSocket /ws   ┌─────────────────┐
│  聚宽策略端       │  ─────────────────────────► │  信号中转服务端    │ ◄───────────────► │  QMT 客户端       │
│ (转换后的代码)    │      Bearer <JWT>           │ (server/, 自建)   │   Bearer <JWT>    │ (桌面程序, Win)   │
└─────────────────┘                             └──────────────────┘                   └────────┬────────┘
                                                                                                 │ xtquant
                                                                                                 ▼
                                                                                          券商 QMT 终端 下单
```

三个部署对象：

| 组件 | 角色 | 运行环境 | 本文章节 |
|---|---|---|---|
| **信号中转服务端** | 转发信号、鉴权、重发 | Linux/Mac/Win 服务器 | [一](#一信号中转服务端) |
| **客户端（前端+后端）** | 接收信号、本地下单、UI | **Windows**（实盘）/ Mac、Linux（仅开发） | [二](#二客户端前端--后端) |
| **聚宽策略端** | 产生交易信号 | 聚宽 JoinQuant 网站 | [三](#三聚宽策略端) |

> 关键约束：**真实下单只能在 Windows 上跑客户端**（依赖 Windows 版 `xtquant` 的 `.pyd`/`.dll`）。Mac/Linux 只能作为开发环境，无法实盘下单。

---

## 一、信号中转服务端

替代官方未开源的 Go 二进制（`qmt-auto-order-server-*`）。代码在仓库 `server/` 目录。

### 1.1 环境要求
- Python 3.8+
- 一台能被客户端与聚宽访问到的机器（本地测试用 `127.0.0.1` 即可）

### 1.2 安装

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 1.3 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
server:
  host: 0.0.0.0      # 本地测试可填 127.0.0.1；对外服务填 0.0.0.0
  port: 8080
security:
  token_salt: "换成一个你自己的长随机字符串"   # ★ 必须与客户端"加密盐"完全一致
forward:
  ack_timeout: 5
  max_retries: 3
```

也可用环境变量覆盖：`HOST`、`PORT`、`TOKEN_SALT`。

### 1.4 运行

```bash
python app.py -c config.yaml
# 后台运行：
nohup python app.py -c config.yaml > server.log 2>&1 &
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
# {"code": 200, "online_clients": 0}
```

### 1.5 自检（无需真实客户端）

```bash
python test_e2e.py     # 看到 ✅ 全部通过 即正常
```

### 1.6 公网部署（强烈建议加 TLS）

token 与下单信号默认明文传输，公网务必在前面挂反向代理换成 `wss://`/`https://`。nginx 示例：

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;        # 支持 WebSocket
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;                      # 长连接不被掐断
    }
}
```

之后客户端地址用 `wss://your.domain.com/ws`，策略端自动用 `https://your.domain.com/send_message`。

### 1.7 systemd 守护（可选）

`/etc/systemd/system/qmt-server.service`：

```ini
[Unit]
Description=qmt signal relay server
After=network.target

[Service]
WorkingDirectory=/opt/qmt-remote-auto-order/server
ExecStart=/opt/qmt-remote-auto-order/server/.venv/bin/python app.py -c config.yaml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now qmt-server
sudo journalctl -u qmt-server -f      # 看日志
```

---

## 二、客户端（前端 + 后端）

客户端是 PyWebView 桌面应用：**Python 后端（`api/`、`main.py`）+ Vue3 前端（`gui/`）** 打包成一个程序。

普通用户直接用打包好的安装包；开发者按下面从源码运行/打包。

### 2.1 直接用安装包（推荐普通用户）

1. 到 Releases 下载 Windows 安装包并安装。
2. 若打开白屏，安装 [WebView2 运行时](https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/) 后重开。
3. 直接跳到 [2.6 客户端连接配置](#26-客户端连接配置)。

### 2.2 源码运行 — 环境要求
- Node.js ≥ 20（建议 22）+ pnpm
- Python ≥ 3.8（实盘需 Windows）
- Windows 还需券商的 QMT/迅投 miniQMT 终端

### 2.3 安装依赖

```bash
# 仓库根目录
mv .example.env .env        # 准备环境变量（见下表）

npm run init                # 清缓存 + 装前端依赖 + 建 Python venv(pyapp/pyenv) + 装 requirements
```

> `npm run init` 会自动创建 Python 虚拟环境 `pyapp/pyenv` 并安装 `pyapp/requirements.txt`。
> Linux 上它还会先 `apt install` 一批 GTK/WebKit 系统库（见 `package.json` 的 `initInstallLinuxPre`）。

`.env` 关键项：

| 变量 | 含义 | 默认 |
|---|---|---|
| `VITE_WS_URL` | 前端默认 WS 地址 | `ws://127.0.0.1:8080/ws` |
| `VITE_API_URL` | 前端默认 HTTP 地址 | `http://127.0.0.1:8080` |
| `AUTO_CONNECTION_WS` | 是否自动连接 WS | `1` |
| `USE_FIXED_WS_URL` | 是否强制使用固定 WS 地址 | `0` |
| `WS_URL_FIXED` | 固定 WS 地址（上一项为 1 时生效） | `ws://127.0.0.1:8080/ws` |

> 把这几个默认值改成你自建服务端的地址（如 `ws://你的IP:8080/ws`）。

### 2.4 初始化数据库

```bash
npm run alembic             # 生成迁移并 upgrade head（SQLite，数据落在用户数据目录）
```

### 2.5 开发模式运行

```bash
npm run start               # 同时起 Vite 前端(5173) + PyWebView 客户端窗口
```

- 前端开发端口：`5173`（`Config.devPort`）
- 后端通过 `main.py` 启动窗口并加载前端

### 2.6 打包

```bash
npm run build               # 按当前系统打包
# Windows 产物：build/ 下的安装包(exe)
# macOS：dmg    Linux：deb
```

常用变体（见 `package.json`）：`npm run build:windows` / `build:macos` / `build:linux` / `build:folder`（文件夹模式）/ `build:cef`（CEF 内核）。

### 2.6 客户端连接配置

在客户端「设置」页：

1. **运行模式**：选 **本地/私有服务器**（即 salt 模式，`run_model_type=1`）。
2. **加密盐**：填写与服务端 `config.yaml` 里 `token_salt` **完全一致**的值。
3. **服务器地址**：`ws://<服务端IP>:8080/ws`（本机测试用 `127.0.0.1`，公网用 `wss://域名/ws`）。
4. **QMT 配置**（Windows 实盘）：填 miniQMT 的 `userdata_mini` 路径与资金账号 `client_id`。
5. 保存后客户端会自动连服务端，服务端日志出现 `QMT 客户端已连接 uid=<机器id>` 即成功。

---

## 三、聚宽策略端

让聚宽策略在下单时把信号 POST 给你的服务端。

1. 在客户端「策略/任务」里新建任务，拿到 `strategy_code`，选择仓位模式（跟随 / 动态调整）。
2. 把你的聚宽策略源码贴进客户端的「一键转换代码」，点转换。
   - 转换器会自动注入：服务端 `/send_message` 地址、签名好的 `TOKEN`、以及 `begin/run/end/dividends` 各状态的上报逻辑（见 `api/tools/template.py`）。
3. 复制转换后的代码 → 粘贴到聚宽，运行**模拟交易/回测**。
4. 策略一旦触发下单（`order_target` / `order_value` / `order` / `order_target_value`），就会 POST 信号到服务端 → 转发到客户端 → QMT 本地下单。

> 转换后的代码里 `TOKEN` 与服务器地址是写死进去的；换了服务端地址或 salt，需要重新转换并替换聚宽里的代码。

---

## 四、端到端连通验证清单

按顺序确认，任一步失败就地排查：

1. **服务端起来**：`curl http://<IP>:8080/health` 返回 `online_clients`。
2. **客户端连上**：客户端「设置」连接状态为已连接；服务端日志有 `QMT 客户端已连接 uid=...`。
3. **手动打一笔信号**（模拟聚宽，验证转发）：

   ```bash
   # 用与 salt 一致的密钥签一个带 u 的 token（u 必须等于客户端机器的 unique_id）
   TOKEN=$(python -c "import jwt;print(jwt.encode({'u':'<客户端unique_id>','p':'local'},'<你的salt>',algorithm='HS256'))")
   curl -X POST http://<IP>:8080/send_message \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"run_params":"sim_trade","strategy_code":"S001","state":"run","positions":[],
          "params":{"security":"000001.XSHE","price":12.3,"amount":100,"is_buy":true,
                    "add_time":"2026-01-01 09:30:00","total_value":100000}}'
   # 返回 {"code":200,"data":"ok"}，客户端应出现"接收到信号单"
   ```

4. **真实聚宽**：跑转换后的策略，触发下单，观察客户端受理 + QMT 下单。

### 常见错误对照

| 现象 | 原因 / 处理 |
|---|---|
| `send_message` 返回 `401` | salt 不一致，或没带 `Authorization: Bearer ` 头 |
| 返回 `{"code":404,"data":"client offline"}` | 该 `u` 没有客户端在线：WS 没连上，或 token 里的 `u` 与客户端 unique_id 不一致 |
| WS 连不上 | 端口/防火墙未放行；URL 末尾不是 `/ws`；公网未配 WebSocket 反代 |
| 客户端打开白屏 | 安装 WebView2 运行时后重开 |
| 客户端连上但不下单 | 未连 QMT / 资金账号未配 / 任务未开启 / 非 Windows 环境 |

---

## 五、端口与默认值速查

| 项 | 默认值 | 出处 |
|---|---|---|
| 服务端端口 | `8080` | `server/config.yaml` |
| WS 路径 | `/ws` | `.example.env` `VITE_WS_URL` |
| 信号上报路径 | `/send_message` | `api/tools/template.py` |
| 健康检查 | `/health` | `server/app.py` |
| 前端开发端口 | `5173` | `pyapp/config/config.py` `devPort` |
| 鉴权算法 | JWT HS256，密钥=salt | `api/tools/token_manager.py` |
| 路由 key | JWT payload 的 `u`（机器 unique_id） | `api/remote.py` / `template.py` |

---

## 六、安全建议

- **salt 当密钥保管**：泄露等于别人能伪造下单信号。用足够长的随机串，不要提交进 git（`server/.gitignore` 已忽略 `config.yaml`）。
- **公网必上 TLS**：用 `wss://`/`https://`，否则 token 与下单信号明文可被截获。
- **最小暴露**：服务端只对需要的来源开放 8080；能用内网就别上公网。
