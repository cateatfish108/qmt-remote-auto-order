#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聚宽(JoinQuant)社区文章爬取工具 -> 汇总到 CSV

特性:
  - 列表页分页抓取, 可选进入详情页抓全文
  - 礼貌爬取: 请求间隔 + 失败指数退避重试
  - 去重 + 断点续爬 (已在 CSV 里的文章自动跳过)
  - 中文 CSV (utf-8-sig, Excel 直接打开不乱码)
  - 选择器 / 接口完全可配置 (聚宽改版时只改 selectors.json, 不动代码)
  - 支持携带登录 Cookie 抓取需要登录的内容

⚠️ 关于选择器:
  本工具内置的 CSS 选择器是"通用候选集", 不保证与聚宽当前页面 100% 匹配
  (聚宽可能改版/需要登录/用 JS 动态渲染)。若抓不到数据:
    1. 用 --dump 保存原始 HTML, 浏览器打开核对真实结构;
    2. 或浏览器 F12 → Network 找到返回文章列表的 JSON 接口, 用 --api 模式;
    3. 把正确的选择器写进 selectors.json (见 --selectors)。

用法示例:
  # 抓列表前 5 页的元数据
  python jq_crawler.py --pages 5 -o articles.csv

  # 带登录 cookie, 并进入详情页抓全文
  python jq_crawler.py --pages 3 --detail --cookie-file cookie.txt -o full.csv

  # 自测解析逻辑(不联网)
  python jq_crawler.py --selftest

  # JSON 接口模式 (你已从 F12 找到接口)
  python jq_crawler.py --api "https://www.joinquant.com/community/post/getList" \
      --api-params '{"page":"{page}","limit":"20"}' --pages 5 -o api.csv
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("缺少依赖, 请先安装: pip install -r requirements.txt")


BASE = "https://www.joinquant.com"
# 默认列表页; {page} 会被替换成页码
DEFAULT_LIST_URL = "https://www.joinquant.com/view/community/list?listType=1&page={page}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# 可配置选择器 (默认值; 可被 selectors.json 覆盖)
# --------------------------------------------------------------------------- #
@dataclass
class Selectors:
    # 列表页: 每篇文章所在的容器, 用候选列表, 命中第一个有结果的
    item: List[str] = field(default_factory=lambda: [
        "div.community-list-item", "li.post-item", "div.post-item",
        "div.article-item", "ul.community-list li", "div.list-item",
    ])
    # 容器内: 标题链接 (取 text 作标题, href 作链接)
    title_link: List[str] = field(default_factory=lambda: [
        "a.title", "h3 a", "h2 a", "a.post-title", ".title a", "a[href*='community/detail']",
    ])
    author: List[str] = field(default_factory=lambda: [
        ".author", ".user-name", ".nickname", "a.user", ".name",
    ])
    publish_time: List[str] = field(default_factory=lambda: [
        ".time", ".date", ".publish-time", ".create-time", "time",
    ])
    views: List[str] = field(default_factory=lambda: [
        ".view", ".views", ".read-count", ".pv",
    ])
    comments: List[str] = field(default_factory=lambda: [
        ".comment", ".comments", ".comment-count", ".reply",
    ])
    likes: List[str] = field(default_factory=lambda: [
        ".like", ".likes", ".like-count", ".vote",
    ])
    summary: List[str] = field(default_factory=lambda: [
        ".summary", ".desc", ".excerpt", ".content-summary", "p.intro",
    ])
    # 详情页正文容器
    detail_body: List[str] = field(default_factory=lambda: [
        "div.article-content", "div.post-content", "div.detail-content",
        "div.content", "article",
    ])

    @classmethod
    def load(cls, path: Optional[str]) -> "Selectors":
        sel = cls()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(sel, k):
                    setattr(sel, k, v if isinstance(v, list) else [v])
            print(f"[选择器] 已从 {path} 加载覆盖")
        return sel


# --------------------------------------------------------------------------- #
# 抓取工具
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, cookie: str = "", delay: float = 2.0,
                 retries: int = 3, timeout: int = 20):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": BASE})
        if cookie:
            self.s.headers["Cookie"] = cookie
        self.delay = delay
        self.retries = retries
        self.timeout = timeout

    def get(self, url: str, as_json: bool = False, params: dict = None):
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                time.sleep(self.delay)  # 礼貌间隔
                return r.json() if as_json else r.text
            except Exception as e:
                last = e
                wait = 2 ** attempt
                print(f"  [重试 {attempt+1}/{self.retries}] {url} 失败: {e}; {wait}s 后重试")
                time.sleep(wait)
        print(f"  [放弃] {url}: {last}")
        return None


def _pick_text(node, selectors: List[str]) -> str:
    for sel in selectors:
        el = node.select_one(sel)
        if el:
            return el.get_text(strip=True)
    return ""


def _pick_link(node, selectors: List[str]):
    for sel in selectors:
        el = node.select_one(sel)
        if el:
            href = el.get("href", "")
            return el.get_text(strip=True), (urljoin(BASE, href) if href else "")
    return "", ""


def _extract_id(url: str) -> str:
    # 从 .../community/detail/123456 这类 URL 提取末尾数字 id
    for part in reversed(url.rstrip("/").split("/")):
        if part.isdigit():
            return part
    return url


def parse_list_html(html: str, sel: Selectors) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    container = None
    for s in sel.item:
        container = soup.select(s)
        if container:
            break
    if not container:
        return []
    for node in container:
        title, link = _pick_link(node, sel.title_link)
        if not title and not link:
            continue
        items.append({
            "id": _extract_id(link),
            "title": title,
            "url": link,
            "author": _pick_text(node, sel.author),
            "publish_time": _pick_text(node, sel.publish_time),
            "views": _pick_text(node, sel.views),
            "comments": _pick_text(node, sel.comments),
            "likes": _pick_text(node, sel.likes),
            "summary": _pick_text(node, sel.summary),
            "content": "",
            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    return items


def parse_detail_html(html: str, sel: Selectors) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in sel.detail_body:
        el = soup.select_one(s)
        if el:
            return el.get_text("\n", strip=True)
    return ""


def parse_api_json(data, mapping: Dict[str, str]) -> List[Dict]:
    """
    JSON 接口模式: data 为接口返回, mapping 把接口字段映射到我们的列。
    需要 mapping 至少包含 'list_path'(列表所在的键路径, 点分) 与各字段名。
    """
    # 取出列表
    node = data
    for key in mapping.get("list_path", "data").split("."):
        if isinstance(node, dict):
            node = node.get(key, [])
    if not isinstance(node, list):
        return []
    out = []
    for it in node:
        row = {"crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "content": ""}
        for col in ["id", "title", "url", "author", "publish_time",
                    "views", "comments", "likes", "summary"]:
            src = mapping.get(col)
            row[col] = str(it.get(src, "")) if src else ""
        if row["url"]:
            row["url"] = urljoin(BASE, row["url"])
            if not row["id"]:
                row["id"] = _extract_id(row["url"])
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
FIELDS = ["id", "title", "author", "publish_time", "views", "comments",
          "likes", "summary", "url", "content", "crawl_time"]


def load_seen(path: str) -> set:
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                key = row.get("url") or row.get("id")
                if key:
                    seen.add(key)
    return seen


def append_csv(path: str, rows: List[Dict]):
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def crawl(args):
    sel = Selectors.load(args.selectors)
    cookie = args.cookie or ""
    if args.cookie_file and os.path.exists(args.cookie_file):
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    fetcher = Fetcher(cookie=cookie, delay=args.delay, retries=args.retries)

    seen = load_seen(args.output)
    print(f"[起始] 输出={args.output} 已存在 {len(seen)} 篇, 将自动跳过")

    api_params = json.loads(args.api_params) if args.api_params else {}
    api_mapping = json.loads(args.api_mapping) if args.api_mapping else {}

    total_new = 0
    for page in range(args.start_page, args.start_page + args.pages):
        print(f"[第 {page} 页]")
        if args.api:
            params = {k: str(v).replace("{page}", str(page)) for k, v in api_params.items()}
            data = fetcher.get(args.api, as_json=True, params=params)
            items = parse_api_json(data, api_mapping) if data else []
        else:
            url = args.list_url.replace("{page}", str(page))
            html = fetcher.get(url)
            if html is None:
                continue
            if args.dump:
                with open(f"dump_page_{page}.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  已保存 dump_page_{page}.html")
            items = parse_list_html(html, sel)

        if not items:
            print("  本页未解析到文章 (选择器可能不匹配, 见 --dump / --selectors)")
            continue

        new_rows = []
        for it in items:
            key = it.get("url") or it.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            if args.detail and it.get("url"):
                dhtml = fetcher.get(it["url"])
                if dhtml:
                    it["content"] = parse_detail_html(dhtml, sel)
            new_rows.append(it)

        if new_rows:
            append_csv(args.output, new_rows)
            total_new += len(new_rows)
            print(f"  本页新增 {len(new_rows)} 篇 (累计 {total_new})")
        if args.max_articles and total_new >= args.max_articles:
            print(f"[停止] 达到上限 {args.max_articles}")
            break

    print(f"[完成] 共新增 {total_new} 篇 -> {args.output}")


# --------------------------------------------------------------------------- #
# 自测 (不联网, 验证解析逻辑)
# --------------------------------------------------------------------------- #
SAMPLE_HTML = """
<html><body>
<ul class="community-list">
  <li class="post-item">
    <h3><a href="/view/community/detail/100001">量化择时策略分享</a></h3>
    <span class="author">张三</span>
    <span class="time">2026-06-01</span>
    <span class="views">1234</span>
    <span class="comment">12</span>
    <span class="like">56</span>
    <p class="summary">这是一篇关于择时的文章摘要</p>
  </li>
  <li class="post-item">
    <h3><a href="/view/community/detail/100002">多因子选股入门</a></h3>
    <span class="author">李四</span>
    <span class="time">2026-06-02</span>
    <span class="views">888</span>
    <span class="comment">3</span>
    <span class="like">20</span>
    <p class="summary">多因子模型基础</p>
  </li>
</ul>
</body></html>
"""

SAMPLE_DETAIL = """
<html><body><div class="article-content">
<p>第一段正文。</p><p>第二段正文。</p>
</div></body></html>
"""


def selftest():
    sel = Selectors()
    rows = parse_list_html(SAMPLE_HTML, sel)
    assert len(rows) == 2, f"应解析出2篇, 实际 {len(rows)}"
    assert rows[0]["title"] == "量化择时策略分享", rows[0]
    assert rows[0]["id"] == "100001", rows[0]
    assert rows[0]["url"].endswith("/view/community/detail/100001"), rows[0]
    assert rows[0]["author"] == "张三"
    assert rows[0]["views"] == "1234"
    assert rows[1]["title"] == "多因子选股入门"
    body = parse_detail_html(SAMPLE_DETAIL, sel)
    assert "第一段正文" in body and "第二段正文" in body, body

    # CSV 往返
    tmp = "._selftest.csv"
    if os.path.exists(tmp):
        os.remove(tmp)
    append_csv(tmp, rows)
    seen = load_seen(tmp)
    assert len(seen) == 2, seen
    append_csv(tmp, rows)  # 重复不应增加(由 crawl 的 seen 控制, 这里只验证读取)
    os.remove(tmp)

    # JSON 接口模式
    api_data = {"data": {"list": [
        {"id": 7, "subject": "API文章", "url": "/view/community/detail/7",
         "uname": "王五", "ctime": "2026-06-03", "read": 9, "reply": 1, "vote": 2,
         "abstract": "摘要"}]}}
    mapping = {"list_path": "data.list", "id": "id", "title": "subject",
               "url": "url", "author": "uname", "publish_time": "ctime",
               "views": "read", "comments": "reply", "likes": "vote",
               "summary": "abstract"}
    arows = parse_api_json(api_data, mapping)
    assert len(arows) == 1 and arows[0]["title"] == "API文章", arows
    assert arows[0]["author"] == "王五" and arows[0]["id"] == "7", arows

    print("✅ 自测全部通过 (列表解析 / 详情解析 / CSV 读写 / JSON接口解析)")


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="聚宽社区文章爬取 -> CSV")
    p.add_argument("-o", "--output", default="jq_articles.csv", help="CSV 输出路径")
    p.add_argument("--pages", type=int, default=3, help="抓取页数")
    p.add_argument("--start-page", type=int, default=1, help="起始页码")
    p.add_argument("--list-url", default=DEFAULT_LIST_URL,
                   help="列表页 URL 模板, 用 {page} 占位页码")
    p.add_argument("--detail", action="store_true", help="进入详情页抓全文(慢)")
    p.add_argument("--delay", type=float, default=2.0, help="请求间隔秒(礼貌爬取)")
    p.add_argument("--retries", type=int, default=3, help="失败重试次数")
    p.add_argument("--max-articles", type=int, default=0, help="最多抓取篇数(0=不限)")
    p.add_argument("--cookie", default="", help="登录 Cookie 字符串")
    p.add_argument("--cookie-file", default="", help="从文件读取 Cookie")
    p.add_argument("--selectors", default="", help="selectors.json 覆盖默认选择器")
    p.add_argument("--dump", action="store_true", help="保存每页原始HTML便于调试选择器")
    # JSON 接口模式
    p.add_argument("--api", default="", help="JSON 接口 URL(启用接口模式)")
    p.add_argument("--api-params", default="", help='接口参数 JSON, 如 {"page":"{page}"}')
    p.add_argument("--api-mapping", default="",
                   help='字段映射 JSON, 含 list_path 与各列对应的接口字段名')
    p.add_argument("--selftest", action="store_true", help="离线自测解析逻辑")
    return p


def main():
    args = build_parser().parse_args()
    if args.selftest:
        selftest()
        return
    crawl(args)


if __name__ == "__main__":
    main()
