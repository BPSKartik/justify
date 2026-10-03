/* Justify — what every page shares: building elements safely, talking to the API, the signed-in
   person in the header, toasts, and loading the 3D city. Every string from the API goes in
   through textContent; nothing is ever parsed as HTML. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) if (kid != null && kid !== false) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  };
  const svg = (tag, attrs = {}, ...kids) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
    for (const kid of kids) if (kid) n.append(kid);
    return n;
  };
  const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const compact = (n) => (n == null ? "—" : n >= 1e6 ? `${fmt(n / 1e6, 1)}M` : n >= 1e4 ? `${fmt(n / 1e3, 0)}k` : n >= 1e3 ? `${fmt(n / 1e3, 1)}k` : fmt(n));
  const short = (sha) => (sha ? sha.slice(0, 7) : "");
  const plural = (n, word, many) => `${fmt(n)} ${n === 1 ? word : (many || `${word}s`)}`;
  const ago = (t) => {
    const ms = typeof t === "number" ? t * 1000 : Date.parse(t);
    if (!ms) return "";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
    return new Date(ms).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  };

  let toastTimer;
  function toast(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }
  async function copy(text, what) {
    try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
    catch { toast("Copy failed — select the text and copy it"); }
  }

  /* ---------------------------------------------------------------- the API */
  let csrf = "";
  async function api(path, opts = {}) {
    const headers = { ...(opts.body && !(opts.body instanceof Blob) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) };
    if (csrf && opts.method && opts.method !== "GET") headers["X-Justify-CSRF"] = csrf;
    let res;
    try { res = await fetch(path, { credentials: "same-origin", ...opts, headers }); }
    catch { return { status: 0, body: { error: "Could not reach Justify. Check your connection and try again." } }; }
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    return { status: res.status, body, retryAfter: Number(res.headers.get("Retry-After") || 0) };
  }

  let config = null, me = null, meLoaded = null;
  async function loadConfig() {
    if (config) return config;
    const { body } = await api("/api/config");
    config = body || { accounts: false, providers: [], quotas: {}, mcp_auth: false };
    return config;
  }
  function loadMe(force) {
    if (meLoaded && !force) return meLoaded;
    meLoaded = (async () => {
      const { status, body } = await api("/api/me");
      me = status === 200 ? body : null;
      csrf = me ? me.csrf : "";
      return me;
    })();
    return meLoaded;
  }

  function initials(u) {
    const s = (u.name || u.login || "?").trim();
    return s.split(/\s+/).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
  }
  function avatar(u, size = "sm") {
    const box = el("span", { class: `avatar avatar-${size}`, "aria-hidden": "true" });
    if (u.avatar && u.avatar.startsWith("https://avatars.githubusercontent.com/")) {
      box.append(el("img", { src: `${u.avatar}${u.avatar.includes("?") ? "&" : "?"}s=96`, alt: "", width: "32", height: "32", loading: "lazy" }));
    } else {
      box.textContent = initials(u);
    }
    return box;
  }

  async function signOut() {
    await api("/auth/logout", { method: "POST" });
    window.location.href = "/";
  }

  /* the right-hand side of the header: a sign-in link, or the person with a small menu */
  async function renderAuth() {
    const slot = $("auth-slot");
    if (!slot) return null;
    const [cfg, user] = await Promise.all([loadConfig(), loadMe()]);
    slot.replaceChildren();
    if (!cfg.accounts) return user;
    if (!user) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      slot.append(el("a", { class: "signin-link", href: `/signin?next=${next}` }, "Sign in"));
      return null;
    }
    const menu = el("div", { class: "menu", id: "account-menu", role: "menu", hidden: true },
      el("p", { class: "menu-who" }, el("b", { text: user.user.name || user.user.login }),
        el("span", { text: user.user.login && user.user.login !== user.user.name ? user.user.login : (user.user.email || "") })),
      el("p", { class: "menu-usage", text: user.usage.limit == null ? "No daily limit" : `${fmt(user.usage.used)} of ${fmt(user.usage.limit)} audits used today` }),
      el("a", { href: "/dashboard", role: "menuitem" }, "Dashboard"),
      el("a", { href: "/dashboard#tokens", role: "menuitem" }, "Connect AI apps"),
      el("button", { type: "button", role: "menuitem", onclick: signOut }, "Sign out"));
    const btn = el("button", { type: "button", class: "who-btn", "aria-haspopup": "true", "aria-expanded": "false", "aria-controls": "account-menu" },
      avatar(user.user), el("span", { class: "who-name", text: user.user.login || user.user.name }));
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => { if (!slot.contains(e.target)) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { menu.hidden = true; btn.focus(); } });
    slot.append(el("a", { class: "dash-link", href: "/dashboard" }, "Dashboard"), el("div", { class: "who" }, btn, menu));
    return user;
  }

  /* ---------------------------------------------------------------- the city */
  let cityModule = null;
  function webglOK() {
    try {
      const c = document.createElement("canvas");
      return !!(window.WebGL2RenderingContext && c.getContext("webgl2"));
    } catch { return false; }
  }
  async function city(host, files, opts) {
    if (!webglOK()) throw new Error("no-webgl");
    if (!cityModule) cityModule = import("/static/city.js");
    const mod = await cityModule;
    return mod.mountCity(host, files, opts);
  }

  /* one tooltip for every city on the page */
  function cityTip() {
    let tip = $("city-tip");
    if (!tip) {
      tip = el("div", { id: "city-tip", class: "city-tip", role: "tooltip", hidden: true });
      document.body.append(tip);
    }
    return (file, x, y) => {
      if (!file) { tip.hidden = true; return; }
      tip.replaceChildren(
        el("b", { text: file.path }),
        el("span", { text: `${fmt(file.lines)} lines · ${file.lang || ""}` }),
        file.dead ? el("span", { class: "t-dead", text: `${fmt(file.dead)} lines of dead weight` }) : null,
        file.dup ? el("span", { class: "t-dup", text: `${fmt(file.dup)} lines copied from elsewhere` }) : null,
        (file.ai || file.human) ? el("span", { class: "t-ai", text: `${fmt(100 * (file.ai || 0) / ((file.ai || 0) + (file.human || 0)), 0)}% from AI-signed commits` }) : null);
      tip.hidden = false;
      const r = tip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - r.width - 12, x + 16);
      const top = y + r.height + 24 > window.innerHeight ? y - r.height - 12 : y + 18;
      tip.style.transform = `translate(${Math.max(8, left)}px, ${Math.max(8, top)}px)`;
    };
  }

  /* ---------------------------------------------------------------- sealed private results
     A private audit is sealed with a key made here, in the browser, and kept only here (and in a file
     the person may save). The server stores ciphertext it cannot open; this page opens it. */
  const VAULT = "jfy_keys";
  const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const fromB64url = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)), (c) => c.charCodeAt(0));
  const vault = {
    all() { try { return JSON.parse(localStorage.getItem(VAULT) || "{}"); } catch { return {}; } },
    get(id) { return this.all()[id] || null; },
    set(id, key) { try { const v = this.all(); v[id] = key; localStorage.setItem(VAULT, JSON.stringify(v)); } catch { /* private mode */ } },
    forget(id) { try { const v = this.all(); delete v[id]; localStorage.setItem(VAULT, JSON.stringify(v)); } catch { /* ignore */ } },
    newKey() { return b64url(crypto.getRandomValues(new Uint8Array(32))); },
  };
  async function unseal(id, keyText) {
    const res = await fetch(`/api/scans/${encodeURIComponent(id)}/sealed`, { credentials: "same-origin" });
    if (!res.ok) throw new Error("missing");
    const bytes = new Uint8Array(await res.arrayBuffer());
    const magic = new TextDecoder().decode(bytes.slice(0, 6));
    if (magic !== "JSEAL1") throw new Error("format");
    const key = await crypto.subtle.importKey("raw", fromB64url(keyText.trim()), "AES-GCM", false, ["decrypt"]);
    const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.slice(6, 18), additionalData: new TextEncoder().encode(id) },
      key, bytes.slice(18));
    return JSON.parse(new TextDecoder().decode(plain));
  }
  function saveFile(name, text, type = "text/plain") {
    const a = el("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  /* ---------------------------------------------------------------- someone's public GitHub repositories
     Asked of GitHub straight from this browser: the server is not in the middle, and keeps nothing. */
  async function githubRepos(owner) {
    const cacheKey = `jfy_repos_${owner.toLowerCase()}`;
    try {
      const hit = JSON.parse(sessionStorage.getItem(cacheKey) || "null");
      if (hit && Date.now() - hit.at < 600000) return hit.repos;
    } catch { /* ignore */ }
    const res = await fetch(`https://api.github.com/users/${encodeURIComponent(owner)}/repos?per_page=100&sort=pushed`,
      { headers: { Accept: "application/vnd.github+json" } });
    if (res.status === 404) throw new Error(`No GitHub user or organisation called ${owner}.`);
    if (res.status === 403 || res.status === 429) throw new Error("GitHub is limiting requests from this network. Try again in a few minutes.");
    if (!res.ok) throw new Error("GitHub did not answer. Try again.");
    const repos = (await res.json()).filter((r) => !r.private).map((r) => ({
      full_name: r.full_name, name: r.name, description: r.description || "", language: r.language || "",
      stars: r.stargazers_count || 0, pushed_at: r.pushed_at, fork: !!r.fork, archived: !!r.archived }));
    try { sessionStorage.setItem(cacheKey, JSON.stringify({ at: Date.now(), repos })); } catch { /* ignore */ }
    return repos;
  }

  const LANG = { Python: "#4f8fdb", TypeScript: "#3fbfb4", JavaScript: "#e6c547", Go: "#5cc8dc", Rust: "#dc875c", Java: "#cf7a4f",
    Kotlin: "#a97bff", C: "#9fb0c6", "C/C++ header": "#8597ad", "C++": "#d0708c", "C#": "#72c477", Ruby: "#e0505f", PHP: "#9384d0",
    Swift: "#f08a4b", Shell: "#86c06c", Dart: "#4ec3e0", Vue: "#4fc08d", Svelte: "#ff6a3d", Markdown: "#55667c", JSON: "#6b7f99",
    YAML: "#7d8fa8", HTML: "#e37b5b", CSS: "#5b8fe3", "Jupyter notebook": "#f0a64b" };
  const langColor = (name) => LANG[name] || "#7189a8";

  window.Justify = { vault, unseal, saveFile, githubRepos, langColor, plural, $, el, svg, fmt, compact, short, ago, toast, copy, api, loadConfig, loadMe, renderAuth, avatar, signOut,
    city, cityTip, webglOK, get me() { return me; }, get csrf() { return csrf; } };
})();
