// 各页面共用：载入资料、导航栏、时间格式、分类规则、头像
const REPO = "4hYang3074/Malaysia-Concert-Radar";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https?:\/\//i.test(u || "") ? u : "#";
const localUrl = u => /^(maps|avatars|posts)\/[\w./-]+$/.test(u || "") ? u : "";   // 图片只允许仓库里的本地路径
// 资料里的时间都是马来西亚时间；有些来源（例如韩国）只有日期 YYYY-MM-DD
const toDate = s => s ? new Date((s.length === 10 ? s + " 00:00" : s).replace(" ", "T") + ":00+08:00") : null;
const WD = "日一二三四五六";
function fmt(s, withTime = true) {
  const d = toDate(s);
  if (!d) return "";
  const m = new Date(d.getTime() + 8 * 3600e3);
  const y = m.getUTCFullYear() !== new Date().getFullYear() ? `${m.getUTCFullYear()}年` : "";  // 不是今年的加上年份
  return `${y}${m.getUTCMonth() + 1}月${m.getUTCDate()}日（周${WD[m.getUTCDay()]}）` + (withTime && s.length > 10 ? " " + s.slice(11) : "");
}
// ---------- 国家 ----------
const COUNTRIES = {MY: "🇲🇾 马来西亚", SG: "🇸🇬 新加坡", KR: "🇰🇷 韩国", TH: "🇹🇭 泰国"};
const flag = c => (COUNTRIES[c] || "").split(" ")[0];
// 目前选的国家（网址 ?c=MY 优先，其次记在浏览器里；ALL = 全部）
function currentCountry() {
  const q = new URLSearchParams(location.search).get("c");
  if (q && (q === "ALL" || COUNTRIES[q])) { try { localStorage.setItem("country", q); } catch (e) {} return q; }
  try { return localStorage.getItem("country") || "ALL"; } catch (e) { return "ALL"; }
}
// 歌星：只看华语、欧美、K-pop；其他（马来、印尼、印度、泰国等）要真的很红（Deezer 100 万粉丝以上）才列
const HOT_REGIONS = ["华语", "K-pop", "欧美"];
// 过滤结果以扫描程序的 hidden（附原因）为准；旧资料没有 hidden 时才用这里的规则
const isWanted = e => "hidden" in e ? !e.hidden : (HOT_REGIONS.includes(e.region) || (e.fans || 0) >= 1e6);
function byCountry(d, c = currentCountry()) {
  const inCountry = e => c === "ALL" || (e.country || "MY") === c;
  const events = d.events.filter(e => isWanted(e) && inCountry(e));
  const filtered = d.events.filter(e => !isWanted(e) && inCountry(e));  // 被过滤的，附原因，可以在“已确定”底部查看
  if (c === "ALL") return {...d, events, filtered, byId: Object.fromEntries(events.map(e => [e.id, e]))};
  const ids = new Set(events.map(e => e.id));
  // 线索只收录提到马来西亚/新加坡/韩国的；没有标国家的当作马来西亚
  const leads = d.leads.filter(l => (l.country || "MY") === c);
  return {...d, events, filtered, leads, byId: Object.fromEntries(events.map(e => [e.id, e])),
    pending: d.pending.filter(l => (l.country || "MY") === c), leadsByEvent: d.leadsByEvent, _ids: ids};
}
function countryBar() {
  const c = currentCountry(), p = new URLSearchParams(location.search);
  const link = k => { p.set("c", k); return location.pathname.split("/").pop() + "?" + p; };
  return `<div class="cbar">${[["ALL", "🌏 全部"], ...Object.entries(COUNTRIES)].map(([k, v]) =>
    `<a class="fbtn ${k === c ? "on" : ""}" href="${esc(link(k))}">${v}</a>`).join("")}</div>`;
}
function countdown(target, now = new Date()) {
  const ms = target - now;
  if (ms <= 0) return "";
  const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60;
  return d ? `还有 ${d} 天 ${h} 小时` : h ? `还有 ${h} 小时 ${mm} 分` : `还有 ${mm} 分钟`;
}
const qs = k => new URLSearchParams(location.search).get(k);

// ---------- 分类：按售票时间 ----------
// 分类规则（综合所有平台和官方公告验证）：
//   soon    = 任何平台或官方公告有未来的开票时间（或开票不到半天：正在抢票）
//   soldout = 平台标示售罄
//   opened  = 开票时间都已过半天 / 平台已撤下售票轮次 / 平台已在卖但没公布开票时间
//   tba     = 平台上架了，但完全没有售票资讯（不会出现在首页）
const CATS = [
  {key: "soon", icon: "🔥", label: "即将开票", desc: "开票时间还没到，或刚开票不到半天（正在抢票）。按开票时间排序。"},
  {key: "opened", icon: "✔️", label: "已开票", desc: "开票时间已经过了半天以上（抢票已结束）。按演出日期排序。"},
  {key: "tba", icon: "❔", label: "等待开票资料", desc: "平台上有这场演出，但还没有任何开票资料。已加入监控名单，每次扫描都会重新查官方来源和新闻。"},
  {key: "soldout", icon: "🔴", label: "已售罄", desc: "官方显示售罄或停止售票，可以留意加场或释票。"},
];
const LIVE_MS = 12 * 3600e3;  // 开票后半天（12 小时）内算“正在抢票”，之后就当这一轮已经结束

// 还没开卖、或开卖不到一天的轮次（最近的一轮）
function nextSale(e, now) {
  return (e.sales || []).filter(s => s.start && toDate(s.start) > now - LIVE_MS).sort((a, b) => a.start.localeCompare(b.start))[0];
}
function lastStart(e) {
  return (e.sales || []).filter(s => s.start).map(s => s.start).sort().pop() || e.opened_at;
}
// 售票状态机：ANNOUNCED → TICKET_INFO_PENDING → PRESALE_ANNOUNCED → GENERAL_SALE_ANNOUNCED → ON_SALE → SOLD_OUT
const SALE_STATES = [
  {key: "ANNOUNCED", label: "已官宣"},
  {key: "TICKET_INFO_PENDING", label: "等待开票资料"},
  {key: "PRESALE_ANNOUNCED", label: "已公布预售"},
  {key: "GENERAL_SALE_ANNOUNCED", label: "已公布公售"},
  {key: "ON_SALE", label: "已开票"},
  {key: "SOLD_OUT", label: "售罄"},
];
function saleStateHTML(e) {
  const st = e.sale_state || "TICKET_INFO_PENDING";
  const closed = st === "SALE_CLOSED";
  const idx = closed ? 4 : SALE_STATES.findIndex(s => s.key === st);
  return `<div class="states">${SALE_STATES.map((s, i) => `<span class="st ${i < idx ? "done" : i === idx ? "cur" : ""}">${i === idx && closed ? "平台已停售" : s.label}</span>`).join('<span class="arr">›</span>')}</div>`;
}
const ADDED_LABEL = {confirmed: "➕ 已加场", rumor: "📢 传出加场"};
// 加场：同一位艺人只列一张卡（优先已加场、马来西亚），其他地区的放在详细页
function addedGroups(d) {
  const by = {};
  for (const e of d.events) if (e.added_status) (by[(e.artist_name || e.name).toLowerCase()] ||= []).push(e);
  const rank = e => (e.added_status === "confirmed" ? 0 : 2) + ((e.country || "MY") === "MY" ? 0 : 1);
  return Object.values(by).map(list => list.sort((a, b) => rank(a) - rank(b)));
}
function openedText(e) {
  if (!e.opened_at) return "";
  return e.opened_approx ? `约 ${fmt(e.opened_at.slice(0, 10) + " 00:00", false)} 开票（推断）` : `${fmt(e.opened_at)} 开票`;
}
function category(e, now = new Date()) {
  if (e.sold_out || e.stop_sales || e.sale_state === "SOLD_OUT") return "soldout";
  if (nextSale(e, now)) return "soon";
  if (lastStart(e) || e.sales_closed) return "opened";
  // Ticket2U、BookMyShow 没有公布开卖时间，但已经上架在卖
  if (platformsOf(e).some(p => p.source === "Ticket2U" || p.source === "BookMyShow")) return "opened";
  return "tba";
}
function status(e, now = new Date()) {
  const c = category(e, now);
  if (c === "soldout") return {cls: "bad", text: e.sold_out || e.sale_state === "SOLD_OUT" ? "已售罄" : "已停止售票"};
  if (c === "soon") {
    const s = nextSale(e, now), t = toDate(s.start), code = s.code_required ? " · 需要预售码" : "";
    if (t <= now) return {cls: "bad", text: `🔥 正在抢票：${saleRound(s).label} ${fmt(s.start)} 开卖${code}`, short: `🔥 正在抢票（${fmt(s.start)} 开卖）`};
    return {cls: "warn", text: `${saleRound(s).label}：${fmt(s.start)}（${countdown(t, now)}）${code}`, short: `${fmt(s.start)} 开卖 · ${countdown(t, now)}`};
  }
  if (c === "opened") {
    const last = lastStart(e);
    return {cls: "sub", text: last ? `已开票（${fmt(last)}）· 抢票已结束，余票请到官网确认` : "平台已结束售票或已在卖 · 余票请到官网确认",
      short: last ? `✔️ ${fmt(last)} 已开票` : "✔️ 已开票"};
  }
  return {cls: "sub", text: "平台没有任何开票资讯", short: "❔ 售票资讯不明"};
}
function sortFor(cat, list, now = new Date()) {
  const first = e => e.dates[0] || "9999";
  if (cat === "soon") return list.sort((a, b) => nextSale(a, now).start.localeCompare(nextSale(b, now).start));
  return list.sort((a, b) => first(a).localeCompare(first(b)));
}

// ---------- 抢票日历：售票平台公布的 + 官方 IG/线索里读出来的开售时间 ----------
function saleCalendar(d, now = new Date(), days = 60) {
  const until = now.getTime() + days * 864e5, items = [];
  const t0 = s => toDate(s.length === 10 ? s + " 00:00" : s);
  for (const e of d.events) for (const s of e.sales || []) {
    // 只放开票时间（不列排队开放、结束时间）
    const t = s.start && toDate(s.start);
    if (!t || t < now - LIVE_MS || t > until) continue;
    items.push({t, s: s.start, confirmed: true, country: e.country || "MY", href: `event.html?id=${encodeURIComponent(e.id)}`,
      name: e.artist_name, avatar: e.avatar, kind: e.avatar_kind,
      label: (s.name || "开票") + (s.code_required ? " · 需要预售码" : ""), src: e.source});
  }
  for (const l of d.leads) for (const st of (l.hidden ? [] : l.sale_times || [])) {  // 被过滤的线索不进日历
    const t = t0(st.time);
    if (!t || t < now - LIVE_MS || t > until) continue;
    const ev = (l.matches || []).map(m => d.byId[m]).find(Boolean);
    // 同一场演出、平台已经公布同一天的开售时间，就不重复列
    if (ev && items.some(i => i.confirmed && i.href.endsWith(encodeURIComponent(ev.id)) && i.s.slice(0, 10) === st.time.slice(0, 10))) continue;
    items.push({t, s: st.time, confirmed: false, href: ev ? `event.html?id=${encodeURIComponent(ev.id)}` : `lead.html?id=${encodeURIComponent(l.id)}`,
      name: ev ? ev.artist_name : l.artist || l.title, avatar: ev ? ev.avatar : l.artist_photo || l.avatar, kind: ev ? ev.avatar_kind : l.artist_photo ? "artist" : "",
      label: st.line, src: l.from});
  }
  const seen = new Set();
  return items.sort((a, b) => a.t - b.t).filter(i => {
    const k = i.href + i.s + i.label;
    return seen.has(k) ? false : seen.add(k);
  });
}
function calendarHTML(items, now = new Date()) {
  if (!items.length) return `<div class="card empty">目前还没有已公布的开售时间。官方一公布（售票平台或 IG 公告），就会出现在这里并通知你。</div>`;
  const byDay = {};
  for (const i of items) (byDay[i.s.slice(0, 10)] ||= []).push(i);
  return Object.entries(byDay).map(([day, list]) => {
    const dd = toDate(day + " 00:00"), diff = Math.round((dd - toDate(new Date(now.getTime() + 8 * 3600e3).toISOString().slice(0, 10) + " 00:00")) / 864e5);
    const rel = diff < 0 ? "昨天" : diff === 0 ? "今天" : diff === 1 ? "明天" : diff === 2 ? "后天" : `${diff} 天后`;
    return `<div class="day"><div class="dayhead"><b>${esc(fmt(day + " 00:00", false))}</b><span class="rel ${diff <= 1 ? "hot" : ""}">${rel}</span></div>
      ${list.map(i => `<a class="row cal" href="${i.href}">
        <div class="time">${i.s.length > 10 ? esc(i.s.slice(11)) : "时间<br>未定"}</div>
        ${avatar(i.avatar, i.name, 48, i.kind)}
        <div class="body">
          <div class="name" style="font-size:15.5px">${i.country ? flag(i.country) + " " : ""}${esc(i.name)}</div>
          <div class="ev">${esc(i.label)}</div>
          <span class="pill ${i.confirmed ? "ok" : "warn"}">${i.confirmed ? "✅ " + esc(i.src) + " 已公布" : "🕒 " + esc(i.src) + " 公告 · 待平台确认"}</span>
          ${i.t > now ? `<span class="pill sub">${esc(countdown(i.t, now))}</span>` : `<span class="pill bad">🔥 正在抢票</span>`}
        </div>
      </a>`).join("")}</div>`;
  }).join("");
}

// ---------- 🌟 当红歌星动态（首页 Highlights） ----------
// 用 Deezer 粉丝数判断当红程度；只列有“新动态”的：14 天内开卖 / 正在抢票 / 7 天内新上架 / 传出要来
function fansText(n) {
  if (!n) return "";
  return n >= 1e4 ? `${(n / 1e4).toFixed(n >= 1e5 ? 0 : 1)} 万粉丝` : `${n} 粉丝`;
}
function highlights(d, now = new Date(), limit = 8) {
  const items = [];
  // 只列经过所有平台验证、还没开票（或正在抢票）的；tier：0 = 即将开票 / 传出要来，1 = 正在抢票
  for (const e of d.events) {
    if (e.sold_out || !isWanted(e)) continue;
    const s = nextSale(e, now), fans = e.fans || 0;
    const base = {ev: e, name: e.artist_name, photo: e.avatar, kind: e.avatar_kind, fans, region: e.region, country: e.country || "MY", href: `event.html?id=${encodeURIComponent(e.id)}`};
    if (s && toDate(s.start) > now) {
      items.push({...base, tier: 0, tag: "⏰ 即将开票", cls: "warn", line: `${fmt(s.start)} 开票 · ${countdown(toDate(s.start), now)}`});
    } else if (s) {
      items.push({...base, tier: 1, tag: "🔥 正在抢票", cls: "bad", line: `${s.name || "开票"} · ${fmt(s.start)} 开票`});
    }
    // 已开票超过半天、售罄、售票资讯不明的不列（首页只看未来要开票的）
  }
  for (const g of pendingGroups(d).groups) {
    if (!g.leads.length || !HOT_REGIONS.includes(g.leads[0].artist_region)) continue;
    const fans = Math.max(0, ...g.leads.map(l => l.artist_fans || 0));
    items.push({group: g, name: g.name, photo: g.photo, fans, region: g.leads[0].artist_region, href: `pending.html#a-${encodeURIComponent(g.name)}`, tier: 0,
      tag: "🗣️ 传出要来", cls: "sub", line: `${g.leads.length} 条消息 · 最新 ${fmt(g.latest, false) || g.latest}`});
  }
  // 同一位歌星只留最优先的一条；排序：先还没开票的，再按人气
  const rank = (a, b) => (a.tier - b.tier) || (b.fans - a.fans);
  const best = {};
  for (const i of items) {
    const k = i.name.toLowerCase();
    if (!best[k] || rank(i, best[k]) < 0) best[k] = i;
  }
  return Object.values(best).filter(i => i.fans > 0).sort(rank).slice(0, limit);
}
function highlightsHTML(list) {
  if (!list.length) return `<div class="card empty">目前没有当红歌星的新动态。</div>`;
  return `<div class="hl">${list.map(i => `
    <a class="hlcard" href="${i.href}">
      ${avatar(i.photo, i.name, 76, i.kind)}
      <div class="n">${i.country ? flag(i.country) + " " : ""}${esc(i.name)}</div>
      <div class="f">${esc(i.region || "")}${i.region && i.fans ? " · " : ""}${esc(fansText(i.fans))}</div>
      <span class="pill ${i.cls}">${esc(i.tag)}</span>
      <div class="l">${esc(i.line)}</div>
    </a>`).join("")}</div>`;
}

// ---------- GoLive 风格：直式海报卡片、横向轮播 ----------
function dateRange(dates) {
  if (!dates || !dates.length) return "日期未公布";
  const a = fmt(dates[0], false), b = fmt(dates[dates.length - 1], false);
  return dates.length > 1 && a !== b ? `${a.replace(/（.*）/, "")} – ${b.replace(/（.*）/, "")}` : a;
}
function posterCard(e, now = new Date()) {
  // 卡片优先用艺人照片（Deezer 头像）；没有才用官方海报
  // 这场演出自己的直式海报最好；没有（或只有横幅 / 平台占位图）就用艺人照片
  const img = (e.poster_portrait && localUrl(e.poster_img)) || (e.avatar_kind === "artist" && localUrl(e.avatar)) || localUrl(e.poster_img) || localUrl(e.avatar);
  const s = nextSale(e, now), cat = category(e, now);
  let cd = "", hot = false;
  // 卡片上直接显示开票日期，不用点进去看
  if (s && toDate(s.start) > now) cd = `⏰ ${fmt(s.start)} 开票<br>${countdown(toDate(s.start), now)}`;
  else if (s) { cd = `🔥 正在抢票 · ${fmt(s.start)} 开票`; hot = true; }
  else if (cat === "soldout") cd = `🔴 已售罄${e.opened_at ? "<br>" + openedText(e) : ""}`;
  else if (cat === "opened") cd = e.sale_state === "SALE_CLOSED" ? `⛔ 平台已停售${e.opened_at ? "<br>" + openedText(e) : ""}`
    : e.opened_at ? `✔️ ${openedText(e)}` : "✔️ 已开票（抢票已结束）";
  else cd = `❔ 等待开票资料 · 监控中`;
  // 新闻说要加场，但平台还没公布新的开票时间
  if (e.added_status && !(s && toDate(s.start) > now)) {
    // 显示是哪个地区加场；同一艺人其他地区也有加场的，只提示数量（详细在内页）
    const where = `${flag(e.country || "MY")} ${esc(e.city || e.venue || COUNTRIES[e.country || "MY"].split(" ")[1])}`;
    cd += `<br>${e.added_status === "confirmed" ? "➕ 已加场" : "📢 传出加场"} · ${where}`
      + (e._added_more ? `<br>另有 ${e._added_more} 个地区（点进去看）` : "");
    hot = true;
  }
  return `<a class="pcard" href="event.html?id=${encodeURIComponent(e.id)}">
    <div class="ph">
      <div class="ini">${esc((e.artist_name || "?").slice(0, 1))}</div>
      ${img ? `<img src="${esc(img)}" alt="${esc(e.artist_name)}" loading="lazy" style="position:relative" onerror="this.remove()">` : ""}
      <div class="corner"><span>${flag(e.country || "MY")} ${esc(e.region || "")}</span>${badges(e, now).includes("新上架") ? "<span>🆕 新</span>" : ""}</div>
      ${cd ? `<div class="cd ${hot ? "hot" : ""}">${cd}</div>` : ""}
    </div>
    <div class="t">${esc(e.artist_name)}</div>
    <div class="v">${esc(e.venue || "场馆未公布")}</div>
    <div class="dd">${esc(dateRange(e.dates))}</div>
    <span class="tag">${esc(platformsOf(e).map(p => p.source).join(" · "))}</span>
  </a>`;
}
function railHTML(title, cards, moreHref, moreLabel = "查看全部") {
  if (!cards.length) return "";
  return `<div class="sec"><div><h2>${title}</h2>${moreHref ? `<a class="more" href="${moreHref}">${moreLabel} ›</a>` : ""}</div>
    <div class="arrows"><button data-r="-1" aria-label="上一组">‹</button><button data-r="1" aria-label="下一组">›</button></div></div>
    <div class="rail">${cards.join("")}</div>`;
}
document.addEventListener("click", ev => {
  const b = ev.target.closest(".sec .arrows button");
  if (!b) return;
  const rail = b.closest(".sec").nextElementSibling;
  rail?.scrollBy({left: +b.dataset.r * rail.clientWidth * .9, behavior: "smooth"});
});

// ---------- 图片放大查看（点座位图、贴文图片 → 全屏看；再点一下放大到原尺寸，可以拖动） ----------
document.addEventListener("click", ev => {
  const img = ev.target.closest(".maps img, .shots img, img.post-img");
  if (!img) return;
  ev.preventDefault();
  const box = document.createElement("div");
  box.className = "lb";
  box.innerHTML = `<button class="lb-x" aria-label="关闭">✕</button><div class="lb-in"><img alt=""></div><div class="lb-tip">点图片放大 · 点背景或 ✕ 关闭</div>`;
  box.querySelector("img").src = img.currentSrc || img.src;
  const close = () => { box.remove(); document.body.style.overflow = ""; document.removeEventListener("keydown", onKey); };
  const onKey = k => { if (k.key === "Escape") close(); };
  box.addEventListener("click", e => {
    if (e.target.tagName === "IMG") { box.classList.toggle("zoom"); return; }
    close();
  });
  document.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
  document.body.appendChild(box);
});

// ---------- 售票平台 ----------
const platformsOf = e => e.platforms && e.platforms.length ? e.platforms : [{source: e.source, url: e.url, sold_out: e.sold_out}];
const platformBadges = e => platformsOf(e).map(p => `<span class="badge plat">🎫 ${esc(p.source)}</span>`).join("");

// ---------- 头像 ----------
function avatar(src, name, size = 56, kind = "") {
  const url = localUrl(src);
  const initial = esc((name || "?").trim().slice(0, 1).toUpperCase());
  const style = `width:${size}px;height:${size}px;font-size:${Math.round(size * .4)}px`;
  if (!url) return `<span class="av" style="${style}">${initial}</span>`;
  return `<img class="av ${kind === "poster" ? "poster" : ""}" style="${style}" src="${esc(url)}" alt="${esc(name)}"
    data-i="${initial}" loading="lazy" onerror="avFail(this)">`;
}
function avFail(img) {  // 图片载入失败时换成名字首字
  const s = document.createElement("span");
  s.className = "av";
  s.style.cssText = img.style.cssText;
  s.textContent = img.dataset.i;
  img.replaceWith(s);
}
function badges(e, now = new Date()) {
  const recent = t => t && (now - toDate(t)) < 3 * 864e5;
  return (recent(e.first_seen) ? '<span class="badge new">新上架</span>' : "") +
    (recent(e.updates?.prices) ? '<span class="badge new">🆕 票价公布</span>' : "") +
    (recent(e.updates?.maps) ? '<span class="badge new">🆕 座位图更新</span>' : "");
}

// ---------- 资料 ----------
// filter=true：按目前选的国家筛选（列表页用）；演出/消息详细页用 false，才找得到任何国家的项目
async function loadData(filter = true) {
  const d = await fetch("data.json?" + Math.floor(Date.now() / 60000)).then(r => r.json());
  d.byId = Object.fromEntries(d.events.map(e => [e.id, e]));
  d.leadsByEvent = {};
  for (const l of d.leads) for (const m of l.matches || []) (d.leadsByEvent[m] ||= []).push(l);
  // 待确定 = 没被过滤、还没在售票平台上架、而且只说要来还没公布开票细节的（公布了开票时间的在抢票日历；过期或已开卖的不显示）
  d.pending = d.leads.filter(l => !l.hidden && !l.listed && !(l.matches || []).some(m => d.byId[m]) && (l.status || "rumor") === "rumor");
  d.filteredLeads = d.leads.filter(l => l.hidden);
  return filter ? byCountry(d) : d;
}

// ---------- 待确定：按明星分组 ----------
// 关注名单里的明星排最前（没消息也列出来），其他按最新消息排序；没认出明星的放 other
function pendingGroups(d) {
  const by = {}, other = [], when = l => l.time || l.first_seen || "";
  for (const l of d.pending) {
    if (!l.artist) { other.push(l); continue; }
    const g = by[l.artist] ||= {name: l.artist, leads: [], photo: ""};
    g.leads.push(l);
    g.photo ||= l.artist_photo || "";
  }
  const watch = d.watch_artists || [];
  for (const w of watch) {
    const g = by[w.name] ||= {name: w.name, leads: [], photo: ""};
    g.photo ||= w.photo || "";
  }
  const groups = Object.values(by);
  for (const g of groups) {
    g.leads.sort((a, b) => when(b).localeCompare(when(a)));
    g.latest = g.leads[0] ? when(g.leads[0]) : "";
    g.watched = watch.some(w => w.name === g.name);
  }
  groups.sort((a, b) => (b.watched - a.watched) || b.latest.localeCompare(a.latest));
  return {groups, other};
}
// 一位明星的摘要：来源、开售时间、贴文里的重点行（去重）
function artistSummary(g) {
  const src = [...new Set(g.leads.map(l => l.kind === "新闻" ? "新闻" : l.from))];
  const sales = [], points = [], seen = new Set();
  const add = (arr, s) => { const k = s.replace(/\s+/g, " ").trim(); if (k && !seen.has(k.toLowerCase())) { seen.add(k.toLowerCase()); arr.push(k); } };
  for (const l of g.leads) for (const st of l.sale_times || []) add(sales, `${fmt(st.time.length === 10 ? st.time + " 00:00" : st.time, st.time.length > 10)}：${st.line}`);
  for (const l of g.leads) for (const h of l.hints || []) add(points, h);
  for (const l of g.leads) if (l.kind === "新闻") add(points, l.title.replace(/\s+-\s+[^-]+$/, ""));
  const platforms = [...new Set(g.leads.flatMap(l => l.platforms || []))];
  return {src, platforms, sales: sales.slice(0, 3), points: points.slice(0, 4), images: g.leads.filter(l => localUrl(l.image)).slice(0, 4)};
}

// ---------- 导航栏与页尾 ----------
function nav(d, active) {
  const tab = (href, key, label, n) => `<a class="tab ${active === key ? "on" : ""}" href="${href}">${label}${n != null ? `<span class="n">${n}</span>` : ""}</a>`;
  document.body.insertAdjacentHTML("afterbegin", `<nav class="nav"><div class="in">
    <a class="logo" href="index.html">🎤 演唱会雷达</a>
    ${tab("index.html", "home", "🏠 首页")}
    ${tab("confirmed.html", "confirmed", "已确定", d ? d.events.length : null)}
    ${tab("pending.html", "pending", "待确定", d ? d.pending.length : null)}
    ${tab("platforms.html", "platforms", "🎫 售票平台")}
  </div></nav>`);
  // 列表页在导航栏下方显示国家切换
  if (["home", "confirmed", "pending"].includes(active)) document.querySelector(".wrap")?.insertAdjacentHTML("afterbegin", countryBar());
}
function footer(d) {
  const h = Object.entries(d.health).map(([k, v]) =>
    `<span class="chip ${v.ok ? (v.stale ? "warn" : "ok") : "bad"}" title="${esc(v.error || (v.local ? "你电脑 " + v.local + " 抓的" : ""))}">${v.ok ? (v.stale ? "⏸" : "✓") : "✕"} ${esc(k)}${v.local ? "（本机抓取）" : ""}</span>`).join("");
  return `<footer>
    <div>最后更新：${esc(d.generated_at)}（马来西亚时间）· 每天 08:17、20:17 自动更新</div>
    <div class="chips" style="margin:6px 0">${h}</div>
    <div>资料来自 GoLive、Ticket2U、BookMyShow 的公开资料，IG 官方 API（${d.ig_accounts.map(a => "@" + esc(a)).join("、")}）与 Google News；明星照片来自 Deezer。票价与余票以官网为准。</div>
    ${d.ig_bad.length ? `<div>⚠️ 这次读不到的 IG：${d.ig_bad.map(b => esc(b.account)).join("、")}</div>` : ""}
    <div><a href="https://github.com/${REPO}/issues/new?template=lead.yml" target="_blank" rel="noopener">＋ 提交线索</a> ·
    <a href="https://github.com/${REPO}/actions" target="_blank" rel="noopener">扫描记录</a></div>
  </footer>`;
}
function fail(el) {
  el.innerHTML = `<div class="card empty">资料载入失败，请稍后重新整理。</div>`;
}

// Google 新闻查证结果（待确定的明星、已上架的演出都用）
function newsCheckHTML(c, place = "马来西亚") {
  if (!c) return "";
  const where = c.country === "SG" ? "新加坡" : place;
  const FL = {MY: "🇲🇾", SG: "🇸🇬", KR: "🇰🇷", TH: "🇹🇭"};
  return `<div class="sum ${c.count ? "ok" : "warn"}" style="margin-top:8px">🔎 ${c.web_count != null ? "Google 新闻 + 网页" : "Google 新闻"}查证（${esc(c.checked || "")}）：${c.count ? `${c.count} 篇${where}相关报导` : `没有找到${where}的相关报导`}
    ${(c.countries || []).length ? `· 提到的国家 ${c.countries.map(k => FL[k] || k).join(" ")}` : ""}
    ${(c.platforms || []).length ? `· 提到 <b>${c.platforms.map(esc).join("、")}</b>` : ""}
    ${(c.sale_times || []).length ? `· 报导里的开票时间 <b>${c.sale_times.map(x => esc(fmt(x.time))).join("、")}</b>` : ""}
    ${c.added ? "· 有提到<b>加场</b>" : ""}
    ${(c.top || []).map(t => `<div style="font-weight:400;margin-top:4px">${t.web ? "🌐" : "📰"} ${(t.countries || []).map(k => FL[k] || "").join("")} <a href="${esc(safeUrl(t.url))}" target="_blank" rel="noopener">${esc(t.title)}</a> <small>${esc(t.time || "")}</small></div>`).join("")}</div>`;
}

// ---------- 所有售票时间（详细页最上方的表格） ----------
// 每一轮：场次（首场 / 加场）、轮次（预售 / 公售 / 开售）、来源
function saleRound(s) {
  const raw = s.name || "开售";
  const added = s.added || /^加场/.test(raw) || /added show/i.test(raw);
  const src = (raw.match(/（([^）]+)）$/) || [])[1];              // 公告来源，例如（东方日报 娱乐）
  const plat = (raw.match(/^([A-Za-z0-9 ]+) · /) || [])[1];       // 合并的平台，例如 GoLive · General Sales
  let name = raw.replace(/^加场 · /, "").replace(/（[^）]+）$/, "").replace(/^[A-Za-z0-9 ]+ · /, "").trim();
  const kind = /pre-?sale|预售|預售|优先|優先|member|会员|會員|v\.?i\.?p|fan ?club|card ?holder|mastercard|preferred/i.test(name) ? "预售"
    : /general|public|公售|公开|公開/i.test(name) ? "公售" : "开售";
  if (/^(开售|开票|公售|预售|开卖)$/.test(name)) name = "";
  return {added, kind, name, source: src || plat || null, label: `${added ? "加场 · " : ""}${kind}${name ? " · " + name : ""}`};
}
function saleTableHTML(e, now = new Date()) {
  const list = (e.sales || []).filter(s => s.start).sort((a, b) => a.start.localeCompare(b.start));
  if (!list.length && !(e.sale_evidence || []).length) return `<div class="card empty" style="margin-top:12px">🎟️ 还没有任何开票时间（预售、公售都还没公布），一公布就会列在这里并通知你。</div>`;
  const rows = list.map(s => {
    const r = saleRound(s);
    const dateOnly = s.start.length === 10;
    const t = toDate(dateOnly ? s.start + " 00:00" : s.start);
    const done = t < now - (dateOnly ? 864e5 : LIVE_MS), live = !done && t <= now;
    const state = done ? '<span class="meta">已结束</span>' : live ? '<span class="soldout">正在抢票</span>' : `<span class="pill warn" style="margin:0">${esc(countdown(t, now))}</span>`;
    const src = r.source || e.source;
    return `<tr class="${done ? "past" : ""}">
      <td style="white-space:nowrap">${r.added ? "➕ 加场" : "🎤 首场"}</td>
      <td style="white-space:nowrap"><b>${esc(r.kind)}</b>${s.code_required ? "<br><small>需要预售码</small>" : ""}</td>
      <td>${esc(s.source_line || r.name || (s.name || "").replace(/（[^）]+）$/, "") || "开售")}</td>
      <td style="white-space:nowrap">${esc(fmt(s.start))}${dateOnly ? "<br><small>几点开还没公布</small>" : ""}</td>
      <td style="white-space:nowrap">${state}</td>
      <td class="meta">${s.url && s.announced ? `<a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener">${esc(src)}</a>` : esc(src)}</td></tr>`;
  }).join("");
  // 售票状态的其他根据（不是开票轮次的）：售罄新闻、票种建立时间、官方图片 OCR 等
  const starts = new Set(list.map(s => s.start.slice(0, 16)));
  const extra = (e.sale_evidence || []).filter(x => !(x.time && starts.has(x.time.slice(0, 16))) && !/售票轮次/.test(x.src));
  const extraRows = extra.map(x => `<tr>
      <td style="white-space:nowrap">📌 根据</td>
      <td></td>
      <td>${x.url ? `<a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener">${esc(x.text)}</a>` : esc(x.text)}</td>
      <td class="meta" style="white-space:nowrap">${esc(x.time ? fmt(x.time.length === 10 ? x.time + " 00:00" : x.time, x.time.length > 10) : "")}</td>
      <td></td>
      <td class="meta">${esc(x.src)}</td></tr>`).join("");
  return `<div style="text-align:left;margin-top:14px"><div style="font-weight:700;margin-bottom:6px">🎟️ 所有售票时间</div>
    <div style="overflow-x:auto"><table class="saletable">
      <tr><th>场次</th><th>轮次</th><th>内容</th><th>开票时间</th><th>状态</th><th>来源</th></tr>${rows}${extraRows}
    </table></div>
    ${e.official_text ? `<details style="margin-top:6px"><summary class="meta">官方图片上读到的文字（OCR）</summary><pre style="white-space:pre-wrap;font-size:12px;color:var(--sub)">${esc(e.official_text)}</pre></details>` : ""}</div>`;
}
