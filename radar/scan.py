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
        # 官方资讯图片：海报、票务横幅、“Ticket Benefit”等段落里的图（常直接印着开票日期、SOLD OUT）
        # 座位图 / 票价段落另外处理；付款合作（BNPL）这类广告图跳过
        info_imgs = [("poster", d.get("portrait_image")), ("banner", d.get("ticket_banner"))]
        info_imgs += [(i["title"], im.get("url")) for i in sections
                      if not re.search(r"(?i)seat|pric|map|bnpl|pay|partner|bank|card", i["title"])
                      for im in i.get("images") or []]
        info_imgs += [(i["title"], u) for i in sections if not re.search(r"(?i)seat|pric|map|bnpl|pay|partner", i["title"])
                      for u in re.findall(r'<img[^>]+src="([^"]+)"', i.get("description") or "")]
        created = [local(t.get("created_at")) for t in d.get("EventTickets") or [] if t.get("created_at")]
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
                "status": s.get("status"),
                "code_required": bool(s.get("require_presale_code")),
                "url": s.get("url"),
            } for s in d.get("EventSalesDate") or []],
            "tiers": tiers,
            "price_text": short(pricing, 600) if pricing and not tiers else None,
            "seat_maps": list(dict.fromkeys(maps)),
            "limit": int(limit.group(1)) if limit else None,
            "notes": {k: short(v, 700) for k, v in info.items()
                      if any(w in k.lower() for w in ("ticketing", "admission", "vip", "important"))},
            # 售罄：整场标记售罄，或公售（General）那一轮标 SOLD_OUT
            "sold_out": bool(d.get("is_sold_out")) or any(
                s.get("status") == "SOLD_OUT" and re.search(r"(?i)general|public|公售", s.get("name") or "")
                for s in d.get("EventSalesDate") or []),
            # 没有任何售票轮次、也不能购买：售票已结束（轮次被官方撤下）或根本没公开售票
            "sales_closed": not (d.get("EventSalesDate") or []) and not d.get("is_purchasable"),
            "selling_fast": bool(d.get("is_selling_fast")),
            "organizers": [o["organizer"]["name"] for o in d.get("EventOrganizers") or [] if (o.get("organizer") or {}).get("name")],
            "info_images": [[t, u] for t, u in info_imgs if isinstance(u, str) and u.startswith("http")],
            "tickets_created": min(created) if created else None,
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


def fantopia(h):
    out = []
    for area, cur in (("MY", "MYR"), ("SG", "SGD"), ("TH", "THB")):
        out += fantopia_area(h, area, cur)
        time.sleep(SLEEP)
    return out


def fantopia_area(h, area, currency):
    # 要带 area 表头才会回传该国场次；Accept-Language 也是必须的（Http 默认有带）
    r = h.json("https://www.fantopia.io/fanapiWeb/eventsInfo/getEventsInfoPageV2?current=1&size=1000",
               headers={"area": area, "Referer": "https://www.fantopia.io/"})
    out = []
    for x in (r.get("data") or {}).get("records") or []:
        if x.get("symbol") != currency or x.get("deleteFlag"):
            continue
        start, end = local(x.get("startTime")), local(x.get("endTime"))
        dates = [start] if start else []
        if start and end and end[:10] > start[:10] and not end.endswith("23:59"):
            dates.append(end)
        sell = x.get("sellTimeStamp")
        sale_start = datetime.fromtimestamp(sell / 1000, MYT).strftime("%Y-%m-%d %H:%M") if sell else None
        url = f"https://www.fantopia.io/events-tickets?eventsKey={x.get('eventsKey')}"
        # 每一轮售票的名称和开卖时间（例如 “2027/01/10 (General Sale)”）在详细资料里；列表只有一个开卖时间
        sales = []
        if not end or end[:10] >= datetime.now(MYT).strftime("%Y-%m-%d"):
            try:
                det = h.json("https://www.fantopia.io/fanapiWeb/eventsInfo/inside/getEventsDetailByKey?eventsKey="
                             + urllib.parse.quote(x.get("eventsKey") or ""), headers={"area": area, "Referer": "https://www.fantopia.io/"},
                             retries=1)
                for sess in det.get("data") or []:
                    if sess.get("sellStartTime"):
                        sales.append({"name": (sess.get("title") or "开售").strip(), "start": sess["sellStartTime"][:16],
                                      "end": None, "queue": None, "available": sess.get("status") == 1,
                                      "code_required": sess.get("sellType") not in (None, 1), "url": url})
                time.sleep(0.2)
            except Exception as ex:
                print(f"  fantopia detail {x.get('eventsKey')}: {ex}", flush=True)
        # 官方活动说明：每一轮的名称和时间（VIP 预售、Trip.com 预售、公售…）、票价表、座位图
        info, maps, tiers = {}, [], []
        if not end or end[:10] >= datetime.now(MYT).strftime("%Y-%m-%d"):
            try:
                info = h.json("https://www.fantopia.io/fanapiWeb/eventsInfo/getEventsInfoByEventsKey?eventsKey="
                              + urllib.parse.quote(x.get("eventsKey") or ""), headers={"area": area, "Referer": "https://www.fantopia.io/"},
                              retries=1).get("data") or {}
                time.sleep(0.2)
            except Exception as ex:
                print(f"  fantopia info {x.get('eventsKey')}: {ex}", flush=True)
        desc = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", info.get("description") or "")))
        # 用活动建立（第一次上架）的时间推算没写年份的日期；最后编辑时间可能是几个月后，会把年份推错
        posted = (info.get("createTime") or info.get("updateTime") or "")[:16] or None
        for st in sale_times(desc, posted, latest=(dates or [None])[-1]) if desc else []:
            same = [s for s in sales if s["start"][:16] == st["time"][:16]]
            if same:  # 同一个时间：用官方说明里的轮次名称
                same[0]["name"] = st.get("name") or same[0]["name"]
                same[0]["kind"] = st.get("kind")
            else:
                sales.append({"name": st.get("name") or {"presale": "Presale", "general": "General Sale"}.get(st.get("kind"), "开售"),
                              "start": st["time"], "end": None, "queue": None, "available": True,
                              "code_required": st.get("kind") == "presale", "url": url, "kind": st.get("kind")})
        if isinstance(info.get("seatImg"), str) and info["seatImg"].startswith("http"):
            maps.append(info["seatImg"])
        sym = {"MYR": "RM", "SGD": "S$", "THB": "฿"}[currency]
        for t in info.get("ticketSimpleInfoVos") or []:
            if t.get("title") and t.get("price") is not None:
                tiers.append({"name": t["title"].strip(), "price": f"{sym} {t['price'] / 100:,.0f}"})
        # 列表的开卖时间（通常是第一轮）如果不在详细资料里，也保留
        if sale_start and not any(s["start"][:10] == sale_start[:10] for s in sales):
            sales.append({"name": "开售", "start": sale_start, "end": None, "queue": None, "available": True,
                          "code_required": False, "url": url})
        sales.sort(key=lambda s: s["start"])
        out.append({
            "id": f"fantopia-{x.get('eventsKey') or x.get('id')}",
            "country": area,
            "source": "Fantopia",
            "name": re.sub(r"^\[[^\]]*\]\s*", "", x.get("title") or "").strip(),
            "artist": None,
            "type": None,
            "venue": x.get("location"),
            "city": None,
            "dates": dates,
            "sales": sales,
            "tiers": tiers,
            "seat_maps": maps,
            "notes": {"官方说明": short(desc.strip(), 1500)} if desc.strip() else {},
            "price_from": f"{ {'MYR': 'RM', 'SGD': 'S$', 'THB': '฿'}[currency]} {x['minPrice'] / 100:,.2f}" if x.get("minPrice") else None,
            "limit": x.get("limitCount") or None,
            "sold_out": x.get("sellStatus") == 2,
            "url": url,
            "poster": x.get("ossUrl") or x.get("ossUrlMini"),
        })
    return out


def nolworld(h):
    """韩国 NOL World（Interpark 国际版）演唱会列表。资料写在网页的 Next.js 串流里；开票时间是韩国时间，换成马来西亚时间（-1 小时）。"""
    b = h.request("https://world.nol.com/en/ticket/genre/CONCERT/products")
    text = "".join(json.loads('"' + c + '"') for c in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', b, re.S))
    i = text.find('"pages":[')
    if i < 0:
        raise ValueError("NOL World 网页结构变了，找不到场次资料")
    pages, _ = json.JSONDecoder().raw_decode(text[i + 8:])
    out = []
    for x in (it for p in pages for it in (p.get("data") or {}).get("content") or []):
        name = unicodedata.normalize("NFKC", x.get("goodsName") or "")
        if "play&stay" in name.lower().replace(" ", "") or not str(x.get("regionCode") or "").startswith("42"):
            continue  # 酒店套票、韩国以外的场次不收
        opens = None
        if x.get("bookingOpenTime"):
            opens = (datetime.strptime(x["bookingOpenTime"][:16], "%Y-%m-%d %H:%M") - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        start, end = x.get("playStartDate"), x.get("playEndDate")
        url = f"https://world.nol.com/en/ticket/places/{x.get('placeCode')}/products/{x.get('goodsCode')}"
        # 官方票价表：商品页资料里的 price（座位等级 + 价格，韩币）
        tiers = []
        try:
            pb = h.request(url, retries=1)
            pt = "".join(json.loads('"' + c + '"') for c in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', pb, re.S))
            j = pt.find('"price":[')
            if j >= 0:
                prices, _ = json.JSONDecoder().raw_decode(pt[j + 8:])
                for p in prices:
                    if p.get("seatGradeName") and p.get("salesPrice") is not None:
                        grade = p["seatGradeName"] + ("" if p.get("priceGradeName") in (None, "", "General", "일반") else f" · {p['priceGradeName']}")
                        tiers.append({"name": grade, "price": f"₩{p['salesPrice']:,}"})
            time.sleep(0.3)
        except Exception as ex:
            print(f"  nol detail {x.get('goodsCode')}: {ex}", flush=True)
        out.append({
            "id": f"nol-{x.get('goodsCode')}",
            "country": "KR",
            "source": "NOL World",
            "name": name.strip(),
            "artist": x.get("mainArtist") or None,
            "type": x.get("subGenreName") or None,
            "venue": x.get("placeName"),
            "city": x.get("regionName"),
            "dates": [d for d in dict.fromkeys([start, end]) if d],  # 只有日期（YYYY-MM-DD）
            "sales": [{"name": x.get("salesTypeName") if x.get("salesTypeName") not in (None, "일반") else "开票",
                       "start": opens, "end": None, "queue": None, "available": True, "code_required": False, "url": url}] if opens else [],
            "tiers": tiers,
            "limit": None,
            "sold_out": False,
            "url": url,
            "poster": x.get("posterImageUrl") or x.get("goodsLargeImageUrl"),
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
                  f"media.limit({n}){{caption,timestamp,permalink,media_type,media_url,thumbnail_url,children{{media_type,media_url}}}}}}")
        try:
            try:
                bd = call(uid, {"fields": fields})["business_discovery"]
            except RuntimeError:  # 多图栏位（children）不被接受时，退回只拿第一张图
                bd = call(uid, {"fields": fields.replace(",children{media_type,media_url}", "")})["business_discovery"]
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
            try:
                res = call(f"{tag_ids[tag]}/recent_media", {"user_id": uid, "limit": 50,
                                                            "fields": "caption,timestamp,permalink,media_type,media_url,children{media_type,media_url}"})
            except RuntimeError:  # 多图栏位不被接受时，退回只拿第一张图
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
            "image_src": m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url"),
            # 多图贴文的其他图片（座位图常在第 2、3 张）：只在需要找座位图时才下载
            "more_images": [c["media_url"] for c in ((m.get("children") or {}).get("data") or [])[1:10]
                            if c.get("media_type") == "IMAGE" and c.get("media_url")]}


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
                       r"开售|開售|公售|预售|預售|抢票|搶票|开抢|開搶|发售|發售|开卖|開賣|售票|jualan tiket|tiket dijual|waiting room|queue|"
                       r"general\s*(admission\s*)?(sale|on-?sale)|public sale|fan ?club sale|member(ship)? (pre-?)?sale")
# “已经在卖”的句子里的日期通常是演出日期，不是开售日期
ONSALE_NOW = re.compile(r"(?i)on sale now|now on sale|available now|selling now|sold out|现正发售|現正發售|火热售票中|熱賣中|售票中")
EN_DATE = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(20\d\d))?"
                     r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?")
ZH_DATE = re.compile(r"(?:(20\d\d)\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]")
TIME = re.compile(r"(?i)(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*(\d{1,2})(?:[:：.](\d{2}))?\s*(am|pm|点|點|时|時)(?!间|間)|(\d{1,2})[:：](\d{2})")


def sale_times(text, posted, latest=None):
    """从官方公告的“开售/预售”行读出开售时间（马来西亚时间）。只看提到售票的行，避免把演出日期当成开售日期。
    latest：最后一场演出的日期。没写年份的开售日期不可能在演出之后，推算出来晚于演出就改成前一年。"""
    base = datetime.strptime(posted, "%Y-%m-%d %H:%M") if posted else datetime.now(MYT).replace(tzinfo=None)
    out = []
    # 先按句子（。！？换行）切，再按分句（，；、）切：一段新闻常同时写预售、公售、加场，还夹着演出日期
    # 只从“提到售票的分句”拿日期；句子提到加场的，这几轮算加场的票
    # 官方公告常把轮次名和日期分两行写（“GENERAL ON-SALE” 下一行才是 “29 Sep (Tue), 10AM”）：
    # 只有日期、没有售票字眼的行，接上前面最近的一行“轮次标题”
    lines, heading, fixed = (text or "").split("\n"), None, []
    for line in lines:
        n = unicodedata.normalize("NFKC", line).strip()
        has_date = EN_DATE.search(n) or ZH_DATE.search(n)
        if n and SALE_LINE.search(n) and not has_date and len(n) <= 80:
            heading = n.rstrip(":： ")
            fixed.append(line)
            continue
        if has_date and heading and not SALE_LINE.search(n):
            fixed.append(f"{heading}: {n}")
            heading = None
            continue
        if n:
            heading = None if has_date else heading
        fixed.append(line)
    text = "\n".join(fixed)
    for sent in re.split(r"[\n。！？]", text or ""):
        sent_added = bool(ADDED_RE.search(unicodedata.normalize("NFKC", sent)))
        # 中文句子按全形标点切；英文句子只按分号切（英文日期里常有逗号，例如 29 September 2026, from 3PM）
        parts = re.split(r"[，；、;]", sent) if re.search(r"[㐀-鿿]", sent) else sent.split(";")
        for clause in parts:
            out += _clause_sale(unicodedata.normalize("NFKC", clause), base, sent_added, latest)  # 𝐎𝐧-𝐒𝐚𝐥𝐞 这类花体字转回普通字母
    seen, uniq = set(), []
    for x in out:
        if x["time"] not in seen:
            seen.add(x["time"])
            uniq.append(x)
    return uniq[:8]


def round_title(s):
    """日期前面的轮次名称（例如 “BIGBANG V.I.P MEMBERSHIP PRESALE”、“GENERAL ON-SALE”）；不像轮次名称就回传 None。"""
    s = re.sub(r"^[^\w]+|[\s:：\-–—|(（]+$", "", s.strip())
    # 去掉句尾的连接词（“Public ticket sales will commence on” → “Public ticket sales”）
    s = re.sub(r"(?i)\s+(?:will\s+)?(?:commences?|starts?|begins?|opens?|goes live|go live|is|are|will be|be)?\s*(?:on|from|at|by)?$|"
               r"(?:将于|将在|于|在|是|为|從|从)$", "", s).strip(" :：-–—")
    return s if 3 <= len(s) <= 60 and SALE_LINE.search(s) else None


def _clause_sale(line, base, added, latest=None):
    """一个分句里的开售时间（没有就回传空）。"""
    out = []
    for _ in (0,):
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
                if (t.group(4) or "").lower() == "pm" or t.group(1) in ("下午", "傍晚", "晚上"):
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
        if not year and latest and when.strftime("%Y-%m-%d") > latest[:10]:
            when = when.replace(year=when.year - 1)  # 开售不可能在演出之后（没写年份时推算错了）
        if latest and when.strftime("%Y-%m-%d") > latest[:10]:
            continue
        # 已经过去的开售日期也回传（past=True），用来判断这则公告是否已过期
        now = datetime.now(MYT).replace(tzinfo=None)
        past = when + (timedelta(days=1) if hh is None else timedelta(hours=12)) < now  # 开票半天后就当已结束（只有日期的算一天）
        kind = "presale" if PRESALE_RE.search(line) and not GENERAL_RE.search(line) else "general" if GENERAL_RE.search(line) else "sale"
        out.append({"time": when.strftime("%Y-%m-%d %H:%M") if hh is not None else when.strftime("%Y-%m-%d"),
                    "line": line.strip(" ⁠⁠")[:140], "past": past, "kind": kind, "added": added,
                    "name": round_title(line[:(m or z).start()])})
    return out


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
            # 标题要提到演唱会；也要提到查询的国家（马来西亚 / 新加坡），马来文查询本身就是本地新闻，不要求
            if not title or key in seen or not relevant(title, cfg["keywords"], False)                     or (q.get("need_malaysia", True) and not place_hit(title, cfg, q.get("country", "MY"))):
                continue
            seen.add(key)
            pub = it.findtext("pubDate")
            t = email.utils.parsedate_to_datetime(pub).astimezone(MYT).strftime("%Y-%m-%d %H:%M") if pub else None
            src = it.find("source")
            leads.append({"id": "news-" + lead_id(key), "kind": "新闻", "country": q.get("country", "MY"), "from": src.text if src is not None else "Google News",
                          "title": title, "text": None, "time": t, "url": it.findtext("link"), "hints": []})
        time.sleep(SLEEP)
    health["Google News"] = {"ok": errors < len(cfg["news_queries"]), "count": len(leads),
                             "error": f"{errors} 个查询失败" if errors else None}
    # 本地媒体的 RSS（Google News 没收录马来西亚华文媒体，直接读它们官方的 RSS）
    for f in cfg.get("rss_feeds", []):
        n = 0
        try:
            root = ET.fromstring(h.request(f["url"], retries=2))
        except Exception as e:
            health[f["name"]] = {"ok": False, "error": short(str(e), 120)}
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not title or key in seen or not relevant(title, cfg["keywords"], False) \
                    or (f.get("need_malaysia", True) and not place_hit(title, cfg, f.get("country", "MY"))):
                continue
            seen.add(key)
            pub = it.findtext("pubDate")
            t = email.utils.parsedate_to_datetime(pub).astimezone(MYT).strftime("%Y-%m-%d %H:%M") if pub else None
            desc = text_of(it.findtext("description") or "")
            leads.append({"id": "news-" + lead_id(key), "kind": "新闻", "country": f.get("country", "MY"), "from": f["name"],
                          "title": title, "text": short(desc, 600) or None, "time": t, "url": it.findtext("link"), "hints": [],
                          "sale_times": sale_times(f"{title}\n{desc}", t)})
            n += 1
        health[f["name"]] = {"ok": True, "count": n}
        time.sleep(SLEEP)
    return leads


def manual_leads(h, health, state):
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
        # 自动读链接内容（FB / IG / 新闻的公开预览文字和图片），再顺着内文里的“全文”链接读一层
        page = link_preview(h, link.group(0), state) if link else {}
        body_all = "\n".join(x for x in (body, page.get("title"), page.get("text")) if x)
        title = i["title"][4:].strip() or page.get("title") or (page.get("text") or "").split("\n")[0][:120] or "（没有标题）"
        out.append({"id": f"manual-{i['number']}", "kind": "人工", "from": "你提交的线索",
                    "title": title, "text": short(body_all, 1500), "image_src": page.get("image"),
                    "time": local(i.get("created_at")), "url": link.group(0) if link else i.get("html_url"),
                    "issue": i.get("html_url"), "hints": sale_hints(body_all),
                    "sale_times": sale_times(body_all, local(i.get("created_at")))})
    health["人工线索"] = {"ok": True, "count": len(out)}
    return out


def article_text(h, url, state):
    """读新闻网页的正文（标题和摘要常没有开票时间，正文才有“预售 / 公售 / 加场”几号几点）。
    从 articleBody 或常见的正文区块开始取段落；遇到以“...”结尾的段落（相关新闻列表）就停。结果存 state。"""
    cache = state.setdefault("article_texts", {})
    if url in cache:
        return cache[url]
    text = ""
    try:
        page = h.request(url, retries=1)
        page = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
        start = re.search(r'itemprop="articleBody"|class="[^"]*(?:article-body|article__body|story-body|entry-content|'
                          r'article-content|news-content|post-content)[^"]*"|<article\b', page)
        paras = []
        for p in re.findall(r"<p\b[^>]*>(.*?)</p>", page[start.start() if start else 0:], re.S):
            p = text_of(p).strip()
            if p.endswith(("...", "…")):
                break  # 正文结束，后面是相关新闻
            if len(p) >= 15:
                paras.append(p)
            if len(paras) >= 15:
                break
        text = "\n".join(paras)[:4000]
    except Exception as ex:
        print(f"  article {url[:60]}: {ex}", flush=True)
    cache[url] = text
    return text


def enrich_articles(h, leads, state, limit=20):
    """对上已上架演出的新闻：读正文，重新找开票时间（预售、公售、加场都列出来）。每次最多读 limit 篇新的。"""
    n = 0
    cache = state.get("article_texts", {})
    used = set()
    for l in leads:
        url = l.get("url") or ""
        if l["kind"] != "新闻" or not l.get("matches") or not url.startswith("http") or "news.google.com" in url:
            continue
        used.add(url)
        if url not in cache:
            if n >= limit:
                continue
            n += 1
        body = article_text(h, url, state)
        if body and body not in (l.get("text") or ""):
            l["text"] = short(f"{l.get('text') or ''}\n{body}".strip(), 4000)
            l["sale_times"] = sale_times(f"{l['title']}\n{l['text']}", l.get("time"))
    for k in [k for k in state.get("article_texts", {}) if k not in used]:
        del state["article_texts"][k]


def link_preview(h, url, state, depth=1):
    """读链接的公开预览（og:title / og:description / og:image，FB、IG、新闻网站都有），不用登录。
    预览文字里如果有“全文”链接（例如 FB 贴文转发新闻），再顺着读一层。结果存 state，一个链接只读一次。"""
    cache = state.setdefault("link_previews", {})
    if cache.get(url):
        return cache[url]
    # Facebook 会挡 GitHub 机房：优先用你电脑读到的（local_fetch.py 存在 state/local/link_previews.json）
    local = ROOT / "state" / "local" / "link_previews.json"
    if local.exists():
        got = json.loads(local.read_text(encoding="utf-8")).get("previews", {}).get(url)
        if got and (got.get("title") or got.get("text")):
            cache[url] = got
            return got
    out = {}
    try:
        page = h.request(url, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}, retries=1)
        page = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
        meta = {}
        for tag in re.findall(r"<meta\b[^>]*>", page[:400000], re.I):
            k = re.search(r'(?:property|name)\s*=\s*"([^"]+)"', tag)
            v = re.search(r'content\s*=\s*"([^"]*)"', tag)
            if k and v:
                meta.setdefault(k.group(1).lower(), html.unescape(v.group(1)))
        title = meta.get("og:title") or html.unescape((re.search(r"<title>(.*?)</title>", page, re.S | re.I) or [None, ""])[1]).strip()
        out = {"title": None if title.lower() in ("facebook", "instagram", "") else title[:200],
               "text": (meta.get("og:description") or meta.get("description") or "")[:1500],
               "image": meta.get("og:image") or meta.get("twitter:image")}
        inner = re.search(r"https?://[^\s\"<>]+", out["text"])
        if depth and inner and inner.group(0) != url:
            sub = link_preview(h, inner.group(0), state, depth - 1)
            out["title"] = out["title"] or sub.get("title")
            out["text"] = "\n".join(x for x in (out["text"], sub.get("title"), sub.get("text")) if x)[:2500]
            out["image"] = out["image"] or sub.get("image")
    except Exception as ex:
        print(f"  link preview {url[:60]}: {ex}", flush=True)
    if out.get("title") or out.get("text"):  # 读不到的不存，下次再试
        cache[url] = out
    else:
        cache.pop(url, None)
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


def ocr_prices(path):
    """用 Tesseract（开源 OCR）把座位图上印的票价读出来。先用 ImageMagick 放大、转灰阶提高准确度。
    没装 Tesseract 的环境（例如本机）就跳过。"""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("tesseract"):
        return None
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        variants = [str(path)]
        magick = shutil.which("magick") or shutil.which("convert")
        if magick:
            for i, extra in enumerate((["-colorspace", "Gray"], ["-colorspace", "Gray", "-negate"])):
                out = str(Path(tmp) / f"v{i}.png")
                if subprocess.run([magick, str(path), "-resize", "200%", *extra, out], capture_output=True).returncode == 0:
                    variants.append(out)
        for v in variants:
            r = subprocess.run(["tesseract", v, "-", "--psm", "11"], capture_output=True, text=True, encoding="utf-8")
            if r.returncode == 0:
                texts.append(r.stdout)
    tiers, seen = [], set()
    for text in texts:
        for line in text.split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            for m in PRICE_RE.finditer(line):
                price = re.sub(r"(?i)RM\s?", "RM ", re.sub(r"\s+", " ", m.group(0)))
                if price_value(price) < 20 or norm(price) in seen:
                    continue
                seen.add(norm(price))
                name = line[:m.start()].strip(" -–:：|•*(（[")
                # 名字要像样（至少一个 3 个字母以上的英文字或 2 个中文字），否则是 OCR 乱码（例如 “By E”）
                ok = len(name) <= 60 and (re.search(r"[A-Za-z]{3,}", name) or re.search(r"[㐀-鿿]{2,}", name))
                tiers.append({"name": name if ok else "票区（颜色见座位图）", "price": price})
    return sorted(tiers, key=lambda t: -price_value(t["price"]))


def ocr_text(path):
    """读出贴文图片上的文字（中英韩）。很多公告只把艺人名、加场、开票时间印在图上。没装 Tesseract 就回传 None。"""
    import shutil
    import subprocess
    if not shutil.which("tesseract"):
        return None
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    lang = "+".join(x for x in ("eng", "chi_sim", "chi_tra", "kor") if x in langs) or "eng"
    r = subprocess.run(["tesseract", str(path), "-", "-l", lang, "--psm", "11"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return ""
    lines = [re.sub(r"\s+", " ", x).strip() for x in r.stdout.split("\n")]
    return "\n".join(x for x in lines if len(re.sub(r"[\W_]", "", x)) >= 2)[:800]  # 去掉只有零星乱码的行


SOLD_OUT_RE = re.compile(r"(?i)sold\s*-?\s*out|售罄|售謦|完售|매진|全部售完")


def official_images(h, events, state):
    """官方来源优先：把售票平台上的海报、票务说明图用 OCR 读出来（每张图只读一次，图片不存进仓库）。
    读到的字放进 e["official_text"]，之后由 sale_status() 判断开票时间、是否售罄。"""
    import tempfile
    cache = state.setdefault("ocr_official", {})
    used = set()
    for e in events:
        texts = []
        for title, url in e.pop("info_images", None) or []:
            key = url.split("?")[0]
            used.add(key)
            if key not in cache:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "img"
                    try:
                        with h.opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
                            path.write_bytes(r.read(6_000_000))
                    except Exception as ex:
                        print(f"  official image {e['id']}: {ex}", flush=True)
                        continue
                    text = ocr_text(path)
                if text is None:  # 没装 OCR（本机测试），下次再读
                    continue
                cache[key] = {"title": title, "text": text}
            if cache[key]["text"]:
                texts.append(cache[key]["text"])
        if texts:
            e["official_text"] = "\n".join(texts)[:3000]
    for k in [k for k in cache if k not in used]:
        del cache[k]


SALE_STATES = ["ANNOUNCED", "TICKET_INFO_PENDING", "PRESALE_ANNOUNCED", "GENERAL_SALE_ANNOUNCED", "ON_SALE", "SOLD_OUT"]
PRESALE_RE = re.compile(r"(?i)pre-?sale|预售|預售|优先|優先|member|会员|會員|v\.?i\.?p\.?|fan ?club|fanclub|card ?holder|선예매|팬클럽")
GENERAL_RE = re.compile(r"(?i)general|public|公售|公開|公开|一般发售|일반")


def sale_status(events, checks, now):
    """每场演出的售票状态（官方来源 > 售票平台 > 新闻），附上证据：
    TICKET_INFO_PENDING 还没有开票资料 → PRESALE_ANNOUNCED 公布了预售 → GENERAL_SALE_ANNOUNCED 公布了公售
    → ON_SALE 已开票 → SOLD_OUT 售罄 / 停售。平台下架了售票轮次、又没说售罄的是 SALE_CLOSED。"""
    now_s = now.strftime("%Y-%m-%d %H:%M")
    for e in events:
        ev = []  # 证据：{src, text, time?}
        text = e.get("official_text") or ""
        # 官方图片（OCR）：SOLD OUT 标记、开票时间
        so = SOLD_OUT_RE.findall(text)
        if so and len(so) >= max(1, len(e.get("dates") or [])):
            ev.append({"src": f"{e['source']} 官方图片", "text": "图上标示 SOLD OUT（每一场都有）"})
            e["sold_out"] = True
        seen_times = {(s.get("start") or "")[:16] for s in e.get("sales") or []}
        for st in sale_times(text, e.get("tickets_created") or e.get("first_seen"), latest=(e.get("dates") or [None])[-1]) if text else []:
            if st["time"][:16] in seen_times:
                continue
            seen_times.add(st["time"][:16])
            e.setdefault("sales", []).append({
                "name": f"{'公售' if GENERAL_RE.search(st['line']) else '开票'}（{e['source']} 官方图片）", "start": st["time"],
                "end": None, "queue": None, "available": True, "code_required": False, "announced": True, "official_img": True})
            ev.append({"src": f"{e['source']} 官方图片", "text": st["line"], "time": st["time"]})
        for s in e.get("sales") or []:
            if s.get("start") and not s.get("official_img"):
                ev.append({"src": s["name"] if s.get("announced") else f"{e['source']} 售票轮次", "text": s.get("name") or "开票", "time": s["start"]})
        c = checks.get(e.get("artist_name") or "") or {}
        if c.get("country", "MY") != (e.get("country") or "MY"):
            c = {}  # 新闻查证是按国家搜的，别国的场次不套用
        so_news, add_news = c.get("soldout_news"), e.get("news_added")
        if so_news and add_news and (add_news.get("time") or "") >= (so_news.get("time") or ""):
            c = dict(c, soldout_news=None)  # 售罄之后又加场：加场的票可能还在卖，不算售罄
        if c.get("soldout_news"):
            n = c["soldout_news"]
            ev.append({"src": "Google 新闻", "text": n["title"], "time": n.get("time"), "url": n.get("url")})
        if c.get("opened_news") and c["opened_news"] != c.get("soldout_news"):
            n = c["opened_news"]
            ev.append({"src": "Google 新闻", "text": n["title"], "time": n.get("time"), "url": n.get("url")})
        if e.get("tickets_created"):
            ev.append({"src": f"{e['source']} 票种建立时间", "text": "平台建立票种（通常就是开票前后）", "time": e["tickets_created"]})
        # 开票时间：平台 / 官方图片的确切时间优先；没有就用新闻、票种建立时间推估
        starts = sorted(s["start"] for s in e.get("sales") or [] if s.get("start") and s["start"] <= now_s)
        future = sorted((s for s in e.get("sales") or [] if s.get("start") and s["start"] > now_s), key=lambda s: s["start"])
        if starts:
            e["opened_at"], e["opened_approx"] = starts[0], False
        elif not future and (c.get("opened_news") or e.get("tickets_created")):
            guess = sorted(x for x in ((c.get("opened_news") or {}).get("time"), (e.get("tickets_created") or "")[:10]) if x)
            e["opened_at"], e["opened_approx"] = guess[0], True
        if e.get("sold_out") or e.get("stop_sales"):
            st = "SOLD_OUT"
        elif future:
            # 还没开票的轮次全部是预售（presale / 会员 / 粉丝会）才算“已公布预售”，否则就是公售
            pre = all(PRESALE_RE.search(s.get("name") or "") and not GENERAL_RE.search(s.get("name") or "") for s in future)
            st = "PRESALE_ANNOUNCED" if pre else "GENERAL_SALE_ANNOUNCED"
        elif starts or e.get("opened_approx") or e["source"] in ("Ticket2U", "BookMyShow") and not e.get("sales_closed"):
            st = "SALE_CLOSED" if e.get("sales_closed") and not starts else "ON_SALE"
            if st == "SALE_CLOSED" and c.get("soldout_news"):
                st = "SOLD_OUT"
        elif e.get("sales_closed"):
            st = "SALE_CLOSED"
        else:
            st = "TICKET_INFO_PENDING"
        e["sale_state"] = st
        e["sale_evidence"] = sorted(ev, key=lambda x: x.get("time") or "", reverse=True)[:8]


def update_watchlist(events, state, now):
    """监控名单：还查不到开票资料（TICKET_INFO_PENDING / SALE_CLOSED）的演出，每次扫描都重新查官方来源和新闻，
    查到了就移出名单并记录状态变化（写进通知）。回传状态有变化的演出。"""
    wl = state.setdefault("watchlist", {})
    hist = state.setdefault("sale_states", {})
    now_s = now.strftime("%Y-%m-%d %H:%M")
    changes = []
    for e in events:
        old = hist.get(e["id"])
        if old and old != e["sale_state"]:
            changes.append((e, old))
        hist[e["id"]] = e["sale_state"]
        if e["sale_state"] in ("TICKET_INFO_PENDING", "SALE_CLOSED") and not e.get("hidden"):
            w = wl.setdefault(e["id"], {"since": now_s, "checks": 0})
            w["checks"] += 1
            w["last_check"] = now_s
            e["watch"] = w
        else:
            wl.pop(e["id"], None)
    live = {e["id"] for e in events}
    for d in (wl, hist):
        for k in [k for k in d if k not in live]:
            del d[k]
    return changes


def ocr_leads(h, leads, state):
    """IG 贴文图片：下载到 docs/posts/，用 OCR 读出图上文字存进 image_text（每张图只读一次）。"""
    cache = state.setdefault("ocr_posts", {})
    for l in leads:
        if l["kind"] not in ("IG", "人工"):
            continue
        rel = f"posts/{l['id']}.jpg"
        src = l.get("image_src")
        if src and not (ROOT / "docs" / rel).exists():
            download_image(h, src, rel, max_bytes=2_000_000)
        if l["id"] not in cache and (ROOT / "docs" / rel).exists():
            text = ocr_text(ROOT / "docs" / rel)
            if text is not None:
                cache[l["id"]] = text
        if cache.get(l["id"]):
            l["image_text"] = cache[l["id"]]
    live = {l["id"] for l in leads}
    for k in [k for k in cache if k not in live]:
        del cache[k]


def lead_blob(l):
    """线索的全部文字：标题、内文、图片上读出来的字。"""
    return f"{l.get('title', '')}\n{l.get('text') or ''}\n{l.get('image_text') or ''}"


def add_ocr_tiers(events, state):
    """座位图上有、但文字票价没列出的价格，补进 ocr_tiers（页面会标注“从座位图读取”）。同一张图只读一次。"""
    cache = state.setdefault("ocr", {})
    for e in events:
        e.pop("ocr_tiers", None)
        known = {norm(t.get("price")) for t in e.get("tiers") or []}
        found = []
        for rel in e.get("seat_maps") or []:
            path = ROOT / "docs" / rel
            if not path.exists():
                continue
            key = hashlib.sha1(path.read_bytes()).hexdigest()
            if key not in cache:
                res = ocr_prices(path)
                if res is None:  # 没有 OCR 工具
                    continue
                cache[key] = res
            found += [t for t in cache[key] if norm(t["price"]) not in known]
            known |= {norm(t["price"]) for t in cache[key]}
        if found:
            e["ocr_tiers"] = found
    live = {hashlib.sha1((ROOT / "docs" / r).read_bytes()).hexdigest()
            for e in events for r in e.get("seat_maps") or [] if (ROOT / "docs" / r).exists()}
    for k in [k for k in cache if k not in live]:
        del cache[k]


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
    raw_title = html.unescape(e["name"]) + " " + (e.get("artist") or "")
    found = {}
    for q in title_queries(e):
        try:
            res = h.json("https://api.deezer.com/search/artist?q=" + urllib.parse.quote(q) + "&limit=10", retries=2)
        except Exception:
            continue
        time.sleep(0.3)
        for a in res.get("data") or []:
            n = norm(a.get("name"))
            # 名字要以“完整的词”出现在演出名称里（避免 ASEAN 里的 sean）；Deezer 没有照片时是预设灰图
            if len(n) < 2 or not mentions(a["name"], raw_title) or n in COMMON_NAMES \
                    or not a.get("picture_medium") or "/artist//" in a["picture_medium"]:
                continue
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
        return None, None, 0
    best = max(found.values(), key=lambda a: (len(norm(a["name"])), a.get("nb_fan", 0)))
    return best["name"], best.get("picture_big") or best["picture_medium"], best.get("nb_fan", 0)


# ---------- 歌手类别：华语 / K-pop / 欧美 / 其他（马来、印尼、印度等） ----------
HAN = re.compile(r"[一-鿿]")
HANGUL = re.compile(r"[가-힯]")
CHINESE_CC = {"CN", "TW", "HK", "MO", "SG"}
WESTERN_CC = {"US", "GB", "CA", "AU", "NZ", "IE", "FR", "DE", "NL", "BE", "LU", "SE", "NO", "DK", "FI", "IS",
              "AT", "CH", "ES", "IT", "PT", "PL", "CZ", "HU", "GR", "RO", "UA", "XE", "XW"}
MB_BUDGET = [80]  # 每次扫描最多查 80 个新艺人（每秒 1 次）
MB_UA = "MalaysiaConcertRadar/1.0 ( https://github.com/4hYang3074/Malaysia-Concert-Radar )"


def musicbrainz(h, name, cache, budget):
    """查 MusicBrainz（开放音乐资料库）的艺人国家；同时搜正式名与别名（周杰倫 的别名是 Jay Chou）。结果存 cache。"""
    key = norm(name)
    if key in cache:
        return cache[key]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    q = urllib.parse.urlencode({"query": f'artist:"{name}" OR alias:"{name}"', "fmt": "json", "limit": 3})
    try:
        res = h.json("https://musicbrainz.org/ws/2/artist/?" + q, headers={"User-Agent": MB_UA}, retries=2)
    except Exception:
        return None
    finally:
        time.sleep(1.1)  # MusicBrainz 限制每秒 1 次
    cands = [{"name": a.get("name"), "country": a.get("country")} for a in res.get("artists") or []
             if a.get("score", 0) >= 80 and a.get("country")]
    # 同名艺人（例如 izna 有日本和韩国两个）：优先韩国、华语地区
    hit = next((c for c in cands if c["country"] == "KR"), None) or \
        next((c for c in cands if c["country"] in CHINESE_CC), None) or (cands[0] if cands else None)
    cache[key] = hit or {}
    return cache[key]


def region_of(h, name, title, cache, budget):
    if not name:
        return "其他"
    if HAN.search(name):
        return "华语"
    if HANGUL.search(name) or HANGUL.search(title or ""):
        return "K-pop"
    mb = musicbrainz(h, name, cache, budget) or {}
    cc = mb.get("country")
    if cc == "KR":
        return "K-pop"
    if cc in CHINESE_CC or (cc == "MY" and HAN.search(mb.get("name") or "")):
        return "华语"  # 马来西亚华人歌手（资料库登记中文名）也算华语
    if cc in WESTERN_CC:
        return "欧美"
    if HAN.search(title or ""):
        return "华语"  # 例如演出名称写着 周杰伦
    return "其他"


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
    cache = state.setdefault("artists_v3", {})  # v3：艺人名改成“完整的词”比对后重新识别
    used = set()
    for e in events:
        c = cache.get(e["id"])
        stale = c and c.get("checked", "") < (datetime.fromisoformat(today) - timedelta(days=7)).strftime("%Y-%m-%d")
        # 没找到的 7 天后重试；找到的也每 7 天更新一次粉丝数（用来判断当红程度）
        if not c or "fans" not in c or stale:
            name, pic, fans = deezer_artist(h, e)
            c = {"name": name, "photo": None, "checked": today, "fans": fans}
            if pic:
                c["photo"] = download_image(h, pic, f"avatars/a-{hashlib.sha1(norm(name).encode()).hexdigest()[:12]}.jpg")
            cache[e["id"]] = c
        guesses = title_queries(e)
        e["artist_name"] = c.get("name") or e.get("artist") or (guesses[0] if guesses else e["name"])
        e["fans"] = c.get("fans") or 0
        e["region"] = region_of(h, e["artist_name"], e["name"], state.setdefault("regions_v2", {}), MB_BUDGET)
        if e["region"] == "其他" and not c.get("name"):
            # 没有在 Deezer 认出艺人时，用演出名称里的其他候选名再查（例如 “MJ116 OGS TOUR” → MJ116）；
            # 只接受华语 / K-pop，避免普通单词撞到同名的欧美艺人
            for g in guesses[:4]:
                r = region_of(h, g, e["name"], state.setdefault("regions_v2", {}), MB_BUDGET)
                if r in ("华语", "K-pop"):
                    e["region"] = r
                    break
        # 韩国场次：默认当作韩国歌手；欧美歌手来韩国巡演（名称写 Tour in Seoul / Asia Tour，或很红）才算欧美
        intl_tour = re.search(r"(?i)\b(tour|live)\b.*\b(in (seoul|korea|busan|incheon))\b|\basia(n)? tour\b|\bworld tour\b", e["name"])
        if e.get("country") == "KR" and not (e["region"] == "欧美" and (e["fans"] >= 100000 or intl_tour)):
            e["region"] = "K-pop"
        # 官方海报（直式大图卡片用）：每场都存一份；签名链接会失效，所以存成本地图片
        ext = ".png" if ".png" in (e.get("poster") or "").split("?")[0].lower() else ".jpg"
        poster_rel = f"avatars/p-{e['id']}{ext}"
        poster = download_image(h, e["poster"], poster_rel) if e.get("poster") else None
        if not poster and (ROOT / "docs" / poster_rel).exists():
            poster = poster_rel  # 来源这次被挡、沿用上次下载的
        if not poster and e.get("poster_img") and (ROOT / "docs" / e["poster_img"]).exists():
            poster = e["poster_img"]
        e["poster_img"] = poster
        avatar, kind = c.get("photo"), "artist"
        if not avatar and e.get("avatar") and e.get("avatar_kind") == "artist" and (ROOT / "docs" / e["avatar"]).exists():
            avatar = e["avatar"]
        if not avatar and poster:
            avatar, kind = poster, "poster"
        e["avatar"], e["avatar_kind"] = (avatar, kind) if avatar else (None, None)
        e.pop("poster", None)  # 签名链接会失效，不放进输出
        used |= {x for x in (avatar, poster) if x}
    for l in leads:
        src = l.pop("avatar_src", None)
        if l["kind"] == "IG" and l["from"].startswith("@"):
            rel = f"avatars/ig-{norm(l['from'])}.jpg"
            if src and not (ROOT / "docs" / rel).exists():
                download_image(h, src, rel)
            if (ROOT / "docs" / rel).exists():
                l["avatar"] = rel
                used.add(rel)
    drop_placeholder_posters(events)
    for k in [k for k in cache if k not in {e["id"] for e in events}]:
        del cache[k]
    return used


def artist_aliases(events, cfg, state):
    """艺人别名：config 的 artist_aliases（手动），加上从新闻标题自动学到的。
    自动学：中英混合的艺名（例如 “DIOR 大穎”），新闻标题里以它的英文部分开头的较长英文字（“Diorlying”）就当别名。"""
    out = {k: list(v) for k, v in (cfg.get("artist_aliases") or {}).items()}
    news = state.get("news_verify_v3", {})
    for e in events:
        a = e.get("artist_name") or ""
        latin = re.findall(r"[A-Za-z]{3,}", a)
        if not latin or not re.search(r"[㐀-鿿]", a):
            continue
        for it in (news.get(a) or {}).get("top") or []:
            for w in re.findall(r"[A-Za-z]{5,}", it.get("title") or ""):
                if w.lower().startswith(latin[0].lower()) and w.lower() != latin[0].lower() and w not in out.setdefault(a, []):
                    out[a].append(w)
    return out


def img_size(path):
    """读图片宽高（PNG / JPEG / WebP / GIF 的档头，不用第三方套件）。读不到回传 None。"""
    try:
        b = Path(path).read_bytes()[:65536]
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        if b[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(b[6:8], "little"), int.from_bytes(b[8:10], "little")
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            if b[12:16] == b"VP8X":
                return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
            if b[12:16] == b"VP8 ":
                return int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
            if b[12:16] == b"VP8L":
                v = int.from_bytes(b[21:25], "little")
                return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
        if b[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(b):
                if b[i] != 0xFF:
                    i += 1
                    continue
                marker, seg = b[i + 1], int.from_bytes(b[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
                i += 2 + seg
    except Exception:
        pass
    return None


def drop_placeholder_posters(events):
    """平台没有海报时会放自己的 logo 当“海报”（例如 Fantopia）：同一张图出现在不同艺人的演出，就当作占位图，
    卡片改用艺人照片。"""
    owners = {}
    for e in events:
        p = e.get("poster_img")
        if p and (ROOT / "docs" / p).exists():
            e["_poster_hash"] = hashlib.sha1((ROOT / "docs" / p).read_bytes()).hexdigest()
            owners.setdefault(e["_poster_hash"], set()).add(norm(e.get("artist_name") or e["name"]))
    for e in events:
        hsh = e.pop("_poster_hash", None)
        if hsh and len(owners[hsh]) > 1:
            if e.get("avatar") == e["poster_img"]:
                e["avatar"], e["avatar_kind"] = None, None
            e["poster_img"] = None
        # 方形或直式海报适合卡片；横幅（宽比高多很多，裁切后看不清）改用艺人照片
        size = img_size(ROOT / "docs" / e["poster_img"]) if e.get("poster_img") else None
        e["poster_portrait"] = bool(size and size[1] >= size[0] * 0.9)


def lead_candidates(l):
    """从贴文猜可能的艺人名：#标签（#AlanTam → Alan Tam）、第一行冒号/书名号前的部分、连续大写开头的英文词。"""
    text = unicodedata.normalize("NFKC", f"{l.get('title', '')}\n{l.get('text') or ''}")
    first = text.split("\n")[0]
    cands = [re.split(r"\s*[:：《「(（|—]|\s[-–]\s", first)[0]]
    for tag in re.findall(r"#([^\s#]{3,40})", text)[:8]:
        tag = re.sub(r"(?i)(in)?(kl|kualalumpur|malaysia|my|live|concert|tour|worldtour|asiatour)$", "", tag)
        cands.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag))
    cands += re.findall(r"\b([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,2})\b", " ".join(text.split("\n")[:3]))
    # 中英混写的标题（“Alin要来马来西亚…”）：英文字紧贴中文时 \b 不成立，另外抓；以及“要来 / 宣布…”前面的中文名
    cands += re.findall(r"(?<![A-Za-z])([A-Za-z][A-Za-z'.-]{2,}(?:\s+[A-Za-z][A-Za-z'.-]+){0,2})(?![A-Za-z])", first)
    cands += re.findall(r"^([一-鿿]{2,4})(?:要|将|將|来|來|宣布|确定|確定|终于|終於|首度|再度|即将|即將)", first)
    # 图片上读出来的字：前几行常是艺人名（例如 “DIOR 大穎 2026 世界巡迴演唱會”）
    img = unicodedata.normalize("NFKC", l.get("image_text") or "").split("\n")[:4]
    for line in img:
        cands.append(re.split(r"\s*[:：《「(（|—]|\s[-–]\s", line)[0])
        cands += re.findall(r"[一-鿿]{2,4}", line)
        cands += re.findall(r"\b([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,2})\b", line)
    seen, out = set(), []
    for c in cands:
        c = re.sub(r"\s+", " ", TITLE_NOISE.sub(" ", c)).strip(" -–'\"!.,")
        if 2 <= len(norm(c)) <= 30 and norm(c) not in seen:
            seen.add(norm(c))
            out.append(c)
    return out[:12]


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
            nm = a.get("name", "").strip()
            # 单一英文字门槛高（避免普通单字）；带 - 或 . 的艺名（A-Lin、G.E.M.）不会是普通单字，门槛放低
            need = 20 if re.search(r"[^\x00-\x7f]", nm) else 300 if " " in nm or re.search(r"[A-Za-z][-.][A-Za-z]", nm) else 5000
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
    cache = state.setdefault("artist_lookup_v2", {})
    state.pop("artist_lookup", None)
    budget = [120]  # 每次扫描最多查 120 个新名字
    watch = cfg.get("watch_artists", [])
    by_event = {e["id"] for e in events}
    for l in leads:
        src = l.pop("image_src", None)
        for k in ("artist", "artist_photo", "artist_fans", "artist_region", "image"):  # 旧线索存在 state 里，每次按最新规则重新归类
            l.pop(k, None)
        pending = not any(m in by_event for m in l.get("matches", []))
        text = lead_blob(l)
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
            l["artist_fans"] = artist.get("fans") or (cache.get(norm(artist["name"])) or {}).get("fans") or 0
            l["artist_region"] = region_of(h, artist["name"], l.get("title"), state.setdefault("regions_v2", {}), MB_BUDGET)
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
                _, url, _ = deezer_artist(h, {"name": n})
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


def detect_platforms(text, cfg):
    """公告里提到的官方售票平台（网址或名字）。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return [p["name"] for p in cfg.get("ticket_platforms", []) if any(m in t for m in p["match"])]


def keyword_hit(keywords, text):
    """关键字比对：英文要整个词（避免 “Long Run” 被 run 命中），中文直接找。回传命中的字。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        if re.search(r"[^\x00-\x7f]", k):
            if k in t:
                return kw.strip()
        elif re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
            return kw.strip()
    return None


def apply_filters(events, cfg):
    """过滤管线：每场演出依序检查，第一个不通过的原因写进 hidden（网页会隐藏，但可以在“已过滤”里看到原因）。
      1. 是不是演唱会（脱口秀、体育、展览、音乐剧等排除；粉丝见面会、音乐节保留）
      2. 歌手类别：只看华语 / K-pop / 欧美；其他类别要够红（Deezer 粉丝数门槛）
    售票状态（未开票 / 已结束 / 售罄）在网页上按当下时间判断，不在这里过滤。"""
    f = cfg.get("filters", {})
    keep = set(f.get("keep_regions", ["华语", "K-pop", "欧美"]))
    min_fans = f.get("min_fans_other_regions", 1_000_000)
    overrides = f.get("region_overrides", {})
    for e in events:
        e["hidden"] = None
        text = f"{e['name']} {e.get('artist') or ''}"
        hit = next((r for k, r in overrides.items() if mentions(k, f"{text} {e.get('artist_name') or ''}")), None)
        if hit:
            e["region"] = hit  # 你手动指定的类别优先
        kw = keyword_hit(f.get("non_concert_keywords", []), text)
        typ = keyword_hit(f.get("non_concert_types", []), e.get("type") or "")
        if kw or typ:
            e["hidden"] = f"不是演唱会（{kw or e.get('type')}）"
        elif e.get("region") not in keep and (e.get("fans") or 0) < min_fans:
            e["hidden"] = f"歌手类别：{e.get('region') or '未知'}（只看{'、'.join(sorted(keep))}，其他要 {min_fans // 10000} 万粉丝以上）"


GENERIC_NAME = re.compile(r"(?i)世界|巡回|巡迴|巡演|演唱会|演唱會|音乐会|音樂會|个人|個人|演出|吉隆坡|马来西亚|馬來西亞|新加坡|"
                          r"\b(world|asia|tour|concert|live|in|the|kuala|lumpur|malaysia|singapore|fan ?meeting|fancon|showcase|kl|20\d\d)\b")


def generic_name(name):
    """“世界巡回演唱会”、“World Tour”这类只有通用字、没有艺人名的名字（平台标题没写艺人时会被当成艺人名）。"""
    return len(norm(GENERIC_NAME.sub(" ", name or ""))) < 2


def lead_countries(l, cfg):
    """线索提到你关注的哪几个国家（马来西亚 / 新加坡 / 韩国 / 泰国）；没提到就回传空（不限制）。"""
    if not cfg:
        return []
    blob = lead_blob(l)
    where = [k for k in PLACES if place_hit(blob, cfg, k)]
    # 内文没写国家（例如马来文“Tiket konsert BigBang licin…”）：用来源的国家（马来西亚媒体 / 账号 → 马来西亚）
    return where or ([l["country"]] if l.get("country") else [])


def filter_matches_by_country(leads, events, cfg):
    """线索说的是哪个国家，就只对上那个国家的场次（例如“大马站加场”不能套到同一艺人的泰国场）；
    也不对上艺人名只是通用字（世界巡回演唱会）的场次。"""
    ev_by_id = {e["id"]: e for e in events}
    for l in leads:
        where = lead_countries(l, cfg)
        l["matches"] = [m for m in l.get("matches", []) if m in ev_by_id
                        and not generic_name(ev_by_id[m].get("artist_name"))
                        and (not where or (ev_by_id[m].get("country") or "MY") in where)]


def merge_announcements(leads, events, cfg=None):
    """官方公告（IG / 你提交的）读到的未来开票时间，并入对应的演出——例如平台还没更新的“加场”开票。重复呼叫不会重复加。"""
    if cfg:
        filter_matches_by_country(leads, events, cfg)
    ev_by_id = {e["id"]: e for e in events}
    for l in leads:
        target = next((ev_by_id[m] for m in l.get("matches", []) if m in ev_by_id), None)
        if not target:
            continue
        added = re.search(r"(?i)加场|加場|added show|additional show|extra show|new show|another chance", lead_blob(l)) \
            or ADDED_RE.search(lead_blob(l))
        if added and not l.get("hidden") and (l.get("time") or "") >= (target.get("added_source") or {}).get("time", ""):
            # 加场的来源（IG / 本地媒体 / 你提交的线索）：“加场”区块和详细页用
            target["added_source"] = {"title": short(l.get("title") or "", 120), "url": l.get("url"), "time": (l.get("time") or "")[:10],
                                      "from": l.get("from")}
        posted = (l.get("time") or "")[:10]
        last_show = (target.get("dates") or ["9999"])[-1][:10]
        for st in l.get("sale_times") or []:
            if st["time"][:10] > last_show:
                continue  # 开售不可能在演出之后
            same_day = [s for s in target.get("sales") or [] if (s.get("start") or "")[:10] == st["time"][:10]]
            for s in same_day:  # 平台的轮次没有名字（只写“开售”）：用公告同一天那一轮的说法补上（例如 VIP 会员预售）
                if re.fullmatch(r"(开售|开票|開售|開票|sale|on sale)?", (s.get("name") or "").strip(), re.I) and not s.get("source_line"):
                    s["source_line"], s["kind"] = st.get("line"), st.get("kind")
            if any((s.get("start") or "")[:len(st["time"])] == st["time"] for s in same_day):
                continue  # 平台（或之前的公告）已经有同一个开票时间
            if same_day and len(st["time"]) > 10 and all(len(s.get("start") or "") == 10 for s in same_day):
                same_day[0]["start"] = st["time"]  # 之前只知道日期，这次有几点开：补上时间
                continue
            if same_day:
                continue
            # 这一轮是不是加场的：句子里提到加场，或整则公告在讲加场、而且开票日期在公告之后
            is_added = st.get("added") or (added and st["time"][:10] >= posted)
            kind = {"presale": "预售", "general": "公售"}.get(st.get("kind"), "开票")
            target.setdefault("sales", []).append({
                "name": f"{'加场 · ' if is_added else ''}{kind}（{l['from']}）", "start": st["time"], "end": None,
                "queue": None, "available": True, "code_required": False, "url": l.get("url"), "announced": True,
                "added": bool(is_added), "source_line": st.get("line"), "kind": st.get("kind")})
            target["sales_closed"] = False


def apply_lead_filters(leads, events, cfg):
    """线索（IG、新闻、你提交的）的过滤：hidden 写原因（网页的待确定、抢票日历、首页都按这个过滤）。
    1. 同一位艺人已经在售票平台上架 → 对应到那场演出（不在待确定重复出现）
    2. 不是演唱会 / 歌手类别不对 → 排除
    3. 贴文公布的开票日期已过、或贴文说已开卖 / 售罄 → 排除
    已公布未来开票时间的（announced）不隐藏：会出现在抢票日历；待确定只放 status = rumor 的。"""
    f = cfg.get("filters", {})
    keep = set(f.get("keep_regions", ["华语", "K-pop", "欧美"]))
    min_fans = f.get("min_fans_other_regions", 1_000_000)
    by_artist = {}
    for e in events:
        if len(norm(e.get("artist_name"))) >= 2 and not generic_name(e.get("artist_name")):
            by_artist.setdefault(norm(e["artist_name"]), []).append(e)
    ev_by_id = {e["id"]: e for e in events}
    for l in leads:
        l["hidden"] = None
        if l.get("artist") and norm(l["artist"]) in by_artist:  # 用艺人名对上已上架的演出
            for e in by_artist[norm(l["artist"])]:
                if e["id"] not in l["matches"]:
                    l["matches"].append(e["id"])
        linked = [ev_by_id[m] for m in l.get("matches", []) if m in ev_by_id]
        kw = keyword_hit(f.get("non_concert_keywords", []), f"{l.get('title', '')}\n{l.get('text') or ''}")
        if linked and all(e.get("hidden") for e in linked):
            l["hidden"] = f"对应的演出已被过滤（{linked[0]['hidden']}）"
        elif linked:
            l["hidden"] = None  # 已上架：显示在那场演出的“相关消息”，不在待确定
        elif kw:
            l["hidden"] = f"不是演唱会（{kw}）"
        elif not l.get("artist"):
            l["hidden"] = "认不出是哪位明星（待人工或 AI 分析）"
        elif keyword_hit(f.get("past_event_keywords", []), f"{l.get('title', '')}\n{l.get('text') or ''}"):
            l["hidden"] = "演出已经结束（回顾 / 感谢贴）"
        elif l.get("artist") and l.get("artist_region") not in keep and (l.get("artist_fans") or 0) < min_fans:
            l["hidden"] = f"歌手类别：{l.get('artist_region') or '未知'}（只看{'、'.join(sorted(keep))}）"
        elif l.get("status") == "expired":
            l["hidden"] = "公布的开票日期已过"
        elif l.get("status") == "onsale":
            l["hidden"] = "贴文说已经开卖或售罄"
        l["listed"] = bool(linked)
    return leads


# Google 新闻查证用的地名（查询词 + 标题要出现的字）
PLACES = {
    "MY": ('(Malaysia OR "Kuala Lumpur" OR KL OR 吉隆坡 OR 马来西亚 OR 馬來西亞 OR 大马 OR 大馬 OR "Bukit Jalil" OR Axiata)', "malaysia"),
    "SG": ('(Singapore OR 新加坡 OR 狮城 OR 獅城 OR "National Stadium" OR "Indoor Stadium")', "singapore"),
    "KR": ('(Korea OR Seoul OR 韩国 OR 韓國 OR 首尔 OR 首爾 OR 서울)', "korea"),
    "TH": ('(Thailand OR Bangkok OR 泰国 OR 泰國 OR 曼谷)', "thailand"),
}
CONCERT_Q = "(concert OR tour OR 演唱会 OR 演唱會 OR 巡演 OR 开唱 OR 開唱 OR konsert OR fanmeeting OR fancon)"
SALE_Q = "(tickets OR presale OR 开票 OR 開票 OR 开售 OR 開售 OR 售票 OR 抢票 OR 搶票 OR 加场 OR 加場 OR \"added show\" OR \"additional show\")"
ADDED_RE = re.compile(r"(?i)加场|加場|加开|加開|第二场|第二場|第三场|第三場|\badd(s|ed|ing)? (a )?(second|third|fourth|another|extra|new|more)\b|additional (show|date|concert|night)|extra (show|date|night)|second (show|night|concert)|(show|date)s? added|tambah (hari|tarikh|pertunjukan|show)|추가 공연|추가 회차")
LANGS = [("en-MY", "MY", "MY:en"), ("zh-TW", "TW", "TW:zh-Hant"), ("zh-HK", "HK", "HK:zh-Hant"), ("zh-CN", "CN", "CN:zh-Hans")]  # Google News 没有马来西亚/新加坡中文版（会被转去英文版），华文报导要用台湾、香港、中国版搜


def gnews(h, q, langs=LANGS):
    """Google News RSS 搜索（免费、不用 API key）；几种语言各搜一次，合并去重。"""
    items, seen = [], set()
    for hl, gl, ceid in langs:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid})
        try:
            root = ET.fromstring(h.request(url))
        except Exception as ex:
            print(f"  gnews {q[:40]}: {ex}", flush=True)
            continue
        finally:
            time.sleep(SLEEP)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not title or key in seen:
                continue
            seen.add(key)
            pub = it.findtext("pubDate")
            t = email.utils.parsedate_to_datetime(pub).astimezone(MYT).strftime("%Y-%m-%d %H:%M") if pub else None
            items.append({"title": title, "url": it.findtext("link"), "time": t})
    return items


def place_hit(title, cfg, country):
    t = f" {title.lower()} "
    return any(k in t for k in cfg["keywords"].get(PLACES.get(country, PLACES["MY"])[1], []))


def news_check(h, artist, country, cfg, today, sale=False, deep=False):
    """搜“艺人 + 演唱会(+开票) + 国家”，标题要同时提到艺人、演唱会、那个国家才算相关报导。
    deep：还查不到开票资料的演出，不限时间地搜（开票可能是一年前的事），找“开卖 / 售罄”的报导。"""
    place = PLACES.get(country, PLACES["MY"])[0]
    words = SALE_Q[:-1] + ' OR "sold out" OR 售罄 OR "tiket habis")' if deep else SALE_Q if sale else CONCERT_Q
    q = f'"{artist}" {words} {place}' + ("" if deep else f" when:{30 if sale else 90}d")
    items = []
    for it in gnews(h, q):
        if mentions(artist, it["title"]) and relevant(it["title"], cfg["keywords"], False) and place_hit(it["title"], cfg, country):
            items.append(dict(it, time=(it["time"] or "")[:10]))
    items.sort(key=lambda i: i["time"] or "", reverse=True)
    blob = "\n".join(i["title"] for i in items)
    sts = []
    for it in items:  # 每篇报导用它自己的发布日期推算年份
        for s in sale_times(it["title"], (it["time"] or today) + " 00:00"):
            if not s["past"] and s["time"] not in [x["time"] for x in sts]:
                sts.append(dict(s, url=it["url"], title=it["title"]))
    added = [i for i in items if ADDED_RE.search(i["title"])]  # 报导说要加场 / 加开新日期
    soldout = [i for i in items if SOLD_OUT_RE.search(i["title"]) or re.search(r"(?i)habis dijual|sell(s)? out|snapped up|licin", i["title"])]
    opened = [i for i in items if re.search(r"(?i)tickets? (now )?(on sale|go on sale|went on sale|available|released)|on sale now|"
                                            r"presale|ticket sales?|开票|開票|开售|開售|抢票|搶票|tiket", i["title"])]
    return {"checked": today, "country": country, "deep": deep,
            "soldout_news": min(soldout, key=lambda i: i["time"] or "9") if soldout else None,
            "opened_news": min(opened + soldout, key=lambda i: i["time"] or "9") if opened or soldout else None, "count": len(items), "top": items[:3],
            "platforms": detect_platforms(blob, cfg), "sale_times": sts,
            "added": bool(added), "added_news": added[0] if added else None}


def _search_budget(cfg, state):
    """网页搜索每月额度：还有就扣一次、回传 True。"""
    month = datetime.now(MYT).strftime("%Y-%m")
    use = state.setdefault("web_search_usage", {})
    for k in [k for k in use if k != month]:
        del use[k]
    if use.get(month, 0) >= (cfg.get("web_search") or {}).get("monthly_limit", 200):
        return False
    use[month] = use.get(month, 0) + 1
    return True


SEATMAP_RE = re.compile(r"(?i)\bstage\b|舞台|무대|panggung|seat ?map|seating plan|座位图|座位圖|좌석")
PRICE_TOKEN = re.compile(r"(?i)(?:RM|S\$|MYR|SGD|₩|฿|KRW|THB)\s?\d|\d[\d,]{2,}\s?(?:원|บาท)")


def looks_like_seatmap(text):
    """OCR 文字像不像座位图：提到舞台 / 座位图，而且有多个票价（或至少很多区号）。"""
    text = text or ""
    return bool(SEATMAP_RE.search(text)) and (len(PRICE_TOKEN.findall(text)) >= 2 or len(re.findall(r"\b\d{3}\b", text)) >= 6)


def extra_seatmaps(h, events, leads, cfg, state, limit=10):
    """售票平台没有座位图的演出（BookMyShow 有防火墙、NOL World 不提供等），从别的来源补：
    1. 对上这场演出的 IG 官方贴文图片：OCR 读到舞台 + 票价，就是座位图（主办 / 场馆常贴座位图和票价）
    2. Google 图片搜索（需要 SERPAPI_KEY；也支援 BRAVE_API_KEY）：只收高清图（长边 ≥ 1000 像素）、OCR 确认是座位图
    存在 docs/maps-extra/（跟平台官方座位图分开管理），每场只搜一次，结果存 state。"""
    import tempfile
    folder = ROOT / "docs" / "maps-extra"
    done = state.setdefault("seatmap_extra", {})
    need = [e for e in events if not e.get("hidden") and not e.get("seat_maps") and e.get("artist_name")]

    def keep(e, blob, source, text=None):
        digest = hashlib.sha1(blob).hexdigest()
        if any(v and v.get("hash") == digest and k != e["id"] for k, v in done.items()):
            return False  # 同一张图已经给了别场演出
        folder.mkdir(parents=True, exist_ok=True)
        ext = ".png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        rel = f"maps-extra/{re.sub(r'[^A-Za-z0-9_.-]', '_', e['id'])}{ext}"
        (ROOT / "docs" / rel).write_bytes(blob)
        done[e["id"]] = {"rel": rel, "source": source, "hash": digest, "text": (text or "")[:1500],
                         "tried": datetime.now(MYT).strftime("%Y-%m-%d")}
        return True

    # 之前找到的图：用现在的规则再检查一次（例如只凭场馆对上、其实是别场演出的座位图 → 删掉，3 天后重搜）
    by_id = {e["id"]: e for e in events}
    for k, v in list(done.items()):
        if not v or not v.get("rel") or k not in by_id:
            continue
        path = ROOT / "docs" / v["rel"]
        text = v.get("text")
        if text is None and path.exists():
            text = ocr_text(path)
            if text is None:
                continue  # 没装 OCR（本机测试），不动
        if not path.exists() or not seatmap_for_event(by_id[k], text):
            if path.exists():
                path.unlink()
            done[k] = {"rel": None, "tried": datetime.now(MYT).strftime("%Y-%m-%d")}
        else:
            v["text"] = text[:1500]

    # 1. IG 贴文图片：第一张已下载在 docs/posts/ 并 OCR 过；多图贴文的其他张（座位图常在后面）这里才下载来读
    ocr_cache = state.setdefault("ocr_ig_more", {})
    used_keys = set()
    for e in need:
        if (done.get(e["id"]) or {}).get("rel"):
            continue
        for l in leads:
            if l["kind"] != "IG" or e["id"] not in (l.get("matches") or []):
                continue
            src = f"IG {l['from']} 贴文（{(l.get('time') or '')[:10]}）"
            path = ROOT / "docs" / f"posts/{l['id']}.jpg"
            if seatmap_for_event(e, l.get("image_text")) and path.exists():
                if keep(e, path.read_bytes(), src, l.get("image_text")):
                    break
            hit = False
            for i, url in enumerate(l.get("more_images") or []):
                key = f"{l['id']}#{i + 1}"
                used_keys.add(key)
                if ocr_cache.get(key) is False:
                    continue  # 之前读过，不是座位图
                try:
                    with h.opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as resp:
                        blob = resp.read(8_000_001)
                except Exception:
                    continue
                with tempfile.TemporaryDirectory() as tmp:
                    tp = Path(tmp) / "img"
                    tp.write_bytes(blob)
                    text = ocr_text(tp)
                if text is None:
                    break  # 没装 OCR（本机测试）
                ocr_cache[key] = seatmap_for_event(e, text)
                if ocr_cache[key] and keep(e, blob, src + f" 第 {i + 2} 张图", text):
                    hit = True
                    break
            if hit:
                break
    for k in [k for k in ocr_cache if k not in used_keys]:
        del ocr_cache[k]

    # 2. Google 图片搜索
    serp, brave = os.environ.get("SERPAPI_KEY"), os.environ.get("BRAVE_API_KEY")
    searched = 0
    today = datetime.now(MYT).strftime("%Y-%m-%d")
    retry = (datetime.now(MYT) - timedelta(days=3)).strftime("%Y-%m-%d")
    for e in need:
        prev = done.get(e["id"]) or {}
        # 只搜马来西亚、新加坡；已经有座位图的不搜；没找到的 3 天后再试（座位图常在开票前后才公布）
        if prev.get("rel") or (prev.get("tried") or "") > retry or (e.get("country") or "MY") not in ("MY", "SG")                 or not (serp or brave) or not e.get("venue") or searched >= limit:
            continue
        if not _search_budget(cfg, state):
            break
        searched += 1
        done[e["id"]] = {"rel": None, "tried": today}
        year = (e.get("dates") or [""])[0][:4]
        q = f'"{e["artist_name"]}" {e["venue"]} {year} seating plan'
        cands = []
        try:
            if serp:
                r = h.json("https://serpapi.com/search.json?" + urllib.parse.urlencode(
                    {"engine": "google_images", "q": q, "gl": (e.get("country") or "MY").lower(), "api_key": serp}), retries=1)
                cands = [(x.get("original"), x.get("original_width") or 0, x.get("original_height") or 0, x.get("link"))
                         for x in r.get("images_results") or []]
            else:
                r = h.json("https://api.search.brave.com/res/v1/images/search?" + urllib.parse.urlencode({"q": q, "count": 30}),
                           headers={"X-Subscription-Token": brave, "Accept": "application/json"}, retries=1)
                cands = [((x.get("properties") or {}).get("url"), (x.get("properties") or {}).get("width") or 0,
                          (x.get("properties") or {}).get("height") or 0, x.get("url")) for x in r.get("results") or []]
        except Exception as ex:
            print(f"  seat map search {e['artist_name']}: {ex}", flush=True)
        for url, w, hgt, page in cands[:12]:
            if not url or (w and hgt and max(w, hgt) < 1000):
                continue  # 只要高清图
            try:
                with h.opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as resp:
                    blob = resp.read(8_000_001)
            except Exception:
                continue
            if len(blob) > 8_000_000:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "img"
                path.write_bytes(blob)
                size, text = img_size(path), ocr_text(path)
            if size and max(size) >= 1000 and seatmap_for_event(e, text):
                if keep(e, blob, f"Google 图片（{urllib.parse.urlparse(page or url).netloc}）", text):
                    break
        time.sleep(SLEEP)

    # 套用（每次扫描都套上之前找到的）并清掉已下架演出的图
    live = {e["id"] for e in events}
    for e in events:
        got = done.get(e["id"]) or {}
        if not e.get("seat_maps") and got.get("rel") and (ROOT / "docs" / got["rel"]).exists():
            e["seat_maps"] = [got["rel"]]
            e["seat_map_source"] = got["source"] + "，非售票平台官方图，请以官网为准"
    for k in [k for k in done if k not in live]:
        rel = (done.pop(k) or {}).get("rel")
        if rel and (ROOT / "docs" / rel).exists():
            (ROOT / "docs" / rel).unlink()


def seatmap_for_event(e, text):
    """图片是不是“这场演出”的座位图（同一个场馆有很多演出，只看场馆会抓错）：
    1. 像座位图（舞台 + 票价 / 区号）
    2. 图上要有艺人名（中英混合艺名的中文或英文部分也算）
    3. 图上写的年份要是这场演出的年份（例如 2024 年的旧座位图不收）"""
    if not looks_like_seatmap(text):
        return False
    t, low = norm(text), (text or "").lower()
    a = e.get("artist_name") or ""
    parts = [a] + re.findall(r"[㐀-鿿]{2,}", a) + [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'&.-]*(?:\s+[A-Za-z0-9'&.-]+)*", a)]
    named = any((len(norm(x)) >= 3 or re.fullmatch(r"[㐀-鿿]{2,}", x)) and norm(x) in t for x in parts if x and not generic_name(x))
    if not named:
        return False
    years = set(re.findall(r"\b(20\d\d)\b", text or ""))
    show_years = {d[:4] for d in e.get("dates") or []}
    return not years or not show_years or bool(years & show_years)


def web_search(h, artist, country, cfg, state):
    """一般网页搜索（Google News 只收新闻网站，XUAN、Facebook 专页等搜不到）。
    GitHub Secrets 有 SERPAPI_KEY 就用 SerpApi（真的 Google 结果），有 BRAVE_API_KEY 就用 Brave；都没有就跳过。
    每月有上限（config 的 web_search.monthly_limit），免得超出免费额度。回传 [{title, snippet, url, time}]。"""
    serp, brave = os.environ.get("SERPAPI_KEY"), os.environ.get("BRAVE_API_KEY")
    if not serp and not brave:
        return []
    if not _search_budget(cfg, state):
        print("  web search: 本月额度已用完", flush=True)
        return []
    # 一次搜你关注的四个国家（马来西亚、新加坡、韩国、泰国），每个结果再判断提到哪个国家
    q = (f'"{artist}" (马来西亚 OR 大马 OR 来马 OR Malaysia OR 新加坡 OR Singapore OR 韩国 OR Korea OR 泰国 OR Thailand) '
         f'(演唱会 OR 演唱會 OR 开唱 OR 開唱 OR 巡演 OR concert OR tour)')
    out = []
    try:
        if serp:
            r = h.json("https://serpapi.com/search.json?" + urllib.parse.urlencode(
                {"engine": "google", "q": q, "gl": country.lower(), "hl": "zh-cn", "num": 20, "api_key": serp}), retries=1)
            for x in r.get("organic_results") or []:
                out.append({"title": x.get("title") or "", "snippet": x.get("snippet") or "", "url": x.get("link"), "time": x.get("date")})
        else:
            r = h.json("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "country": country, "count": 20}),
                       headers={"X-Subscription-Token": brave, "Accept": "application/json"}, retries=1)
            for x in (r.get("web") or {}).get("results") or []:
                out.append({"title": x.get("title") or "", "snippet": text_of(x.get("description") or ""), "url": x.get("url"), "time": x.get("age")})
    except Exception as ex:
        print(f"  web search {artist}: {ex}", flush=True)
    return out


def add_web_results(c, results, artist, country, cfg):
    """网页搜索结果：标题或摘要要同时提到艺人、演唱会、那个国家才算相关，并入查证结果。"""
    hits, countries = [], []
    for x in results:
        blob = f"{x['title']}\n{x['snippet']}"
        where = [k for k in PLACES if place_hit(blob, cfg, k)]  # 提到你关注的哪几个国家
        if mentions(artist, blob) and relevant(blob, cfg["keywords"], False) and where:
            hits.append({"title": x["title"], "url": x["url"], "time": (x.get("time") or "")[:20], "web": True,
                         "countries": where})
            countries += where
    if not hits:
        return
    c["web_count"] = len(hits)
    c["countries"] = list(dict.fromkeys((c.get("countries") or []) + countries))
    c["count"] = c.get("count", 0) + sum(1 for x in hits if country in x["countries"])
    hits.sort(key=lambda x: country not in x["countries"])  # 同一个国家的排前面
    c["top"] = (c.get("top") or []) + hits[:3]
    blob = "\n".join(f"{x['title']}\n{x['snippet']}" for x in results)
    c["platforms"] = list(dict.fromkeys((c.get("platforms") or []) + detect_platforms(blob, cfg)))


def news_verify(h, leads, events, cfg, state, today, limit=45, watch=()):
    """Google 通道：
    1. 待确定的明星 → 查有没有马来西亚（或新加坡）的相关报导、提到哪个售票平台
    2. 已上架、还没结束的演出 → 查新闻有没有公布开票时间 / 加场（平台还没更新时先知道）
    每位艺人每天查一次（结果存 state）。回传 {艺人: 查证结果}。"""
    cache = state.setdefault("news_verify_v3", {})
    state.pop("news_verify", None)
    todo = []  # (key, artist, country, sale)
    for l in leads:
        if not l.get("hidden") and not l.get("listed") and l.get("status") == "rumor" and l.get("artist"):
            todo.append((l["artist"], l["artist"], l.get("country") or "MY", False))
    now_s = today
    for e in sorted(events, key=lambda e: -(e.get("fans") or 0)):
        if e.get("hidden") or e.get("sold_out") or (e.get("country") or "MY") not in ("MY", "SG") or not e.get("artist_name"):
            continue
        if not e["dates"] or e["dates"][-1][:10] < now_s:
            continue
        todo.append((e["artist_name"], e["artist_name"], e.get("country") or "MY", True))
    deep = {e["artist_name"] for e in events if e["id"] in watch and e.get("artist_name")}
    out, n = {}, 0
    for key, artist, country, sale in dict.fromkeys(todo):
        if key in out:
            continue
        c = cache.get(key)
        want_deep = sale and key in deep
        if not c or c.get("checked") != today or c.get("country") != country or want_deep and not c.get("deep"):
            if n >= limit:  # 这次额度用完，剩下的下次再查
                if c:
                    out[key] = c
                continue
            n += 1
            c = cache[key] = news_check(h, artist, country, cfg, today, sale, deep=want_deep)
            if not sale or want_deep:  # 待确定的明星、监控名单的演出：再用一般网页搜索（XUAN、FB 等 Google News 没收录的）
                add_web_results(c, web_search(h, artist, country, cfg, state), artist, country, cfg)
        out[key] = c
    for k in [k for k in cache if k not in out]:
        del cache[k]
    print(f"  Google 新闻查证 {len(out)} 位艺人（今天新查 {n} 位）", flush=True)
    return out


def track_added(events, state):
    """加场状态：
    confirmed 已加场 = 售票平台上的场次比第一次看到时多了，或新闻说加场、平台也已经有两场以上
    rumor 传出加场 = 只有新闻说加场，平台还只有一场"""
    seen = state.setdefault("dates_first_seen", {})
    for e in events:
        n = len(e.get("dates") or [])
        first = seen.setdefault(e["id"], n)
        grew = n > first
        # 加场来源：Google 新闻，或 IG / 本地媒体 RSS / 你提交的线索（已对上这场演出的）
        e["news_added"] = e.get("news_added") or e.get("added_source")
        # 加场公布之后才开卖的轮次，算加场的票（平台的轮次名字不一定写“加场”）
        since = (e.get("news_added") or {}).get("time") or ""
        if since:
            for s in e.get("sales") or []:
                if (s.get("start") or "")[:10] >= since[:10]:
                    s["added"] = True
        if e.get("news_added") and n >= 2 or grew:
            e["added_status"] = "confirmed"
        elif e.get("news_added"):
            e["added_status"] = "rumor"
        else:
            e["added_status"] = None
    live = {e["id"] for e in events}
    for k in [k for k in seen if k not in live]:
        del seen[k]


def merge_news_sales(events, checks):
    """Google 新闻报导里读到的未来开票时间（例如加场），平台还没有的，就并进那场演出。"""
    for e in events:
        c = checks.get(e.get("artist_name") or "")
        # 查证是按国家搜的（例如“BIGBANG + 马来西亚”），不能套到同一艺人在别的国家的场次
        if not c or e.get("hidden") or c.get("country", "MY") != (e.get("country") or "MY"):
            e["news_added"] = None
            continue
        # 新闻说有加场（最近两星期的报导），平台还没上架新场次 → 卡片和首页先提示
        a = c.get("added_news")
        e["news_added"] = a if a and (a.get("time") or "") >= (datetime.now(MYT) - timedelta(days=14)).strftime("%Y-%m-%d") else None
        last_show = (e["dates"] or ["9999"])[-1][:10]
        for st in c.get("sale_times") or []:
            if st["time"][:10] > last_show:
                continue  # 开票日期在最后一场演出之后，不合理
            if any((s.get("start") or "")[:len(st["time"])] == st["time"] for s in e.get("sales") or []):
                continue
            e.setdefault("sales", []).append({
                "name": f"{'加场开票' if c.get('added') else '开票'}（Google 新闻报导）", "start": st["time"], "end": None,
                "queue": None, "available": True, "code_required": False, "url": st.get("url"), "announced": True, "news": True})
            e["sales_closed"] = False


SOURCE_RANK = {"GoLive": 0, "Fantopia": 1, "BookMyShow": 2, "Ticket2U": 3}


def merge_events(events):
    """同一场演出常同时在几个平台卖：同一天、同一位艺人就合并成一张卡，列出所有售票平台。
    回传合并后的演出，以及 旧 id → 合并后 id 的对照。"""
    groups, alone = {}, []
    for e in events:
        n = norm(e.get("artist_name"))
        if e["dates"] and len(n) >= 2:
            groups.setdefault((e["dates"][0][:10], n), []).append(e)
        else:
            alone.append([e])
    out, idmap = [], {}
    for g in list(groups.values()) + alone:
        # 资料最完整的当主卡（有票价、座位图、售票轮次的优先）
        g.sort(key=lambda e: (-(len(e.get("tiers") or []) + len(e.get("seat_maps") or []) + len(e.get("sales") or [])),
                              SOURCE_RANK.get(e["source"], 9)))
        p = dict(g[0])
        p["platforms"] = [{"source": e["source"], "url": e["url"], "id": e["id"], "sold_out": e.get("sold_out"),
                           "price_from": e.get("price_from")} for e in g]
        if len(g) > 1:
            p["sales"] = [dict(s, name=f"{e['source']} · {s.get('name') or '开售'}") for e in g for s in e.get("sales") or []]
            p["dates"] = sorted({d for e in g for d in e["dates"]})
            p["seat_maps"] = list(dict.fromkeys(m for e in g for m in e.get("seat_maps") or []))
            p["tiers"] = next((e["tiers"] for e in g if e.get("tiers")), [])
            p["ocr_tiers"] = next((e["ocr_tiers"] for e in g if e.get("ocr_tiers")), None)
            # 几个平台里，优先用方形 / 直式的海报
            best = next((e for e in g if e.get("poster_img") and e.get("poster_portrait")), None) \
                or next((e for e in g if e.get("poster_img")), None)
            p["poster_img"], p["poster_portrait"] = (best["poster_img"], best.get("poster_portrait")) if best else (None, False)
            p["limit"] = p.get("limit") or next((e.get("limit") for e in g if e.get("limit")), None)
            p["sold_out"] = all(e.get("sold_out") for e in g)
            p["stop_sales"] = all(e.get("stop_sales") or e.get("sold_out") for e in g)
            p["sales_closed"] = all(e.get("sales_closed") for e in g)  # 所有平台都没在卖才算结束
            p["first_seen"] = min(e.get("first_seen") or "9999" for e in g)
            p["fans"] = max(e.get("fans") or 0 for e in g)
            p["official_text"] = "\n".join(e["official_text"] for e in g if e.get("official_text")) or None
            p["tickets_created"] = min((e["tickets_created"] for e in g if e.get("tickets_created")), default=None)
            p["organizers"] = list(dict.fromkeys(o for e in g for o in e.get("organizers") or []))
            p["updates"] = {k: v for e in g for k, v in (e.get("updates") or {}).items()}
            if p.get("avatar_kind") != "artist":
                best = next((e for e in g if e.get("avatar_kind") == "artist"), None)
                if best:
                    p["avatar"], p["avatar_kind"] = best["avatar"], "artist"
        for e in g:
            idmap[e["id"]] = p["id"]
        out.append(p)
    out.sort(key=lambda e: (e["dates"][0] if e["dates"] else "9999", e["name"]))
    return out, idmap


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

    for name, fn in (("GoLive", golive), ("Fantopia", fantopia), ("BookMyShow", bookmyshow), ("Ticket2U", ticket2u),
                     ("NOL World", nolworld)):
        t0 = time.time()
        try:
            got = fn(h)
            health[name] = {"ok": True, "count": len(got)}
        except Exception as e:
            got = []
            health[name] = {"ok": False, "error": short(str(e), 160)}
        raw = state.setdefault("raw_events", {})
        local_path = ROOT / "state" / "local" / f"{name.lower()}.json"
        if not got and local_path.exists():  # GitHub 被挡时，改用你电脑抓的（36 小时内）
            loc = json.loads(local_path.read_text(encoding="utf-8"))
            age = now - datetime.strptime(loc.get("fetched_at", "2000-01-01 00:00"), "%Y-%m-%d %H:%M").replace(tzinfo=MYT)
            if age < timedelta(hours=36) and loc.get("events"):
                got = loc["events"]
                health[name] = {"ok": True, "count": len(got), "local": loc["fetched_at"]}
        if got:
            raw[name] = json.loads(json.dumps(got))  # 存下各平台的原始资料（合并前的副本），下次被挡时沿用
        elif raw.get(name):  # 抓不到就沿用上一次的资料，不让页面突然变空
            got = [dict(e) for e in raw[name]]
            health[name].update(stale=True, count=len(got))
        for e in got:
            e.setdefault("country", "MY")  # 马来西亚平台的场次没有国家栏位
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
    official_images(h, events, state)  # 官方来源优先：海报、票务说明图上的开票时间 / SOLD OUT
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
    fresh += manual_leads(h, health, state)
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
    ocr_leads(h, leads, state)  # 贴文图片上的字（艺人名、加场、平台常只印在图上）
    for l in leads:
        blob = lead_blob(l).lower()
        l["matches"] = [eid for eid, ns in names.items() if any(n in blob for n in ns)][:5]
        if l["kind"] in ("IG", "人工"):  # 旧线索也用最新规则重读开售时间
            # 开票时间来源：贴文文字 + 图片上 OCR 读出来的字（例如只印在海报上的加场开票日）
            found = sale_times(l.get("text") or "", l.get("time"))
            for s in sale_times(l.get("image_text") or "", l.get("time")):
                if all(s["time"] != x["time"] for x in found):
                    found.append(dict(s, line="（图片）" + s["line"]))
            l["sale_times"] = [s for s in found if not s["past"]]
        else:
            found, l["sale_times"] = [], []
        # 状态：rumor = 只说要来、还没售票细节（待确定）；announced = 已公布未来开售时间（放抢票日历）；
        # expired = 公布的开售日期都已经过了（不显示）
        # onsale = 贴文说已经在卖/售罄（也不算待确定）
        live_now = ONSALE_NOW.search(unicodedata.normalize("NFKC", f"{l.get('title', '')}\n{l.get('text') or ''}"))
        l["status"] = "announced" if l["sale_times"] else "expired" if found else "onsale" if live_now else "rumor"
    used = add_avatars(h, events, leads, state, today)
    # 同一场演出在几个平台卖 → 合并成一张卡；线索里对应的演出 id 也跟着换
    events, idmap = merge_events(events)
    apply_filters(events, cfg)
    for l in leads:
        l["matches"] = list(dict.fromkeys(idmap.get(m, m) for m in l.get("matches", [])))
        l["platforms"] = detect_platforms(lead_blob(l), cfg)
    # 用 Deezer 确认过的艺人名（和别名）再对一次线索（例如贴文写 “Siti Nurhaliza”，演出名称很长）
    aliases = artist_aliases(events, cfg, state)
    for l in leads:
        raw = unicodedata.normalize("NFKC", lead_blob(l)).lower()
        blob = norm(raw)
        for e in events:
            if e["id"] in l["matches"] or generic_name(e.get("artist_name")):
                continue
            a = e.get("artist_name") or ""
            # 中英混合艺名（“DIOR 大穎”）：报导常只写中文部分，中文部分（2 个字以上）也拿来对
            cjk = "".join(re.findall(r"[㐀-鿿]+", a)) if re.search(r"[A-Za-z]", a) else ""
            for name in [a] + aliases.get(a, []) + ([cjk] if len(cjk) >= 2 and not generic_name(cjk) else []):
                n = norm(name)
                zh = bool(re.fullmatch(r"[㐀-鿿]+", n))
                if len(n) < (2 if zh else 3):
                    continue
                # 短名字（FKJ、BTS）要整个词出现才算，避免撞到别的字；中文名直接找
                if n in blob if zh or len(n) >= 4 else re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", raw):
                    l["matches"].append(e["id"])
                    break
    filter_matches_by_country(leads, events, cfg)
    enrich_articles(h, leads, state)  # 对上演出的新闻读正文：预售、公售、加场的开票时间
    merge_announcements(leads, events, cfg)
    more, watch_out = group_pending(h, events, leads, state, cfg)
    used |= more
    cleanup_images(used)
    apply_lead_filters(leads, events, cfg)
    merge_announcements(leads, events, cfg)  # 用艺人名新对上的公告，开票时间也并进去
    watch = set(state.get("watchlist", {}))
    checks = news_verify(h, leads, events, cfg, state, today, watch=watch)  # Google 通道：查证待确定的明星、已上架演出的开票 / 加场
    merge_news_sales(events, checks)
    track_added(events, state)  # 加场：传出加场（只有新闻）/ 已加场（平台已上架新场次）
    sale_status(events, checks, now)  # 售票状态机（附证据）
    extra_seatmaps(h, events, leads, cfg, state)  # 平台没有座位图的：IG 贴文 → Google 图片（高清 + OCR 确认）
    add_ocr_tiers(events, state)  # 座位图上印的票价（包括补来的座位图）
    state_changes = update_watchlist(events, state, now)
    for l in leads:  # 只来自新闻、又查证不到马来西亚相关报导的：可信度太低，不放进待确定
        if not l.get("hidden") and not l.get("listed") and l["kind"] == "新闻" and checks.get(l.get("artist"), {}).get("count", 1) == 0:
            l["hidden"] = "Google 新闻查证不到马来西亚的相关报导"

    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "health": health,
        "ig_bad": state.get("ig_bad", []),
        "ig_accounts": cfg["ig_accounts"],
        "hashtags": cfg["hashtags"],
        "watch_artists": watch_out,
        "news_checks": checks,
        "platforms": cfg.get("ticket_platforms", []),
        "promoters": cfg.get("promoters", []),
        "events": events,
        "leads": leads,
    }
    data_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_alert(new_events, new_leads, events, changed, state_changes)
    print(f"events={len(events)} leads={len(leads)} new_events={len(new_events)} new_leads={len(new_leads)}")


STATE_LABEL = {"TICKET_INFO_PENDING": "等待开票资料", "PRESALE_ANNOUNCED": "已公布预售", "GENERAL_SALE_ANNOUNCED": "已公布公售",
               "ON_SALE": "已开票", "SOLD_OUT": "售罄", "SALE_CLOSED": "平台已停售"}


def write_alert(new_events, new_leads, events, changed, state_changes=()):
    """有新演出、票价/座位图更新、新的 IG/人工线索时写 data/alert.md，由 workflow 开 Issue 通知。"""
    path = ROOT / "data" / "alert.md"
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        path.unlink()
    state_changes = [(e, o) for e, o in state_changes if not e.get("hidden")]
    if not new_events and not new_leads and not changed and not state_changes:
        return
    by_id = {e["id"]: e for e in events}
    lines = []
    if state_changes:
        lines.append("## 🔄 售票状态更新\n")
        for e, old in state_changes:
            lines.append(f"- **{e['name']}**：{STATE_LABEL.get(old, old)} → **{STATE_LABEL.get(e['sale_state'], e['sale_state'])}** · [{e['source']}]({e['url']})")
        lines.append("")
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
