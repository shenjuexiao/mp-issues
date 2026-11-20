# mp-issues-0.4.0.py
# github.com/shenjuexiao
# 20261124

# mp-issues.py
#!/usr/bin/env python3
"""
mp-issues.py v0.4.0
同步 mp.md 中的公众号文章到 GitHub Issues。
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

# ============ 配置 ============
TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).parent
MP_MD = ROOT / "mp.md"
MP_ISSUES_JSON = ROOT / "mp-issues.json"
MP_ISSUES_LOG = ROOT / "mp-issues.log"
ISSUES_MD = ROOT / "issues.md"
MP_JSON_DIR = ROOT / "mp_json"
ISSUES_JSON_DIR = ROOT / "issues_json"
ISSUES_MD_DIR = ROOT / "issues_md"

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")  # owner/repo
API = "https://api.github.com"

MP_URL_RE = re.compile(r"https://mp\.weixin\.qq\.com/s/([A-Za-z0-9_-]+)")
HEADER_RE = re.compile(r"^\s*\|?\s*:?-{3,}")


# ============ 工具函数 ============
def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def now_date():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def log(level: str, issues_id="", mp_string="", msg=""):
    line = f"[{now_iso()}] [{level}] [{issues_id}] [{mp_string}] {msg}"
    print(line)
    with open(MP_ISSUES_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_mp_md(path: Path) -> dict[str, dict]:
    """解析 mp.md，返回 {mp_string: {mp_link, mp_string}}"""
    result = {}
    if not path.exists():
        return result

    lines = [l.rstrip("\n") for l in path.read_text(encoding="utf-8").splitlines()]
    # 找分隔行（第二行或之后）
    sep_idx = None
    for i, line in enumerate(lines):
        if HEADER_RE.match(line):
            sep_idx = i
            break
    if sep_idx is None:
        return result

    for line in lines[sep_idx + 1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            log("WARNING", msg=f"skip malformed row: {line[:80]}")
            continue
        m = MP_URL_RE.search(cols[1])
        if not m:
            log("WARNING", msg=f"skip non-mp-url: {cols[1][:80]}")
            continue
        mp_string = m.group(1)
        result[mp_string] = {
            "mp_link": f"https://mp.weixin.qq.com/s/{mp_string}",
            "mp_string": mp_string,
        }
    return result


def clean_title(title: str) -> str:
    if not title:
        return "Untitled"
    t = title.strip().replace("\n", " ").replace("|", "")
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    if len(t) > 50:
        t = t[:50] + "…"
    return t


# ============ 微信公众号抓取 ============
def fetch_mp_article(mp_link: str, mp_string: str) -> dict | None:
    """
    抓取公众号文章。返回 dict 或 None（失败）。
    注意：微信页面结构可能变化，以下选择器需按实际页面调整。
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
    })

    for attempt, wait in enumerate([0, 2, 4]):
        if wait:
            time.sleep(wait)
        try:
            resp = session.get(mp_link, timeout=(10, 20))
            if resp.status_code != 200:
                log("WARNING", mp_string=mp_string,
                    msg=f"HTTP {resp.status_code} attempt {attempt + 1}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # ---- 标题 ----
            title_el = soup.select_one("#activity-name") or soup.find("h1")
            title = title_el.get_text(strip=True) if title_el else ""

            # ---- 作者 ----
            author_el = soup.select_one("#js_name") or soup.select_one("#meta_content .rich_media_meta_text")
            author = author_el.get_text(strip=True) if author_el else ""

            # ---- 发布时间 ----
            time_el = soup.select_one("#publish_time") or soup.select_one("em#publish_time")
            publish_time = time_el.get_text(strip=True) if time_el else ""
            # 尝试转为 ISO；若失败用当前时间兜底
            mp_time = now_iso()
            if publish_time:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(publish_time.strip(), fmt)
                        dt = dt.replace(tzinfo=TZ)
                        mp_time = dt.isoformat(timespec="seconds")
                        break
                    except ValueError:
                        pass
            mp_date = mp_time[:10]

            # ---- 正文 HTML ----
            content_el = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
            if not content_el:
                log("WARNING", mp_string=mp_string,
                    msg=f"content not found attempt {attempt + 1}")
                continue
            mp_content = str(content_el)

            # ---- 阅读原文链接 ----
            source_el = soup.select_one("#js_article_source") or soup.select_one(".rich_media_meta_link")
            mp_source = source_el.get("href") if source_el else None

            return {
                "mp_link": mp_link,
                "mp_string": mp_string,
                "mp_title": clean_title(title),
                "mp_time": mp_time,
                "mp_date": mp_date,
                "mp_author": author,
                "mp_content": mp_content,
                "mp_source": mp_source,
            }

        except (requests.Timeout, requests.ConnectionError) as e:
            log("WARNING", mp_string=mp_string, msg=f"attempt {attempt + 1} failed: {e}")
        except Exception as e:
            log("ERROR", mp_string=mp_string, msg=f"parse error: {e}")
            return None

    log("ERROR", mp_string=mp_string, msg="all retries failed")
    return None


# ============ GitHub Issues API ============
def gh_headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get_issue(issues_id: int) -> dict | None:
    url = f"{API}/repos/{REPO}/issues/{issues_id}"
    resp = requests.get(url, headers=gh_headers(), timeout=(10, 20))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def gh_create_issue(title: str, body: str, labels: list[str]) -> dict:
    url = f"{API}/repos/{REPO}/issues"
    payload = {"title": title, "body": body, "labels": labels}
    resp = requests.post(url, json=payload, headers=gh_headers(), timeout=(10, 30))
    resp.raise_for_status()
    return resp.json()


def gh_update_issue(issues_id: int, **kwargs) -> dict:
    url = f"{API}/repos/{REPO}/issues/{issues_id}"
    resp = requests.patch(url, json=kwargs, headers=gh_headers(), timeout=(10, 30))
    resp.raise_for_status()
    return resp.json()


def gh_list_mp_issues() -> list[dict]:
    """列出所有带 MP 标签的 Issues（用于 issues.md 重建）。"""
    url = f"{API}/repos/{REPO}/issues"
    params = {"labels": "MP", "state": "all", "per_page": 100}
    resp = requests.get(url, params=params, headers=gh_headers(), timeout=(10, 20))
    resp.raise_for_status()
    # 过滤掉 PR
    return [i for i in resp.json() if "pull_request" not in i]


# ============ Issues.md 重建 ============
def rebuild_issues_md(index: dict):
    lines = [
        "| 编号 | 标题 | Issue 创建时间 | 状态 | 公众号链接 |",
        "| :---: | :---: | :---: | :---: | :---: |",
    ]
    items = sorted(index.get("items", {}).values(), key=lambda x: x.get("issues_id", 0))
    for it in items:
        if not it.get("issues_id"):
            continue
        # 仅展示状态为 open 或标签包含 MP 的
        lines.append(
            f"| {it['issues_id']} | [{it.get('mp_title', '')}]({it.get('issues_url', '')}) "
            f"| {it.get('issues_created', '')} | {it.get('issues_state', 'open')} "
            f"| [{it['mp_string']}]({it['mp_link']}) |"
        )
    ISSUES_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============ 主流程 ============
def main():
    MP_JSON_DIR.mkdir(exist_ok=True)
    ISSUES_JSON_DIR.mkdir(exist_ok=True)
    ISSUES_MD_DIR.mkdir(exist_ok=True)

    mp_entries = parse_mp_md(MP_MD)
    index = load_json(MP_ISSUES_JSON, default={"version": 1, "next_number": 1, "items": {}})
    items = index.setdefault("items", {})

    # ---------- 第一步：mp_string 更新 ----------
    for mp_string, entry in mp_entries.items():
        if mp_string in items:
            # 文件存在，索引存在
            it = items[mp_string]
            if it.get("issues_state") == "closed":
                it["issues_state"] = "open"
                log("INFO", mp_string=mp_string, msg="reopened")
            continue

        # 文件存在，索引不存在 → 抓取
        article = fetch_mp_article(entry["mp_link"], mp_string)
        if not article:
            continue  # 抓取失败，跳过，next_number 不变

        mp_number = index["next_number"]
        article["mp_number"] = mp_number
        mp_json_path = MP_JSON_DIR / f"{mp_number}_{mp_string}.json"
        save_json(mp_json_path, article)

        items[mp_string] = {
            "mp_number": mp_number,
            "mp_link": article["mp_link"],
            "mp_string": mp_string,
            "mp_title": article["mp_title"],
            "mp_time": article["mp_time"],
            "mp_date": article["mp_date"],
            "mp_json": str(mp_json_path.relative_to(ROOT)),
            "issues_id": None,
            "issues_url": None,
            "issues_state": "open",
            "issues_labels": ["MP"],
            "issues_created": None,
            "issues_updated": None,
            "issues_json": None,
        }
        index["next_number"] = mp_number + 1
        log("INFO", mp_string=mp_string, msg=f"fetched as mp_number={mp_number}")

    # 处理 mp.md 不存在但索引存在的
    for mp_string, it in items.items():
        if mp_string not in mp_entries and it.get("issues_state") == "open":
            it["issues_state"] = "closed"
            it["issues_updated"] = now_iso()
            log("INFO", mp_string=mp_string, msg="closed (removed from mp.md)")

    # ---------- 第二步：issues_id 更新 ----------
    for mp_string, it in items.items():
        issues_id = it.get("issues_id")
        gh_issue = gh_get_issue(issues_id) if issues_id else None

        # 读取文章内容
        mp_json_path = ROOT / it.get("mp_json", "")
        article = load_json(mp_json_path) if mp_json_path.exists() else None
        if not article:
            continue

        body_md = md(article.get("mp_content", ""), heading_style="ATX")
        issues_title = article.get("mp_title", "Untitled")

        if gh_issue:
            # Issue 存在 → 更新
            update_kwargs = {}
            if gh_issue.get("title") != issues_title:
                update_kwargs["title"] = issues_title
            if gh_issue.get("body") != body_md:
                update_kwargs["body"] = body_md
            if update_kwargs:
                gh_issue = gh_update_issue(issues_id, **update_kwargs)

            # 回写索引
            it["issues_state"] = gh_issue["state"]
            it["issues_labels"] = [l["name"] for l in gh_issue.get("labels", [])]
            it["issues_created"] = gh_issue.get("created_at", it.get("issues_created"))
            it["issues_updated"] = gh_issue.get("updated_at", now_iso())
            it["issues_url"] = gh_issue.get("html_url", it.get("issues_url"))

            # 更新 issues_json 与 issues_md
            iss_json = {
                "issues_id": issues_id,
                "issues_url": it["issues_url"],
                "issues_state": it["issues_state"],
                "issues_labels": it["issues_labels"],
                "issues_title": issues_title,
                "issues_created": it["issues_created"],
                "issues_updated": it["issues_updated"],
                "issues_author": gh_issue.get("user", {}).get("login", ""),
                "issues_body": body_md,
                "mp_string": mp_string,
            }
            save_json(ISSUES_JSON_DIR / f"{issues_id}_{mp_string}.json", iss_json)
            (ISSUES_MD_DIR / f"{issues_id}_{mp_string}.md").write_text(body_md, encoding="utf-8")
            it["issues_json"] = f"issues_json/{issues_id}_{mp_string}.json"

        else:
            # Issue 不存在 → 新建
            gh_issue = gh_create_issue(issues_title, body_md, ["MP"])
            issues_id = gh_issue["number"]
            it["issues_id"] = issues_id
            it["issues_url"] = gh_issue["html_url"]
            it["issues_state"] = gh_issue["state"]
            it["issues_labels"] = [l["name"] for l in gh_issue.get("labels", [])]
            it["issues_created"] = gh_issue.get("created_at", now_iso())
            it["issues_updated"] = gh_issue.get("updated_at", now_iso())
            it["issues_json"] = f"issues_json/{issues_id}_{mp_string}.json"

            iss_json = {
                "issues_id": issues_id,
                "issues_url": it["issues_url"],
                "issues_state": it["issues_state"],
                "issues_labels": it["issues_labels"],
                "issues_title": issues_title,
                "issues_created": it["issues_created"],
                "issues_updated": it["issues_updated"],
                "issues_author": gh_issue.get("user", {}).get("login", ""),
                "issues_body": body_md,
                "mp_string": mp_string,
            }
            save_json(ISSUES_JSON_DIR / f"{issues_id}_{mp_string}.json", iss_json)
            (ISSUES_MD_DIR / f"{issues_id}_{mp_string}.md").write_text(body_md, encoding="utf-8")
            log("INFO", mp_string=mp_string, issues_id=issues_id, msg="issue created")

    # 处理索引不存在但 GitHub 存在 MP Issue 的情况（标记 OUT）
    # 注意：这需要先拉取所有 MP issues，再与 items 对比。
    # 这里做简化处理：仅在 mp-issues.json 存在条目时处理。
    # 如需完整支持，可扩展 gh_list_mp_issues 后逐条判断。

    # ---------- 保存索引与重建 issues.md ----------
    save_json(MP_ISSUES_JSON, index)
    rebuild_issues_md(index)
    log("INFO", msg="sync completed")


if __name__ == "__main__":
    main()