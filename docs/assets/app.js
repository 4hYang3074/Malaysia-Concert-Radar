// 各页面共用：载入资料、导航栏、时间格式、分类规则、头像
const REPO = "4hYang3074/Malaysia-Concert-Radar";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https?:\/\//i.test(u || "") ? u : "#";
const localUrl = u => /^(maps|avatars)\/[\w./-]+$/.test(u || "") ? u : "";   // 图片只允许仓库里的本地路径
const toDate = s => s ? new Date(s.replace(" ", "T") + ":00+08:00") : null;   // 资料里的时间都是马来西亚时间
const WD = "日一二三四五六";
function fmt(s, withTime = true) {
  const d = toDate(s);
  if (!d) return "";
  const m = new Date(d.getTime() + 8 * 3600e3);
  return `${m.getUTCMonth() + 1}月${m.getUTCDate()}日（周${WD[m.getUTCDay()]}）` + (withTime ? " " + s.slice(11) : "");
}
function countdown(target, now = new Date()) {
  const ms = target - now;
  if (ms <= 0) return "";
  const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60;
  return d ? `还有 ${d} 天 ${h} 小时` : h ? `还有 ${h} 小时 ${mm} 分` : `还有 ${mm} 分钟`;
}
const qs = k => new URLSearchParams(location.search).get(k);

// ---------- 分类：按售票时间 ----------
const CATS = [
  {key: "soon", icon: "🔥", label: "即将开售", desc: "已公布开售时间，还没开始卖。按开售时间排序。"},
  {key: "onsale", icon: "🟢", label: "售票中", desc: "现在就可以买（余票以官网为准）。按演出日期排序。"},
  {key: "tba", icon: "⏳", label: "开售时间未公布", desc: "演出已经上架，但还没公布什么时候开卖。"},
  {key: "soldout", icon: "🔴", label: "已售罄", desc: "官方显示售罄或停止售票，可以留意加场或释票。"},
];

function nextSale(e, now) {
  return (e.sales || []).filter(s => s.start && toDate(s.start) > now).sort((a, b) => a.start.localeCompare(b.start))[0];
}
function category(e, now = new Date()) {
  if (e.sold_out || e.stop_sales) return "soldout";
  if (nextSale(e, now)) return "soon";
  const open = (e.sales || []).some(s => s.available && s.start && toDate(s.start) <= now && (!s.end || toDate(s.end) > now));
  if (open || e.source === "Ticket2U" || e.source === "BookMyShow") return "onsale";
  return "tba";
}
function status(e, now = new Date()) {
  const c = category(e, now);
  if (c === "soldout") return {cls: "bad", text: e.sold_out ? "已售罄" : "已停止售票"};
  if (c === "soon") {
    const s = nextSale(e, now);
    return {cls: "warn", text: `${s.name || "开售"}：${fmt(s.start)}（${countdown(toDate(s.start), now)}）${s.code_required ? " · 需要预售码" : ""}`, short: `${fmt(s.start)} 开售 · ${countdown(toDate(s.start), now)}`};
  }
  if (c === "onsale") {
    const end = (e.sales || []).find(s => s.end && toDate(s.end) > now && e.source === "Ticket2U");
    return {cls: "ok", text: `正在售票${end ? `，${fmt(end.end)} 截止` : ""}（余票以官网为准）`, short: "正在售票"};
  }
  return {cls: "sub", text: "开售时间还没公布，公布后会通知你", short: "开售时间未公布"};
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
    for (const [k, what] of [["start", "开售"], ["queue", "排队开放"]]) {
      const t = s[k] && toDate(s[k]);
      if (!t || t < now - 3 * 3600e3 || t > until) continue;
      items.push({t, s: s[k], confirmed: true, href: `event.html?id=${encodeURIComponent(e.id)}`,
        name: e.artist_name, avatar: e.avatar, kind: e.avatar_kind,
        label: `${s.name || ""} ${what}`.trim() + (s.code_required ? " · 需要预售码" : ""), src: e.source});
    }
  }
  for (const l of d.leads) for (const st of l.sale_times || []) {
    const t = t0(st.time);
    if (!t || t < now - 3 * 3600e3 || t > until) continue;
    const ev = (l.matches || []).map(m => d.byId[m]).find(Boolean);
    // 同一场演出、平台已经公布同一天的开售时间，就不重复列
    if (ev && items.some(i => i.confirmed && i.href.endsWith(encodeURIComponent(ev.id)) && i.s.slice(0, 10) === st.time.slice(0, 10))) continue;
    items.push({t, s: st.time, confirmed: false, href: ev ? `event.html?id=${encodeURIComponent(ev.id)}` : `lead.html?id=${encodeURIComponent(l.id)}`,
      name: ev ? ev.artist_name : l.title, avatar: ev ? ev.avatar : l.avatar, kind: ev ? ev.avatar_kind : "",
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
    const rel = diff === 0 ? "今天" : diff === 1 ? "明天" : diff === 2 ? "后天" : `${diff} 天后`;
    return `<div class="day"><div class="dayhead"><b>${esc(fmt(day + " 00:00", false))}</b><span class="rel ${diff <= 1 ? "hot" : ""}">${rel}</span></div>
      ${list.map(i => `<a class="row cal" href="${i.href}">
        <div class="time">${i.s.length > 10 ? esc(i.s.slice(11)) : "时间<br>未定"}</div>
        ${avatar(i.avatar, i.name, 48, i.kind)}
        <div class="body">
          <div class="name" style="font-size:15.5px">${esc(i.name)}</div>
          <div class="ev">${esc(i.label)}</div>
          <span class="pill ${i.confirmed ? "ok" : "warn"}">${i.confirmed ? "✅ " + esc(i.src) + " 已公布" : "🕒 " + esc(i.src) + " 公告 · 待平台确认"}</span>
          ${i.t > now ? `<span class="pill sub">${esc(countdown(i.t, now))}</span>` : `<span class="pill ok">进行中</span>`}
        </div>
      </a>`).join("")}</div>`;
  }).join("");
}

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
async function loadData() {
  const d = await fetch("data.json?" + Math.floor(Date.now() / 60000)).then(r => r.json());
  d.byId = Object.fromEntries(d.events.map(e => [e.id, e]));
  d.leadsByEvent = {};
  for (const l of d.leads) for (const m of l.matches || []) (d.leadsByEvent[m] ||= []).push(l);
  d.pending = d.leads.filter(l => !(l.matches || []).some(m => d.byId[m]));
  return d;
}

// ---------- 导航栏与页尾 ----------
function nav(d, active) {
  const tab = (href, key, label, n) => `<a class="tab ${active === key ? "on" : ""}" href="${href}">${label}${n != null ? `<span class="n">${n}</span>` : ""}</a>`;
  document.body.insertAdjacentHTML("afterbegin", `<nav class="nav"><div class="in">
    <a class="logo" href="index.html">🎤 演唱会雷达</a>
    ${tab("index.html", "home", "📅 抢票日历")}
    ${tab("confirmed.html", "confirmed", "已确定", d ? d.events.length : null)}
    ${tab("pending.html", "pending", "待确定", d ? d.pending.length : null)}
  </div></nav>`);
}
function footer(d) {
  const h = Object.entries(d.health).map(([k, v]) =>
    `<span class="chip ${v.ok ? (v.stale ? "warn" : "ok") : "bad"}" title="${esc(v.error || "")}">${v.ok ? (v.stale ? "⏸" : "✓") : "✕"} ${esc(k)}</span>`).join("");
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
