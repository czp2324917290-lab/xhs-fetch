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
import json
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


def _extract_share_title(raw, url):
    """从手机分享文本里提取链接前面的标题文字（用户所见即所得，最可靠）。"""
    if not raw:
        return ""
    text = raw
    if url:
        idx = text.find(url)
        if idx > 0:
            text = text[:idx]
    text = re.sub(r"(先复制.*|打开【小红书】.*|长按复制.*|点击链接.*|复制打开.*|查看原帖.*|来看这篇好文.*|这篇好文.*)$",
                  "", text, flags=re.S)
    text = text.strip(" \t\n\r-—…,.。，：:")
    text = re.split(r"https?://", text)[0].strip(" \t\n\r-—…,.。，")
    return text


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
    """从页面内嵌 JSON 提取标题/描述候选，优先 noteCard 作用域，再按可信度挑选。"""
    def _titles():
        out = []
        for pat in (r'\\"title\\"\s*:\s*\\"((?:[^"\\]|\\.)*?)\\"',
                    r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"'):
            for mm in re.finditer(pat, html):
                c = _clean(mm.group(1))
                if c:
                    out.append(c)
        return out

    def _descs():
        out = []
        for pat in (r'\\"desc\\"\s*:\s*\\"((?:[^"\\]|\\.)*?)\\"',
                    r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"'):
            for mm in re.finditer(pat, html):
                c = _clean(mm.group(1))
                if c:
                    out.append(c)
        return out

    # 1) noteCard 作用域优先（最可能是笔记本体），escaped 与 unescaped 都试
    t = d = ""
    m = re.search(r'noteCard"\s*:\s*\{', html)
    if m:
        seg = html[m.end(): m.end() + 8000]
        mt = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', seg) \
            or re.search(r'\\"title\\"\s*:\s*\\"((?:[^"\\]|\\.)*?)\\"', seg)
        if mt:
            t = _clean(mt.group(1))
        md = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', seg) \
            or re.search(r'\\"desc\\"\s*:\s*\\"((?:[^"\\]|\\.)*?)\\"', seg)
        if md:
            d = _clean(md.group(1))

    # 2) 兜底：整页所有 title 候选里挑最像笔记标题的（排除站点通用标题）
    if not t:
        t = _pick_title(_titles())
    # 3) 兜底：desc 取最长候选
    if not d:
        ds = _descs()
        d = max(ds, key=len) if ds else ""
    return t, d


def _pick_title(cands):
    cands = [c for c in cands if c and c not in GENERIC_TITLES]
    if not cands:
        return ""
    # 含中文、长度适中(5~40字)的，取最长（笔记标题通常比昵称/卡片标题更长更完整）
    good = [c for c in cands if re.search(r'[\u4e00-\u9fff]', c) and 5 <= len(c) <= 40]
    if good:
        return max(good, key=len)
    cjk = [c for c in cands if re.search(r'[\u4e00-\u9fff]', c)]
    if cjk:
        return max(cjk, key=len)
    return cands[0]


def _get_note_map(html):
    """从 window.__INITIAL_STATE__ 解析出 noteDetailMap（noteId -> note 数据）。"""
    m = re.search(r'(?:window\.)?__INITIAL_STATE__\s*=\s*', html)
    if not m:
        return {}
    pos = m.end()
    if pos >= len(html) or html[pos] != "{":
        return {}
    depth = 0
    end = pos
    in_str = False
    esc = False
    for i in range(pos, min(pos + 500000, len(html))):
        ch = html[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"' and not esc:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= pos:
        return {}
    raw = html[pos:end]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return (data.get("note") or {}).get("noteDetailMap") or {}


def _extract_note_id(url):
    """从（跟随重定向后的）最终 URL 提取笔记 ID。"""
    m = re.search(r'(?:/explore/|/discovery/item/|/search_result/|/note/)([0-9a-zA-Z]+)', url) \
        or re.search(r'noteId=([0-9a-zA-Z]+)', url) \
        or re.search(r'/([0-9a-zA-Z]{8,})(?:[/?#]|$)', url)
    return m.group(1) if m else ""


def _pick_note(note_map, prefer_id=None):
    """从 noteDetailMap 选目标笔记：优先 prefer_id 匹配（兼容 explore/ 前缀差异），否则选 desc 最长的。"""
    if not note_map:
        return {}
    if prefer_id:
        if prefer_id in note_map:
            return note_map[prefer_id].get("note") or {}
        # 兼容 key 格式差异：noteDetailMap 的 key 常为 'explore/xxx'，而我们提取的是纯 id
        for k, v in note_map.items():
            if prefer_id in k or k.endswith(prefer_id):
                return v.get("note") or {}
    best = None
    best_len = -1
    for v in note_map.values():
        nd = v.get("note") or {}
        dlen = len(nd.get("desc") or "")
        if dlen > best_len:
            best_len = dlen
            best = nd
    return best or {}


def _extract_initial_state(html, prefer_id=None):
    """从 window.__INITIAL_STATE__ 提取笔记真实数据（小红书 SSR 注入）。
    这是最可靠的数据源——标题、正文、标签、图片都在里面，不需要登录态。
    返回 (title, desc, tags_str, images_list)。"""
    # 用新拆分的 _get_note_map / _pick_note 选目标笔记（支持 noteId 精确匹配）
    note_map = _get_note_map(html)
    nd = _pick_note(note_map, prefer_id=prefer_id)
    if not nd:
        return "", "", "", []
    title = _clean(nd.get("title") or nd.get("displayTitle") or "")
    desc = _clean(nd.get("desc") or "")
    tags = [t.get("name", "") for t in (nd.get("tagList") or []) if t.get("name")]
    tags_str = " ".join("#" + t for t in tags) if tags else ""
    images = [img.get("url", "") for img in (nd.get("imageList") or []) if img.get("url")]

    return title, desc, tags_str, images


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
        return data.decode(charset, errors="ignore"), resp.geturl()


def fetch_note(raw_url, debug=False):
    url = _extract_url(raw_url)
    share_title = _extract_share_title(raw_url, url)
    if not url:
        return {"ok": False, "reason": "未提供有效链接（请把手机复制的链接/分享文本粘贴进来）"}

    status = 200
    debug_info = {}
    html = ""
    final_url = url
    try:
        html, final_url = _get_html(url)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            charset = e.headers.get_content_charset() or "utf-8"
            html = e.read().decode(charset, errors="ignore")
            final_url = getattr(e, "url", url)
        except Exception:
            return {"ok": False, "reason": f"页面返回 HTTP {e.code}（小红书需登录态 / 反爬拦截）"}
    except urllib.error.URLError as e:
        return {"ok": False, "reason": f"网络无法访问：{e.reason}"}
    except Exception as e:  # noqa
        return {"ok": False, "reason": f"抓取失败：{e}"}

    # ===== 第一优先级：window.__INITIAL_STATE__（小红书 SSR 注入的笔记完整数据） =====
    note_id = _extract_note_id(final_url)
    note_map = _get_note_map(html)
    if note_id in note_map:
        # 普通笔记：noteId 精确匹配，标题/正文最可靠
        title, desc, tags, images = _extract_initial_state(html, prefer_id=note_id)
    else:
        # 商品/带货笔记：__INITIAL_STATE__ 无同步笔记数据，标题交给分享文本兜底，正文仍尝试兜底
        _, desc, tags, images = _extract_initial_state(html, prefer_id=note_id)
        title = ""
    if debug:
        debug_info["has_initial_state"] = bool(re.search(r'(?:window\.)?__INITIAL_STATE__\s*=', html))
        debug_info["initial_state_hit"] = bool(title or desc)
        debug_info["og_title"] = _meta(html, prop="og:title")
        debug_info["final_url"] = final_url
        debug_info["note_id"] = note_id
        _dbg_map = _get_note_map(html)
        debug_info["map_keys"] = list(_dbg_map.keys())
        debug_info["note_previews"] = [
            {"key": k,
             "title": (v.get("note") or {}).get("title", "")[:40],
             "desc": (v.get("note") or {}).get("desc", "")[:40]}
            for k, v in _dbg_map.items()
        ]

    # 去掉站点后缀（如 "真实标题 - 小红书"）
    title = re.sub(r"\s*[-–|]\s*小红书.*$", "", title).strip()

    # 商品/带货笔记页面 __INITIAL_STATE__ 不含 noteDetailMap，改用分享文本里用户看到的标题（最可靠）
    if not title and share_title:
        title = share_title

    # ===== 第二优先级：og:title / og:description（兜底） =====
    if not title:
        title = _meta(html, prop="og:title")
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m:
                title = _clean(m.group(1))
        title = re.sub(r"\s*[-–|]\s*小红书.*$", "", title).strip()

    if not desc:
        desc = _meta(html, prop="og:description")
    if not desc:
        desc = _meta(html, name="description")

    # ===== 第三优先级：页面内嵌 JSON（最后兜底） =====
    if not title or title in GENERIC_TITLES:
        xt, xd = _extract_xhs_json(html)
        if xt and xt not in GENERIC_TITLES:
            title = xt
        if not desc and xd:
            desc = xd

    # 从标题/正文中补抽标签（如果 __INITIAL_STATE__ 没有标签）
    if not tags:
        tags_found = re.findall(r"#([^\s#@]+)", title + " " + desc)
        tags = " ".join("#" + t for t in dict.fromkeys(tags_found)) if tags_found else ""

    if title in GENERIC_TITLES or (not title or len(title) < 2):
        return {
            "ok": False,
            "reason": "小红书返回了拦截页（云端 IP 被识别）。"
                      "建议：①把笔记文字直接粘贴到输入框；②或把链接发到 WorkBuddy 对话由我代抓。",
            "debug": debug_info,
        }

    # 商品/带货笔记：页面异步加载，抓取可能不完整
    is_goods = ("discovery/item" in final_url) or ("noteAttributes=goods" in final_url)
    if is_goods and not _get_note_map(html):
        note = ("⚠️ 该链接为商品/带货笔记，小红书页面为异步加载，自动抓取可能不完整。"
                "标题已用您分享文本中的原文，正文请核对；若有误请在「正文」框手动补全。")
    if not note:
        note = ""
    if not desc and not note:
        note = ("自动抓取仅取到标题「" + title + "」（正文需登录态）。"
                "请在「正文」框手动粘贴笔记内容后再点拆解。")
    elif status != 200:
        note = f"（页面返回 HTTP {status} 但仍提取到了部分内容）"
    if images:
        note = (note + " 含" + str(len(images)) + "张图片") if note else ""

    result = {"ok": True, "title": title, "body": desc, "tags": tags, "note": note, "images": images[:9]}
    if debug:
        result["debug"] = debug_info
    return result


@app.route("/")
def root():
    return jsonify({"service": "xhs-fetch-proxy", "status": "ok",
                    "usage": "GET /api/fetch?url=<笔记链接或手机分享文本>"})


@app.route("/api/fetch", methods=["GET", "POST", "OPTIONS"])
def api_fetch():
    if request.method == "OPTIONS":
        return _cors(jsonify({}))
    url = ""
    debug = False
    if request.method == "GET":
        url = request.args.get("url", "")
        debug = request.args.get("debug", "") in ("1", "true", "on")
    else:
        data = request.get_json(force=True, silent=True) or {}
        url = (data.get("url") or "").strip()
        debug = bool(data.get("debug"))
    result = fetch_note(url, debug=debug)
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
