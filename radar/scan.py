"""马来西亚演唱会雷达：抓售票平台 API、IG（官方 Graph API）、Google News 与人工线索，输出 docs/data.json。

只用 Python 标准库。token 只从环境变量 IG_PAGE_TOKEN 读取，任何日志与输出都不会包含它。
"""
import email.utils
import hashlib
import html
import http.cookiejar
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
MYT = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
SLEEP = 1.0


# ---------- 通用 ----------

class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, url, data=None, headers=None, retries=3):
        h = {"User-Agent": UA, "Accept-Language": "en"}
        h.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        last = None
        for attempt in range(retries):
            try:
                with self.opener.open(urllib.request.Request(url, data=body, headers=h), timeout=30) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = HttpError(e.code, e.read().decode("utf-8", "replace"))
                if e.code in (400, 401, 403, 404):  # 这些重试也没用
                    break
            except Exception as e:  # 网络错误、超时
                last = e
            time.sleep(3 * (attempt + 1))
        raise last

    def json(self, url, data=None, headers=None, retries=3):
        return json.loads(self.request(url, data, {"Accept": "application/json", **(headers or {})}, retries))


class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}")
        self.code, self.body = code, body


def now_myt():
    return datetime.now(MYT)


def local(s):
    """来源给的时间都是马来西亚当地时间（GoLive/BookMyShow 虽然带 Z，实测是当地时间），统一成 'YYYY-MM-DD HH:MM'。"""
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s)
    return f"{m.group(1)} {m.group(2)}" if m else None


def text_of(fragment):
    """HTML 片段转成保留换行的纯文字。"""
    if not fragment:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", fragment)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in s.split("\n")]
    return "\n".join(l for l in lines if l)


def short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def lead_id(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


# ---------- 售票平台 ----------

PRICE_RE = re.compile(r"RM\s?[\d,]+(?:\.\d+)?(?:\s*[-–~至]\s*(?:RM\s?)?[\d,]+(?:\.\d+)?)?", re.I)


def parse_prices(text):
    """官网票价文字格式不一（'CAT 1: RM428'、'BOX - RM4,000'、'STANDING RM398'），逐行找价格，价格前面的字当票区名。"""
    tiers = []
    for line in text.split("\n"):
        prev = 0
        for m in PRICE_RE.finditer(line):
            name = line[prev:m.start()].strip(" -–:：|•*")
            prev = m.end()
            if name and len(name) <= 80:
                price = re.sub(r"(?i)RM\s?", "RM ", re.sub(r"\s+", " ", m.group(0)))
                tiers.append({"name": name, "price": price})
    return tiers


def collapse_tiers(tiers, limit=12):
    """票区太多（例如体育场按区卖）时，同价的区合并成一行。"""
    if len(tiers) <= limit:
        return tiers
    groups = {}
    for t in tiers:
        g = groups.setdefault(t.get("price"), {"price": t.get("price"), "names": [], "sold": []})
        g["names"].append(t["name"])
        g["sold"].append(t.get("sold_out", False))
    out = []
    for g in groups.values():
        n = g["names"]
        out.append({"name": ", ".join(n[:3]) + (f" 等 {len(n)} 区" if len(n) > 3 else ""),
                    "price": g["price"], "sold_out": all(g["sold"])})
    return sorted(out, key=lambda t: -price_value(t["price"]))


def price_value(p):
    m = re.search(r"[\d,]+(?:\.\d+)?", p or "")
    return float(m.group(0).replace(",", "")) if m else 0.0


def golive(h):
    base = "https://www.golive-asia.com/api"
    items, page = [], 1
    while True:
        res = h.json(f"{base}/event/list?itemsPerPage=99&page={page}&sortBy=created_at&sortDesc=true")["result"]
        items += res["result"]
        if page >= (res.get("totalPages") or 1):
            break
        page += 1
    out = []
    for e in items:
        d = h.json(f"{base}/event/{e['id']}")["result"]
        time.sleep(SLEEP)
        sections = d.get("EventInformations") or []
        info = {i["title"].strip(): text_of(i.get("description")) for i in sections}
        pricing = next((v for k, v in info.items() if "pric" in k.lower()), "")
        tiers = parse_prices(pricing)
        if not tiers:  # 没有票价段落时，价格通常只在座位图上；先收录文字里写明的 VIP 套票价格
            vip = next((v for k, v in info.items() if "vip" in k.lower()), "")
            tiers = [t for t in parse_prices(vip) if "package" in t["name"].lower() or "vip" in t["name"].lower()]
            for t in tiers:
                t["name"] = t["name"].rstrip(" (（")
        ticketing = next((v for k, v in info.items() if "ticketing" in k.lower()), "")
        limit = re.search(r"(?i)(?:maximum|up to)\s+(?:of\s+)?(\d+)\s+tickets?", ticketing)
        # 官方座位图 / 票价图：在“Seat Map / Pricing”段落里的图片，加上活动本身的座位图栏位
        maps = [u for i in sections if re.search(r"(?i)seat|pric|map", i["title"])
                for u in re.findall(r'<img[^>]+src="([^"]+)"', i.get("description") or "")]
        maps += [u for u in (d.get("seat_map"), d.get("ticket_seat_map")) if isinstance(u, str) and u.startswith("http")]
        venue = d.get("Venue") or {}
        cats = [c["event_category"]["name"] for c in d.get("EventDetailCategories") or [] if c.get("event_category")]
        out.append({
            "id": f"golive-{d['id']}",
            "source": "GoLive",
            "name": d["name"],
            "artist": d.get("artist_name"),
            "type": " / ".join(cats) or None,
            "venue": venue.get("name"),
            "city": venue.get("city"),
            "dates": sorted(filter(None, (local(x["event_date"]) for x in d.get("EventDates") or [] if not x.get("deleted_at")))),
            "sales": [{
                "name": s.get("name"),
                "start": local(s.get("start_date")),
                "end": local(s.get("end_date")),
                "queue": local(s.get("early_queue_date")),
                "available": s.get("status") == "ACTIVE",
                "code_required": bool(s.get("require_presale_code")),
                "url": s.get("url"),
            } for s in d.get("EventSalesDate") or []],
            "tiers": tiers,
            "price_text": short(pricing, 600) if pricing and not tiers else None,
            "seat_maps": list(dict.fromkeys(maps)),
            "limit": int(limit.group(1)) if limit else None,
            "notes": {k: short(v, 700) for k, v in info.items()
                      if any(w in k.lower() for w in ("ticketing", "admission", "vip", "important"))},
            "sold_out": bool(d.get("is_sold_out")),
            "selling_fast": bool(d.get("is_selling_fast")),
            "url": f"https://www.golive-asia.com/event/{d['id']}",
            "poster": d.get("portrait_image") or (d.get("images") or [None])[0] or d.get("event_logo"),
        })
    return out


def ticket2u(h):
    site = "https://www.ticket2u.com.my"
    ref = {"Referer": f"{site}/event/list/?cc=entertainment&scc=concert"}
    h.request(f"{site}/event/list/?cc=entertainment&scc=concert", headers=ref)
    h.request(f"{site}/api/RefreshToken.ashx", headers=ref)
    rows = []
    for page in range(1, 6):
        r = h.json(f"{site}/api/api2.ashx", {"method": "eventlisting", "data": {
            "cc": "entertainment", "scc": "concert", "stateid": "", "rowpp": 100, "currentpage": page}}, ref)
        if r.get("haserror"):
            raise RuntimeError(r.get("message") or "eventlisting error")
        batch = [x["row"] for x in r.get("data") or []]
        upcoming = [b for b in batch if b.get("status") == "upcoming"]
        rows += upcoming
        if len(upcoming) < len(batch) or not batch:  # 列表按即将举行排在前面，出现已过期就可以停
            break
    rows = [r for r in rows if r.get("countryid") == "1" and r.get("online") == "0"
            and "outside" not in (r.get("statename") or "").lower()]
    # “BNPL - xxx” 是同一场演出的分期付款入口，有原版就不重复收录
    names = {r["name"].strip().lower() for r in rows}
    rows = [r for r in rows if not (r["name"].lower().startswith("bnpl - ") and r["name"][7:].strip().lower() in names)]
    out = []
    for r in rows:
        tiers, sale_end, sold = [], None, []
        try:
            info = h.json(f"https://api1.tiket2u.my/api//event/GetTicketPurchaseInfo?EventID={r['id']}&PerfID=null",
                          headers={"Origin": site, "Referer": site + "/"})
            for perf in info.get("TicketPurchaseInfo") or []:
                sale_end = local(perf.get("SaleEndDate")) or sale_end
                for sec in perf.get("SectionTicketInfo") or []:
                    if sec.get("IsHide"):
                        continue
                    prices = [v["Price"] for v in sec.get("TicketVariant") or [] if not v.get("IsHide") and not v.get("HidePrice")]
                    tiers.append({
                        "name": sec.get("SectionName") or sec.get("TicketTypeName"),
                        "price": f"RM {min(prices):,.2f}" if prices else None,
                        "desc": sec.get("TicketTypeDesc"),
                        "sold_out": bool(sec.get("IsSoldOut")),
                    })
                    sold.append(bool(sec.get("IsSoldOut")))
        except Exception as e:
            print(f"  ticket2u detail {r['id']}: {e}", flush=True)
        time.sleep(SLEEP)
        out.append({
            "id": f"t2u-{r['id']}",
            "source": "Ticket2U",
            "name": html.unescape(r.get("titlename") or r["name"]),
            "artist": None,
            "type": r.get("eventsubcat"),
            "venue": r.get("locname"),
            "city": r.get("statename"),
            "dates": [d for d in [local(r.get("datefrom"))] if d],
            "sales": [{"name": "售票", "start": None, "end": sale_end, "queue": None, "available": True,
                       "code_required": False, "url": r.get("link")}] if sale_end else [],
            "tiers": collapse_tiers(tiers),
            "price_from": f"RM {float(r['pricefrom']):,.2f}" if r.get("pricefrom") else None,
            "limit": None,
            "sold_out": bool(sold) and all(sold),
            "url": r.get("link"),
            "poster": r.get("avatar"),
        })
    return out


def bookmyshow(h):
    # 有 Cloudflare，连续请求会被挡：只请求一次、不重试
    r = h.json("https://my.bookmyshow.com/api/v2/public/live/collections/e/items?lang=en-GB",
               headers={"Referer": "https://my.bookmyshow.com/en"}, retries=1)
    out = []
    for x in r.get("data") or []:
        if (x.get("timeZone") or {}).get("code", "").upper() != "ASIA/KUALA_LUMPUR":
            continue
        if any("outside malaysia" in (c or "").lower() for c in x.get("cities") or []):
            continue  # BookMyShow MY 也卖海外场次
        slug = ((x.get("content") or {}).get("slug") or {}).get("name") or ""
        img = x.get("cardImageUrl") or x.get("bannerImageUrl")
        out.append({
            "id": f"bms-{x.get('code')}",
            "source": "BookMyShow",
            "name": x.get("name"),
            "artist": None,
            "type": None,
            "venue": x.get("venue"),
            "city": next((c for c in x.get("cities") or [] if c and c.strip("* ")), None),
            "dates": sorted(filter(None, (local(t) for t in x.get("starttimes") or []))),
            "sales": [],
            "tiers": [],
            "limit": x.get("maxTicketPerTxn"),
            "organiser": (x.get("org") or {}).get("name"),
            "sold_out": False,
            "stop_sales": bool(x.get("stopSales")),
            "url": f"https://my.bookmyshow.com/en/events/{slug}/{x.get('code')}",
            "poster": f"https://{img}" if img and not img.startswith("http") else img,
        })
    return out


# ---------- 线索：IG、新闻、人工 ----------

def relevant(text, kw, need_malaysia):
    t = f" {text.lower()} "
    if not any(k in t for k in kw["concert"]):
        return False
    return not need_malaysia or any(k in t for k in kw["malaysia"])


def ig_leads(h, cfg, token, state, health):
    if not token:
        health["Instagram"] = {"ok": False, "error": "没有设定 IG_PAGE_TOKEN"}
        return []
    base = f"https://graph.facebook.com/{cfg['graph_version']}/"
    uid, kw = cfg["ig_user_id"], cfg["keywords"]

    def call(path, params):
        q = urllib.parse.urlencode({**params, "access_token": token})
        try:
            return h.json(f"{base}{path}?{q}", retries=2)
        except HttpError as e:  # 只取 Graph 返回的错误讯息，不含网址（网址里有 token）
            try:
                msg = json.loads(e.body)["error"]["message"]
            except Exception:
                msg = str(e)
            raise RuntimeError(msg) from None

    leads, bad, ok = [], [], 0
    n = cfg.get("ig_posts_per_account", 10)
    for user in cfg["ig_accounts"]:
        fields = (f"business_discovery.username({user}){{username,name,profile_picture_url,"
                  f"media.limit({n}){{caption,timestamp,permalink,media_type,media_url,thumbnail_url}}}}")
        try:
            bd = call(uid, {"fields": fields})["business_discovery"]
            ok += 1
        except Exception as e:
            bad.append({"account": user, "error": short(str(e), 160)})
            continue
        global_acct = user in cfg.get("ig_accounts_global", [])
        for m in (bd.get("media") or {}).get("data") or []:
            cap = m.get("caption") or ""
            if relevant(cap, kw, need_malaysia=global_acct):
                lead = ig_lead(m, f"@{bd.get('username', user)}", bd.get("name"))
                lead["avatar_src"] = bd.get("profile_picture_url")
                leads.append(lead)
        time.sleep(SLEEP)

    tag_ids = state.setdefault("hashtag_ids", {})
    for tag in cfg["hashtags"]:
        try:
            if tag not in tag_ids:  # 每 7 天最多查 30 个不同标签，id 查一次就存起来
                tag_ids[tag] = call("ig_hashtag_search", {"user_id": uid, "q": tag})["data"][0]["id"]
            res = call(f"{tag_ids[tag]}/recent_media", {"user_id": uid, "limit": 50,
                                                        "fields": "caption,timestamp,permalink,media_type,media_url"})
            ok += 1
        except Exception as e:
            bad.append({"account": f"#{tag}", "error": short(str(e), 160)})
            continue
        for m in res.get("data") or []:
            cap = m.get("caption") or ""
            if relevant(cap, kw, need_malaysia=True):
                leads.append(ig_lead(m, f"#{tag}", None))
        time.sleep(SLEEP)

    health["Instagram"] = {"ok": ok > 0, "count": len(leads), "checked": ok,
                           "error": f"{len(bad)} 个账号/标签失败" if bad else None}
    state["ig_bad"] = bad
    return leads


def ig_lead(m, where, name):
    cap = m.get("caption") or ""
    ts = m.get("timestamp")
    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(MYT).strftime("%Y-%m-%d %H:%M") if ts else None
    return {"id": "ig-" + lead_id(m.get("permalink") or cap), "kind": "IG", "from": where, "from_name": name,
            "title": short(cap.split("\n")[0], 120), "text": short(cap, 900), "time": t,
            "url": m.get("permalink"), "hints": sale_hints(cap), "sale_times": sale_times(cap, t),
            # IG 图片网址会过期，下载成本地图片后这个栏位会被移除
            "image_src": m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")}


def sale_hints(text):
    """把贴文里跟售票有关的行挑出来（开售/预售/日期/场馆），方便一眼看到重点。"""
    keys = re.compile(r"(?i)sale|presale|on-sale|ticket|开售|開售|公售|预售|預售|门票|門票|📅|📍|🎫|🎟|tiket|jualan", re.I)
    out = []
    for line in text.split("\n"):
        line = line.strip(" ⁠⁠")
        if line and keys.search(line) and len(line) < 160:
            out.append(line)
        if len(out) >= 5:
            break
    return out


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
SALE_LINE = re.compile(r"(?i)on-?\s?sale|presale|pre-sale|general sale|ticket(s|ing)? (sale|on sale|go live|available)|"
                       r"开售|開售|公售|预售|預售|抢票|搶票|开抢|開搶|jualan tiket|tiket dijual|waiting room|queue")
# “已经在卖”的句子里的日期通常是演出日期，不是开售日期
ONSALE_NOW = re.compile(r"(?i)on sale now|now on sale|available now|selling now|sold out|现正发售|現正發售|火热售票中|熱賣中|售票中")
EN_DATE = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(20\d\d))?"
                     r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?")
ZH_DATE = re.compile(r"(?:(20\d\d)\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]")
TIME = re.compile(r"(?i)(上午|中午|下午|晚上)?\s*(\d{1,2})(?:[:：.](\d{2}))?\s*(am|pm|点|點)|(\d{1,2})[:：](\d{2})")


def sale_times(text, posted):
    """从官方公告的“开售/预售”行读出开售时间（马来西亚时间）。只看提到售票的行，避免把演出日期当成开售日期。"""
    base = datetime.strptime(posted, "%Y-%m-%d %H:%M") if posted else datetime.now(MYT).replace(tzinfo=None)
    out = []
    for line in unicodedata.normalize("NFKC", text).split("\n"):  # 𝐎𝐧-𝐒𝐚𝐥𝐞 这类花体字转回普通字母
        if not SALE_LINE.search(line) or ONSALE_NOW.search(line):
            continue
        m = EN_DATE.search(line)
        z = ZH_DATE.search(line)
        if m:
            day = int(m.group(1) or m.group(5))
            mon = MONTHS[(m.group(2) or m.group(4)).lower()[:3]]
            year = m.group(3) or m.group(6)
        elif z:
            year, mon, day = z.group(1), int(z.group(2)), int(z.group(3))
        else:
            continue
        rest = line[(m or z).end():]
        t = TIME.search(rest) or TIME.search(line)
        hh = mm = None
        if t:
            if t.group(5):
                hh, mm = int(t.group(5)), int(t.group(6))
            else:
                hh, mm = int(t.group(2)), int(t.group(3) or 0)
                if (t.group(4) or "").lower() == "pm" or t.group(1) in ("下午", "晚上"):
                    hh = hh % 12 + 12
                elif (t.group(4) or "").lower() == "am" and hh == 12:
                    hh = 0
        y = int(year) if year else base.year
        try:
            when = datetime(y, mon, day, hh if hh is not None and hh < 24 else 0, mm or 0)
        except ValueError:
            continue
        if not year and when < base - timedelta(days=60):  # 例如 12 月贴文讲 1 月开售
            when = when.replace(year=y + 1)
        if when < base - timedelta(days=1):
            continue
        out.append({"time": when.strftime("%Y-%m-%d %H:%M") if hh is not None else when.strftime("%Y-%m-%d"),
                    "line": line.strip(" ⁠⁠")[:140]})
    return out[:4]


def news_leads(h, cfg, health):
    leads, seen, errors = [], set(), 0
    for q in cfg["news_queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({k: q[k] for k in ("q", "hl", "gl", "ceid")})
        try:
            root = ET.fromstring(h.request(url))
        except Exception as e:
            errors += 1
            print(f"  news {q['q']}: {e}", flush=True)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = re.sub(r"\W+", "", title.lower())[:80]
            # 英文/中文新闻常混进外国演出，要求标题提到马来西亚；马来文查询本身就是本地新闻，不要求
            if not title or key in seen or not relevant(title, cfg["keywords"], q.get("need_malaysia", True)):
                continue
            seen.add(key)
            pub = it.findtext("pubDate")
            t = email.utils.parsedate_to_datetime(pub).astimezone(MYT).strftime("%Y-%m-%d %H:%M") if pub else None
            src = it.find("source")
            leads.append({"id": "news-" + lead_id(key), "kind": "新闻", "from": src.text if src is not None else "Google News",
                          "title": title, "text": None, "time": t, "url": it.findtext("link"), "hints": []})
        time.sleep(SLEEP)
    health["Google News"] = {"ok": errors < len(cfg["news_queries"]), "count": len(leads),
                             "error": f"{errors} 个查询失败" if errors else None}
    return leads


def manual_leads(h, health):
    """GitHub Issue 标题以 [线索] 开头的，当作人工线索（例如小红书链接）。"""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo:
        return []
    try:
        issues = h.json(f"https://api.github.com/repos/{repo}/issues?state=open&per_page=50",
                        headers={"Authorization": f"Bearer {token}"} if token else {}, retries=2)
    except Exception as e:
        health["人工线索"] = {"ok": False, "error": short(str(e), 120)}
        return []
    out = []
    for i in issues:
        if i.get("pull_request") or not i.get("title", "").startswith("[线索]"):
            continue
        body = i.get("body") or ""
        link = re.search(r"https?://\S+", body)
        out.append({"id": f"manual-{i['number']}", "kind": "人工", "from": "你提交的线索",
                    "title": i["title"][4:].strip() or "（没有标题）", "text": short(body, 600),
                    "time": local(i.get("created_at")), "url": link.group(0) if link else i.get("html_url"),
                    "issue": i.get("html_url"), "hints": sale_hints(body),
                    "sale_times": sale_times(body, local(i.get("created_at")))})
    health["人工线索"] = {"ok": True, "count": len(out)}
    return out


# ---------- 合并与输出 ----------

def save_maps(h, events, state, now_s, first_run):
    """官方座位图是 10 小时就失效的签名链接，下载成仓库里的本地图片（docs/maps/）。
    每次都重新下载比对指纹：官方换图（例如先出无价座位图、之后换成含价版）就更新本地图片并记录。
    演出下架、或官方撤下的旧图会删除。返回有更新的演出。"""
    base = ROOT / "docs" / "maps"
    seen = state.setdefault("map_hashes", {})
    updated = []
    for e in events:
        old = seen.get(e["id"], {})
        cur, local_paths = {}, []
        for u in e.get("seat_maps") or []:
            if not u.startswith("http"):  # 来源这次没抓到、沿用上次资料时已经是本地路径
                local_paths.append(u)
                cur[u.rsplit("/", 1)[-1]] = old.get(u.rsplit("/", 1)[-1])
                continue
            name = re.sub(r"[^\w.-]", "_", urllib.parse.urlparse(u).path.rsplit("/", 1)[-1])[-80:] or "map"
            rel = f"maps/{e['id']}/{name}"
            path = ROOT / "docs" / rel
            try:
                with h.opener.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=60) as r:
                    blob = r.read(8_000_001)
                    ctype = r.headers.get("Content-Type") or ""
                if len(blob) > 8_000_000 or not ctype.startswith("image/"):
                    raise ValueError("不是图片或太大")
                digest = hashlib.sha1(blob).hexdigest()
                if not path.exists() or old.get(name) != digest:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(blob)
                cur[name] = digest
                time.sleep(SLEEP)
            except Exception as ex:
                print(f"  seat map {e['id']}: {ex}", flush=True)
                if not path.exists():
                    continue
                cur[name] = old.get(name)
            local_paths.append(rel)
        e["seat_maps"] = local_paths
        if cur != old and cur and e["id"] in seen and not first_run:
            e.setdefault("updates", {})["maps"] = now_s
            updated.append(e)
        if cur:
            seen[e["id"]] = cur
        # 官方撤下的旧图删除
        folder = base / e["id"]
        if folder.exists():
            for f in folder.iterdir():
                if f.name not in cur:
                    f.unlink()
    live = {e["id"] for e in events}
    for k in [k for k in seen if k not in live]:
        del seen[k]
    if base.exists():
        for d in base.iterdir():
            if d.is_dir() and d.name not in live:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
    return updated


def track_prices(events, state, now_s, first_run):
    """票价从“未公布”变成有价格时记录下来（例如之前只有座位图）。"""
    seen = state.setdefault("tier_counts", {})
    updated = []
    for e in events:
        n = len(e.get("tiers") or []) + (1 if e.get("price_text") else 0)
        if e["id"] in seen and seen[e["id"]] == 0 and n > 0 and not first_run:
            e.setdefault("updates", {})["prices"] = now_s
            updated.append(e)
        seen[e["id"]] = n
    return updated


# ---------- 明星名字与头像 ----------

TITLE_NOISE = re.compile(
    r"(?i)\b(konsert|concert|live in concert|live in|live|world tour|asia tour|asian tour|tour|showcase|"
    r"in kuala lumpur|kuala lumpur|in malaysia|malaysia|johor bahru|in sabah|sabah|penang|"
    r"20\d\d(-\d\d)?|anniversary|\d+(st|nd|rd|th)|feat\.?|ft\.?|presents?|bnpl|the)\b")


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def title_queries(e):
    """从演出名称猜几个可能的艺人名，拿去 Deezer 搜索。"""
    # Deezer 搜索不会忽略多余字词（“Air Supply A Matter of Time” 搜不到，“Air Supply” 才行），
    # 所以试：官方艺人栏位、“by 某某”、冒号前的部分，再从名称开头取前 3/2/1 个词
    name = re.sub(r"\[[^\]]*\]|【[^】]*】|<[^>]*>", " ", html.unescape(e["name"]))
    out = [e.get("artist")]
    by = re.search(r"(?i)\bby\s+([^:：《(（\[|]+)", name)
    if by:
        out.append(TITLE_NOISE.sub(" ", by.group(1)))
    out.append(TITLE_NOISE.sub(" ", re.split(r"\s*[:：《「(（|]|\s[-–]\s", name)[0]))
    words = re.sub(r"\s+", " ", TITLE_NOISE.sub(" ", re.sub(r"[^\w\s'&.]", " ", name))).strip().split(" ")
    for n in (3, 2, 1):
        if len(words) >= n:
            out.append(" ".join(words[:n]))
    seen, res = set(), []
    for q in out:
        q = (q or "").strip(" -–'\"")
        if len(norm(q)) >= 3 and norm(q) not in seen:
            seen.add(norm(q))
            res.append(q)
    return res


def deezer_artist(h, e):
    """回传 (艺人名, 头像网址)。只接受名字确实出现在演出名称里的结果，避免认错人。"""
    title = norm(html.unescape(e["name"]) + " " + (e.get("artist") or ""))
    found = {}
    for q in title_queries(e):
        try:
            res = h.json("https://api.deezer.com/search/artist?q=" + urllib.parse.quote(q) + "&limit=10", retries=2)
        except Exception:
            continue
        time.sleep(0.3)
        for a in res.get("data") or []:
            n = norm(a.get("name"))
            if len(n) < 2 or n not in title or not a.get("picture_medium") or "/artist//" in a["picture_medium"]:
                continue  # 名字要出现在演出名称里；Deezer 没有照片时是预设灰图
            # 演出名称里的普通单词（Spotlight、Cadenza、Dior…）常撞到同名小账号：用粉丝数把关
            if re.search(r"[^\x00-\x7f]", a["name"]):
                need = 20            # 中文等名字本身就很特定
            elif " " not in a["name"].strip():
                need = 5000          # 单个英文词
            else:
                need = 300
            if a.get("nb_fan", 0) >= need:
                found[a["id"]] = a
    if not found:
        return None, None
    best = max(found.values(), key=lambda a: (len(norm(a["name"])), a.get("nb_fan", 0)))
    return best["name"], best.get("picture_big") or best["picture_medium"]


def download_image(h, url, rel, max_bytes=3_000_000):
    path = ROOT / "docs" / rel
    if path.exists():
        return rel
    try:
        with h.opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
            blob = r.read(max_bytes + 1)
            ctype = r.headers.get("Content-Type") or ""
        if len(blob) > max_bytes or not ctype.startswith("image/"):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return rel
    except Exception as ex:
        print(f"  image {rel}: {ex}", flush=True)
        return None


def add_avatars(h, events, leads, state, today):
    """每场演出：先找 Deezer 的艺人照片，找不到用官方海报。结果存进 state 避免每次重查；
    没找到的 7 天后再试。图片下载到 docs/avatars/。"""
    cache = state.setdefault("artists", {})
    used = set()
    for e in events:
        c = cache.get(e["id"])
        if not c or (not c.get("photo") and c.get("checked", "") < (datetime.fromisoformat(today) - timedelta(days=7)).strftime("%Y-%m-%d")):
            name, pic = deezer_artist(h, e)
            c = {"name": name, "photo": None, "checked": today}
            if pic:
                c["photo"] = download_image(h, pic, f"avatars/a-{hashlib.sha1(norm(name).encode()).hexdigest()[:12]}.jpg")
            cache[e["id"]] = c
        guesses = title_queries(e)
        e["artist_name"] = c.get("name") or e.get("artist") or (guesses[0] if guesses else e["name"])
        avatar, kind = c.get("photo"), "artist"
        if not avatar and not e.get("poster") and e.get("avatar") and (ROOT / "docs" / e["avatar"]).exists():
            avatar, kind = e["avatar"], e.get("avatar_kind") or "poster"  # 来源这次被挡、沿用上次资料
        elif not avatar and e.get("poster"):
            ext = ".png" if ".png" in e["poster"].split("?")[0].lower() else ".jpg"
            avatar, kind = download_image(h, e["poster"], f"avatars/p-{e['id']}{ext}"), "poster"
        e["avatar"], e["avatar_kind"] = (avatar, kind) if avatar else (None, None)
        e.pop("poster", None)  # 签名链接会失效，不放进输出
        if avatar:
            used.add(avatar)
    for l in leads:
        src = l.pop("avatar_src", None)
        if l["kind"] == "IG" and l["from"].startswith("@"):
            rel = f"avatars/ig-{norm(l['from'])}.jpg"
            if src and not (ROOT / "docs" / rel).exists():
                download_image(h, src, rel)
            if (ROOT / "docs" / rel).exists():
                l["avatar"] = rel
                used.add(rel)
    for k in [k for k in cache if k not in {e["id"] for e in events}]:
        del cache[k]
    return used


def lead_candidates(l):
    """从贴文猜可能的艺人名：#标签（#AlanTam → Alan Tam）、第一行冒号/书名号前的部分、连续大写开头的英文词。"""
    text = unicodedata.normalize("NFKC", f"{l.get('title', '')}\n{l.get('text') or ''}")
    first = text.split("\n")[0]
    cands = [re.split(r"\s*[:：《「(（|—]|\s[-–]\s", first)[0]]
    for tag in re.findall(r"#([^\s#]{3,40})", text)[:8]:
        tag = re.sub(r"(?i)(in)?(kl|kualalumpur|malaysia|my|live|concert|tour|worldtour|asiatour)$", "", tag)
        cands.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag))
    cands += re.findall(r"\b([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,2})\b", " ".join(text.split("\n")[:3]))
    seen, out = set(), []
    for c in cands:
        c = re.sub(r"\s+", " ", TITLE_NOISE.sub(" ", c)).strip(" -–'\"!.,")
        if 2 <= len(norm(c)) <= 30 and norm(c) not in seen:
            seen.add(norm(c))
            out.append(c)
    return out[:8]


# 跟艺人同名的普通字词（Deezer 上真的有叫 “Arena”、“WE” 的艺人），贴文里出现不代表是在讲他们
COMMON_NAMES = {"arena", "we", "soldout", "sold out", "say hi", "hello", "love", "live", "concert", "music", "tuesday",
                "friday", "saturday", "sunday", "today", "tonight", "vip", "ticket", "tickets", "legends", "alpha", "crowd"}


def mentions(name, text):
    """贴文有没有提到这个名字。中文名直接找；英文名要是完整的词（避免 “ALPHA RAIH” 被当成 A-Lin），
    3 个字母以内的名字还要大小写一致（避免 “We're” 被当成 WE）。"""
    name = unicodedata.normalize("NFKC", name or "").strip()
    text = unicodedata.normalize("NFKC", text or "")
    if not norm(name):
        return False
    if re.search(r"[^\x00-\x7f]", name):
        return norm(name) in norm(text)
    words = [w for w in re.split(r"[\W_]+", name) if w]
    pat = r"(?<![A-Za-z0-9])" + r"[\W_]*".join(map(re.escape, words)) + r"(?![A-Za-z0-9])"
    return bool(re.search(pat, text, 0 if len(norm(name)) <= 3 else re.I))


def lookup_artist(h, cand, text, cache, budget):
    """用 Deezer 确认候选名字真的是艺人；结果存进 cache（包括“不是艺人”），避免重复查询。"""
    key = norm(cand)
    if key not in cache:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            res = h.json("https://api.deezer.com/search/artist?q=" + urllib.parse.quote(cand) + "&limit=5", retries=2)
            time.sleep(0.3)
        except Exception:
            return None
        hit = None
        for a in res.get("data") or []:
            n = norm(a.get("name"))
            need = 20 if re.search(r"[^\x00-\x7f]", a.get("name", "")) else 5000 if " " not in a.get("name", "").strip() else 300
            if n == key and a.get("nb_fan", 0) >= need and "/artist//" not in (a.get("picture_medium") or "/artist//"):
                hit = {"name": a["name"], "photo_url": a.get("picture_big") or a["picture_medium"], "fans": a["nb_fan"]}
                break
        cache[key] = hit
    hit = cache[key]
    return hit if hit and hit["name"].lower() not in COMMON_NAMES and mentions(hit["name"], text) else None


def group_pending(h, events, leads, state, cfg):
    """待确定的线索（售票平台还没上架）按艺人归类：先看设定里的关注名单，再用 Deezer 确认贴文里提到的名字。
    也把艺人照片与 IG 贴文图片下载成本地图片。回传用到的图片路径。"""
    used = set()
    cache = state.setdefault("artist_lookup", {})
    budget = [120]  # 每次扫描最多查 120 个新名字
    watch = cfg.get("watch_artists", [])
    by_event = {e["id"] for e in events}
    for l in leads:
        src = l.pop("image_src", None)
        for k in ("artist", "artist_photo", "image"):  # 旧线索存在 state 里，每次按最新规则重新归类
            l.pop(k, None)
        pending = not any(m in by_event for m in l.get("matches", []))
        text = f"{l.get('title', '')}\n{l.get('text') or ''}"
        artist = None
        for w in watch:
            if any(mentions(a, text) for a in [w["name"], *w.get("aliases", [])]):
                artist = {"name": w["name"], "photo_url": None}
                break
        if not artist and pending:
            found = [x for x in (lookup_artist(h, c, text, cache, budget) for c in lead_candidates(l)) if x]
            if found:
                artist = max(found, key=lambda a: (a.get("fans", 0), len(a["name"])))
        if artist:
            l["artist"] = artist["name"]
            rel = artist_photo(h, artist, watch, cache)
            if rel:
                l["artist_photo"] = rel
                used.add(rel)
        # IG 贴文图片（只留待确定的，已上架的看售票平台就好）
        rel = f"posts/{l['id']}.jpg"
        if pending and l["kind"] == "IG":
            if src and not (ROOT / "docs" / rel).exists():
                download_image(h, src, rel, max_bytes=2_000_000)
            if (ROOT / "docs" / rel).exists():
                l["image"] = rel
                used.add(rel)
    # 关注名单里的明星就算还没有消息，也在页面上列出来（附照片）
    watch_out = []
    for w in watch:
        rel = artist_photo(h, {"name": w["name"]}, watch, cache)
        if rel:
            used.add(rel)
        watch_out.append({"name": w["name"], "photo": rel})
    return used, watch_out


def artist_photo(h, artist, watch, cache):
    """艺人照片存成 docs/avatars/a-<名字哈希>.jpg；关注名单的明星会用每个别名去 Deezer 找（例如 周兴哲 → Eric Chou）。"""
    rel = f"avatars/a-{hashlib.sha1(norm(artist['name']).encode()).hexdigest()[:12]}.jpg"
    if not (ROOT / "docs" / rel).exists():
        url = artist.get("photo_url") or (cache.get(norm(artist["name"])) or {}).get("photo_url")
        w = next((w for w in watch if w["name"] == artist["name"]), None)
        for n in [artist["name"], *(w or {}).get("aliases", [])]:
            if url:
                break
            if w:  # 你指定的明星：名字完全一样就收，不用粉丝数把关（A-Lin 在 Deezer 粉丝不多）
                try:
                    res = h.json("https://api.deezer.com/search/artist?q=" + urllib.parse.quote(n) + "&limit=5", retries=2)
                except Exception:
                    res = {}
                url = next((a.get("picture_big") or a["picture_medium"] for a in res.get("data") or []
                            if norm(a.get("name")) == norm(n) and "/artist//" not in (a.get("picture_medium") or "/artist//")), None)
            else:
                _, url = deezer_artist(h, {"name": n})
        if url:
            download_image(h, url, rel)
    return rel if (ROOT / "docs" / rel).exists() else None


def cleanup_images(used):
    for folder in ("avatars", "posts"):
        d = ROOT / "docs" / folder
        if d.exists():
            for f in d.iterdir():
                if f"{folder}/{f.name}" not in used:
                    f.unlink()


def match_names(ev):
    """用来在贴文里找这场演出的名字片段。"""
    names = set()
    for s in (ev.get("artist"), ev.get("name")):
        if not s:
            continue
        s = re.split(r"\s[-–:|]\s|[:：《(（\[]| - |\bin kuala lumpur\b|\blive in\b|\bworld tour\b|\basia tour\b",
                     s, flags=re.I)[0]
        s = re.sub(r"^(bnpl|\[[^\]]*\])\s*-?\s*|^\d{4}\s+", "", s.strip(), flags=re.I).strip()
        if len(s) >= 4:
            names.add(s.lower())
    return names


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    state_path = ROOT / "state" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    data_path = ROOT / "docs" / "data.json"
    prev = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {}
    now = now_myt()
    today = now.strftime("%Y-%m-%d")
    h = Http()
    health, events = {}, []

    for name, fn in (("GoLive", golive), ("Ticket2U", ticket2u), ("BookMyShow", bookmyshow)):
        t0 = time.time()
        try:
            got = fn(h)
            health[name] = {"ok": True, "count": len(got)}
        except Exception as e:
            got = []
            health[name] = {"ok": False, "error": short(str(e), 160)}
        if not got:  # 抓不到就沿用上一次的资料，不让页面突然变空
            old = [e for e in prev.get("events", []) if e.get("source") == name]
            if old:
                got = old
                health[name].update(stale=True, count=len(old))
        print(f"{name}: {health[name]} ({time.time() - t0:.0f}s)", flush=True)
        events += got

    # 只保留还没结束的（没有日期的也保留）
    events = [e for e in events if not e["dates"] or e["dates"][-1][:10] >= today]
    seen = state.setdefault("events_first_seen", {})
    first_run = not seen
    new_events = []
    for e in events:
        if e["id"] not in seen:
            seen[e["id"]] = now.strftime("%Y-%m-%d %H:%M")
            if not first_run:
                new_events.append(e)
        e["first_seen"] = seen[e["id"]]
    events.sort(key=lambda e: (e["dates"][0] if e["dates"] else "9999", e["name"]))
    now_s = now.strftime("%Y-%m-%d %H:%M")
    changed = save_maps(h, events, state, now_s, first_run) + track_prices(events, state, now_s, first_run)
    ups = state.setdefault("updates", {})
    for e in changed:
        ups.setdefault(e["id"], {}).update(e["updates"])
    live = {e["id"] for e in events}
    for k in [k for k in ups if k not in live]:
        del ups[k]
    for e in events:
        if e["id"] in ups:
            e["updates"] = ups[e["id"]]

    fresh = ig_leads(h, cfg, os.environ.get("IG_PAGE_TOKEN"), state, health)
    fresh += news_leads(h, cfg, health)
    fresh += manual_leads(h, health)
    print(f"leads: {len(fresh)}", flush=True)

    store = state.setdefault("leads", {})
    new_leads = []
    for l in fresh:
        old = store.get(l["id"])
        l["first_seen"] = old["first_seen"] if old else now.strftime("%Y-%m-%d %H:%M")
        if not old and not first_run and l["kind"] in ("IG", "人工"):
            new_leads.append(l)
        store[l["id"]] = l
    # 设定改了（例如账号改成要求提到马来西亚），旧线索也按新规则重新过滤
    global_from = {"@" + a for a in cfg.get("ig_accounts_global", [])}
    for k in [k for k, l in store.items() if l["kind"] == "IG" and l["from"] in global_from
              and not relevant(l.get("text") or "", cfg["keywords"], need_malaysia=True)]:
        del store[k]
    keep_after = (now - timedelta(days=cfg.get("leads_keep_days", 30))).strftime("%Y-%m-%d")
    for k in [k for k, l in store.items() if (l.get("time") or l["first_seen"])[:10] < keep_after]:
        del store[k]
    # 人工线索以 Issue 当前状态为准：关掉的 Issue 就移除
    if "人工线索" in health and health["人工线索"].get("ok"):
        live = {l["id"] for l in fresh if l["kind"] == "人工"}
        for k in [k for k, l in store.items() if l["kind"] == "人工" and k not in live]:
            del store[k]

    names = {e["id"]: match_names(e) for e in events}
    leads = sorted(store.values(), key=lambda l: l.get("time") or l["first_seen"], reverse=True)
    for l in leads:
        blob = f"{l.get('title', '')} {l.get('text') or ''}".lower()
        l["matches"] = [eid for eid, ns in names.items() if any(n in blob for n in ns)][:5]
        if l["kind"] in ("IG", "人工"):  # 旧线索也用最新规则重读开售时间
            l["sale_times"] = sale_times(l.get("text") or "", l.get("time"))
    used = add_avatars(h, events, leads, state, today)
    # 用 Deezer 确认过的艺人名再对一次线索（例如贴文写 “Siti Nurhaliza”，演出名称很长）
    for l in leads:
        raw = unicodedata.normalize("NFKC", f"{l.get('title', '')} {l.get('text') or ''}").lower()
        blob = norm(raw)
        for e in events:
            n = norm(e.get("artist_name"))
            if e["id"] in l["matches"] or len(n) < 3:
                continue
            # 短名字（FKJ、BTS）要整个词出现才算，避免撞到别的字
            hit = n in blob if len(n) >= 4 else re.search(rf"(?<![a-z0-9]){re.escape(e['artist_name'].lower())}(?![a-z0-9])", raw)
            if hit:
                l["matches"].append(e["id"])
    more, watch_out = group_pending(h, events, leads, state, cfg)
    used |= more
    cleanup_images(used)

    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "health": health,
        "ig_bad": state.get("ig_bad", []),
        "ig_accounts": cfg["ig_accounts"],
        "hashtags": cfg["hashtags"],
        "watch_artists": watch_out,
        "events": events,
        "leads": leads,
    }
    data_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_alert(new_events, new_leads, events, changed)
    print(f"events={len(events)} leads={len(leads)} new_events={len(new_events)} new_leads={len(new_leads)}")


def write_alert(new_events, new_leads, events, changed):
    """有新演出、票价/座位图更新、新的 IG/人工线索时写 data/alert.md，由 workflow 开 Issue 通知。"""
    path = ROOT / "data" / "alert.md"
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        path.unlink()
    if not new_events and not new_leads and not changed:
        return
    by_id = {e["id"]: e for e in events}
    lines = []
    if changed:
        lines.append("## 🆕 票价 / 座位图更新\n")
        for e in {x["id"]: x for x in changed}.values():
            what = "、".join(w for k, w in (("prices", "票价公布了"), ("maps", "座位图更新了")) if k in e.get("updates", {}))
            lines.append(f"- **{e['name']}**：{what} · [{e['source']}]({e['url']})")
        lines.append("")
    if new_events:
        lines.append(f"## 🎤 新上架的演出（{len(new_events)}）\n")
        for e in new_events:
            when = ", ".join(d[:10] for d in e["dates"][:3]) or "日期未公布"
            lines.append(f"- **{e['name']}** — {when} · {e.get('venue') or '场馆未公布'} · [{e['source']}]({e['url']})")
    if new_leads:
        lines.append(f"\n## 📣 新的 IG / 人工线索（{len(new_leads)}）\n")
        for l in new_leads[:20]:
            linked = [by_id[m]["name"] for m in l.get("matches", []) if m in by_id]
            tag = f"（已对应：{linked[0]}）" if linked else "（**尚未在售票平台找到**）"
            lines.append(f"- {l['from']}：[{l['title']}]({l['url']}) {tag}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
