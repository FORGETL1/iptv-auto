# -*- coding: utf-8 -*-
"""
IPTV 直播源自动更新脚本
========================
采集多份公开源 -> 合并去重 -> 频道归一化分类 -> 逐条可用性探测 -> 输出播放列表

用法：
    python build.py                # 输出到 ./out
    python build.py --outdir D:\\x  # 指定输出目录
    python build.py --engine curl  # 用 curl 探测（默认 urllib）
    python build.py --max-probe 3000   # 限制探测条数（加快速度）

设计要点（踩过的坑，别改）：
  1. 探测**不能跟随 302 跳转**——大量 IPTV 源返回 302，跟随会把活源误判为死链。
     所以 301/302/303/307/308 一律视为「可达」。
  2. raw.githubusercontent.com 在国内常被墙，每个 GitHub 源都带 ghproxy.net 兜底。
  3. 组播地址（rtp/udp/igmp）播放器无法直接播，必须剔除。
  4. 频道名归一化是质量关键：CCTV-1综合 / CCTV1 / 央视一套 必须归到同一个频道。
"""

import os
import re
import sys
import csv
import json
import time
import argparse
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

# ---------------------------------------------------------------- 配置

# 源清单：(原始 raw 地址, 显示名, 权重=越小越优先)
# 权重小的都是「每日自动校验」的源，它们的线路质量更高
SOURCES = [
    ("https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_all.m3u8",
     "best-fan 每日校验合集", 1),
    ("https://raw.githubusercontent.com/mzky/checklist/refs/heads/master/itvlist.m3u",
     "mzky/checklist 每日校验源", 2),
    ("https://raw.githubusercontent.com/zhi35/iptv/master/iptv.m3u",
     "zhi35/iptv 每日更新源", 3),
    ("https://live.zbds.top/tv/iptv4.m3u",
     "ZBDS 每6小时更新", 4),
    ("https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u",
     "Guovin/iptv-api", 5),
    ("https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8",
     "hujingguang/ChinaIPTV", 6),
    ("https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_cctv.m3u8",
     "best-fan 央视频道", 7),
    ("https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_province.m3u8",
     "best-fan 卫视频道", 8),
    ("https://raw.githubusercontent.com/zhi35/iptv/master/live-china.m3u",
     "zhi35 国内精简", 9),
    ("https://cdn.jsdelivr.net/gh/Kimentanm/aptv/m3u/iptv.m3u",
     "APTV 官方测试源", 10),
    ("https://cdn.jsdelivr.net/gh/suxuang/myIPTV@main/ipv4.m3u",
     "suxuang/myIPTV", 11),
    ("https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u",
     "YanG 聚合源", 12),
    ("https://raw.githubusercontent.com/imDazui/Tvlist-awesome-m3u-m3u8/master/m3u/台湾香港澳门202506.m3u",
     "中国港澳台频道源", 13),
    ("https://www.3kjs.com/tv.txt", "3kjs 精选", 14),
    ("https://iptv-org.github.io/iptv/countries/cn.m3u", "iptv-org 中国区", 15),
]

# GitHub 被墙时的加速前缀（按顺序尝试）
PROXIES = ["", "https://ghproxy.net/", "https://gh-proxy.com/"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GROUP_ORDER = ["央视频道", "卫视频道", "地方频道", "中国港澳台", "影视剧场",
               "少儿动漫", "体育频道", "音乐频道", "国际频道", "其他频道"]

EPG_URLS = "https://live.zhi35.com/epg.xml.gz,http://epg.51zmt.top:8000/e.xml.gz"


# ---------------------------------------------------------------- 下载

def _ascii_url(url):
    """把 URL 里的非 ASCII 字符（如中文文件名）转成 %XX，urllib 不认中文"""
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        scheme, _, rest = url.partition("://")
        return scheme + "://" + quote(rest, safe="/:?&=%#[]@!$()*+,;~-._")


def fetch(url, timeout=20, retries=2):
    """带加速前缀兜底的下载。raw.githubusercontent 被墙时自动走镜像。"""
    url = _ascii_url(url)
    last = None
    for pre in PROXIES:
        target = pre + url if pre else url
        for _ in range(retries):
            try:
                req = urllib.request.Request(target, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5"):
                    try:
                        return raw.decode(enc)
                    except Exception:      # noqa: BLE001
                        continue
                return raw.decode("utf-8", "ignore")
            except Exception as e:         # noqa: BLE001
                last = e
    print("    !! 下载失败 %s (%s)" % (url, last))
    return ""


def fetch_all_sources():
    """并发下载所有源——串行下载时只要有一个源卡住，整体就会被拖很久"""
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, url): (url, desc, w)
                for url, desc, w in SOURCES}
        for f in as_completed(futs):
            url, desc, w = futs[f]
            try:
                text = f.result()
            except Exception:              # noqa: BLE001
                text = ""
            results[url] = (desc, w, text)
    return results


# ---------------------------------------------------------------- 解析

def parse_m3u(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            cur = {}
            for key, pat in (("tvgid", r'tvg-id="([^"]*)"'),
                             ("logo", r'tvg-logo="([^"]*)"'),
                             ("group", r'group-title="([^"]*)"'),
                             ("rt", r'response-time="(\d+)')):
                m = re.search(pat, line)
                cur[key] = m.group(1) if m else ""
            cur["rt"] = int(cur["rt"]) if cur["rt"] else 9999
            cur["name"] = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            if not cur["name"]:
                m = re.search(r'tvg-name="([^"]*)"', line)
                cur["name"] = m.group(1) if m else ""
        elif line.startswith("#"):
            continue
        else:
            if cur and re.match(r"^(https?|rtmp|rtsp)://", line):
                cur["url"] = line
                out.append(cur)
            cur = None
    return out


def parse_txt(text):
    """兼容 频道名,url / 分组名,#genre# / 裸 url 三种风格"""
    out, group = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(",#genre#"):
            group = line[:-8].strip()
            continue
        if "," in line:
            name, _, url = line.rpartition(",")
            url = url.strip()
            if re.match(r"^(https?|rtmp|rtsp)://", url):
                out.append({"name": name.strip(), "url": url, "logo": "",
                            "group": group, "tvgid": "", "rt": 9999})
                continue
        if re.match(r"^(https?|rtmp|rtsp)://", line):
            out.append({"name": "", "url": line, "logo": "",
                        "group": group, "tvgid": "", "rt": 9999})
    return out


# ---------------------------------------------------------------- 归一化

ALIAS = {
    "中央电视台综合频道": "CCTV1", "央视一套": "CCTV1", "中央电视台财经频道": "CCTV2",
    "央视二套": "CCTV2", "中央电视台综艺频道": "CCTV3", "央视三套": "CCTV3",
    "中央电视台中文国际频道": "CCTV4", "央视四套": "CCTV4", "中央电视台体育频道": "CCTV5",
    "央视五套": "CCTV5", "中央电视台电影频道": "CCTV6", "央视六套": "CCTV6",
    "中央电视台国防军事频道": "CCTV7", "央视七套": "CCTV7", "中央电视台电视剧频道": "CCTV8",
    "央视八套": "CCTV8", "中央电视台纪录频道": "CCTV9", "央视九套": "CCTV9",
    "中央电视台科教频道": "CCTV10", "央视十套": "CCTV10", "中央电视台戏曲频道": "CCTV11",
    "央视十一套": "CCTV11", "中央电视台社会与法频道": "CCTV12", "央视十二套": "CCTV12",
    "中央电视台新闻频道": "CCTV13", "央视新闻": "CCTV13", "中央电视台少儿频道": "CCTV14",
    "央视十四套": "CCTV14", "中央电视台音乐频道": "CCTV15", "央视十五套": "CCTV15",
    "中央电视台奥林匹克频道": "CCTV16", "央视十六套": "CCTV16",
    "中央电视台农业农村频道": "CCTV17", "央视十七套": "CCTV17",
}


def norm_name(n):
    """把各种写法的同一个频道归到同一个 key"""
    if not n:
        return ""
    s = re.sub(r"\[[^\]]*\]", "", n.strip())        # 去掉 [1920*1080]
    s = re.sub(r"[（(][^）)]*[）)]", "", s)           # 去掉 (1080p)
    s = s.replace(" ", "").replace("\u3000", "").upper()
    s = re.sub(r"(高清|超清|标清|蓝光|HD|FHD|UHD|4K|8K|SD|IPV6|IPV4)$", "", s)
    s = s.replace("_", "")
    # 央视必须先处理，否则 CCTV1 会被后面的「纯英文名」规则当垃圾删掉
    m = re.match(r"^CCTV[-—]?(\d{1,2})(\+|PLUS|加)?", s)
    if m:
        return "CCTV%d%s" % (int(m.group(1)), "PLUS" if m.group(2) else "")
    s = s.replace("-", "")
    return ALIAS.get(s, s)


CCTV_CN = {
    "CCTV1": "CCTV-1 综合", "CCTV2": "CCTV-2 财经", "CCTV3": "CCTV-3 综艺",
    "CCTV4": "CCTV-4 中文国际", "CCTV5": "CCTV-5 体育", "CCTV5PLUS": "CCTV-5+ 体育赛事",
    "CCTV6": "CCTV-6 电影", "CCTV7": "CCTV-7 国防军事", "CCTV8": "CCTV-8 电视剧",
    "CCTV9": "CCTV-9 纪录", "CCTV10": "CCTV-10 科教", "CCTV11": "CCTV-11 戏曲",
    "CCTV12": "CCTV-12 社会与法", "CCTV13": "CCTV-13 新闻", "CCTV14": "CCTV-14 少儿",
    "CCTV15": "CCTV-15 音乐", "CCTV16": "CCTV-16 奥林匹克", "CCTV17": "CCTV-17 农业农村",
}


def pretty_name(key):
    return CCTV_CN.get(key, key)


JUNK_RE = re.compile(
    r"虎牙|斗鱼|哔哩|B站|主播|直播回放|一起看|测试|undefined|"
    r"^\d{4}年|^[A-Za-z0-9_\-\.&+\s]{1,14}$")
JUNK_LOGO = ("huyaimg.msstatic.com", "douyucdn", "live.bilibili")
REAL_LOCAL = re.compile(
    r"电视台|电视|频道|综合|新闻|公共|都市|影视|科教|生活|经济|少儿|体育|农业|"
    r"法治|文旅|资讯|民生|党建|卫视|广播|交通|文艺|州|盟|县|区|市")


def is_junk(e):
    name = (e.get("name") or "").strip()
    key = e.get("key") or ""
    if not name:
        return True
    if re.match(r"^(rtp|udp|igmp)://", e.get("url", "")):
        return True                       # 组播播不了
    if re.match(r"^CCTV\d", key) or re.match(r"^CCTV\d", name):
        return False
    if JUNK_RE.search(name):
        return True
    if any(x in (e.get("logo") or "") for x in JUNK_LOGO):
        return True
    if len(key) > 16 and not REAL_LOCAL.search(key):
        return True
    if re.fullmatch(r"[\d\W]+", key):
        return True
    return False


def classify(key, name):
    if re.match(r"^CCTV\d", key) or re.search(r"CCTV|中央电视|央视", name):
        return "央视频道"
    if "卫视" in key or "卫视" in name:
        return "卫视频道"
    if re.search(r"香港|澳门|台湾|翡翠|明珠|凤凰|TVB|ViuTV|HOY|澳视|中天|东森|"
                 r"三立|民视|台视|华视|中视|无线", name):
        return "中国港澳台"
    if re.search(r"电影|剧场|影院|电视剧|美剧|韩剧|港剧|轮播|经典|Cinem", name, re.I):
        return "影视剧场"
    if re.search(r"少儿|卡通|动漫|动画|优漫|金鹰卡通|卡酷|哈哈", name):
        return "少儿动漫"
    if re.search(r"体育|足球|篮球|NBA|赛事", name, re.I):
        return "体育频道"
    if re.search(r"音乐|MV|演唱会|歌曲", name, re.I):
        return "音乐频道"
    if re.search(r"NHK|BBC|CNN|Discovery|National|HBO|Arirang|KBS|MBC|SBS|"
                 r"France|Germany|Italy|Russia|Japan|Korea|Euronews|Bloomberg|CNBC",
                 name, re.I):
        return "国际频道"
    if REAL_LOCAL.search(key):
        return "地方频道"
    return "其他频道"


# ---------------------------------------------------------------- 探测

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转——跳转本身就是「源活着」的证据"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_opener.addheaders = [("User-Agent", UA), ("Accept", "*/*")]


def probe_urllib(url):
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        p = url.split("://", 1)
        url = p[0] + "://" + quote(p[1], safe=":/?&=%#[]@!$()*+,;~")
    try:
        with _opener.open(url, timeout=8) as r:
            code, ct = r.getcode(), (r.headers.get("Content-Type") or "").lower()
            if code in (200, 206):
                if "mpegurl" in ct or ".m3u8" in url:
                    return True, "m3u8 可访问"
                if ct.startswith("video/") or "octet-stream" in ct or "mp2t" in ct:
                    return True, ct[:28]
                if ct.startswith("text/html"):
                    return False, "HTML(非流)"
                return True, "200"
            return False, "HTTP %s" % code
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return True, "%d 跳转" % e.code
        if e.code in (401, 403):
            return False, "HTTP %d 需鉴权" % e.code
        return False, "HTTP %d" % e.code
    except Exception:                      # noqa: BLE001
        return False, "连接失败"


def probe_curl(url):
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        p = url.split("://", 1)
        url = p[0] + "://" + quote(p[1], safe=":/?&=%#[]@!$()*+,;~")
    cmd = ["curl", "-sS", "-m", "9", "--noproxy", "*", "-A", UA,
           "-o", os.devnull, "-w", "%{http_code}|%{content_type}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        out = (r.stdout or b"").decode("utf-8", "ignore").strip()
        if "|" not in out:
            return False, "无响应"
        code, ct = [x.strip() for x in out.split("|", 1)]
        ct = ct.lower()
        if code in ("301", "302", "303", "307", "308"):
            return True, "%s 跳转" % code
        if code in ("200", "206"):
            if "mpegurl" in ct or ".m3u8" in url:
                return True, "m3u8 可访问"
            if ct.startswith("video/") or "octet-stream" in ct or "mp2t" in ct:
                return True, ct[:28]
            if ct.startswith("text/html"):
                return False, "HTML(非流)"
            return True, "200"
        if code in ("401", "403"):
            return False, "HTTP %s 需鉴权" % code
        return False, "HTTP %s" % code
    except Exception:                      # noqa: BLE001
        return False, "超时/失败"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=None, help="输出目录，默认脚本同级 out/")
    ap.add_argument("--engine", default="urllib", choices=["urllib", "curl"])
    ap.add_argument("--max-probe", type=int, default=0, help="最多探测多少条 URL，0=不限")
    ap.add_argument("--workers", type=int, default=80)
    ap.add_argument("--cache", default=None, help="探测结果缓存文件，重跑时复用")
    # 供 .bat / 计划任务透传，避免「unrecognized arguments」；不影响脚本行为
    ap.add_argument("--silent", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = args.outdir or os.path.join(here, "out")
    os.makedirs(outdir, exist_ok=True)
    # 缓存放在脚本旁边而不是输出目录，避免被一起发布出去
    cache_path = args.cache or os.path.join(here, ".probe_cache.json")

    probe = probe_curl if args.engine == "curl" else probe_urllib

    print("[1/4] 采集源文件 …")
    fetched = fetch_all_sources()
    entries = []
    for url, desc, w in SOURCES:
        text = fetched.get(url, ("", 0, ""))[2]
        if not text:
            continue
        recs = parse_m3u(text) if "#EXTINF" in text[:4000] else parse_txt(text)
        if not recs:
            recs = parse_txt(text)
        for r in recs:
            r["src"], r["w"] = desc, w
        entries.extend(recs)
        print("    %-22s %5d 条" % (desc, len(recs)))
    print("    合计 %d 条" % len(entries))

    print("[2/4] 清洗 / 归一化 / 去重 …")
    clean = []
    for e in entries:
        if not e.get("url") or not e.get("name"):
            continue
        if "undefined" in e["url"] or "[session-" in e["name"]:
            continue
        e["key"] = norm_name(e["name"])
        if len(e["key"]) < 2:
            continue
        e["group2"] = classify(e["key"], e["name"])
        clean.append(e)
    best = {}
    for e in sorted(clean, key=lambda x: x["w"]):
        best.setdefault(e["url"], e)
    uniq = list(best.values())
    print("    %d 条 -> 去重后 %d 条" % (len(clean), len(uniq)))

    print("[3/4] 可用性探测（引擎 %s，并发 %d）…" % (args.engine, args.workers))
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            cache = {}
    todo = [e["url"] for e in uniq if e["url"] not in cache]
    if args.max_probe:
        todo = todo[:args.max_probe]
    t0 = time.time()
    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(probe, u): u for u in todo}
            for f in as_completed(futs):
                u = futs[f]
                try:
                    ok, note = f.result()
                except Exception as e:     # noqa: BLE001
                    ok, note = False, type(e).__name__
                cache[u] = [ok, note]
                done += 1
                if done % 500 == 0:
                    print("      %d/%d  %.0fs" % (done, len(todo), time.time() - t0))
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    n_alive = sum(1 for e in uniq if cache.get(e["url"], [False])[0])
    print("    探测 %d 条，用时 %.0fs；本机可达 %d 条" % (len(todo), time.time() - t0, n_alive))

    print("[4/4] 生成播放列表 …")
    alive = lambda u: cache.get(u, [False])[0]          # noqa: E731

    bykey = {}
    for e in uniq:
        bykey.setdefault(e["key"], []).append(e)
    for k in bykey:
        bykey[k].sort(key=lambda x: (not alive(x["url"]), x["w"], x.get("rt", 9999)))

    def best_of(v, n=2):
        v = [e for e in v if not re.match(r"^(rtp|udp|igmp)://", e["url"])]
        a = [e for e in v if alive(e["url"])]
        b = [e for e in v if not alive(e["url"])]
        return (a + b)[:n]

    def sortkey(g, k):
        if g == "央视频道":
            m = re.match(r"CCTV(\d+)(PLUS)?", k)
            if m:
                return (0, int(m.group(1)), 1 if m.group(2) else 0, k)
        return (1, 0, 0, k)

    ordered = []
    for g in GROUP_ORDER:
        items = [(k, v) for k, v in bykey.items() if v[0]["group2"] == g]
        items.sort(key=lambda it: sortkey(g, it[0]))
        for k, v in items:
            ordered.append((g, k, v))
    curated = [(g, k, v) for (g, k, v) in ordered
               if g != "其他频道" and not is_junk(v[0]) and best_of(v)]

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ['#EXTM3U x-tvg-url="%s"' % EPG_URLS,
             "# 自动生成于 %s | 由 %d 份公开源合并去重" % (stamp, len(SOURCES)),
             "# (本机可达) = 生成时实测可连通；(备用线路) = 同频道备选"]
    for g, k, v in curated:
        for e in best_of(v):
            name = pretty_name(k)
            tag = "本机可达" if alive(e["url"]) else "备用线路"
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" '
                         'group-title="%s",%s (%s)'
                         % (e.get("tvgid") or k, name, e.get("logo") or "", g, name, tag))
            lines.append(e["url"])
    open(os.path.join(outdir, "直播源-精选.m3u"), "w", encoding="utf-8").write(
        "\n".join(lines) + "\n")

    txt = []
    for g in GROUP_ORDER:
        items = [(k, v) for (gg, k, v) in curated if gg == g]
        if not items:
            continue
        txt.append("%s,#genre#" % g)
        for k, v in items:
            for e in best_of(v):
                txt.append("%s,%s" % (pretty_name(k), e["url"]))
    open(os.path.join(outdir, "直播源-精选.txt"), "w", encoding="utf-8").write(
        "\n".join(txt) + "\n")

    with open(os.path.join(outdir, "频道清单.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["分组", "频道名", "可达线路数", "首选线路"])
        for g, k, v in curated:
            w.writerow([g, pretty_name(k), sum(1 for e in v if alive(e["url"])),
                        best_of(v)[0]["url"]])

    stats = {}
    for g, k, v in curated:
        stats.setdefault(g, [0, 0])
        stats[g][0] += 1
        if any(alive(e["url"]) for e in best_of(v)):
            stats[g][1] += 1
    report = {"生成时间": stamp, "原始条目": len(entries), "去重后": len(uniq),
              "本机可达": n_alive, "精选频道数": len(curated),
              "有可达线路的频道": sum(v[1] for v in stats.values()),
              "分组": {k: {"频道数": v[0], "含可达线路": v[1]} for k, v in stats.items()}}
    json.dump(report, open(os.path.join(outdir, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("    精选 %d 个频道，其中 %d 个含实测可达线路"
          % (len(curated), report["有可达线路的频道"]))
    print("    输出目录：%s" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
