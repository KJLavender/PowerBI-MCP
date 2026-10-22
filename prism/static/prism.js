// shared header (logo + navigation) and small helpers for the PRISM pages
const LOGO = `<svg width="30" height="30" viewBox="0 0 64 64" aria-hidden="true">
  <defs><linearGradient id="pg" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#a5b4fc"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs>
  <path d="M8 34 L30 10 L40 50 Z" fill="url(#pg)" opacity=".95"/>
  <path d="M2 31 L22 33" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M37 30 L62 18" stroke="#f43f5e" stroke-width="3" stroke-linecap="round"/>
  <path d="M38 33 L62 30" stroke="#f59e0b" stroke-width="3" stroke-linecap="round"/>
  <path d="M38 36 L62 42" stroke="#22c55e" stroke-width="3" stroke-linecap="round"/>
  <path d="M38 39 L62 54" stroke="#0ea5e9" stroke-width="3" stroke-linecap="round"/>
</svg>`;

function header(active) {
  const nav = [["/", "我的報表", "home"], ["/map", "報表地圖", "map"], ["/how", "運作原理", "how"]]
    .map(([href, text, key]) => `<a href="${href}" class="${key === active ? "on" : ""}">${text}</a>`).join("");
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="topbar"><div class="in"><a class="logo" href="/">${LOGO}<span>PRISM<br><small>Proven Report Insights &amp; Semantic Mapping</small></span></a><nav>${nav}</nav></div></div>`);
}

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
async function api(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
  return data;
}
