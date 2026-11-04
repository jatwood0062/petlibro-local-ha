// Pets sidebar app for PetLibro Local: Overview / Schedule / Alerts. Ships with petlibro_lan (no build step).
// Custom element: petlibro-local-panel (Home Assistant sets hass, narrow, route and panel on it).
// Reads are open to every user; every change goes through admin-only websocket commands.

const TABS = [
  ["overview", "Overview", "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"],
  ["schedule", "Schedule", "M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"],
  ["alerts", "Alerts", "M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"],
];
const MENU = "M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z";
const DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const STORE_KEY = "petlibro_local_panel_tab";
const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;
const MAX_MEALS = 24;
const CLEAR_WORD = "CLEAR";
const DAY_MS = 86400000;

const svg = (d, size = 24) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true"><path fill="currentColor" d="${d}"/></svg>`;
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const clone = (o) => JSON.parse(JSON.stringify(o));
const store = {
  get() {
    try {
      return localStorage.getItem(STORE_KEY);
    } catch (e) {
      return null;
    }
  },
  set(v) {
    try {
      localStorage.setItem(STORE_KEY, v);
    } catch (e) {
      /* storage blocked: the tab just is not remembered */
    }
  },
};

const STYLE = `
  :host { display: flex; flex-direction: column; height: 100%; min-height: 100vh; box-sizing: border-box;
          background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  @supports (height: 100dvh) { :host { min-height: 100dvh; } }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  button { font: inherit; color: inherit; cursor: pointer; border: none; background: none; padding: 0;
           -webkit-tap-highlight-color: transparent; }
  button:disabled { cursor: default; }
  :focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  header { display: flex; align-items: center; gap: 4px; min-height: 56px; padding: 0 8px 0 12px; flex: none;
           background: var(--app-header-background-color, var(--primary-color));
           color: var(--app-header-text-color, var(--text-primary-color, #fff)); }
  h1 { font-size: 20px; font-weight: 400; margin: 0 16px 0 4px; white-space: nowrap; }
  .menu { width: 48px; height: 48px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; }
  .menu:hover { background: rgba(255,255,255,.15); }
  nav { display: flex; }
  nav button { display: inline-flex; align-items: center; gap: 6px; padding: 0 16px; height: 56px; font-size: 14px;
               font-weight: 500; text-transform: uppercase; letter-spacing: .04em; opacity: .7; }
  nav button[aria-selected=true] { opacity: 1; box-shadow: inset 0 -3px 0 currentColor; }
  nav button:hover { opacity: 1; }
  main { flex: 1; overflow: auto; padding: 16px; width: 100%; max-width: 960px; margin: 0 auto; }
  main > * + * { margin-top: 16px; }
  .box { background: var(--card-background-color, #fff); border-radius: var(--ha-card-border-radius, 12px);
         border: 1px solid var(--divider-color); padding: 16px; line-height: 1.5; }
  .box h2 { font-size: 16px; font-weight: 500; margin: 0 0 8px; }
  .box h3 { font-size: 14px; font-weight: 500; margin: 12px 0 4px; }
  .muted { color: var(--secondary-text-color); font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
  .grid > .box { margin: 0; }
  .dh { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .dh .t { flex: 1; min-width: 0; }
  .dh .n { font-size: 18px; font-weight: 500; overflow-wrap: anywhere; }
  .dot { flex: none; width: 12px; height: 12px; border-radius: 50%; background: var(--error-color, #db4437); }
  .dot.on { background: var(--success-color, #43a047); }
  .rows { margin: 0; }
  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 7px 0;
         border-top: 1px solid var(--divider-color); font-size: 14px; }
  .row:first-child { border-top: none; }
  .row .k { color: var(--secondary-text-color); flex: none; }
  .row .v { text-align: right; min-width: 0; overflow-wrap: anywhere; }
  .row.bad { background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); margin: 0 -8px; padding: 7px 8px; border-radius: 8px; }
  .chip { display: inline-block; font-size: 12px; font-weight: 500; padding: 1px 9px; border-radius: 10px;
          border: 1px solid var(--divider-color); color: var(--secondary-text-color); white-space: nowrap; }
  .chip.ok { color: var(--success-color, #2e7d32); border-color: currentColor; }
  .chip.warn { color: var(--warning-color, #b36b00); border-color: currentColor; }
  .chip.bad { color: var(--error-color, #db4437); border-color: currentColor; font-weight: 700; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .btn { font-size: 14px; font-weight: 500; padding: 8px 16px; min-height: 40px; border-radius: 20px;
         background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.alt { background: none; color: var(--primary-color); border: 1px solid var(--divider-color); }
  .btn.danger { background: var(--error-color, #db4437); color: #fff; }
  .btn.alt.danger { background: none; color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  .btn:disabled { opacity: .45; }
  .notice { border-radius: 10px; padding: 10px 12px; font-size: 14px; border: 1px solid var(--divider-color); }
  .notice.warn { border-color: var(--warning-color, #b36b00); background: color-mix(in srgb, var(--warning-color, #b36b00) 10%, transparent); }
  .notice.bad { border-color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 10%, transparent); }
  .notice.info { border-color: var(--primary-color); }
  .empty { text-align: center; padding: 28px 16px; color: var(--secondary-text-color); }
  input, select { font: inherit; font-size: 14px; padding: 6px 8px; min-height: 40px; max-width: 100%;
                  border: 1px solid var(--divider-color); border-radius: 8px;
                  background: var(--card-background-color, #fff); color: var(--primary-text-color); }
  input[type=checkbox] { min-height: 0; width: 20px; height: 20px; accent-color: var(--primary-color); margin: 0; flex: none; }
  input:disabled, select:disabled { opacity: .6; }
  label.chk { display: flex; align-items: flex-start; gap: 10px; min-height: 36px; font-size: 14px; cursor: pointer; padding: 6px 0; }
  .tgl { flex: none; width: 44px; height: 26px; border-radius: 13px; position: relative; background: var(--disabled-text-color, #9e9e9e); }
  .tgl::after { content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; transition: left .15s; }
  .tgl[aria-checked=true] { background: var(--primary-color); }
  .tgl[aria-checked=true]::after { left: 21px; }
  .tgl:disabled { opacity: .5; }
  .seg { display: flex; flex-wrap: wrap; gap: 8px; }
  .pill { font-size: 14px; padding: 6px 14px; min-height: 40px; border-radius: 20px; border: 1px solid var(--divider-color);
          background: var(--card-background-color, #fff); }
  .pill[aria-pressed=true] { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  /* schedule */
  .meal { border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px 12px; margin-top: 10px; }
  .meal.off { opacity: .6; }
  .meal .l1, .meal .l2 { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
  .meal .l2 { margin-top: 8px; }
  .meal .l1 input.lbl { flex: 1 1 140px; min-width: 0; }
  .meal .rm { width: 40px; height: 40px; border-radius: 50%; color: var(--error-color, #db4437); font-size: 20px; line-height: 1; }
  .meal .rm:hover { background: var(--secondary-background-color, rgba(127,127,127,.15)); }
  .days { display: flex; gap: 4px; }
  .day { width: 36px; height: 36px; border-radius: 50%; border: 1px solid var(--divider-color); font-size: 13px; font-weight: 500; }
  .day[aria-pressed=true] { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .err { color: var(--error-color, #db4437); font-size: 13px; margin-top: 4px; }
  ol.up { margin: 4px 0 0; padding-left: 22px; }
  /* alerts */
  .arow { padding: 10px 0; border-top: 1px solid var(--divider-color); }
  .arow:first-of-type { border-top: none; }
  .ah { display: flex; align-items: center; gap: 12px; }
  .ah .t { flex: 1; min-width: 0; font-weight: 500; }
  .ap { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; margin-top: 8px; font-size: 14px; }
  .ap label { display: flex; align-items: center; gap: 6px; color: var(--secondary-text-color); }
  .ap input[type=number] { width: 84px; }
  .tlist label.chk { border-top: 1px solid var(--divider-color); }
  .tlist label.chk:first-child { border-top: none; }
  .chk .sub { display: block; color: var(--secondary-text-color); font-size: 12px; }
  /* dialog + toast */
  .ov { position: fixed; inset: 0; z-index: 20; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; padding: 16px; }
  .dlg { background: var(--card-background-color, #fff); color: var(--primary-text-color); border-radius: 14px;
         padding: 20px; width: 100%; max-width: 420px; max-height: 90vh; overflow: auto; line-height: 1.5; }
  .dlg h2 { margin: 0 0 8px; font-size: 18px; font-weight: 500; }
  .dlg .dbody > * + * { margin-top: 10px; }
  .dlg input[type=text], .dlg select { width: 100%; }
  .dact { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  .toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: 24px; z-index: 30; max-width: min(92vw, 520px);
           background: var(--primary-text-color); color: var(--primary-background-color); padding: 10px 16px; border-radius: 10px; font-size: 14px; }
  .toast.bad { background: var(--error-color, #db4437); color: #fff; }
  @media (max-width: 700px) {
    nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 5; background: var(--card-background-color, #fff);
          color: var(--secondary-text-color); border-top: 1px solid var(--divider-color); padding-bottom: env(safe-area-inset-bottom); }
    nav button { flex: 1; flex-direction: column; justify-content: center; gap: 2px; height: 60px; padding: 0 2px; font-size: 11px;
                 text-transform: none; letter-spacing: 0; opacity: 1; }
    nav button[aria-selected=true] { color: var(--primary-color); box-shadow: inset 0 3px 0 var(--primary-color); }
    main { padding: 12px 12px calc(76px + env(safe-area-inset-bottom)); }
    .grid { grid-template-columns: 1fr; }
    .toast { bottom: calc(76px + env(safe-area-inset-bottom)); }
    .acts .btn { flex: 1 1 auto; }
  }
`;

class PetsPanel extends HTMLElement {
  constructor() {
    super();
    const saved = store.get();
    this._tab = TABS.some((t) => t[0] === saved) ? saved : "overview";
    this._state = null; // {devices, active_alerts, time_zone, admin} from petlibro_lan/subscribe
    this._sched = null; // {serial: schedule view}
    this._drafts = {}; // {serial: {meals, dirty, ack}}
    this._sel = null; // selected feeder
    this._alerts = null; // {settings, active, devices}
    this._draftA = null; // settings being edited
    this._savedA = "";
    this._targets = null;
    this._testResults = null;
    this._error = null;
  }

  // --- lifecycle ----------------------------------------------------------
  connectedCallback() {
    this._subscribe();
    this._timer = setInterval(() => {
      if (this._tab === "overview" && this._state) this._render(); // relative times ("5 min ago") move on
    }, 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    clearInterval(this._poll);
    this._poll = null;
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._build();
      this._loadState();
      if (this.isConnected) this._subscribe();
      // the remembered tab is not the overview: load its data now, tab clicks are not coming
      if (this._tab === "schedule") this._loadSchedules();
      else if (this._tab === "alerts" && this._isAdmin) this._loadAlerts();
    }
    const admin = !!hass.user?.is_admin;
    if (this._admin !== admin) {
      this._admin = admin;
      this._render();
    }
  }

  get hass() {
    return this._hass;
  }

  set narrow(v) {
    this._narrow = !!v;
    if (this._menu) this._menu.hidden = !this._narrow;
  }

  set route(v) {
    this._route = v;
  }

  set panel(v) {
    this._panel = v;
  }

  get _isAdmin() {
    return !!this._hass?.user?.is_admin;
  }

  // --- data ---------------------------------------------------------------
  async _loadState() {
    try {
      this._onState(await this._hass.callWS({ type: "petlibro_lan/state" }));
    } catch (err) {
      this._error = err.message || "Could not load PetLibro Local";
      this._render();
    }
  }

  async _subscribe() {
    if (this._unsub || this._subscribing || !this._hass?.connection) return;
    this._subscribing = true;
    try {
      const unsub = await this._hass.connection.subscribeMessage((ev) => this._onState(ev), {
        type: "petlibro_lan/subscribe",
      });
      if (this.isConnected) this._unsub = unsub;
      else unsub();
    } catch (err) {
      /* the periodic refresh below still keeps the page current */
      this._poll = this._poll || setInterval(() => this._loadState(), 15000);
    } finally {
      this._subscribing = false;
    }
  }

  _onState(state) {
    this._state = state;
    this._error = null;
    if (this._tab === "overview") this._render();
    else if (this._tab === "alerts" && this._alerts) {
      this._alerts.active = state.active_alerts || [];
      const box = this._main.querySelector("#active");
      if (box) box.innerHTML = this._activeHtml();
    }
  }

  async _call(msg, ok) {
    try {
      const res = await this._hass.callWS(msg);
      if (ok) this._toast(ok);
      return res;
    } catch (err) {
      this._toast(err?.message || "Something went wrong", true);
      return null;
    }
  }

  _toast(text, bad = false) {
    const t = this._toastEl;
    t.textContent = text;
    t.className = `toast${bad ? " bad" : ""}`;
    t.hidden = false;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => (t.hidden = true), bad ? 8000 : 3500);
  }

  // --- formatting -----------------------------------------------------------
  _fmt(opts, date) {
    const loc = this._hass?.locale?.language;
    const tz = this._hass?.config?.time_zone;
    try {
      return new Intl.DateTimeFormat(loc, { ...opts, timeZone: tz }).format(date);
    } catch (e) {
      return new Intl.DateTimeFormat(undefined, opts).format(date);
    }
  }

  _when(ms) {
    if (typeof ms !== "number") return "";
    const d = new Date(ms);
    const day = (x) => this._fmt({ year: "numeric", month: "2-digit", day: "2-digit" }, x);
    const time = this._fmt({ hour: "numeric", minute: "2-digit" }, d);
    const that = day(d);
    const now = Date.now();
    if (that === day(new Date(now))) return `today ${time}`;
    if (that === day(new Date(now + DAY_MS))) return `tomorrow ${time}`;
    if (that === day(new Date(now - DAY_MS))) return `yesterday ${time}`;
    return this._fmt({ weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }, d);
  }

  _ago(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  // --- layout ---------------------------------------------------------------
  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>${STYLE}</style>
      <header>
        <button class="menu" aria-label="Open the Home Assistant menu" hidden>${svg(MENU)}</button>
        <h1>Pets</h1>
        <nav role="tablist" aria-label="Pets"></nav>
      </header>
      <main aria-live="polite"></main>
      <div class="ov" hidden></div>
      <div class="toast" role="status" hidden></div>`;
    this._root = root;
    this._main = root.querySelector("main");
    this._nav = root.querySelector("nav");
    this._menu = root.querySelector(".menu");
    this._ov = root.querySelector(".ov");
    this._toastEl = root.querySelector(".toast");
    this._menu.hidden = !this._narrow;
    this._menu.addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })));
    this._nav.addEventListener("click", (e) => {
      const b = e.target.closest("[data-tab]");
      if (b) this._setTab(b.dataset.tab);
    });
    this._main.addEventListener("click", (e) => this._onClick(e));
    this._main.addEventListener("input", (e) => this._onInput(e));
    this._main.addEventListener("change", (e) => this._onInput(e));
    this._render();
  }

  _setTab(tab) {
    if (tab === "alerts" && !this._isAdmin) tab = "overview";
    this._tab = tab;
    store.set(tab);
    this._render();
    if (tab === "schedule") this._loadSchedules();
    if (tab === "alerts") this._loadAlerts();
  }

  _render() {
    if (!this._main) return;
    const tabs = TABS.filter((t) => t[0] !== "alerts" || this._isAdmin);
    if (!tabs.some((t) => t[0] === this._tab)) this._tab = "overview";
    this._nav.innerHTML = tabs
      .map(([id, label, icon]) =>
        `<button role="tab" data-tab="${id}" aria-selected="${id === this._tab}">${svg(icon, 20)}<span>${label}</span></button>`)
      .join("");
    const scroll = this._main.scrollTop;
    if (this._error) {
      this._main.innerHTML = `<div class="box"><div class="notice bad">${esc(this._error)}</div>
        <p class="muted">Is the PetLibro Local integration set up and loaded?</p></div>`;
    } else if (!this._state) {
      this._main.innerHTML = `<div class="empty">Loading...</div>`;
    } else if (this._tab === "overview") {
      this._main.innerHTML = this._overviewHtml();
    } else if (this._tab === "schedule") {
      this._main.innerHTML = this._scheduleHtml();
    } else {
      this._main.innerHTML = this._alertsHtml();
    }
    this._main.scrollTop = scroll;
  }

  // --- overview ---------------------------------------------------------------
  _overviewHtml() {
    const devs = this._state.devices;
    if (!devs.length) {
      return `<div class="box empty">No PetLibro devices have connected yet.<br>
        Point <code>mqtt.us.petlibro.com</code> at Home Assistant and power-cycle the device (see the integration's README).</div>`;
    }
    const alerts = this._state.active_alerts || [];
    const banner = alerts.length
      ? `<div class="notice bad"><strong>${plural(alerts.length, "active alert")}:</strong> ${alerts
          .map((a) => esc(a.title || a.type)).join(" &middot; ")}</div>` : "";
    return `${banner}${this._admin ? "" : `<p class="muted">You can look but not change anything: edits need an administrator.</p>`}
      <div class="grid">${devs.map((d) => (d.kind === "fountain" ? this._fountainCard(d) : this._feederCard(d))).join("")}</div>`;
  }

  _row(key, value, cls = "") {
    return `<div class="row ${cls}"><span class="k">${esc(key)}</span><span class="v">${value}</span></div>`;
  }

  _chip(text, kind = "") {
    return `<span class="chip ${kind}">${esc(text)}</span>`;
  }

  _head(d) {
    const sub = d.online
      ? `${esc(d.model_name)} &middot; connected`
      : `${esc(d.model_name)} &middot; offline${d.last_seen_ts ? `, last seen ${esc(this._ago(d.last_seen_ts))}` : ""}`;
    return `<div class="dh"><span class="dot ${d.online ? "on" : ""}" aria-hidden="true"></span>
      <div class="t"><div class="n">${esc(d.name)}</div><div class="muted">${sub}</div></div></div>`;
  }

  _btn(label, act, d, { alt = true, danger = false, extra = "" } = {}) {
    const off = !d.online || !this._isAdmin;
    const why = !this._isAdmin ? "Administrators only" : !d.online ? "Device is offline" : "";
    return `<button class="btn ${alt ? "alt" : ""} ${danger ? "danger" : ""}" data-act="${act}" data-id="${esc(d.id)}" ${extra}
      ${off ? `disabled title="${why}"` : ""}>${esc(label)}</button>`;
  }

  _feederCard(d) {
    const food = d.food_low === null || d.food_low === undefined ? this._chip("Unknown")
      : d.food_low ? this._chip("Low - refill soon", "bad") : this._chip("OK", "ok");
    const last = d.last_feed_ms
      ? `${plural(d.last_feed_portions || 0, "portion")}, ${esc(this._when(d.last_feed_ms))} <span class="muted">(${esc(
        this._ago(d.last_feed_ms))}${d.last_feed_scheduled ? ", scheduled" : ", manual"})</span>`
      : `<span class="muted">none since Home Assistant started</span>`;
    const next = !d.plan_managed
      ? `<span class="muted">schedule stored on the feeder (set in the PetLibro app)</span>`
      : d.next_feed ? `${esc(this._when(d.next_feed.ts))}, ${plural(d.next_feed.portions, "portion")}`
        : `<span class="muted">none (no enabled meals)</span>`;
    let rows = this._row("Food", food) + this._row("Last feed", last) + this._row("Feeds today", String(d.feeds_today ?? 0))
      + this._row("Next feed", next);
    if (d.feed_blocked) rows += this._row("Dispenser", this._chip("Blocked or feed failed", "bad"), "bad");
    if (d.has_battery && d.battery != null) rows += this._row("Battery", `${Math.round(d.battery)}%`);
    const acts = [];
    if (d.can_feed) acts.push(this._btn("Feed now", "feed", d, { alt: false }));
    if (d.can_light) acts.push(this._btn(d.light ? "Light off" : "Light on", "light", d, { extra: `data-on="${!d.light}"` }));
    return `<div class="box">${this._head(d)}<div class="rows">${rows}</div><div class="acts">${acts.join("")}</div></div>`;
  }

  _fountainCard(d) {
    const tri = (v, on, off) => (v === true ? this._chip(on, "ok") : v === false ? this._chip(off, "warn") : this._chip("Unknown"));
    let water = `<span class="muted">waiting for a reading</span>`;
    if (typeof d.water_weight === "number") {
      const src = { user: "your threshold", device: "device threshold", default: "default threshold" }[d.water_threshold_source];
      water = `${Math.round(d.water_weight)} g ${d.water_low ? this._chip("Low", "bad") : this._chip("OK", "ok")}
        <div class="muted">low below ${Math.round(d.water_threshold)} g (${src})</div>`;
    }
    let filter = `<span class="muted">unknown</span>`;
    let filterCls = "";
    if (typeof d.filter_due_ms === "number") {
      const days = (d.filter_due_ms - Date.now()) / DAY_MS;
      const date = this._fmt({ month: "short", day: "numeric" }, new Date(d.filter_due_ms));
      if (days <= 0) {
        const late = Math.floor(-days);
        filter = `${this._chip(late === 0 ? "Due today" : `Overdue by ${plural(late, "day")}`, "bad")}
          <div class="muted">was due ${esc(date)}</div>`;
        filterCls = "bad";
      } else if (days <= 3) {
        filter = `${this._chip(`Due in ${plural(Math.ceil(days), "day")}`, "warn")} <div class="muted">${esc(date)}</div>`;
      } else {
        filter = `${plural(Math.ceil(days), "day")} left <div class="muted">due ${esc(date)}</div>`;
      }
    }
    let battery = `<span class="muted">unknown</span>`;
    if (typeof d.battery === "number") {
      const state = d.charging ? "charging" : d.plugged_in ? "charged, on charger" : "on battery";
      battery = `${Math.round(d.battery)}% <span class="muted">${state}</span>`;
    }
    const radar = d.radar_seen_ms ? `${esc(this._ago(d.radar_seen_ms))} <span class="muted">(${esc(this._when(d.radar_seen_ms))})</span>`
      : `<span class="muted">not seen yet</span>`;
    const rows = this._row("Pump", tri(d.pump_on, "On", "Off")) + this._row("Light", tri(d.light, "On", "Off"))
      + this._row("Battery", battery) + this._row("Water", water) + this._row("Filter", filter, filterCls)
      + this._row("Pet last seen drinking", radar);
    const acts = [
      this._btn(d.pump_on ? "Pump off" : "Pump on", "pump", d, { alt: false, extra: `data-on="${!d.pump_on}"` }),
      this._btn(d.light ? "Light off" : "Light on", "light", d, { extra: `data-on="${!d.light}"` }),
      this._btn("Reset filter", "reset_filter", d, { danger: d.filter_overdue === true }),
    ];
    return `<div class="box">${this._head(d)}<div class="rows">${rows}</div><div class="acts">${acts.join("")}</div></div>`;
  }

  async _overviewAction(btn) {
    const d = this._state.devices.find((x) => x.id === btn.dataset.id);
    if (!d) return;
    const act = btn.dataset.act;
    const send = async (extra, ok) => {
      btn.disabled = true;
      const res = await this._call({ type: "petlibro_lan/command", serial: d.id, action: act, ...extra }, ok);
      if (res) {
        const i = this._state.devices.findIndex((x) => x.id === d.id);
        if (i >= 0) this._state.devices[i] = res;
      }
      this._render();
    };
    if (act === "feed") {
      const max = d.max_portions || 12;
      const opts = Array.from({ length: max }, (_, i) => i + 1)
        .map((n) => `<option value="${n}" ${n === (d.feed_portions || 1) ? "selected" : ""}>${plural(n, "portion")}</option>`).join("");
      const got = await this._dialog({
        title: `Feed ${d.name} now?`,
        body: `<p>This dispenses food immediately.</p><label class="muted">Amount<br><select id="pp" aria-label="Portions">${opts}</select></label>`,
        confirm: "Feed now",
        collect: (ov) => Number(ov.querySelector("#pp").value),
      });
      if (got) await send({ portions: got }, `Feeding ${plural(got, "portion")}`);
    } else if (act === "reset_filter") {
      const ok = await this._dialog({
        title: "Reset the filter timer?",
        body: `<p>Only do this after you have replaced the filter in ${esc(d.name)}. The next replacement date is set 30 days from now.</p>`,
        confirm: "Reset filter",
        danger: true,
      });
      if (ok) await send({}, "Filter timer reset");
    } else {
      await send({ on: btn.dataset.on === "true" }, null);
    }
  }

  // --- dialog -------------------------------------------------------------------
  _dialog({ title, body, confirm = "OK", danger = false, check = null, collect = null }) {
    return new Promise((resolve) => {
      const ov = this._ov;
      const back = this._root.activeElement;
      ov.innerHTML = `<div class="dlg" role="dialog" aria-modal="true" aria-label="${esc(title)}"><h2>${esc(title)}</h2>
        <div class="dbody">${body}</div>
        <div class="dact"><button class="btn alt" data-d="cancel">Cancel</button>
        <button class="btn ${danger ? "danger" : ""}" data-d="ok">${esc(confirm)}</button></div></div>`;
      ov.hidden = false;
      const ok = ov.querySelector('[data-d="ok"]');
      const update = () => (ok.disabled = check ? !check(ov) : false);
      const done = (accepted) => {
        const value = accepted ? (collect ? collect(ov) : true) : null;
        ov.hidden = true;
        ov.innerHTML = "";
        ov.onclick = ov.onkeydown = ov.oninput = null;
        if (back && back.focus) back.focus();
        resolve(value);
      };
      ov.oninput = update;
      ov.onclick = (e) => {
        const b = e.target.closest("[data-d]");
        if (e.target === ov || b?.dataset.d === "cancel") done(false);
        else if (b?.dataset.d === "ok" && !ok.disabled) done(true);
      };
      ov.onkeydown = (e) => {
        if (e.key === "Escape") done(false);
        else if (e.key === "Enter" && e.target.tagName === "INPUT" && !ok.disabled) done(true);
      };
      update();
      (ov.querySelector("input, select") || ok).focus();
    });
  }

  // --- schedule -------------------------------------------------------------------
  async _loadSchedules() {
    const res = await this._call({ type: "petlibro_lan/get_schedule" });
    if (!res) return;
    this._sched = {};
    for (const f of res.feeders) {
      this._sched[f.id] = f;
      const old = this._drafts[f.id];
      if (!old || !old.dirty) this._drafts[f.id] = { meals: this._editable(f.meals), dirty: false, ack: old?.ack || false };
    }
    if (!this._sel || !this._sched[this._sel]) this._sel = res.feeders[0]?.id || null;
    if (this._tab === "schedule") this._render();
  }

  _editable(meals) {
    return clone(meals).map((m) => ({
      time: m.time, portions: m.portions, days: m.days || [1, 2, 3, 4, 5, 6, 7], label: m.label || "", enabled: m.enabled !== false,
    }));
  }

  _scheduleHtml() {
    if (!this._sched) return `<div class="empty">Loading...</div>`;
    const ids = Object.keys(this._sched);
    if (!ids.length) return `<div class="box empty">No feeder that supports feeding plans has connected yet.</div>`;
    if (!this._sel || !this._sched[this._sel]) this._sel = ids[0];
    const s = this._sched[this._sel];
    const dr = this._drafts[this._sel];
    const ro = !this._isAdmin;
    const pick = ids.length > 1
      ? `<div class="seg" role="group" aria-label="Feeder">${ids.map((id) =>
        `<button class="pill" data-feeder="${esc(id)}" aria-pressed="${id === this._sel}">${esc(this._sched[id].name)}</button>`).join("")}</div>` : "";
    const sync = !s.configured ? "" : s.sync === "synced" ? this._chip("Confirmed by the feeder", "ok")
      : this._chip(s.online ? "Waiting for the feeder to confirm" : "Feeder offline: will send when it reconnects", "warn");
    const intro = s.configured
      ? `<div class="notice info">The schedule below is managed by Home Assistant and re-sent to <strong>${esc(s.name)}</strong> at least daily
         and after daylight-saving changes. A schedule edited in the PetLibro app will be overwritten. Times are in
         Home Assistant's time zone (${esc(s.time_zone)}).</div>`
      : `<div class="notice warn"><strong>Heads up before the first save.</strong> ${esc(s.name)} already runs a schedule you set in the PetLibro app,
         and Home Assistant cannot read it. <strong>Saving here replaces that schedule completely</strong> with the meals below, so enter every
         meal you want. Nothing is sent until you press Save.
         <label class="chk"><input type="checkbox" id="ack" ${dr.ack ? "checked" : ""} ${ro ? "disabled" : ""}>
         <span>I understand that saving replaces the schedule stored on ${esc(s.name)}.</span></label></div>`;
    const up = s.upcoming.length
      ? `<ol class="up">${s.upcoming.map((u) => `<li>${esc(this._when(u.ts))}: ${plural(u.portions, "portion")}${u.label ? ` (${esc(u.label)})` : ""}</li>`).join("")}</ol>`
      : `<p class="muted">${s.configured ? "No enabled meals." : "Nothing known: the schedule on the feeder cannot be read."}</p>`;
    return `${pick}
      <div class="box"><div class="dh"><div class="t"><div class="n">${esc(s.name)}</div></div>${sync}</div>${intro}</div>
      <div class="box"><h2>Next 3 feeds</h2>${up}</div>
      <div class="box"><h2>Meals</h2>
        <div id="meals">${dr.meals.map((m, i) => this._mealHtml(m, i, s.max_portions, ro)).join("")}</div>
        ${dr.meals.length ? "" : `<p class="muted">No meals yet.</p>`}
        <div class="acts">
          <button class="btn alt" data-act="add-meal" ${ro || dr.meals.length >= MAX_MEALS ? "disabled" : ""}>Add meal</button>
          <button class="btn" data-act="save-plan" ${ro ? "disabled" : ""}>Save schedule</button>
          <button class="btn alt" data-act="discard" ${ro ? "disabled" : ""}>Discard changes</button>
        </div>
        <div class="err" id="planerr" role="alert"></div>
        <p class="muted" id="plannote"></p>
        ${ro ? `<p class="muted">Only administrators can change the schedule.</p>` : ""}
      </div>
      <div class="box"><h2>Clear schedule</h2>
        <p class="muted">Removes every meal from the feeder so it stops feeding on its own. This is the only way an empty schedule is sent.</p>
        <button class="btn alt danger" data-act="clear-plan" ${ro ? "disabled" : ""}>Clear schedule...</button></div>`;
  }

  _mealHtml(m, i, max, ro) {
    const opts = Array.from({ length: max }, (_, k) => k + 1)
      .map((n) => `<option value="${n}" ${n === m.portions ? "selected" : ""}>${n}</option>`).join("");
    const days = DAY_LETTERS.map((l, k) =>
      `<button class="day" data-day="${k + 1}" data-i="${i}" aria-pressed="${m.days.includes(k + 1)}" aria-label="${DAY_NAMES[k]}" ${ro ? "disabled" : ""}>${l}</button>`).join("");
    return `<div class="meal ${m.enabled ? "" : "off"}" data-i="${i}">
      <div class="l1">
        <input class="lbl" type="text" maxlength="40" placeholder="Label, e.g. Breakfast" value="${esc(m.label)}" data-f="label" data-i="${i}"
          aria-label="Meal label" ${ro ? "disabled" : ""}>
        <button class="tgl" role="switch" aria-checked="${m.enabled}" aria-label="Meal enabled" data-f="enabled" data-i="${i}" ${ro ? "disabled" : ""}></button>
        <button class="rm" data-act="rm-meal" data-i="${i}" aria-label="Remove meal" ${ro ? "disabled" : ""}>&times;</button>
      </div>
      <div class="l2">
        <input type="time" value="${esc(m.time)}" data-f="time" data-i="${i}" aria-label="Time" required ${ro ? "disabled" : ""}>
        <label class="muted">Portions <select data-f="portions" data-i="${i}" ${ro ? "disabled" : ""}>${opts}</select></label>
        <div class="days" role="group" aria-label="Days">${days}</div>
      </div></div>`;
  }

  _planProblem(dr, s) {
    const m = dr.meals;
    if (!m.length) return "Add at least one meal, or use Clear schedule.";
    for (const [i, x] of m.entries()) {
      if (!TIME_RE.test(x.time)) return `Meal ${i + 1}: choose a time.`;
      if (!x.days.length) return `Meal ${i + 1}: choose at least one day.`;
    }
    if (!m.some((x) => x.enabled)) return "Enable at least one meal, or use Clear schedule.";
    return "";
  }

  _refreshPlanState() {
    const s = this._sched?.[this._sel];
    const dr = this._drafts[this._sel];
    if (!s || !dr) return;
    const problem = this._planProblem(dr, s);
    const save = this._main.querySelector('[data-act="save-plan"]');
    const disc = this._main.querySelector('[data-act="discard"]');
    const need = !s.configured && !dr.ack;
    if (save) save.disabled = !this._isAdmin || !dr.dirty || !!problem || need;
    if (disc) disc.disabled = !this._isAdmin || !dr.dirty;
    const err = this._main.querySelector("#planerr");
    if (err) err.textContent = dr.dirty ? problem : "";
    const note = this._main.querySelector("#plannote");
    if (note) note.textContent = dr.dirty ? (need ? "Tick the box above to allow replacing the schedule on the feeder." : "Unsaved changes.") : "";
  }

  async _savePlan() {
    const s = this._sched[this._sel];
    const dr = this._drafts[this._sel];
    if (!s.configured) {
      const ok = await this._dialog({
        title: `Replace the schedule on ${s.name}?`,
        body: `<p>The schedule currently stored on the feeder (set in the PetLibro app) will be <strong>replaced</strong> by ${plural(
          dr.meals.filter((m) => m.enabled).length, "enabled meal")}. Home Assistant cannot restore the old one.</p>`,
        confirm: "Replace schedule",
        danger: true,
      });
      if (!ok) return;
    }
    const meals = dr.meals.map((m) => {
      const out = { time: m.time, portions: m.portions, days: [...m.days].sort(), enabled: m.enabled };
      if (m.label.trim()) out.label = m.label.trim();
      return out;
    });
    const res = await this._call(
      { type: "petlibro_lan/set_schedule", serial: this._sel, meals, replace_ack: s.configured || dr.ack }, "Schedule saved");
    if (res) {
      this._sched[this._sel] = res;
      this._drafts[this._sel] = { meals: this._editable(res.meals), dirty: false, ack: true };
      this._render();
    }
  }

  async _clearPlan() {
    const s = this._sched[this._sel];
    const got = await this._dialog({
      title: `Clear the schedule on ${s.name}?`,
      body: `<p>${esc(s.name)} will stop feeding automatically until you save a new schedule here or in the PetLibro app.</p>
        <label class="muted">Type <strong>${CLEAR_WORD}</strong> to confirm<br><input type="text" id="cw" autocomplete="off" autocapitalize="characters"></label>`,
      confirm: "Clear schedule",
      danger: true,
      check: (ov) => ov.querySelector("#cw").value.trim() === CLEAR_WORD,
      collect: (ov) => ov.querySelector("#cw").value.trim(),
    });
    if (!got) return;
    const res = await this._call({ type: "petlibro_lan/clear_schedule", serial: this._sel, confirm: got }, "Schedule cleared");
    if (res) {
      this._sched[this._sel] = res;
      this._drafts[this._sel] = { meals: [], dirty: false, ack: true };
      this._render();
    }
  }

  // --- alerts ------------------------------------------------------------------------
  async _loadAlerts() {
    const [a, t] = await Promise.all([
      this._call({ type: "petlibro_lan/get_alerts" }),
      this._call({ type: "petlibro_lan/notify_targets" }),
    ]);
    if (!a) return;
    this._alerts = a;
    this._targets = t ? t.targets : [];
    if (!this._draftA || !this._dirtyA()) {
      this._draftA = clone(a.settings);
      this._savedA = JSON.stringify(a.settings);
    }
    if (this._tab === "alerts") this._render();
  }

  _dirtyA() {
    return !!this._draftA && JSON.stringify(this._draftA) !== this._savedA;
  }

  _activeHtml() {
    const act = this._alerts?.active || [];
    if (!act.length) return `<p class="muted">No alerts right now.</p>`;
    return act.map((a) => `<div class="notice bad" style="margin-top:8px"><strong>${esc(a.title || a.type)}</strong><br>${esc(a.message)}
      <div class="muted">since ${esc(this._when(a.since * 1000))}</div></div>`).join("");
  }

  _toggle(path, on, label) {
    return `<button class="tgl" role="switch" aria-checked="${on}" data-path="${path}" aria-label="${esc(label)}"></button>`;
  }

  _num(path, value, min, max, step = 1, ph = "") {
    return `<input type="number" inputmode="decimal" min="${min}" max="${max}" step="${step}" value="${value ?? ""}" placeholder="${esc(ph)}" data-path="${path}">`;
  }

  _alertsHtml() {
    if (!this._alerts || !this._draftA) return `<div class="empty">Loading...</div>`;
    const s = this._draftA;
    const a = s.alerts;
    const known = new Set((this._targets || []).map((t) => t.id));
    const list = [...(this._targets || []), ...s.targets.filter((t) => !known.has(t)).map((id) => ({
      id, label: id.replace(/^service:/, "notify.").replace(/^entity:/, ""), name: "No longer available", gone: true }))];
    const targets = list.length
      ? list.map((t) => `<label class="chk"><input type="checkbox" data-target="${esc(t.id)}" ${s.targets.includes(t.id) ? "checked" : ""}>
          <span>${esc(t.name || t.label)}<span class="sub">${esc(t.label)}${t.gone ? " (not found)" : ""}</span></span></label>`).join("")
      : `<p class="muted">No notify services or entities were found in Home Assistant.</p>`;
    const row = (key, title, desc, params) => `<div class="arow"><div class="ah"><div class="t">${title}<div class="muted">${desc}</div></div>
      ${this._toggle(`alerts.${key}.enabled`, a[key].enabled, title)}</div>${params ? `<div class="ap">${params}</div>` : ""}</div>`;
    const remind = (key, label = "Remind every") => `<label>${label} ${this._num(`alerts.${key}.remind_hours`, a[key].remind_hours, 0, 168)} h (0 = never)</label>`;
    const wl = this._alerts.devices.filter((d) => d.kind === "fountain");
    const hint = wl.length && wl[0].device_low_water != null ? `${wl[0].device_low_water}` : "device";
    const dirty = this._dirtyA();
    const problem = this._alertProblem();
    return `<div class="box"><h2>Active alerts</h2><div id="active">${this._activeHtml()}</div></div>
      <div class="box"><h2>Send alerts to</h2>
        <div class="tlist">${targets}</div>
        <label class="chk"><input type="checkbox" data-path="persistent" ${s.persistent ? "checked" : ""}>
          <span>Also show a notification in Home Assistant<span class="sub">Appears in the sidebar bell and clears itself when the problem ends.</span></span></label>
      </div>
      <div class="box"><h2>What to alert on</h2>
        ${row("food_low", "Food low", "The feeder's hopper sensor reports low food.", remind("food_low"))}
        ${row("water_low", "Water low", "Fountain water weight is below the threshold.",
          `<label>Alert below ${this._num("alerts.water_low.threshold_g", a.water_low.threshold_g, 50, 20000, 10, hint)} g</label>${remind("water_low")}
           <span class="muted">Leave empty to use the fountain's own low-water value.</span>`)}
        ${row("filter_due", "Filter due", "Before the filter is due, and daily once it is overdue.",
          `<label>Warn ${this._num("alerts.filter_due.days_before", a.filter_due.days_before, 0, 60)} days before</label>
           <label>Overdue reminder at ${this._num("alerts.filter_due.remind_hour", a.filter_due.remind_hour, 0, 23)} :00</label>`)}
        ${row("offline", "Device offline", "A device has not been heard from.",
          `<label>For more than ${this._num("alerts.offline.minutes", a.offline.minutes, 5, 1440)} min</label>${remind("offline")}`)}
        ${row("feed_failed", "Feed failed or dispenser blocked", "Fewer portions came out than requested, or the feeder reported a jam.", "")}
        ${row("battery_low", "Fountain battery low", "Not sent while charging.",
          `<label>Below ${this._num("alerts.battery_low.percent", a.battery_low.percent, 1, 99)} %</label>${remind("battery_low")}`)}
        ${row("daily_summary", "Daily summary", "One message a day with feeds, food, water and filter status.",
          `<label>At <input type="time" value="${esc(a.daily_summary.time)}" data-path="alerts.daily_summary.time"></label>`)}
      </div>
      <div class="box"><h2>Quiet hours</h2>
        <div class="ah"><div class="t"><div class="muted">Pushes for filter, battery, offline and the summary wait until quiet hours end.
          Feed failed, food low and water low always go through. The Home Assistant notification is still shown.</div></div>
          ${this._toggle("quiet.enabled", s.quiet.enabled, "Quiet hours")}</div>
        <div class="ap"><label>From <input type="time" value="${esc(s.quiet.start)}" data-path="quiet.start"></label>
          <label>until <input type="time" value="${esc(s.quiet.end)}" data-path="quiet.end"></label></div>
      </div>
      <div class="box">
        <div class="acts" style="margin-top:0">
          <button class="btn" data-act="save-alerts" ${dirty && !problem ? "" : "disabled"}>Save</button>
          <button class="btn alt" data-act="discard-alerts" ${dirty ? "" : "disabled"}>Discard changes</button>
          <button class="btn alt" data-act="test" ${dirty ? "disabled title='Save first: the test uses the saved targets'" : ""}>Send test notification</button>
        </div>
        <div class="err" id="alerr" role="alert">${esc(problem)}</div>
        <div id="testres">${this._testHtml()}</div>
      </div>`;
  }

  _testHtml() {
    if (!this._testResults) return "";
    if (!this._testResults.length) return `<p class="muted">No targets selected: only the Home Assistant notification was created (if enabled).</p>`;
    return this._testResults.map((r) => `<div class="${r.ok ? "muted" : "err"}">${r.ok ? "Sent to" : "Failed:"} ${esc(r.target.replace(/^service:/, "notify."))}${r.ok ? "" : ` - ${esc(r.error)}`}</div>`).join("");
  }

  _alertProblem() {
    const a = this._draftA?.alerts;
    if (!a) return "";
    const whole = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
    if (!whole(a.filter_due.days_before, 0, 60)) return "Filter: days before must be 0-60.";
    if (!whole(a.filter_due.remind_hour, 0, 23)) return "Filter: reminder hour must be 0-23.";
    if (!whole(a.offline.minutes, 5, 1440)) return "Offline: minutes must be 5-1440.";
    if (!whole(a.battery_low.percent, 1, 99)) return "Battery: percent must be 1-99.";
    for (const k of ["food_low", "water_low", "offline", "battery_low"]) {
      if (!whole(a[k].remind_hours, 0, 168)) return "Reminder hours must be 0-168.";
    }
    const t = a.water_low.threshold_g;
    if (t !== null && !(typeof t === "number" && t >= 50 && t <= 20000)) return "Water threshold must be 50-20000 g, or empty.";
    return "";
  }

  _setPath(path, value) {
    const keys = path.split(".");
    let o = this._draftA;
    for (const k of keys.slice(0, -1)) o = o[k];
    o[keys[keys.length - 1]] = value;
  }

  _getPath(path) {
    return path.split(".").reduce((o, k) => o[k], this._draftA);
  }

  _refreshAlertButtons() {
    const dirty = this._dirtyA();
    const problem = this._alertProblem();
    const q = (a) => this._main.querySelector(`[data-act="${a}"]`);
    if (q("save-alerts")) q("save-alerts").disabled = !dirty || !!problem;
    if (q("discard-alerts")) q("discard-alerts").disabled = !dirty;
    if (q("test")) q("test").disabled = dirty;
    const err = this._main.querySelector("#alerr");
    if (err) err.textContent = problem;
  }

  async _saveAlerts() {
    const res = await this._call({ type: "petlibro_lan/set_alerts", settings: this._draftA }, "Alert settings saved");
    if (res) {
      this._alerts.settings = res.settings;
      this._alerts.active = res.active;
      this._draftA = clone(res.settings);
      this._savedA = JSON.stringify(res.settings);
      this._render();
    }
  }

  async _sendTest() {
    const btn = this._main.querySelector('[data-act="test"]');
    if (btn) btn.disabled = true;
    const res = await this._call({ type: "petlibro_lan/test_notification" });
    if (res) {
      this._testResults = res.results;
      const box = this._main.querySelector("#testres");
      if (box) box.innerHTML = this._testHtml();
      this._toast(res.results.every((r) => r.ok) ? "Test sent" : "Some targets failed", !res.results.every((r) => r.ok));
    }
    if (btn) btn.disabled = this._dirtyA();
  }

  // --- events ---------------------------------------------------------------------------
  _onClick(e) {
    const target = e.target.closest("button, input");
    if (!target || target.disabled) return;
    if (target.dataset.feeder) {
      this._sel = target.dataset.feeder;
      return this._render();
    }
    if (target.dataset.day) return this._planEdit(Number(target.dataset.i), "day", Number(target.dataset.day));
    if (target.dataset.f === "enabled") return this._planEdit(Number(target.dataset.i), "enabled");
    if (target.dataset.path && target.classList.contains("tgl")) {
      this._setPath(target.dataset.path, !this._getPath(target.dataset.path));
      return this._render();
    }
    switch (target.dataset.act) {
      case "feed": case "pump": case "light": case "reset_filter":
        return this._overviewAction(target);
      case "add-meal": {
        const dr = this._drafts[this._sel];
        dr.meals.push({ time: "08:00", portions: 1, days: [1, 2, 3, 4, 5, 6, 7], label: "", enabled: true });
        dr.dirty = true;
        return this._render(), this._refreshPlanState();
      }
      case "rm-meal": {
        const dr = this._drafts[this._sel];
        dr.meals.splice(Number(target.dataset.i), 1);
        dr.dirty = true;
        return this._render(), this._refreshPlanState();
      }
      case "discard": {
        this._drafts[this._sel] = { meals: this._editable(this._sched[this._sel].meals), dirty: false, ack: this._drafts[this._sel].ack };
        return this._render();
      }
      case "save-plan": return this._savePlan();
      case "clear-plan": return this._clearPlan();
      case "save-alerts": return this._saveAlerts();
      case "discard-alerts":
        this._draftA = JSON.parse(this._savedA);
        return this._render();
      case "test": return this._sendTest();
      default:
    }
  }

  _planEdit(i, field, value) {
    const dr = this._drafts[this._sel];
    const m = dr?.meals[i];
    if (!m) return;
    if (field === "day") {
      m.days = m.days.includes(value) ? m.days.filter((d) => d !== value) : [...m.days, value].sort();
    } else if (field === "enabled") {
      m.enabled = !m.enabled;
    } else if (field === "portions") {
      m.portions = Number(value);
    } else {
      m[field] = value;
    }
    dr.dirty = true;
    if (field === "day" || field === "enabled") this._render();
    this._refreshPlanState();
  }

  _onInput(e) {
    const t = e.target;
    if (t.id === "ack") {
      this._drafts[this._sel].ack = t.checked;
      return this._refreshPlanState();
    }
    if (t.dataset.f && t.dataset.f !== "enabled" && t.dataset.i !== undefined) {
      return this._planEdit(Number(t.dataset.i), t.dataset.f, t.value);
    }
    if (t.dataset.target) {
      const ts = new Set(this._draftA.targets);
      if (t.checked) ts.add(t.dataset.target);
      else ts.delete(t.dataset.target);
      this._draftA.targets = [...ts];
      return this._refreshAlertButtons();
    }
    if (t.dataset.path && t.type === "checkbox") {
      this._setPath(t.dataset.path, t.checked);
      return this._refreshAlertButtons();
    }
    if (t.dataset.path && t.type === "number") {
      const raw = t.value.trim();
      const isWater = t.dataset.path.endsWith("threshold_g");
      this._setPath(t.dataset.path, raw === "" ? (isWater ? null : NaN) : Number(raw));
      return this._refreshAlertButtons();
    }
    if (t.dataset.path && t.type === "time") {
      this._setPath(t.dataset.path, t.value);
      return this._refreshAlertButtons();
    }
  }
}

customElements.define("petlibro-local-panel", PetsPanel);
