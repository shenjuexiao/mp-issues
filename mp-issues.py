# mp-issues-0.4.0.py
# github.com/shenjuexiao
# 20261124

# mp-issues.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
公众号文章转换 Issues 主脚本 v0.4.0-20261124

流程：
  第一步：{mp_string} 更新 —— 保证 mp-issues.json 与 mp.md 一致
  第二步：{issues_id} 更新 —— 保证 GitHub Issues 与 mp-issues.json 一致
"""

import os
import re
import sys
import json
import time
import html
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import html2text

# ---------------------------- 常量 ----------------------------
CST = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent
MP_MD = ROOT / "mp.md"
INDEX_JSON = ROOT / "mp-issues.json"
LOG_FILE = ROOT / "mp-issues.log"
ISSUES_MD = ROOT / "issues.md"
MP_JSON_DIR = ROOT / "mp_json"
ISSUES_JSON_DIR = ROOT / "issues_json"
ISSUES_MD_DIR = ROOT / "issues_md"

MP_LINK_RE = re.compile(r"https://mp\.weixin\.qq\.com/s/([A-Za-z0-9_-]+)")
MP_STRING_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")

LABEL_MP = "MP"
LABEL_OUT = "OUT"

INDEX_VERSION = 1

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

HTTP_CONNECT_TIMEOUT = 10
HTTP_READ_TIMEOUT = 20
FETCH_RETRY_DELAYS = [2, 4]  # 第 1 次失败等 2s，第 2 次失败等 4s

# ---------------------------- 日志 ----------------------------
class MpLogFilter(logging.Filter):
    """为日志注入上下文字段（mp_number/mp_string/issues_id），缺失时填空。"""

    def filter(self, record):
        for f in ("mp_number", "mp_string", "issues_id"):
            if not hasattr(record, f):
                setattr(record, f, "-")
        return True


def setup_logger():
    """初始化日志：同时输出到 stderr 与 mp-issues.log，格式 [时间戳] [级别] [mp_number] [mp_string] [issues_id] [结果]。"""
    logger = logging.getLogger("mp-issues")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(mp_number)s] [%(mp_string)s] [%(issues_id)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(MpLogFilter())
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.addFilter(MpLogFilter())
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logger()


def log_with(level, message, mp_number="-", mp_string="-", issues_id="-"):
    """带上下文写一条日志（兼容 logging 的 extra 机制）。"""
    log.log(level, message, extra={"mp_number": mp_number, "mp_string": mp_string, "issues_id": issues_id})


# ---------------------------- 工具函数 ----------------------------
def now_iso():
    """返回当前 CST 时间的 ISO 8601 字符串（YYYY-MM-DDTHH:MM:SS+08:00）。"""
    return datetime.now(CST).replace(microsecond=0).isoformat()


def iso_to_date(iso_str):
    """从 ISO 8601 字符串中取出 YYYY-MM-DD；解析失败返回 None。"""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).astimezone(CST).strftime("%Y-%m-%d")
    except Exception:
        return None


def sanitize_title(title):
    """标题清洗：strip → 换行换空格 → 删 | → 转义 <> → 截断 50 字符（含省略号）。"""
    if not title:
        return ""
    t = title.strip()
    t = re.sub(r"[\r\n]+", " ", t)
    t = t.replace("|", "")
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    if len(t) > 50:
        t = t[:49] + "…"
    return t


def load_json(path, default=None):
    """读取 JSON 文件；文件不存在或解析失败时返回 default。"""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_with(logging.ERROR, f"读取 JSON 失败 {path}: {e}")
        return default


def save_json(path, data):
    """写入 JSON 文件（UTF-8、缩进 2、保留中文），自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_text(path, text):
    """写入文本文件（UTF-8），自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_index():
    """加载 mp-issues.json；不存在时返回初始结构。"""
    data = load_json(INDEX_JSON)
    if not data:
        return {"version": INDEX_VERSION, "next_number": 1, "items": {}}
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("next_number", 1)
    data.setdefault("items", {})
    return data


def save_index(index):
    """保存 mp-issues.json（规范顺序：version、next_number、items）。"""
    save_json(INDEX_JSON, {
        "version": index["version"],
        "next_number": index["next_number"],
        "items": index["items"],
    })


def parse_mp_md():
    """
    解析 mp.md，返回有序的 [(mp_string, mp_link, title_col)]。
    规则：跳过空行/# 行；首行表头、第二行分隔行；其后每行取第 2 列做 URL 提取。
    """
    if not MP_MD.exists():
        return []
    result = []
    seen = set()
    lines = MP_MD.read_text(encoding="utf-8").splitlines()
    body_started = False
    header_seen = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not header_seen:
            header_seen = True
            continue
        if not body_started and SEP_RE.match(line):
            body_started = True
            continue
        if not body_started:
            # 表头/分隔行之外的提前内容，若含 URL 也尝试解析
            body_started = True
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            log_with(logging.WARNING, f"非法表格行，跳过: {line}")
            continue
        title_col, url_cell = cols[0], cols[1]
        m = MP_LINK_RE.search(url_cell)
        if not m:
            log_with(logging.WARNING, f"非法 URL 行，跳过: {line}")
            continue
        mp_string = m.group(1)
        if not MP_STRING_RE.match(mp_string):
            log_with(logging.WARNING, f"非法 mp_string，跳过: {mp_string}")
            continue
        if mp_string in seen:
            log_with(logging.WARNING, f"重复 mp_string，跳过: {mp_string}")
            continue
        seen.add(mp_string)
        result.append((mp_string, f"https://mp.weixin.qq.com/s/{mp_string}", title_col))
    return result


def http_session():
    """构造带默认 UA 与重试策略的 requests.Session。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; mp-issues/0.4.0; +https://github.com)",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def fetch_mp_article(mp_link):
    """
    抓取公众号文章，返回 dict（mp_title/mp_time/mp_author/mp_content/mp_source），失败返回 None。
    连接超时 10s、读取超时 20s；共 3 次尝试，失败间隔 2s/4s；非 200/超时/解析异常均视为失败。
    """
    session = http_session()
    attempts = 3
    for i in range(attempts):
        try:
            resp = session.get(mp_link, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return parse_mp_html(resp.text)
        except Exception as e:
            if i < attempts - 1:
                delay = FETCH_RETRY_DELAYS[i]
                log_with(logging.WARNING, f"抓取失败（第 {i + 1} 次）：{e}，{delay}s 后重试")
                time.sleep(delay)
            else:
                log_with(logging.ERROR, f"抓取失败（第 {i + 1} 次）：{e}，放弃")
                return None
    return None


def parse_mp_html(html_text):
    """解析公众号文章 HTML，提取标题、发布时间、作者、内容、阅读原文链接；解析异常抛 RuntimeError。"""
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop})
        return tag.get("content", "").strip() if tag else ""

    title = (
        meta("og:title")
        or meta("twitter:title")
        or (soup.title.get_text(strip=True) if soup.title else "")
    ).strip()
    if not title:
        raise RuntimeError("解析异常：缺少标题")

    content_node = soup.find(id="js_content") or soup.find("div", class_="rich_media_content")
    if content_node:
        mp_content = "".join(str(c) for c in content_node.contents)
    else:
        mp_content = str(soup)

    author = (
        meta("og:article:author")
        or meta("author")
        or (soup.find(id="js_name").get_text(strip=True) if soup.find(id="js_name") else "")
    ).strip()

    mp_time = ""
    t = soup.find(id="publish_time")
    if t and t.get_text(strip=True):
        mp_time = t.get_text(strip=True)
    if not mp_time:
        mp_time = meta("article:published_time") or meta("og:release_date")
    mp_time = normalize_mp_time(mp_time)

    mp_source = None
    a = soup.find("a", id="js_view_source") or soup.find("a", string=re.compile("阅读原文"))
    if a and a.get("href"):
        mp_source = a["href"].strip()

    return {
        "mp_title": title,
        "mp_time": mp_time,
        "mp_author": author,
        "mp_content": mp_content,
        "mp_source": mp_source,
    }


def normalize_mp_time(raw):
    """将多种时间文本规范化为 ISO 8601（CST）；无法解析时返回当前时间。"""
    if raw:
        raw = raw.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
                return dt.astimezone(CST).replace(microsecond=0).isoformat()
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(CST).replace(microsecond=0).isoformat()
        except ValueError:
            pass
    return now_iso()


def html_to_markdown(html_text):
    """将文章 HTML 转换为 Markdown（issues_body），用于 Issue 正文与 issues_md 备份。"""
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = False
    h.ignore_links = False
    h.unicode_snob = True
    try:
        md = h.handle(html_text or "")
    except Exception as e:
        log_with(logging.WARNING, f"HTML→Markdown 失败：{e}，使用原文")
        md = html_text or ""
    return md.strip()


# ---------------------------- GitHub API ----------------------------
def gh_request(method, url, **kwargs):
    """调用 GitHub API，返回 (status_code, json_or_none)。"""
    try:
        resp = requests.request(method, url, headers=GITHUB_HEADERS, timeout=30, **kwargs)
        data = resp.json() if resp.content else None
        return resp.status_code, data
    except Exception as e:
        log_with(logging.ERROR, f"GitHub API 请求异常 {method} {url}: {e}")
        return 0, None


def gh_get_issue(issues_id):
    """获取指定 Issue；不存在返回 None，出错抛异常。"""
    code, data = gh_request("GET", f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{issues_id}")
    if code == 200:
        return data
    if code == 404:
        return None
    raise RuntimeError(f"获取 Issue #{issues_id} 失败: {code} {data}")


def gh_create_issue(title, body, labels):
    """创建 Issue，返回 Issue 数据。"""
    code, data = gh_request(
        "POST",
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues",
        json={"title": title, "body": body, "labels": labels},
    )
    if code not in (200, 201):
        raise RuntimeError(f"创建 Issue 失败: {code} {data}")
    return data


def gh_update_issue(issues_id, **fields):
    """更新 Issue 的 title/body/state/labels，返回更新后的 Issue 数据。"""
    code, data = gh_request(
        "PATCH",
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{issues_id}",
        json=fields,
    )
    if code != 200:
        raise RuntimeError(f"更新 Issue #{issues_id} 失败: {code} {data}")
    return data


def gh_list_issues():
    """列出所有 Issue（含 closed，分页拉取，排除 PR）。"""
    results = []
    page = 1
    while True:
        code, data = gh_request(
            "GET",
            f"{GITHUB_API}/repos/{GITHUB_REPO}/issues",
            params={"state": "all", "per_page": 100, "page": page},
        )
        if code != 200 or not isinstance(data, list):
            break
        results.extend([x for x in data if "pull_request" not in x])
        if len(data) < 100:
            break
        page += 1
    return results


# ---------------------------- 文件构造 ----------------------------
def build_mp_json(mp_number, mp_string, fetched):
    """构造 mp_json/{mp_number}_{mp_string}.json 的数据。"""
    mp_link = f"https://mp.weixin.qq.com/s/{mp_string}"
    mp_title = sanitize_title(fetched["mp_title"])
    mp_time = fetched["mp_time"]
    return {
        "mp_number": mp_number,
        "mp_link": mp_link,
        "mp_string": mp_string,
        "mp_title": mp_title,
        "mp_time": mp_time,
        "mp_date": iso_to_date(mp_time),
        "mp_author": fetched.get("mp_author") or "",
        "mp_content": fetched["mp_content"],
        "mp_source": fetched.get("mp_source"),
    }


def build_issues_json(issues_id, mp_string, mp_title, issues_body, issue):
    """构造 issues_json/{issues_id}_{mp_string}.json 的数据。"""
    return {
        "issues_id": issues_id,
        "issues_url": issue["html_url"],
        "issues_state": issue["state"],
        "issues_labels": [l["name"] for l in issue.get("labels", [])],
        "issues_title": mp_title,
        "issues_created": issue["created_at"],
        "issues_updated": issue["updated_at"],
        "issues_author": (issue.get("user") or {}).get("login", ""),
        "issues_body": issues_body,
        "mp_string": mp_string,
    }


def rebuild_issues_md(index):
    """全量重建 issues.md：仅保留 labels 等于或包含 ["MP"] 的条目，按 issues_id 排序。"""
    rows = []
    for mp_string, item in index["items"].items():
        labels = item.get("issues_labels") or []
        if LABEL_MP not in labels:
            continue
        issues_id = item.get("issues_id")
        if not issues_id:
            continue
        rows.append(item)
    rows.sort(key=lambda x: x["issues_id"])

    lines = [
        "| 编号 | 标题 | Issue 创建时间 | 状态 | 公众号链接 |",
        "| :---: | :---: | :---: | :---: | :---: |",
    ]
    for it in rows:
        title = it.get("issues_title") or it.get("mp_string")
        url = it.get("issues_url", "")
        created = it.get("issues_created", "")
        state = it.get("issues_state", "open")
        mp_link = it.get("mp_link", "")
        mp_string = it.get("mp_string", "")
        lines.append(
            f"| {it['issues_id']} | [{title}]({url}) | {created} | {state} | [{mp_string}]({mp_link}) |"
        )
    save_text(ISSUES_MD, "\n".join(lines) + "\n")


# ---------------------------- 第一步：mp_string 更新 ----------------------------
def step1_mp_string(index):
    """第一步：比对 mp.md 与 mp-issues.json，保证两者一致；返回是否有数据变更。"""
    entries = parse_mp_md()
    mp_map = {s: (link, title_col) for s, link, title_col in entries}
    items = index["items"]
    changed = False

    # 1. mp.md 存在，mp-issues.json 不存在：抓取并新建
    for mp_string, (mp_link, _title_col) in mp_map.items():
        if mp_string in items:
            continue
        log_with(logging.INFO, f"抓取新文章 {mp_link}", mp_string=mp_string)
        fetched = fetch_mp_article(mp_link)
        if not fetched:
            # 失败不写入、不递增 next_number
            continue
        mp_number = index["next_number"]
        mp_data = build_mp_json(mp_number, mp_string, fetched)
        save_json(MP_JSON_DIR / f"{mp_number}_{mp_string}.json", mp_data)
        items[mp_string] = {
            "mp_number": mp_number,
            "mp_link": mp_data["mp_link"],
            "mp_string": mp_string,
            "mp_time": mp_data["mp_time"],
            "mp_date": mp_data["mp_date"],
            "mp_json": f"mp_json/{mp_number}_{mp_string}.json",
            "issues_id": None,
            "issues_url": None,
            "issues_state": "open",
            "issues_labels": [LABEL_MP],
            "issues_created": None,
            "issues_updated": None,
            "issues_json": None,
        }
        index["next_number"] = mp_number + 1
        changed = True
        log_with(
            logging.INFO,
            f"已抓取并登记 mp_json/{mp_number}_{mp_string}.json",
            mp_number=mp_number, mp_string=mp_string,
        )

    # 2. mp.md 存在，mp-issues.json 存在：closed → open
    for mp_string in mp_map:
        item = items.get(mp_string)
        if not item:
            continue
        if item.get("issues_state") == "closed":
            item["issues_state"] = "open"
            item["issues_updated"] = now_iso()
            changed = True
            log_with(
                logging.INFO, "状态 closed → open",
                mp_number=item.get("mp_number"), mp_string=mp_string,
                issues_id=item.get("issues_id") or "-",
            )

    # 3. mp.md 不存在，mp-issues.json 存在：open → closed
    for mp_string, item in items.items():
        if mp_string in mp_map:
            continue
        if item.get("issues_state") == "open":
            item["issues_state"] = "closed"
            item["issues_updated"] = now_iso()
            changed = True
            log_with(
                logging.INFO, "状态 open → closed",
                mp_number=item.get("mp_number"), mp_string=mp_string,
                issues_id=item.get("issues_id") or "-",
            )

    if changed:
        save_index(index)
    return changed


# ---------------------------- 第二步：issues_id 更新 ----------------------------
def step2_issues(index):
    """第二步：比对 mp-issues.json 与 GitHub Issues，保证两者一致；返回是否有数据变更。"""
    items = index["items"]
    changed = False

    # 2.1 mp-issues.json 存在，GitHub Issues 不存在：新建
    for mp_string, item in items.items():
        if item.get("issues_id"):
            continue
        mp_number = item["mp_number"]
        mp_path = ROOT / item["mp_json"]
        mp_data = load_json(mp_path)
        if not mp_data:
            log_with(logging.ERROR, f"mp_json 缺失，跳过新建 Issue: {item['mp_json']}",
                     mp_number=mp_number, mp_string=mp_string)
            continue
        issues_body = html_to_markdown(mp_data["mp_content"])
        title = mp_data["mp_title"]
        try:
            issue = gh_create_issue(title, issues_body, [LABEL_MP])
        except Exception as e:
            log_with(logging.ERROR, f"新建 Issue 失败: {e}",
                     mp_number=mp_number, mp_string=mp_string)
            continue
        issues_id = issue["number"]
        issues_md_path = f"issues_md/{issues_id}_{mp_string}.md"
        save_text(ISSUES_MD_DIR / f"{issues_id}_{mp_string}.md", issues_body + "\n")
        issues_json_data = build_issues_json(issues_id, mp_string, title, issues_body, issue)
        save_json(ISSUES_JSON_DIR / f"{issues_id}_{mp_string}.json", issues_json_data)
        item.update({
            "issues_id": issues_id,
            "issues_url": issue["html_url"],
            "issues_state": issue["state"],
            "issues_labels": [l["name"] for l in issue.get("labels", [])],
            "issues_created": issue["created_at"],
            "issues_updated": issue["updated_at"],
            "issues_json": f"issues_json/{issues_id}_{mp_string}.json",
        })
        changed = True
        log_with(logging.INFO, f"新建 Issue {issue['html_url']}",
                 mp_number=mp_number, mp_string=mp_string, issues_id=issues_id)

    # 2.2 mp-issues.json 存在，GitHub Issues 存在：更新（以 mp-issues.json 为准的字段）
    for mp_string, item in items.items():
        issues_id = item.get("issues_id")
        if not issues_id:
            continue
        mp_path = ROOT / item["mp_json"]
        mp_data = load_json(mp_path)
        if not mp_data:
            continue
        try:
            issue = gh_get_issue(issues_id)
        except Exception as e:
            log_with(logging.ERROR, f"获取 Issue 失败: {e}",
                     mp_number=item["mp_number"], mp_string=mp_string, issues_id=issues_id)
            continue
        if issue is None:
            log_with(logging.WARNING, "Issue 已不存在，跳过",
                     mp_number=item["mp_number"], mp_string=mp_string, issues_id=issues_id)
            continue

        current_labels = [l["name"] for l in issue.get("labels", [])]
        target_labels = item.get("issues_labels") or [LABEL_MP]
        want_state = item.get("issues_state") or "open"

        # 需要更新的字段
        fields = {}
        if issue.get("title") != mp_data["mp_title"]:
            fields["title"] = mp_data["mp_title"]
        # body 是否需更新：用 issues_md 备份与当前 issues_body 比较
        body_md = html_to_markdown(mp_data["mp_content"])
        if issue.get("body", "").strip() != body_md.strip():
            fields["body"] = body_md
        if issue.get("state") != want_state:
            fields["state"] = want_state
        if sorted(current_labels) != sorted(target_labels):
            fields["labels"] = target_labels

        if not fields:
            # 一致，no-op
            continue

        try:
            issue = gh_update_issue(issues_id, **fields)
        except Exception as e:
            log_with(logging.ERROR, f"更新 Issue 失败: {e}",
                     mp_number=item["mp_number"], mp_string=mp_string, issues_id=issues_id)
            continue

        # 回写 issues_json / issues_md / mp-issues.json
        issues_body = fields.get("body", issue.get("body", ""))
        save_text(ISSUES_MD_DIR / f"{issues_id}_{mp_string}.md", issues_body + "\n")
        issues_json_data = build_issues_json(issues_id, mp_string, mp_data["mp_title"], issues_body, issue)
        save_json(ISSUES_JSON_DIR / f"{issues_id}_{mp_string}.json", issues_json_data)
        item.update({
            "issues_url": issue["html_url"],
            "issues_state": issue["state"],
            "issues_labels": [l["name"] for l in issue.get("labels", [])],
            "issues_updated": issue["updated_at"],
        })
        changed = True
        log_with(logging.INFO, f"更新 Issue 字段 {list(fields.keys())}",
                 mp_number=item["mp_number"], mp_string=mp_string, issues_id=issues_id)

    # 2.3 mp-issues.json 不存在，GitHub Issues 存在：closed + 移除 MP / 追加 OUT
    known_ids = {it.get("issues_id") for it in items.values() if it.get("issues_id")}
    try:
        all_issues = gh_list_issues()
    except Exception as e:
        log_with(logging.ERROR, f"列出 Issues 失败: {e}")
        all_issues = []

    for issue in all_issues:
        if issue["number"] in known_ids:
            continue
        labels = [l["name"] for l in issue.get("labels", [])]
        if LABEL_MP not in labels:
            continue
        new_labels = [l for l in labels if l != LABEL_MP]
        if LABEL_OUT not in new_labels:
            new_labels.append(LABEL_OUT)
        try:
            gh_update_issue(issue["number"], state="closed", labels=new_labels)
            changed = True
            log_with(logging.INFO, f"孤儿 Issue 关闭并改标签 {new_labels}",
                     issues_id=issue["number"], mp_string="-")
        except Exception as e:
            log_with(logging.ERROR, f"关闭孤儿 Issue 失败: {e}", issues_id=issue["number"])

    if changed:
        save_index(index)
    # issues.md 全量重建
    rebuild_issues_md(index)
    return changed


# ---------------------------- 主入口 ----------------------------
def main():
    """主流程：加载索引 → 第一步 → 第二步；异常时返回非零退出码。"""
    log_with(logging.INFO, "=== mp-issues 开始 ===")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log_with(logging.ERROR, "缺少 GITHUB_TOKEN 或 GITHUB_REPOSITORY 环境变量")
        return 1
    try:
        index = load_index()
        step1_mp_string(index)
        step2_issues(index)
        log_with(logging.INFO, "=== mp-issues 结束 ===")
        return 0
    except Exception as e:
        log_with(logging.ERROR, f"未捕获异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
