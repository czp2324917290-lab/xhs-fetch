# -*- coding: utf-8 -*-
"""小红书链接抓取代理（Flask 单文件版，部署到 Render/Railway 等云平台）。

提供：  GET /api/fetch?url=<笔记链接或手机分享文本>
返回：  {"ok":true,"title":"...","body":"...","tags":"#a #b","note":"..."}
        {"ok":false,"reason":"..."}

特点：
- 独立云端 IP（不同于 Cloudflare Worker 的数据中心段），被小红书封禁概率更低
- 手机端 UA 随机 + Referer，模拟真实用户
- 即使被拦也从页面残留数据尝试提取标题/正文
- 开启 CORS，允许云端前端（*.agentos-app.net）跨域调用
"""
import os
import re
import random
import gzip
import urllib.request
import urllib.error

from flask import Flask, request, jsonify, after_this_request

UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

GENERIC_TITLES = [
    "小红书 - 你的生活兴趣社区",
    "小红书 - 你访问的页面不见了",
    "小红书 - 页面不存在",
    "小红书 - 笔记不存在",
    "小红书",
    "Access Denied",
    "Just a moment...",
]

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 10000))


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _extract_url(raw):
    m = re.search(r"https?://[^\s\"'）)>,，。；]+", raw or "")
    if not m:
        return ""
    return m.group(0).rstrip(".,;。；")


def _meta(html, prop=None, name=None):
    if prop:
        m = re.search(r'<meta[^>]*property=["\']%s["\'][^>]*content=["\'](.*?)["\']'
                      % re.escape(prop), html, re.I | re.S)
        if m:
            return _clean(m.group(1))
    if name:
        m = re.search(r'<meta[^>]*name=["\']%s["\'][^>]*content=["\'](.*?)["\']'
                      % re.escape(name), html, re.I | re.S)
        if m:
            return _clean(m.group(1))
    return ""


def _extract_xhs_json(html):
    """从页面内嵌 JSON 粗提取标题/描述（多种转义格式）。"""
    t = d = ""
    # 格式1: \"title\":\"...\"
    m = re.search(r'\\"title\\"\s*:\s*\\"(.*?)\\"', html)
    if m:
        t = _clean(m.group(1))
    m = re.search(r'\\"desc\\"\s*:\s*\\"(.*?)\\"', html)
    if m:
        d = _clean(m.group(1))
    # 格式2: "title":"..." （未转义）
    if not t:
        m = re.search(r'"title"\s*:\s*"(.*?)"', html)
        if m:
            t = _clean(m.group(1))
    if not d:
        m = re.search(r'"desc"\s*:\s*"(.*?)"', html)
        if m:
            d = _clean(m.group(1))
    return t, d


def _get_html(url, timeout=15):
    ua = UA_POOL[random.randint(0, len(UA_POOL) - 1)]
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.xiaohongshu.com/",
            "Origin": "https://www.xiaohongshu.com",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Cache-Control": "max-age=0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode(charset, errors="ignore")


def fetch_note(raw_url):
    url = _extract_url(raw_url)
    if not url:
        return {"ok": False, "reason": "未提供有效链接（请把手机复制的链接/分享文本粘贴进来）"}

    status = 200
    try:
        html = _get_html(url)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            charset = e.headers.get_content_charset() or "utf-8"
            html = e.read().decode(charset, errors="ignore")
        except Exception:
            return {"ok": False, "reason": f"页面返回 HTTP {e.code}（小红书需登录态 / 反爬拦截）"}
    except urllib.error.URLError as e:
        return {"ok": False, "reason": f"网络无法访问：{e.reason}"}
    except Exception as e:  # noqa
        return {"ok": False, "reason": f"抓取失败：{e}"}

    # og:title / <title>
    title = _meta(html, prop="og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = _clean(m.group(1))
    xt, xd = _extract_xhs_json(html)
    title = xt or title

    desc = _meta(html, prop="og:description")
    if not desc:
        desc = _meta(html, name="description")
    desc = xd or desc

    tags_found = re.findall(r"#([^\s#@]+)", title + " " + desc)
    tags = " ".join("#" + t for t in dict.fromkeys(tags_found)) if tags_found else ""

    if title in GENERIC_TITLES or (not title or len(title) < 2):
        return {
            "ok": False,
            "reason": "小红书返回了拦截页（云端 IP 被识别）。"
                      "建议：①把笔记文字直接粘贴到输入框；②或把链接发到 WorkBuddy 对话由我代抓。",
        }

    note = ""
    if not desc:
        note = ("自动抓取仅取到标题「" + title + "」（正文需登录态）。"
                "请在「正文」框手动粘贴笔记内容后再点拆解。")
    elif status != 200:
        note = f"（页面返回 HTTP {status} 但仍提取到了部分内容）"

    return {"ok": True, "title": title, "body": desc, "tags": tags, "note": note}


@app.route("/")
def root():
    return jsonify({"service": "xhs-fetch-proxy", "status": "ok",
                    "usage": "GET /api/fetch?url=<笔记链接或手机分享文本>"})


@app.route("/api/fetch", methods=["GET", "POST", "OPTIONS"])
def api_fetch():
    if request.method == "OPTIONS":
        return _cors(jsonify({}))
    url = ""
    if request.method == "GET":
        url = request.args.get("url", "")
    else:
        data = request.get_json(force=True, silent=True) or {}
        url = (data.get("url") or "").strip()
    result = fetch_note(url)
    return _cors(jsonify(result))


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return resp


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
