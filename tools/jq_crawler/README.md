# 聚宽社区文章爬取工具

抓取聚宽(JoinQuant)社区文章并汇总到 CSV。

> ⚠️ **重要前提**：本工具的默认 CSS 选择器是“通用候选集”，**不保证与聚宽当前页面 100% 匹配**——
> 聚宽可能改版、需要登录、或用 JS 动态渲染列表。开发环境无法联网核对其真实结构，
> 所以**你在本机首次运行时，很可能需要按下面“适配步骤”调整一次选择器或改用接口模式**。
> 工具框架（分页 / 限速 / 重试 / 去重 / 续爬 / CSV）是完整且经过自测的。

## 安装

```bash
cd tools/jq_crawler
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 先自测（不联网，验证脚本本身没问题）

```bash
python jq_crawler.py --selftest
# ✅ 自测全部通过 (列表解析 / 详情解析 / CSV 读写 / JSON接口解析)
```

## 基本用法

```bash
# 抓列表前 5 页元数据 -> jq_articles.csv
python jq_crawler.py --pages 5 -o jq_articles.csv

# 进入详情页抓全文（慢，且常需登录）
python jq_crawler.py --pages 3 --detail --cookie-file cookie.txt -o full.csv
```

CSV 列：`id, title, author, publish_time, views, comments, likes, summary, url, content, crawl_time`
（用 `utf-8-sig` 编码，Excel 直接打开不乱码。）

## 如果抓不到数据 —— 两种适配方式

### 方式 A：调 HTML 选择器（页面是服务端渲染时）

1. 加 `--dump` 把原始 HTML 存下来核对：
   ```bash
   python jq_crawler.py --pages 1 --dump
   # 生成 dump_page_1.html，浏览器打开看真实结构
   ```
2. 浏览器 F12「检查元素」，找到每篇文章的容器、标题链接、作者等真实 CSS 选择器。
3. 复制 `selectors.example.json` 为 `selectors.json`，填入真实选择器：
   ```bash
   python jq_crawler.py --pages 5 --selectors selectors.json -o out.csv
   ```

### 方式 B：用 JSON 接口模式（页面是 JS 动态渲染时，**更稳**）

很多社区列表其实是前端调一个 JSON 接口拿到的。这种情况下直接打接口比解析 HTML 可靠得多。

1. 浏览器 F12 → **Network** → 刷新社区列表页 → 找到返回文章列表的 XHR/Fetch 请求（响应是 JSON）。
2. 记下它的 **URL、请求参数、以及 JSON 里字段名**（哪个是标题、作者、时间…）。
3. 用 `--api` 模式跑：
   ```bash
   python jq_crawler.py \
     --api "https://www.joinquant.com/community/post/getList" \
     --api-params '{"page":"{page}","limit":"20"}' \
     --api-mapping '{"list_path":"data.list","id":"id","title":"subject","url":"url","author":"uname","publish_time":"ctime","views":"read","comments":"reply","likes":"vote","summary":"abstract"}' \
     --pages 5 -o api.csv
   ```
   - `--api-params` 里 `{page}` 会被替换成页码。
   - `--api-mapping` 的 `list_path` 是“文章数组在 JSON 里的键路径”（点分），其余是各列对应的接口字段名。**字段名按你在 Network 里看到的实际 JSON 改。**

## 需要登录的内容：传 Cookie

1. 浏览器登录聚宽 → F12 → Network → 任一请求的 Request Headers 里复制整段 `Cookie:` 值。
2. 存进 `cookie.txt`，运行时加 `--cookie-file cookie.txt`（或 `--cookie "..."`）。

## 全部参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `-o, --output` | CSV 输出路径 | `jq_articles.csv` |
| `--pages` | 抓取页数 | 3 |
| `--start-page` | 起始页码 | 1 |
| `--list-url` | 列表页 URL 模板，`{page}` 占位 | 社区列表 |
| `--detail` | 进入详情页抓全文 | 关 |
| `--delay` | 请求间隔秒（礼貌爬取） | 2.0 |
| `--retries` | 失败重试次数 | 3 |
| `--max-articles` | 最多抓取篇数（0=不限） | 0 |
| `--cookie` / `--cookie-file` | 登录 Cookie | 空 |
| `--selectors` | 选择器覆盖文件 | 内置默认 |
| `--dump` | 保存每页原始 HTML 调试 | 关 |
| `--api` / `--api-params` / `--api-mapping` | JSON 接口模式 | 关 |
| `--selftest` | 离线自测 | — |

## 断点续爬

输出 CSV 已存在时，工具会读出里面所有 `url` 自动跳过，重复运行只追加新文章，可随时中断再继续。

## 合规与礼貌

- 默认 2 秒间隔，请勿调太小；大量抓取请加大 `--delay`。
- 仅抓取你有权访问的公开内容，遵守聚宽的服务条款与 `robots.txt`，抓取数据请勿用于商业再分发。
- 本工具仅供个人学习研究使用。
