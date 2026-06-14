/* ScanRelay dashboard v3 — all feature logic
 *
 * Features:
 *  1. ntfy push notifications (config + test push)
 *  2. Map view (Leaflet + Nominatim geocoding)
 *  3. Full-text search panel with date range
 *  4. System health card (polls /api/health every 10s)
 *  5. Config editor (full TOML sections)
 *  6. OTA updates (update banner, confirm + apply)
 *  7. Dark / light / auto theme + accent picker
 *  8. DVR scrubber (last-hour timeline strip)
 *  9. Auto-categorize (category pills from backend)
 * 10. Daily summaries tab
 *  +  All v2 features preserved
 */
(function () {
  "use strict";

  const MAX_FEED = 400;
  const STALE_AFTER_MS = 30 * 60 * 1000;

  // -----------------------------------------------------------------------
  // DOM refs
  const $ = id => document.getElementById(id);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const els = {
    body:       document.body,
    htmlEl:     document.documentElement,
    dot:        $("live-dot"),
    liveLabel:  $("live-label"),
    feed:       $("feed"),
    feedMeta:   $("feed-meta"),
    feedTitle:  $("feed-title"),
    feedFooter: $("feed-footer"),
    statToday:  $("stat-today"),
    statHits:   $("stat-hits"),
    statLastHit:$("stat-last-hit"),
    statUptime: $("stat-uptime"),
    statAudio:  $("stat-audio"),
    chart7:     $("chart7"),
    chartDays:  $("chart-days"),
    kChips:     $("keyword-chips"),
    toast:      $("toast"),
    matchAll:   $("match-all"),
    fab:        $("fab"),
    fabPause:   $("fab-pause"),
    fabLatest:  $("fab-latest"),
    fabBadge:   $("fab-badge"),
    fabLasthit: $("fab-lasthit"),
    fabDvr:     $("fab-dvr"),
    ptr:        $("ptr"),
    ptrLabel:   $("ptr-label"),
    drawer:     $("drawer"),
    drawerSound:$("drawer-sound"),
    updateBanner: $("update-banner"),
    updateBannerText: $("update-banner-text"),
    btnUpdateNow: $("btn-update-now"),
    btnUpdateDismiss: $("btn-update-dismiss"),
  };

  // -----------------------------------------------------------------------
  // Persisted store
  const store = {
    get(k, d) { try { const v = localStorage.getItem("sr3:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("sr3:" + k, JSON.stringify(v)); } catch {} },
  };

  // -----------------------------------------------------------------------
  // Settings
  const settings = {
    filter:   store.get("filter",   "all"),
    catFilter:store.get("catFilter", null),
    search:   "",
    sound:    store.get("sound",    false),
    autopause:store.get("autopause", false),
    density:  store.get("density",  "comfortable"),
    theme:    store.get("theme",    "dark"),
    accent:   store.get("accent",   "orange"),
  };
  applyTheme(settings.theme, settings.accent);
  els.body.dataset.density = settings.density;

  // -----------------------------------------------------------------------
  // State
  let allEvents   = [];
  let seenIds     = new Set();
  let paused      = false;
  let unseenHits  = 0;
  let unseenNew   = 0;
  let lastInterMs = Date.now();
  let chimeCtx    = null;
  let currentTab  = "live";
  let leafletMap  = null;
  let healthPollId= null;
  let dvrData     = [];
  let dvrAudio    = new Audio();
  let dvrPlaying  = null;

  const player = new Audio();
  let playingBtn = null;
  player.addEventListener("ended", () => stopPlayer());

  function stopPlayer() {
    if (playingBtn) playingBtn.classList.remove("playing");
    playingBtn = null;
  }

  // -----------------------------------------------------------------------
  // Helpers
  function fmtTime(ts) {
    if (!ts) return "--:--";
    const d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
  }
  function fmtTimeLong(ts) {
    if (!ts) return "--:--:--";
    return new Date(ts * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
  }
  function fmtRelative(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
    if (s < 60)    return s + "s ago";
    if (s < 3600)  return Math.floor(s/60) + "m ago";
    if (s < 86400) return Math.floor(s/3600) + "h ago";
    return Math.floor(s/86400) + "d ago";
  }
  function fmtUptime(sec) {
    if (sec == null || sec < 0) return "—";
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }
  function eventKey(ev) {
    return ev.id || `${ev.ts}-${(ev.text||"").slice(0,32)}`;
  }
  function showToast(msg, kind, ms) {
    els.toast.textContent = msg;
    els.toast.className = "toast" + (kind ? " " + kind : "");
    els.toast.hidden = false;
    void els.toast.offsetHeight;
    els.toast.classList.add("show");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      els.toast.classList.remove("show");
      setTimeout(() => { els.toast.hidden = true; }, 260);
    }, ms || 3000);
  }

  function setConn(state) {
    els.dot.classList.remove("live","stale","dead");
    const labels = {live:"live", stale:"reconnecting", dead:"offline"};
    els.dot.classList.add(state);
    if (els.liveLabel) els.liveLabel.textContent = labels[state] || state;
  }

  // -----------------------------------------------------------------------
  // Theme / accent
  function applyTheme(theme, accent) {
    const html = document.documentElement;
    if (theme === "auto") {
      const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      html.dataset.theme = dark ? "dark" : "light";
    } else {
      html.dataset.theme = theme;
    }
    html.dataset.accent = accent;
  }

  function setTheme(t) {
    settings.theme = t;
    store.set("theme", t);
    applyTheme(t, settings.accent);
    // sync all theme segmented controls
    $$("[id='set-theme'] button, #drawer-theme button").forEach(b => b.classList.toggle("active", b.dataset.val === t));
  }
  function setAccent(a) {
    settings.accent = a;
    store.set("accent", a);
    applyTheme(settings.theme, a);
    $$(".swatch").forEach(s => s.classList.toggle("active", s.dataset.accent === a));
  }

  // Init theme controls
  function initThemeControls() {
    $$("[id='set-theme'] button, #drawer-theme button").forEach(b => {
      b.classList.toggle("active", b.dataset.val === settings.theme);
      b.addEventListener("click", () => setTheme(b.dataset.val));
    });
    $$(".swatch").forEach(s => {
      s.classList.toggle("active", s.dataset.accent === settings.accent);
      s.addEventListener("click", () => setAccent(s.dataset.accent));
    });
  }

  // -----------------------------------------------------------------------
  // Tab routing
  function switchTab(tab) {
    currentTab = tab;
    $$(".tab-panel").forEach(p => p.hidden = true);
    const panel = $("tab-" + tab);
    if (panel) { panel.hidden = false; }

    $$(".rail-item").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));

    // Tab-specific init
    if (tab === "map")       initMap();
    if (tab === "health")    startHealthPoll();
    if (tab === "config")    loadConfig();
    if (tab === "summaries") loadSummaries();
    if (tab !== "health")    stopHealthPoll();
  }

  $$(".rail-item").forEach(b => {
    b.addEventListener("click", () => {
      switchTab(b.dataset.tab);
      // Close mobile rail
      if (window.innerWidth <= 768) {
        closeMobileRail();
      }
    });
  });

  // Hamburger (mobile rail)
  const rail = $("rail");
  const btnHamburger = $("btn-hamburger");
  let railScrim = null;

  function openMobileRail() {
    rail.classList.add("open");
    btnHamburger.classList.add("open");
    btnHamburger.setAttribute("aria-expanded", "true");
    railScrim = document.createElement("div");
    railScrim.className = "rail-scrim";
    railScrim.addEventListener("click", closeMobileRail);
    document.body.appendChild(railScrim);
  }
  function closeMobileRail() {
    rail.classList.remove("open");
    btnHamburger.classList.remove("open");
    btnHamburger.setAttribute("aria-expanded", "false");
    if (railScrim) { railScrim.remove(); railScrim = null; }
  }
  btnHamburger.addEventListener("click", () => {
    if (rail.classList.contains("open")) closeMobileRail();
    else openMobileRail();
  });

  // -----------------------------------------------------------------------
  // Keyword highlight
  function highlightText(text, keyword) {
    const safe = esc(text || "");
    if (!keyword) return safe;
    const re = new RegExp(`(${esc(keyword).replace(/[-\/\\^$*+?.()|[\]{}]/g,"\\$&")})`, "ig");
    return safe.replace(re, '<span class="kw-hi">$1</span>');
  }

  // -----------------------------------------------------------------------
  // Event row rendering
  function renderEvent(ev, opts) {
    const li = document.createElement("li");
    const fresh = opts && opts.fresh;
    li.className = "event" + (ev.hit ? " hit" : "") + (fresh ? " fresh" : "");
    li.dataset.id  = eventKey(ev);
    li.dataset.hit = ev.hit ? "1" : "0";
    li.dataset.ts  = ev.ts || 0;
    li.dataset.text = (ev.text || "").toLowerCase();
    li.dataset.cat  = ev.category || "UNKNOWN";

    const htmlText = highlightText(ev.text || "(no transcript)", ev.keyword);
    const hasAudio = !!ev.audio_file;
    const cat = ev.category || "UNKNOWN";

    li.innerHTML = `
      <span class="ev-time">${fmtTime(ev.ts)}</span>
      <span class="ev-badge ${ev.hit ? "hit" : "miss"}">${ev.hit ? "HIT" : "MISS"}</span>
      <span class="ev-cat"><span class="cat-pill ${esc(cat)}">${esc(cat)}</span></span>
      <button class="ev-play" type="button" title="${hasAudio ? "Play clip" : "No audio"}" ${hasAudio ? "" : "disabled"} aria-label="Play">
        <svg viewBox="0 0 24 24" class="ico-play"><path d="M7 5v14l12-7z" fill="currentColor"/></svg>
        <svg viewBox="0 0 24 24" class="ico-pause"><rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor"/><rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor"/></svg>
      </button>
      <div class="ev-text">${htmlText}</div>
      <div class="ev-expand">
        <div class="ev-meta">
          ${fmtTimeLong(ev.ts)}
          ${ev.duration != null ? ` · ${Number(ev.duration).toFixed(1)}s` : ""}
          ${ev.hit && ev.keyword ? ` · <span class="kw-name">match: ${esc(ev.keyword)}</span>` : ""}
          · <span class="cat-pill ${esc(cat)}">${esc(cat)}</span>
        </div>
        <div class="ev-actions">
          <button class="link-btn" data-action="copy">Copy text</button>
          ${hasAudio ? `<a class="link-btn" href="/api/audio/${encodeURIComponent(ev.audio_file)}" download>Download audio</a>` : ""}
          <button class="link-btn" data-action="share">Share</button>
        </div>
      </div>`;
    return li;
  }

  // Feed event delegation
  els.feed.addEventListener("click", e => {
    const li = e.target.closest(".event");
    if (!li) return;
    const playBtn = e.target.closest(".ev-play");
    const action  = e.target.closest("[data-action]");
    const link    = e.target.closest("a");
    if (playBtn)  { handlePlay(li, playBtn); return; }
    if (link)     return;
    if (action)   {
      const ev = allEvents.find(x => eventKey(x) === li.dataset.id);
      if (!ev) return;
      if (action.dataset.action === "copy")  handleCopy(ev);
      if (action.dataset.action === "share") handleShare(ev);
      return;
    }
    li.classList.toggle("expanded");
  });

  function handlePlay(li, btn) {
    const ev = allEvents.find(x => eventKey(x) === li.dataset.id);
    if (!ev || !ev.audio_file) return;
    if (playingBtn === btn) { player.pause(); stopPlayer(); return; }
    if (playingBtn) { player.pause(); stopPlayer(); }
    player.src = `/api/audio/${encodeURIComponent(ev.audio_file)}`;
    player.play().then(() => {
      playingBtn = btn; btn.classList.add("playing");
    }).catch(err => showToast("Couldn't play: " + (err.message || err), "err"));
  }

  function handleCopy(ev) {
    const text = `[${fmtTimeLong(ev.ts)}] ${ev.text || ""}`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => showToast("Copied", "ok")).catch(() => showToast("Copy failed", "err"));
    } else {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); showToast("Copied", "ok"); } catch { showToast("Copy failed","err"); }
      ta.remove();
    }
  }

  function handleShare(ev) {
    const text = `[${fmtTimeLong(ev.ts)}] ${ev.text || ""}`;
    if (navigator.share) {
      navigator.share({ title: ev.hit ? `ScanRelay HIT · ${ev.keyword}` : "ScanRelay", text }).catch(()=>{});
    } else {
      handleCopy(ev);
    }
  }

  // -----------------------------------------------------------------------
  // Filtering
  function applyFilter() {
    const now = Date.now() / 1000;
    let cutoff = 0;
    if (settings.filter === "hour") cutoff = now - 3600;
    if (settings.filter === "24h")  cutoff = now - 86400;
    const onlyHits = settings.filter === "hits";
    const needle   = settings.search.trim().toLowerCase();
    const catF     = settings.catFilter;

    let shown = 0;
    Array.from(els.feed.children).forEach(el => {
      if (!el.dataset || !el.dataset.id) return;
      const isHit = el.dataset.hit === "1";
      const ts    = Number(el.dataset.ts);
      const text  = el.dataset.text || "";
      const cat   = el.dataset.cat  || "";
      let hide = false;
      if (onlyHits && !isHit)          hide = true;
      if (cutoff && ts < cutoff)        hide = true;
      if (needle && !text.includes(needle)) hide = true;
      if (catF  && cat !== catF)        hide = true;
      el.style.display = hide ? "none" : "";
      if (!hide) shown++;
    });
    updateFooter(shown);

    $$(".chips .chip[data-filter]").forEach(c => c.classList.toggle("active", c.dataset.filter === settings.filter));
    $$(".chips .chip.cat-chip").forEach(c => c.classList.toggle("active", c.dataset.cat === settings.catFilter));

    els.feedTitle.textContent =
        settings.filter === "hits" ? "Live feed · hits only"
      : settings.filter === "hour" ? "Live feed · last hour"
      : settings.filter === "24h"  ? "Live feed · last 24h"
      : settings.catFilter         ? `Live feed · ${settings.catFilter}`
      : "Live feed";
  }

  function applyFilterToRow(el) {
    const now = Date.now() / 1000;
    let cutoff = 0;
    if (settings.filter === "hour") cutoff = now - 3600;
    if (settings.filter === "24h")  cutoff = now - 86400;
    const onlyHits = settings.filter === "hits";
    const needle   = settings.search.trim().toLowerCase();
    const catF     = settings.catFilter;
    let hide = false;
    if (onlyHits && el.dataset.hit !== "1")     hide = true;
    if (cutoff && Number(el.dataset.ts) < cutoff) hide = true;
    if (needle && !(el.dataset.text||"").includes(needle)) hide = true;
    if (catF && el.dataset.cat !== catF)         hide = true;
    el.style.display = hide ? "none" : "";
  }

  function updateFooter(vis) {
    if (!allEvents.length) { els.feedFooter.textContent = ""; return; }
    if (vis == null) vis = Array.from(els.feed.children).filter(el => el.dataset.id && el.style.display !== "none").length;
    const cap = allEvents.length >= MAX_FEED ? ` · cap ${MAX_FEED}` : "";
    els.feedFooter.textContent = `${vis} shown · ${allEvents.length} loaded${cap}`;
  }

  function renderEmpty() {
    els.feed.innerHTML = `
      <li class="empty">
        <div class="empty-icon">📡</div>
        <div>Listening for the next transmission…</div>
        <div class="pulse-dots"><span></span><span></span><span></span></div>
      </li>`;
  }

  // Filter chips
  $("filter-chips").addEventListener("click", e => {
    const chip = e.target.closest(".chip[data-filter]");
    const catChip = e.target.closest(".chip.cat-chip");
    if (chip) {
      settings.filter    = chip.dataset.filter;
      settings.catFilter = null;
      store.set("filter", settings.filter);
      store.set("catFilter", null);
      applyFilter();
    } else if (catChip) {
      const c = catChip.dataset.cat;
      settings.catFilter = settings.catFilter === c ? null : c;
      settings.filter = "all";
      store.set("catFilter", settings.catFilter);
      store.set("filter", "all");
      applyFilter();
    }
  });

  // -----------------------------------------------------------------------
  // Add events
  function addEventToTop(ev, fresh) {
    const key = eventKey(ev);
    if (seenIds.has(key)) return false;
    seenIds.add(key);
    const empty = els.feed.querySelector(".empty");
    if (empty) empty.remove();
    allEvents.unshift(ev);
    const li = renderEvent(ev, {fresh: !!fresh});
    els.feed.insertBefore(li, els.feed.firstChild);
    while (allEvents.length > MAX_FEED) {
      allEvents.pop();
      const last = els.feed.lastElementChild;
      if (last) last.remove();
    }
    applyFilterToRow(li);
    updateFooter();
    if (fresh) {
      if (window.scrollY > 240) {
        unseenNew++;
        if (ev.hit) unseenHits++;
        updateBadge(); updateTitle();
      }
      updateTitle();
      if (ev.hit) {
        showToast(`HIT ${fmtTime(ev.ts)} · ${ev.keyword || "match"}`, "ok");
        if (settings.sound) playChime();
      }
    }
    return true;
  }

  // -----------------------------------------------------------------------
  // Initial load
  async function loadInitial() {
    try {
      const r = await fetch("/api/events?limit=200");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const events = await r.json();
      els.feed.innerHTML = "";
      allEvents = []; seenIds = new Set();
      if (!events.length) { renderEmpty(); } else {
        events.forEach(ev => seenIds.add(eventKey(ev)));
        allEvents = events.slice();
        const frag = document.createDocumentFragment();
        events.forEach(ev => frag.appendChild(renderEvent(ev)));
        els.feed.appendChild(frag);
      }
      applyFilter();
      els.feedMeta.textContent = events.length ? "loaded" : "waiting…";
    } catch(e) {
      els.feedMeta.textContent = "load failed";
      showToast("Couldn't load events: " + e.message, "err");
    }
  }

  // -----------------------------------------------------------------------
  // SSE
  let sse = null, sseRetry = 1000;
  function startStream() {
    if (paused) return;
    try { if (sse) sse.close(); } catch(_) {}
    setConn("stale");
    sse = new EventSource("/api/stream");
    sse.onopen  = () => { setConn("live"); els.feedMeta.textContent = "live"; sseRetry = 1000; };
    sse.onmessage = msg => {
      if (!msg.data) return;
      let ev; try { ev = JSON.parse(msg.data); } catch(_) { return; }
      addEventToTop(ev, true);
    };
    sse.onerror = () => {
      setConn("stale"); els.feedMeta.textContent = "reconnecting…";
      try { sse.close(); } catch(_) {}
      setTimeout(() => { if (!paused) startStream(); }, Math.min(sseRetry, 15000));
      sseRetry = Math.min(sseRetry * 2, 15000);
    };
  }
  function stopStream() {
    try { if (sse) sse.close(); } catch(_) {}
    sse = null; setConn("dead"); els.feedMeta.textContent = "paused";
  }

  // -----------------------------------------------------------------------
  // Stats + summary
  async function refreshStats() {
    try {
      const r = await fetch("/api/stats");
      if (!r.ok) return;
      const s = await r.json();
      setCountUp(els.statToday, s.total_today ?? "—");
      setCountUp(els.statHits,  s.hits_today  ?? "—");
      els.statLastHit.textContent = s.last_hit_ts ? "last hit " + fmtRelative(s.last_hit_ts) : "no hits today";
      els.statUptime.textContent  = fmtUptime(s.uptime_seconds);
      els.statAudio.textContent   = `${s.audio_files || 0} clips · ${s.audio_mb || 0} MB`;
    } catch(_) {}
  }

  function setCountUp(el, val) {
    if (typeof val !== "number") { el.textContent = val; return; }
    const prev = parseInt(el.textContent) || 0;
    if (prev === val) return;
    el.textContent = val;
    el.style.color = "var(--accent)";
    setTimeout(() => { el.style.color = ""; }, 400);
  }

  async function refreshSummary() {
    try {
      const r = await fetch("/api/summary");
      if (!r.ok) return;
      const s = await r.json();
      drawChart(s.days || []);
      drawKeywordChips(s.today_keywords || []);
    } catch(_) {}
  }

  function drawChart(days) {
    const canvas = els.chart7;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.offsetWidth || 400;
    const H = 72;
    canvas.width  = W * window.devicePixelRatio;
    canvas.height = H * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    const n = days.length || 7;
    const max = Math.max(1, ...days.map(d => d.total));
    const colW = W / n;
    const bw = colW * 0.62;
    const pad = 4;

    const style = getComputedStyle(document.documentElement);
    const missColor = style.getPropertyValue("--miss").trim() || "#3a4560";
    const hitColor  = style.getPropertyValue("--hit").trim()  || "#fb923c";

    ctx.clearRect(0, 0, W, H);

    days.forEach((d, i) => {
      const totalH = ((d.total / max) * (H - pad * 2));
      const hitsH  = ((d.hits  / max) * (H - pad * 2));
      const x = i * colW + (colW - bw) / 2;

      // Area fill under total
      const grad = ctx.createLinearGradient(0, H - pad - totalH, 0, H);
      grad.addColorStop(0, missColor + "88");
      grad.addColorStop(1, missColor + "11");
      ctx.beginPath();
      ctx.moveTo(x, H - pad);
      ctx.lineTo(x, H - pad - totalH);
      ctx.lineTo(x + bw, H - pad - totalH);
      ctx.lineTo(x + bw, H - pad);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // Total bar
      ctx.fillStyle = missColor;
      ctx.globalAlpha = 0.7;
      roundRect(ctx, x, H - pad - totalH, bw, Math.max(totalH, 2), 2);
      ctx.fill();

      // Hits bar
      if (hitsH > 0) {
        ctx.fillStyle = hitColor;
        ctx.globalAlpha = 1;
        roundRect(ctx, x, H - pad - hitsH, bw, Math.max(hitsH, 2), 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    });

    const dow = ["S","M","T","W","T","F","S"];
    els.chartDays.innerHTML = days.map(d => {
      try { const dt = new Date(d.date + "T00:00:00"); return `<div>${dow[dt.getDay()]}</div>`; }
      catch { return "<div>·</div>"; }
    }).join("");
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h);
    ctx.lineTo(x, y + h);
    ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r);
    ctx.closePath();
  }

  function drawKeywordChips(items) {
    if (!items.length) { els.kChips.innerHTML = ""; return; }
    els.kChips.innerHTML = items.map(it =>
      `<span class="kw-chip">${esc(it.keyword)}<strong>${it.count}</strong></span>`
    ).join("");
  }

  // -----------------------------------------------------------------------
  // Config (match_all initial state)
  async function refreshConfig() {
    try {
      const r = await fetch("/api/config");
      if (!r.ok) return;
      const c = await r.json();
      if (typeof c.filter?.match_all === "boolean") els.matchAll.checked = c.filter.match_all;
    } catch(_) {}
  }

  // Match-all toggle
  let matchBusy = false;
  els.matchAll.addEventListener("change", async () => {
    if (matchBusy) return;
    matchBusy = true;
    const target = els.matchAll.checked;
    els.matchAll.disabled = true;
    showToast(target ? "Match-all on — restarting daemon…" : "Filter on — restarting daemon…");
    try {
      const r = await fetch("/api/match_all", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({enabled: target}),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.restart_ok) showToast(target ? "Match-all ON." : "Filter ON.", "ok");
      else showToast("Config saved, restart failed: " + (data.restart_error || "?"), "warn");
    } catch(e) {
      els.matchAll.checked = !target;
      showToast("Toggle failed: " + e.message, "err");
    } finally { els.matchAll.disabled = false; matchBusy = false; }
  });

  // -----------------------------------------------------------------------
  // FAB
  els.fabPause.addEventListener("click", () => {
    paused = !paused;
    els.fabPause.classList.toggle("paused", paused);
    const [ip, ipp] = els.fabPause.querySelectorAll("svg");
    if (paused) { ip.hidden = true; ipp.hidden = false; stopStream(); showToast("Feed paused", "warn"); }
    else        { ip.hidden = false; ipp.hidden = true; startStream(); showToast("Feed resumed", "ok"); }
  });
  els.fabLatest.addEventListener("click", () => {
    window.scrollTo({top:0, behavior:"smooth"});
    unseenNew = 0; unseenHits = 0; updateBadge(); updateTitle();
  });
  els.fabLasthit.addEventListener("click", () => {
    const hit = els.feed.querySelector('.event.hit[style=""], .event.hit:not([style])');
    if (!hit) { showToast("No hits to jump to", "warn"); return; }
    hit.scrollIntoView({behavior:"smooth", block:"center"});
    hit.classList.add("fresh");
    setTimeout(() => hit.classList.remove("fresh"), 2200);
  });

  function updateBadge() {
    if (unseenNew > 0) { els.fabBadge.hidden = false; els.fabBadge.textContent = unseenNew > 99 ? "99+" : String(unseenNew); }
    else               { els.fabBadge.hidden = true; }
  }
  function updateTitle() {
    document.title = unseenHits > 0 ? `(${unseenHits} hit${unseenHits > 1 ? "s" : ""}) ScanRelay` : "ScanRelay";
  }

  window.addEventListener("scroll", () => {
    if (window.scrollY < 80 && (unseenNew || unseenHits)) {
      unseenNew = 0; unseenHits = 0; updateBadge(); updateTitle();
    }
    lastInterMs = Date.now();
  }, {passive:true});
  ["click","keydown","touchstart"].forEach(e => document.addEventListener(e, () => { lastInterMs = Date.now(); }, {passive:true}));
  setInterval(() => {
    if (settings.autopause && !paused && (Date.now() - lastInterMs) > STALE_AFTER_MS) {
      paused = true; els.fabPause.classList.add("paused");
      const [ip,ipp] = els.fabPause.querySelectorAll("svg");
      ip.hidden = true; ipp.hidden = false; stopStream();
      showToast("Auto-paused after 30 min idle", "warn", 4000);
    }
  }, 60000);

  // -----------------------------------------------------------------------
  // Settings drawer
  $("btn-settings").addEventListener("click", () => openDrawer());
  document.addEventListener("click", e => { if (e.target.closest("[data-close-drawer]")) closeDrawer(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !els.drawer.hidden) closeDrawer(); });

  function openDrawer() { els.drawer.hidden = false; els.drawer.setAttribute("aria-hidden","false"); document.body.style.overflow = "hidden"; refreshHealthMini(); }
  function closeDrawer() { els.drawer.hidden = true; els.drawer.setAttribute("aria-hidden","true"); document.body.style.overflow = ""; }

  // Sound in drawer
  if (els.drawerSound) {
    els.drawerSound.checked = settings.sound;
    els.drawerSound.addEventListener("change", () => {
      settings.sound = els.drawerSound.checked;
      store.set("sound", settings.sound);
      const cfg_s = $("set-sound"); if (cfg_s) cfg_s.checked = settings.sound;
      if (settings.sound) { try { initChime(); playChime(0.05); } catch{} showToast("Sound ON","ok"); }
      else showToast("Sound off");
    });
  }

  // -----------------------------------------------------------------------
  // Chime
  function initChime() {
    if (chimeCtx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    chimeCtx = new AC();
  }
  function playChime(vol) {
    try {
      initChime(); if (!chimeCtx) return;
      if (chimeCtx.state === "suspended") chimeCtx.resume();
      const ctx = chimeCtx, now = ctx.currentTime;
      [[880,0],[1320,0.10]].forEach(([freq,off]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = "sine"; o.frequency.value = freq;
        g.gain.setValueAtTime(0, now+off);
        g.gain.linearRampToValueAtTime(vol != null ? vol : 0.15, now+off+0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, now+off+0.32);
        o.connect(g).connect(ctx.destination);
        o.start(now+off); o.stop(now+off+0.34);
      });
    } catch{}
  }

  // -----------------------------------------------------------------------
  // Pull-to-refresh
  let ptrY = 0, ptrD = 0, ptrActive = false;
  document.addEventListener("touchstart", e => {
    if (window.scrollY > 0) return;
    ptrY = e.touches[0].clientY; ptrActive = true; ptrD = 0;
  }, {passive:true});
  document.addEventListener("touchmove", e => {
    if (!ptrActive) return;
    ptrD = e.touches[0].clientY - ptrY;
    if (ptrD > 0 && window.scrollY === 0) {
      els.ptr.hidden = false;
      const px = Math.min(ptrD * 0.6, 80);
      els.ptr.style.transform = `translate(-50%, ${px-80}px)`;
      els.ptr.classList.toggle("show", px > 20);
      els.ptrLabel.textContent = ptrD > 90 ? "Release to refresh" : "Pull to refresh";
    }
  }, {passive:true});
  document.addEventListener("touchend", () => {
    if (!ptrActive) return; ptrActive = false;
    if (ptrD > 90) {
      els.ptrLabel.textContent = "Refreshing…";
      Promise.all([loadInitial(), refreshStats(), refreshSummary(), refreshConfig()])
        .finally(() => {
          setTimeout(() => {
            els.ptr.classList.remove("show");
            setTimeout(() => { els.ptr.hidden = true; els.ptr.style.transform = ""; }, 200);
          }, 300);
        });
    } else {
      els.ptr.classList.remove("show");
      setTimeout(() => { els.ptr.hidden = true; els.ptr.style.transform = ""; }, 200);
    }
    ptrD = 0;
  }, {passive:true});

  // -----------------------------------------------------------------------
  // Feature 6: OTA update banner
  async function checkVersion() {
    try {
      const r = await fetch("/api/version");
      if (!r.ok) return;
      const v = await r.json();
      if (v.update_available && v.behind > 0) {
        els.updateBannerText.textContent = `Update available — ${v.behind} commit${v.behind > 1 ? "s" : ""} behind (${v.latest_remote || ""})`;
        els.updateBanner.hidden = false;
      }
    } catch(_) {}
  }

  if (els.btnUpdateNow) {
    els.btnUpdateNow.addEventListener("click", async () => {
      if (!confirm("Update ScanRelay now? The dashboard will restart.")) return;
      els.btnUpdateNow.textContent = "Updating…";
      els.btnUpdateNow.disabled = true;
      try {
        const r = await fetch("/api/update", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({confirm:"yes"}),
        });
        const d = await r.json();
        if (!r.ok) { showToast("Update failed: " + (d.detail || "?"), "err"); return; }
        showToast("Update applied! Reloading in 5s…", "ok", 6000);
        setTimeout(() => window.location.reload(), 5000);
      } catch(e) {
        showToast("Update error: " + e.message, "err");
      } finally {
        els.btnUpdateNow.disabled = false; els.btnUpdateNow.textContent = "Update now";
      }
    });
  }
  if (els.btnUpdateDismiss) {
    els.btnUpdateDismiss.addEventListener("click", () => { els.updateBanner.hidden = true; });
  }

  // -----------------------------------------------------------------------
  // Feature 2: Map view (Leaflet)
  function initMap() {
    if (leafletMap) return;
    const container = $("map-container");
    if (!container || typeof L === "undefined") return;

    leafletMap = L.map(container).setView([39.5, -98.35], 4);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(leafletMap);

    loadMapMarkers();
  }

  const catColors = {FIRE:"#f87171", MEDICAL:"#34d399", TRAFFIC:"#fbbf24", WEATHER:"#60a5fa", DISPATCH:"#a78bfa", UNKNOWN:"#64748b"};

  function mkIcon(cat) {
    const color = catColors[cat] || catColors.UNKNOWN;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="28" viewBox="0 0 22 28"><ellipse cx="11" cy="11" rx="10" ry="10" fill="${color}" stroke="#fff" stroke-width="2"/><line x1="11" y1="21" x2="11" y2="28" stroke="${color}" stroke-width="2"/></svg>`;
    return L.divIcon({html: svg, iconSize:[22,28], iconAnchor:[11,28], popupAnchor:[0,-28], className:""});
  }

  let mapMarkers = [];
  async function loadMapMarkers() {
    if (!leafletMap) return;
    mapMarkers.forEach(m => m.remove());
    mapMarkers = [];
    try {
      const r = await fetch("/api/map/markers");
      if (!r.ok) return;
      const d = await r.json();
      if (d.center) {
        leafletMap.setView([d.center.lat, d.center.lon], d.center.zoom);
      }
      (d.markers || []).forEach(m => {
        const marker = L.marker([m.lat, m.lon], {icon: mkIcon(m.category)})
          .bindPopup(`<b>${esc(m.addr||"")}</b><br>${esc((m.text||"").slice(0,120))}`)
          .addTo(leafletMap);
        marker.on("click", () => {
          // Jump to event in live feed
          switchTab("live");
          setTimeout(() => {
            const el = els.feed.querySelector(`[data-id="${CSS.escape(m.id||"")}"]`);
            if (el) { el.scrollIntoView({behavior:"smooth", block:"center"}); el.classList.add("fresh"); setTimeout(()=>el.classList.remove("fresh"),2000); }
          }, 300);
        });
        mapMarkers.push(marker);
      });
    } catch(e) { console.warn("Map markers error:", e); }
  }

  const btnMapRefresh = $("btn-map-refresh");
  if (btnMapRefresh) btnMapRefresh.addEventListener("click", loadMapMarkers);

  // -----------------------------------------------------------------------
  // Feature 3: Full-text search
  const searchQ    = $("search-q");
  const searchFrom = $("search-from");
  const searchTo   = $("search-to");
  const btnSearchGo = $("btn-search-go");
  const searchResultsList = $("search-results");
  const searchResultsHeader = $("search-results-header");

  if (btnSearchGo) {
    btnSearchGo.addEventListener("click", runSearch);
    searchQ.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
  }

  async function runSearch() {
    const q = searchQ ? searchQ.value.trim() : "";
    if (!q) { showToast("Enter a search term", "warn"); return; }
    const params = new URLSearchParams({q});
    if (searchFrom && searchFrom.value) params.set("from_", searchFrom.value);
    if (searchTo   && searchTo.value)   params.set("to",   searchTo.value);
    btnSearchGo.textContent = "Searching…";
    btnSearchGo.disabled = true;
    try {
      const r = await fetch("/api/search?" + params.toString());
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      searchResultsList.innerHTML = "";
      if (searchResultsHeader) {
        searchResultsHeader.textContent = `${d.total} match${d.total !== 1 ? "es" : ""} across ${d.days} day${d.days !== 1 ? "s" : ""}`;
        searchResultsHeader.hidden = false;
      }
      if (!d.results.length) {
        searchResultsList.innerHTML = `<li class="empty"><div class="empty-icon">🔍</div><div>No results for "${esc(q)}"</div></li>`;
      } else {
        const frag = document.createDocumentFragment();
        d.results.forEach(ev => {
          const li = renderEvent({...ev, keyword: ev.keyword || q});
          frag.appendChild(li);
        });
        searchResultsList.appendChild(frag);
      }
    } catch(e) { showToast("Search failed: " + e.message, "err"); }
    finally { btnSearchGo.textContent = "Search"; btnSearchGo.disabled = false; }
  }

  // -----------------------------------------------------------------------
  // Feature 4: System health
  function startHealthPoll() {
    refreshHealth();
    if (!healthPollId) healthPollId = setInterval(refreshHealth, 10000);
  }
  function stopHealthPoll() {
    if (healthPollId) { clearInterval(healthPollId); healthPollId = null; }
  }

  async function refreshHealth() {
    try {
      const r = await fetch("/api/health");
      if (!r.ok) return;
      const h = await r.json();
      renderHealthGrid(h);
      renderHealthMini(h);
      const label = $("health-refresh-label");
      if (label) label.textContent = "updated " + new Date().toLocaleTimeString();
    } catch(_) {}
  }

  function renderHealthGrid(h) {
    const grid = $("health-grid");
    if (!grid) return;

    const cpuPct  = h.cpu_percent != null ? Math.round(h.cpu_percent) : null;
    const ramPct  = h.mem_total_mb ? Math.round((h.mem_used_mb / h.mem_total_mb) * 100) : null;
    const diskPct = h.disk_total_gb ? Math.round((h.disk_used_gb / h.disk_total_gb) * 100) : null;

    const cards = [
      { label:"CPU Usage", val: cpuPct != null ? cpuPct + "%" : "—", sub: "", pct: cpuPct },
      { label:"Temperature", val: h.cpu_temp_c != null ? h.cpu_temp_c.toFixed(1) + " °C" : "—", sub:"", pct: null },
      { label:"Memory", val: ramPct != null ? ramPct + "%" : "—", sub: h.mem_used_mb != null ? `${h.mem_used_mb} / ${h.mem_total_mb} MB` : "", pct: ramPct },
      { label:"Disk", val: diskPct != null ? diskPct + "%" : "—", sub: h.disk_used_gb != null ? `${h.disk_used_gb} / ${h.disk_total_gb} GB` : "", pct: diskPct },
      { label:"Audio Cache", val: h.audio_dir_mb != null ? h.audio_dir_mb.toFixed(1) + " MB" : "—", sub:"", pct: null },
      { label:"Events Total", val: h.events_count ?? "—", sub: `queue: ${h.queue_depth_estimate ?? "?"} pending`, pct: null },
      { label:"Daemon Uptime", val: fmtUptime(h.daemon_uptime_seconds), sub:"", pct: null },
      { label:"Dashboard Up", val: fmtUptime(h.dashboard_uptime_seconds), sub:"", pct: null },
      { label:"Last Event", val: h.last_event_ago_seconds != null ? fmtRelative(Math.floor(Date.now()/1000 - h.last_event_ago_seconds)) : "—", sub:"", pct: null },
    ];

    grid.innerHTML = cards.map(c => {
      const barClass = c.pct > 90 ? "err" : c.pct > 70 ? "warn" : "";
      return `<div class="health-card">
        <div class="hc-label">${esc(c.label)}</div>
        <div class="hc-value">${esc(String(c.val))}</div>
        ${c.sub ? `<div class="hc-sub">${esc(c.sub)}</div>` : ""}
        ${c.pct != null ? `<div class="hc-bar-wrap"><div class="hc-bar ${barClass}" style="width:${c.pct}%"></div></div>` : ""}
      </div>`;
    }).join("");
  }

  function renderHealthMini(h) {
    const cpuPct  = h.cpu_percent != null ? Math.round(h.cpu_percent) : null;
    const ramPct  = h.mem_total_mb ? Math.round((h.mem_used_mb / h.mem_total_mb) * 100) : null;
    const diskPct = h.disk_total_gb ? Math.round((h.disk_used_gb / h.disk_total_gb) * 100) : null;

    const setCpuBar  = el => el && (el.style.width = (cpuPct || 0) + "%");
    const setRamBar  = el => el && (el.style.width = (ramPct  || 0) + "%");
    const setDiskBar = el => el && (el.style.width = (diskPct || 0) + "%");
    setCpuBar($("hm-cpu")); setRamBar($("hm-ram")); setDiskBar($("hm-disk"));
    const cpuVal = $("hm-cpu-val"); if (cpuVal) cpuVal.textContent = cpuPct != null ? cpuPct + "%" : "—";
    const ramVal = $("hm-ram-val"); if (ramVal) ramVal.textContent = ramPct != null ? ramPct + "%" : "—";
    const diskVal = $("hm-disk-val"); if (diskVal) diskVal.textContent = diskPct != null ? diskPct + "%" : "—";
    const tempEl = $("hm-temp"); if (tempEl) tempEl.textContent = h.cpu_temp_c != null ? h.cpu_temp_c.toFixed(1) + "°C" : "—";
    const lastEl = $("hm-last-ev"); if (lastEl) lastEl.textContent = h.last_event_ago_seconds != null ? Math.round(h.last_event_ago_seconds) + "s ago" : "—";
  }

  async function refreshHealthMini() {
    try {
      const r = await fetch("/api/health"); if (!r.ok) return;
      renderHealthMini(await r.json());
    } catch(_) {}
  }

  // -----------------------------------------------------------------------
  // Feature 5: Config editor
  let _configData = null;

  async function loadConfig() {
    try {
      const r = await fetch("/api/config"); if (!r.ok) return;
      _configData = await r.json();
      populateConfigForm(_configData);
    } catch(e) { showToast("Config load failed: " + e.message, "err"); }
  }

  function populateConfigForm(c) {
    const filt = c.filter || {};
    const ntfy = c.ntfy   || {};
    const map  = c.map    || {};
    const qh   = c.quiet_hours || {};
    const sum  = c.summary     || {};

    const kwEl = $("cfg-keywords");
    if (kwEl) kwEl.value = (filt.keywords || []).join("\n");
    const maEl = $("cfg-match-all");
    if (maEl) maEl.checked = !!filt.match_all;

    const neEl = $("cfg-ntfy-enabled");
    if (neEl) neEl.checked = !!ntfy.enabled;
    const ntEl = $("cfg-ntfy-topic");
    if (ntEl) ntEl.value = ntfy.topic || "";
    const npEl = $("cfg-ntfy-priority");
    if (npEl) npEl.value = ntfy.priority ?? 4;

    const mlEl = $("cfg-map-lat");  if (mlEl) mlEl.value  = map.default_lat  ?? 39.5;
    const mnEl = $("cfg-map-lon");  if (mnEl) mnEl.value  = map.default_lon  ?? -98.35;
    const mzEl = $("cfg-map-zoom"); if (mzEl) mzEl.value  = map.default_zoom ?? 4;

    const qeEl = $("cfg-quiet-enabled"); if (qeEl) qeEl.checked = !!qh.enabled;
    const qsEl = $("cfg-quiet-start");   if (qsEl) qsEl.value   = qh.start || "22:00";
    const qeEl2= $("cfg-quiet-end");     if (qeEl2) qeEl2.value = qh.end   || "06:00";

    const suEl = $("cfg-summary-enabled"); if (suEl) suEl.checked = sum.enabled !== false;
    const stEl = $("cfg-summary-time");    if (stEl) stEl.value   = sum.time    || "18:00";
    const snEl = $("cfg-summary-ntfy");    if (snEl) snEl.checked = !!sum.ntfy_push;

    // UI settings
    const sdEl = $("set-sound"); if (sdEl) { sdEl.checked = settings.sound; sdEl.addEventListener("change", () => { settings.sound = sdEl.checked; store.set("sound",settings.sound); if(els.drawerSound) els.drawerSound.checked=settings.sound; }); }
    const apEl = $("set-autopause"); if (apEl) { apEl.checked = settings.autopause; apEl.addEventListener("change", () => { settings.autopause = apEl.checked; store.set("autopause",settings.autopause); }); }
    const cfEl = $("set-clearfeed"); if (cfEl) cfEl.addEventListener("click", () => { els.feed.innerHTML=""; allEvents=[]; seenIds=new Set(); renderEmpty(); updateFooter(0); showToast("Feed cleared","ok"); });

    // Density
    const densEl = $("set-density");
    if (densEl) {
      $$("button", densEl).forEach(b => {
        b.classList.toggle("active", b.dataset.val === settings.density);
        b.addEventListener("click", () => {
          settings.density = b.dataset.val;
          store.set("density", settings.density);
          els.body.dataset.density = settings.density;
          $$("button", densEl).forEach(x => x.classList.toggle("active", x === b));
        });
      });
    }

    // Theme in config tab
    const thEl = $("set-theme");
    if (thEl) {
      $$("button", thEl).forEach(b => {
        b.classList.toggle("active", b.dataset.val === settings.theme);
        b.addEventListener("click", () => setTheme(b.dataset.val));
      });
    }
  }

  function collectConfigForm() {
    const c = _configData ? JSON.parse(JSON.stringify(_configData)) : {};
    c.filter = c.filter || {};
    c.ntfy   = c.ntfy   || {};
    c.map    = c.map    || {};
    c.quiet_hours = c.quiet_hours || {};
    c.summary     = c.summary     || {};

    const kwEl = $("cfg-keywords");
    if (kwEl) c.filter.keywords = kwEl.value.split("\n").map(s => s.trim()).filter(Boolean);
    const maEl = $("cfg-match-all"); if (maEl) c.filter.match_all = maEl.checked;

    const neEl = $("cfg-ntfy-enabled"); if (neEl) c.ntfy.enabled  = neEl.checked;
    const ntEl = $("cfg-ntfy-topic");   if (ntEl) c.ntfy.topic    = ntEl.value.trim();
    const npEl = $("cfg-ntfy-priority");if (npEl) c.ntfy.priority = parseInt(npEl.value) || 4;

    const mlEl = $("cfg-map-lat");  if (mlEl) c.map.default_lat  = parseFloat(mlEl.value)  || 39.5;
    const mnEl = $("cfg-map-lon");  if (mnEl) c.map.default_lon  = parseFloat(mnEl.value)  || -98.35;
    const mzEl = $("cfg-map-zoom"); if (mzEl) c.map.default_zoom = parseInt(mzEl.value)    || 4;

    const qeEl  = $("cfg-quiet-enabled"); if (qeEl) c.quiet_hours.enabled = qeEl.checked;
    const qsEl  = $("cfg-quiet-start");   if (qsEl) c.quiet_hours.start   = qsEl.value;
    const qeEl2 = $("cfg-quiet-end");     if (qeEl2) c.quiet_hours.end    = qeEl2.value;

    const suEl = $("cfg-summary-enabled"); if (suEl) c.summary.enabled   = suEl.checked;
    const stEl = $("cfg-summary-time");    if (stEl) c.summary.time      = stEl.value;
    const snEl = $("cfg-summary-ntfy");    if (snEl) c.summary.ntfy_push = snEl.checked;

    return c;
  }

  const btnConfigSave = $("btn-config-save");
  if (btnConfigSave) {
    btnConfigSave.addEventListener("click", async () => {
      const data = collectConfigForm();
      btnConfigSave.textContent = "Saving…"; btnConfigSave.disabled = true;
      try {
        const r = await fetch("/api/config", {
          method: "PUT",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({data}),
        });
        const d = await r.json();
        if (!r.ok) { showToast("Save failed: " + (d.detail||"?"), "err"); return; }
        _configData = data;
        showToast("Config saved", "ok");
        const rb = $("config-restart-banner");
        if (rb) rb.hidden = !d.restart_required;
        refreshConfig();
      } catch(e) { showToast("Save error: " + e.message, "err"); }
      finally { btnConfigSave.textContent = "Save"; btnConfigSave.disabled = false; }
    });
  }
  const btnConfigReload = $("btn-config-reload");
  if (btnConfigReload) btnConfigReload.addEventListener("click", loadConfig);

  const btnConfigRestart = $("btn-config-restart");
  if (btnConfigRestart) {
    btnConfigRestart.addEventListener("click", async () => {
      btnConfigRestart.textContent = "Restarting…"; btnConfigRestart.disabled = true;
      try {
        const r = await fetch("/api/match_all", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled: !!(_configData?.filter?.match_all)})});
        const d = await r.json();
        if (d.restart_ok) { showToast("Daemon restarted","ok"); $("config-restart-banner").hidden = true; }
        else showToast("Restart failed: " + (d.restart_error||"?"), "err");
      } catch(e) { showToast("Restart error: " + e.message, "err"); }
      finally { btnConfigRestart.textContent = "Restart daemon"; btnConfigRestart.disabled = false; }
    });
  }

  // ntfy test push
  const btnNtfyTest = $("btn-ntfy-test");
  if (btnNtfyTest) {
    btnNtfyTest.addEventListener("click", async () => {
      btnNtfyTest.textContent = "Sending…"; btnNtfyTest.disabled = true;
      try {
        const r = await fetch("/api/ntfy/test", {method:"POST"});
        const d = await r.json();
        if (r.ok) showToast("Test push sent!", "ok");
        else showToast("Push failed: " + (d.detail||"?"), "err");
      } catch(e) { showToast("Push error: " + e.message, "err"); }
      finally { btnNtfyTest.textContent = "Send test push"; btnNtfyTest.disabled = false; }
    });
  }

  // -----------------------------------------------------------------------
  // Feature 8: DVR scrubber
  els.fabDvr.addEventListener("click", () => {
    const strip = $("dvr-strip");
    if (strip.hidden) {
      strip.hidden = false;
      loadDvr();
    } else {
      strip.hidden = true;
      dvrAudio.pause();
    }
  });

  async function loadDvr() {
    try {
      const r = await fetch("/api/dvr/timeline");
      if (!r.ok) return;
      dvrData = await r.json();
      renderDvrTimeline();
    } catch(_) {}
  }

  function renderDvrTimeline() {
    const track = $("dvr-track");
    if (!track) return;
    track.innerHTML = "";
    if (!dvrData.length) { track.innerHTML = '<span style="color:var(--text-faint);font-size:11px;padding:12px">No clips in the last hour</span>'; return; }

    const now   = dvrData[dvrData.length - 1]?.ts || (Date.now() / 1000);
    const start = now - 3600;
    const span  = now - start || 1;

    dvrData.forEach((ev, idx) => {
      const pct = ((ev.ts - start) / span) * 100;
      const tick = document.createElement("div");
      tick.className = "dvr-tick " + (ev.hit ? "hit" : "miss");
      const durSecs = Math.max(0.5, ev.duration || 1);
      const widthPct = Math.max(0.3, (durSecs / span) * 100);
      const heightPx = ev.hit ? 28 : 16;
      tick.style.cssText = `position:absolute;left:${pct.toFixed(2)}%;width:${widthPct.toFixed(2)}%;height:${heightPx}px;bottom:0;`;
      tick.title = fmtTime(ev.ts) + " " + (ev.text || "").slice(0,60);
      tick.addEventListener("click", () => playDvrClip(idx));
      track.appendChild(tick);
    });
  }

  function playDvrClip(idx) {
    const ev = dvrData[idx];
    if (!ev) return;
    const nowPlaying = $("dvr-now-playing");
    if (ev.audio_file) {
      dvrAudio.pause();
      dvrAudio.src = `/api/audio/${encodeURIComponent(ev.audio_file)}`;
      dvrAudio.play().catch(() => {});
      if (nowPlaying) nowPlaying.textContent = `▶ ${fmtTime(ev.ts)} — ${(ev.text||"").slice(0,60)}`;
    } else {
      if (nowPlaying) nowPlaying.textContent = `No audio · ${fmtTime(ev.ts)} — ${(ev.text||"").slice(0,60)}`;
    }
    // Move scrubber head
    const head = $("dvr-head");
    if (head && dvrData.length > 1) {
      const last  = dvrData[dvrData.length-1].ts;
      const first = dvrData[0].ts;
      const pct = ((ev.ts - first) / (last - first || 1)) * 100;
      head.style.left = pct + "%";
    }
  }

  // -----------------------------------------------------------------------
  // Feature 10: Daily summaries
  async function loadSummaries() {
    const list   = $("summaries-list");
    const detail = $("summary-detail");
    if (!list) return;
    if (detail) detail.hidden = true;
    list.hidden = false;
    list.innerHTML = '<p style="color:var(--text-faint);font-size:13px">Loading…</p>';
    try {
      const r = await fetch("/api/summaries"); if (!r.ok) throw new Error();
      const items = await r.json();
      if (!items.length) {
        list.innerHTML = '<p style="color:var(--text-faint);font-size:13px">No summaries yet. The first will be generated at the configured time.</p>';
        return;
      }
      list.innerHTML = "";
      items.forEach(item => {
        const card = document.createElement("div");
        card.className = "summary-card-item";
        card.innerHTML = `<span class="summary-date">${esc(item.date)}</span><span class="summary-size">${(item.size/1024).toFixed(1)} KB</span>`;
        card.addEventListener("click", () => loadSummaryDetail(item.date));
        list.appendChild(card);
      });
    } catch(_) { list.innerHTML = '<p style="color:var(--text-faint)">Failed to load summaries.</p>'; }
  }

  async function loadSummaryDetail(date) {
    const list   = $("summaries-list");
    const detail = $("summary-detail");
    const content = $("summary-detail-content");
    if (!detail || !content) return;
    if (list) list.hidden = true;
    detail.hidden = false;
    content.innerHTML = '<p style="color:var(--text-faint)">Loading…</p>';
    try {
      const r = await fetch(`/api/summaries/${encodeURIComponent(date)}`); if (!r.ok) throw new Error();
      const md = await r.text();
      content.innerHTML = renderMarkdown(md);
    } catch(_) { content.innerHTML = '<p style="color:var(--text-faint)">Failed to load summary.</p>'; }
  }

  const btnSummaryBack = $("btn-summary-back");
  if (btnSummaryBack) {
    btnSummaryBack.addEventListener("click", () => {
      $("summary-detail").hidden = true;
      $("summaries-list").hidden = false;
    });
  }
  const btnSummRefresh = $("btn-summaries-refresh");
  if (btnSummRefresh) btnSummRefresh.addEventListener("click", loadSummaries);

  function renderMarkdown(md) {
    // Simple renderer (no deps)
    return md
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/^# (.+)$/gm,    "<h1>$1</h1>")
      .replace(/^## (.+)$/gm,   "<h2>$1</h2>")
      .replace(/^### (.+)$/gm,  "<h3>$1</h3>")
      .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
      .replace(/`([^`]+)`/g,    "<code>$1</code>")
      .replace(/^- (.+)$/gm,    "<li>$1</li>")
      .replace(/(<li>.*<\/li>)/gs, "<ul>$1</ul>")
      .replace(/\n\n/g, "<br><br>");
  }

  // -----------------------------------------------------------------------
  // Visibility + periodic refresh
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refreshStats(); refreshSummary();
      if (!paused && (!sse || sse.readyState === 2)) startStream();
      unseenNew = 0; unseenHits = 0; updateBadge(); updateTitle();
    }
  });
  setInterval(() => { if (!document.hidden) refreshStats(); }, 30000);
  setInterval(() => { if (!document.hidden) refreshSummary(); }, 5 * 60000);
  setInterval(() => { if (!document.hidden && !$("dvr-strip").hidden) loadDvr(); }, 60000);

  // Canvas resize
  window.addEventListener("resize", () => { if (!document.hidden) refreshSummary(); });

  // System prefers-color-scheme change
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (settings.theme === "auto") applyTheme("auto", settings.accent);
  });

  // -----------------------------------------------------------------------
  // Boot
  initThemeControls();
  setConn("stale");
  loadInitial();
  refreshStats();
  refreshSummary();
  refreshConfig();
  startStream();
  checkVersion();

  // -----------------------------------------------------------------------
  // PWA v3.1: Service Worker Registration
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" })
        .then(reg => {
          // Check for updates when tab becomes visible
          document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") reg.update();
          });

          // New SW waiting — show update toast
          reg.addEventListener("updatefound", () => {
            const nw = reg.installing;
            if (nw) {
              nw.addEventListener("statechange", () => {
                if (nw.state === "installed" && navigator.serviceWorker.controller) {
                  showSwUpdateToast(nw);
                }
              });
            }
          });
        })
        .catch(err => console.warn("[ScanRelay] SW registration failed:", err));
    });
  }

  // SW update toast
  function showSwUpdateToast(newWorker) {
    let toast = document.getElementById("sw-update-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "sw-update-toast";
      toast.className = "sw-update-toast";
      toast.innerHTML = '<span>Update ready — reload to get v3.1</span><button id="sw-reload-btn">Reload</button>';
      document.body.appendChild(toast);
      document.getElementById("sw-reload-btn").addEventListener("click", () => {
        if (newWorker) newWorker.postMessage({ type: "SKIP_WAITING" });
        window.location.reload();
      });
    }
    requestAnimationFrame(() => toast.classList.add("visible"));
  }

  // -----------------------------------------------------------------------
  // PWA v3.1: Install Prompt
  (function initInstallPrompt() {
    const DISMISSED_KEY = "scanrelay-install-dismissed";
    if (localStorage.getItem(DISMISSED_KEY)) return;

    const banner = document.getElementById("pwa-install-banner");
    const btnInstall = document.getElementById("pwa-btn-install");
    const btnDismiss = document.getElementById("pwa-btn-dismiss");
    const dontShow = document.getElementById("pwa-dont-show-check");
    const subtitle = document.getElementById("pwa-install-subtitle");
    if (!banner) return;

    let deferredPrompt = null;

    function showBanner() {
      banner.hidden = false;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => banner.classList.add("visible"));
      });
    }

    function hideBanner(permanent) {
      banner.classList.remove("visible");
      setTimeout(() => { banner.hidden = true; }, 400);
      if (permanent) localStorage.setItem(DISMISSED_KEY, "1");
    }

    // Android/Chrome/Edge: capture beforeinstallprompt
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      deferredPrompt = e;
      setTimeout(showBanner, 5000);
    });

    // iOS Safari detection
    const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
    const isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
                          window.navigator.standalone === true;

    if (isIOS && !isStandalone) {
      if (subtitle) {
        subtitle.innerHTML =
          'Tap <svg style="display:inline;vertical-align:middle" viewBox="0 0 24 24" width="16" height="16" fill="none"><path d="M12 2v13M8 6l4-4 4 4M20 16v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Share &rarr; <strong>Add to Home Screen</strong>';
      }
      if (btnInstall) {
        btnInstall.textContent = "How?";
        btnInstall.addEventListener("click", () => {
          showToast("Tap the Share button in Safari, then choose ‘Add to Home Screen’", "ok", 5000);
          hideBanner(false);
        });
      }
      setTimeout(showBanner, 5000);
    }

    if (btnInstall && !isIOS) {
      btnInstall.addEventListener("click", async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        const { outcome } = await deferredPrompt.userChoice;
        deferredPrompt = null;
        hideBanner(outcome === "accepted");
      });
    }

    if (btnDismiss) {
      btnDismiss.addEventListener("click", () => {
        const permanent = dontShow && dontShow.checked;
        hideBanner(permanent);
      });
    }

    // Hide once already installed
    window.addEventListener("appinstalled", () => hideBanner(true));
  })();

  // -----------------------------------------------------------------------
  // PWA v3.1: Offline UI
  (function initOfflineUI() {
    const offlineBadge = document.getElementById("offline-badge");
    const liveDot = document.getElementById("live-dot");

    function setOfflineState(offline) {
      document.body.classList.toggle("is-offline", offline);
      if (offlineBadge) offlineBadge.hidden = !offline;
      if (offline) {
        showToast("You’re offline. Showing cached events.", "warn", 5000);
      } else {
        showToast("Back online", "ok", 3000);
      }
    }

    window.addEventListener("offline", () => setOfflineState(true));
    window.addEventListener("online", () => setOfflineState(false));

    // Set initial state
    if (!navigator.onLine) setOfflineState(true);
  })();

})();
