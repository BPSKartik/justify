/* Justify — the page. No framework: every string from the API goes in through textContent. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const ORIGIN = window.location.origin;
  const MCP_URL = `${ORIGIN}/mcp`;
  const STAGES = ["clone", "ingest", "facts", "candidates", "attribution", "metrics"];
  const VERDICTS = ["ALL", "REMOVE", "SIMPLIFY", "AMBIGUOUS"];
  const PAGE = 80;

  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const kid of kids) if (kid != null) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  };
  const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const short = (sha) => (sha ? sha.slice(0, 7) : "");

  let toastTimer;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2200);
  }
  async function copy(text, what) {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} copied`);
    } catch {
      toast("Copy failed — select the text and copy it");
    }
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    return { status: res.status, body, retryAfter: Number(res.headers.get("Retry-After") || 0) };
  }

  /* ---------------------------------------------------------------- scanning */
  let current = null;
  let pollTimer = null;
  let clockTimer = null;
  let started = 0;

  function normalise(raw) {
    const s = raw.trim();
    if (!s) return "";
    if (/^(https?:\/\/)?(www\.)?github\.com\//i.test(s)) return s;
    return s.replace(/^\/+/, "");
  }

  function showFieldError(msg) {
    const e = $("repo-error");
    e.textContent = msg;
    e.hidden = !msg;
    $("repo").setAttribute("aria-invalid", msg ? "true" : "false");
  }

  async function startScan(repo) {
    showFieldError("");
    if (!repo) { showFieldError("Type a repository, like owner/name."); $("repo").focus(); return; }
    setBusy(true);
    const { status, body, retryAfter } = await api("/api/scans", { method: "POST", body: JSON.stringify({ repo }) });
    setBusy(false);
    if (status === 429) {
      countdown(retryAfter || 60);
      return;
    }
    if (!body || status >= 400) {
      showFieldError((body && body.error) || "Something went wrong. Try again.");
      return;
    }
    history.pushState({ id: body.id }, "", `/s/${body.id}`);
    follow(body);
  }

  function countdown(seconds) {
    let left = seconds;
    const tick = () => {
      showFieldError(`That is a lot of scans from one address. You can start another in ${Math.ceil(left / 60)} min.`);
      if (left-- <= 0) { showFieldError(""); return; }
      setTimeout(tick, 1000);
    };
    tick();
  }

  function setBusy(on) {
    $("scan-btn").disabled = on;
    $("scan-btn").textContent = on ? "Starting…" : "Audit it";
  }

  function follow(scan, scroll = "smooth") {
    current = scan.id;
    clearTimeout(pollTimer);
    if (scan.status === "done") { renderResult(scan, scroll); return; }
    renderScan(scan);
    const poll = async () => {
      if (current !== scan.id) return;
      const { status, body } = await api(`/api/scans/${encodeURIComponent(scan.id)}`);
      if (current !== scan.id) return;
      if (status === 404 || !body) { renderFailed({ error: "That scan no longer exists. Start it again." }); return; }
      if (body.status === "done") { renderResult(body, "smooth"); return; }
      if (body.status === "failed") { renderFailed(body); return; }
      renderScan(body);
      pollTimer = setTimeout(poll, 1500);
    };
    pollTimer = setTimeout(poll, 1200);
  }

  function renderScan(scan) {
    $("result").hidden = true;
    $("scan").hidden = false;
    $("scan-failed").hidden = true;
    $("scan-eyebrow").textContent = scan.status === "queued" ? "Waiting in line" : "Auditing";
    $("scan-repo").textContent = scan.repo;
    $("scan-meta").textContent = scan.sha ? `${scan.ref || "default branch"} · commit ${short(scan.sha)}` : "";
    const stage = (scan.stage && scan.stage.stage) || (scan.status === "queued" ? "" : "clone");
    const idx = STAGES.indexOf(stage);
    for (const li of $("rail").querySelectorAll("li")) {
      const i = STAGES.indexOf(li.dataset.stage);
      li.classList.toggle("done", i >= 0 && idx >= 0 && i < idx);
      li.classList.toggle("active", i >= 0 && i === idx);
      if (i === idx) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    }
    const msg = scan.status === "queued"
      ? (scan.position > 0 ? `${scan.position} audit${scan.position === 1 ? "" : "s"} ahead of this one` : "Starting…")
      : ((scan.stage && scan.stage.message) || "Working…");
    $("scan-status").textContent = msg;
    if (!clockTimer) {
      started = Date.now();
      clockTimer = setInterval(() => {
        const s = Math.floor((Date.now() - started) / 1000);
        $("scan-clock").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      }, 1000);
    }
  }

  function stopClock() { clearInterval(clockTimer); clockTimer = null; $("scan-clock").textContent = ""; }

  function renderFailed(scan) {
    stopClock();
    $("scan").hidden = false;
    $("result").hidden = true;
    $("scan-eyebrow").textContent = "Stopped";
    if (scan.repo) $("scan-repo").textContent = scan.repo;
    for (const li of $("rail").querySelectorAll("li")) li.classList.remove("active");
    $("scan-status").textContent = "";
    $("scan-failed").hidden = false;
    $("fail-text").textContent = scan.error || "The audit could not finish.";
    $("fail-local").hidden = !["too_large", "scan_timeout", "scan_failed", "not_found", "clone_timeout"].includes(scan.error_code);
  }

  /* ---------------------------------------------------------------- the statement */
  let findings = [];
  let filterVerdict = "ALL";
  let shown = PAGE;

  function renderResult(scan, scroll = "smooth") {
    stopClock();
    $("scan").hidden = true;
    $("result").hidden = false;
    const r = scan.result;
    const m = r.metrics || {};
    const a = m.attribution;
    document.title = `${scan.repo} — Justify`;
    $("result-title").textContent = scan.repo;
    $("result-meta").textContent = `${scan.ref || "default branch"} · commit ${short(scan.sha)} · ${fmt(r.files)} Python files · ${fmt(r.lines)} lines` +
      (scan.duration_s ? ` · audited in ${fmt(scan.duration_s, 1)} s` : "");
    $("jlr").textContent = fmt(m.jlr_percent, 2);
    $("jlr-sub").textContent = `of every line justifies itself: ${fmt(m.dead_weight_lines)} lines in ${fmt(m.dead_weight_units)} units have no use anywhere.`;

    const ledger = $("ledger");
    ledger.replaceChildren();
    const row = (label, value, unit) => el("div", {}, el("dt", { text: label }), el("dd", {}, value, unit ? el("small", { text: unit }) : null));
    ledger.append(
      row("Dead weight", fmt(m.per_1000_lines, 2), "per 1k lines"),
      row("Duplicate code", fmt(m.duplicate_lines), "lines"),
      row("Candidates to remove", fmt(m.dead_weight_units), "units"),
      row("Need judgement", fmt(countBy(r.findings, "AMBIGUOUS")), "units"),
    );

    renderAuthors(a);

    // the hosted audit never judges or proves, so a finding's own verdict is the one to show:
    // AMBIGUOUS stays AMBIGUOUS ("needs judgement") instead of collapsing into KEEP
    findings = (r.findings || []).map((f) => ({ ...f, v: f.verdict }));
    findings.sort((x, y) => VERDICTS.indexOf(x.v) - VERDICTS.indexOf(y.v) || x.file.localeCompare(y.file) || x.line - y.line);
    filterVerdict = "ALL";
    shown = PAGE;
    $("search").value = "";
    $("kind").value = "";
    renderFilters();
    renderItems(r.github_blob_base || "");

    const md = `/api/scans/${encodeURIComponent(scan.id)}/report.md`;
    $("download-md").href = md;
    $("download-md").setAttribute("download", `justify-${scan.repo.replace("/", "-")}.md`);
    $("copy-report").onclick = async () => {
      const res = await fetch(md);
      if (res.ok) copy(await res.text(), "Report"); else toast("The report is not ready");
    };
    $("copy-link").onclick = () => copy(`${ORIGIN}/s/${scan.id}`, "Link");
    // a shared link lands on the statement at once; a scan the visitor just started glides there
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (scroll) $("result").scrollIntoView({ behavior: still || scroll === "auto" ? "auto" : "smooth", block: "start" });
    loadRecent();
  }

  const countBy = (list, v) => (list || []).filter((f) => f.verdict === v).length;

  function renderAuthors(a) {
    $("authors").hidden = !a;
    $("no-git").hidden = !!a;
    if (!a) return;
    const total = (a.ai_lines || 0) + (a.human_lines || 0);
    const aiShare = total ? (100 * a.ai_lines) / total : 0;
    $("authors-meta").textContent = `${fmt(a.ai_commits)} of ${fmt(a.commits)} commits AI-assisted`;
    const split = $("split");
    // widths go through the CSSOM: the page's CSP forbids inline style attributes
    const aiBar = el("span", { class: "ai" });
    const humanBar = el("span", { class: "human" });
    aiBar.style.width = `${aiShare}%`;
    humanBar.style.width = `${100 - aiShare}%`;
    split.replaceChildren(aiBar, humanBar);
    split.setAttribute("aria-label", `${fmt(aiShare, 1)}% of surviving lines from AI-assisted commits, ${fmt(100 - aiShare, 1)}% human`);

    const cmp = $("compare");
    cmp.replaceChildren();
    const card = (title, ai, human, unit, lowerIsBetter = true) => {
      const verdict = ai == null || human == null ? "" :
        ai === human ? "Even." : (ai < human) === lowerIsBetter ? "AI-assisted code does better here." : "AI-assisted code costs more here.";
      return el("div", { class: "cmp" },
        el("h4", { text: title }),
        el("div", { class: "row" }, el("span", { class: "who ai", text: "AI-assisted" }), el("span", { text: ai == null ? "—" : `${fmt(ai, unit === "%" ? 1 : 2)}${unit}` })),
        el("div", { class: "row" }, el("span", { class: "who", text: "Human" }), el("span", { text: human == null ? "—" : `${fmt(human, unit === "%" ? 1 : 2)}${unit}` })),
        verdict ? el("p", { class: "verdict", text: verdict }) : null);
    };
    cmp.append(
      el("div", { class: "cmp" }, el("h4", { text: "Lines that survive today" }),
        el("div", { class: "row" }, el("span", { class: "who ai", text: "AI-assisted" }), el("span", { text: fmt(a.ai_lines) })),
        el("div", { class: "row" }, el("span", { class: "who", text: "Human" }), el("span", { text: fmt(a.human_lines) }))),
      card("Unused code, per 1,000 lines", a.ai_dead_per_1000, a.human_dead_per_1000, ""),
      card("Duplicate code, per 1,000 lines", a.ai_dup_per_1000, a.human_dup_per_1000, ""),
    );
    const rw = a.rework;
    if (rw && rw.ai && rw.ai.rewritten_percent != null) {
      cmp.append(card(`Lines later rewritten (since ${rw.since})`, rw.ai.rewritten_percent, rw.human ? rw.human.rewritten_percent : null, "%"));
    }
  }

  function renderFilters() {
    const box = $("verdict-filters");
    box.replaceChildren();
    for (const v of VERDICTS) {
      const n = v === "ALL" ? findings.length : findings.filter((f) => f.v === v).length;
      if (v !== "ALL" && !n) continue;
      box.append(el("button", {
        type: "button", "aria-pressed": String(v === filterVerdict),
        onclick: () => { filterVerdict = v; shown = PAGE; renderFilters(); renderItems(); },
      }, `${v === "ALL" ? "All" : v[0] + v.slice(1).toLowerCase()} · ${n}`));
    }
  }

  let blobBase = "";
  function renderItems(base) {
    if (base !== undefined) blobBase = base;
    const q = $("search").value.trim().toLowerCase();
    const kind = $("kind").value;
    const list = findings.filter((f) => (filterVerdict === "ALL" || f.v === filterVerdict) &&
      (!kind || f.kind === kind) && (!q || f.file.toLowerCase().includes(q) || f.name.toLowerCase().includes(q)));
    $("findings-count").textContent = `${fmt(list.length)} of ${fmt(findings.length)}`;
    const ol = $("items");
    ol.replaceChildren();
    for (const f of list.slice(0, shown)) {
      const loc = `${f.file}:${f.line}`;
      const link = blobBase ? el("a", { class: "item-loc", href: `${blobBase}${f.file.split("/").map(encodeURIComponent).join("/")}#L${f.line}`, target: "_blank", rel: "noopener" }, loc)
        : el("span", { class: "item-loc" }, loc);
      const who = f.authored_by === "ai" ? "AI-assisted" : f.authored_by === "human" ? "Human" : "Unknown";
      ol.append(el("li", { class: "item" },
        el("span", { class: `stamp ${f.v}`, text: f.v }),
        el("div", { class: "item-main" },
          el("p", { class: "item-title" }, el("span", { class: "kind", text: `${f.kind} ` }), el("span", { class: "name", text: f.name })),
          link,
          el("p", { class: "item-reason", text: f.reason })),
        el("span", { class: `chip ${f.authored_by || ""}`, text: who })));
    }
    if (list.length > shown) {
      ol.append(el("li", { class: "more" }, el("button", { type: "button", class: "ghost", onclick: () => { shown += PAGE; renderItems(); } },
        `Show ${fmt(Math.min(PAGE, list.length - shown))} more`)));
    }
    $("items-empty").hidden = list.length > 0;
  }

  /* ---------------------------------------------------------------- recent */
  async function loadRecent() {
    const { body } = await api("/api/recent");
    const scans = (body && body.scans) || [];
    $("recent-wrap").hidden = scans.length === 0;
    const ul = $("recent");
    ul.replaceChildren();
    for (const s of scans) {
      ul.append(el("li", {}, el("a", { href: `/s/${s.id}`, onclick: (e) => { e.preventDefault(); open(s.id, true); } },
        el("span", { class: "r-name", text: s.repo }),
        el("span", { class: "r-meta", text: `JLR ${fmt(s.jlr_percent, 2)}% · ${fmt(s.lines)} lines` + (s.ai_share_percent != null ? ` · ${fmt(s.ai_share_percent, 1)}% AI-assisted` : "") }))));
    }
  }

  async function open(id, push) {
    if (push) history.pushState({ id }, "", `/s/${id}`);
    const { status, body } = await api(`/api/scans/${encodeURIComponent(id)}`);
    if (status !== 200 || !body) { renderFailed({ error: "That audit no longer exists. Start it again from the box above." }); return; }
    if (body.status === "failed") renderFailed(body); else follow(body, push ? "smooth" : "auto");
  }

  /* ---------------------------------------------------------------- connect your AI */
  const CLIENTS = [
    { id: "claude-code", name: "Claude Code", steps: [
      ["code", `claude mcp add --transport http justify ${MCP_URL}`],
      ["p", "Then ask: “Audit https://github.com/owner/repo with justify.” Check the connection with claude mcp list."]] },
    { id: "vscode", name: "Copilot in VS Code", steps: [
      ["p", "Add this to .vscode/mcp.json in your project (or run “MCP: Add Server” from the command palette), then use Copilot Chat in Agent mode:"],
      ["code", JSON.stringify({ servers: { justify: { type: "http", url: MCP_URL } } }, null, 2)]] },
    { id: "claude", name: "Claude app", steps: [
      ["ol", ["Open Customize → Connectors (Settings → Connectors on some plans).", "Choose “Add custom connector”.",
        `Name it Justify, paste ${MCP_URL} as the URL, and leave authentication off.`,
        "In a chat, turn it on from the + menu → Connectors. It works on claude.ai, Claude Desktop and mobile."]]] },
    { id: "chatgpt", name: "ChatGPT", steps: [
      ["ol", ["On the web, open Settings → Apps & Connectors → Advanced, and turn on Developer mode.",
        `Create a connector with the MCP server URL ${MCP_URL} and no authentication.`,
        "Developer mode is offered on paid plans; your workspace admin may need to allow it."]]] },
    { id: "cursor", name: "Cursor", steps: [
      ["p", "Add this to ~/.cursor/mcp.json (or .cursor/mcp.json in a project):"],
      ["code", JSON.stringify({ mcpServers: { justify: { url: MCP_URL } } }, null, 2)]] },
    { id: "gemini", name: "Gemini CLI", steps: [
      ["code", `gemini mcp add --transport http justify ${MCP_URL}`],
      ["p", "In settings.json, use \"httpUrl\" — in Gemini CLI a plain \"url\" means the older SSE transport."]] },
    { id: "copilot-studio", name: "Copilot Studio", steps: [
      ["ol", ["In your agent, open Tools → Add a tool → New tool → Model Context Protocol.",
        `Server URL: ${MCP_URL}. Authentication: None.`, "Generative orchestration must be on for the agent to call it."]]] },
  ];

  function renderClients() {
    $("endpoint").textContent = MCP_URL;
    const tabs = $("client-tabs");
    const panes = $("client-panes");
    CLIENTS.forEach((c, i) => {
      tabs.append(el("button", { type: "button", role: "tab", id: `tab-${c.id}`, "aria-controls": `pane-${c.id}`,
        "aria-selected": String(i === 0), tabindex: i === 0 ? "0" : "-1", onclick: () => selectClient(c.id) }, c.name));
      const pane = el("div", { class: "pane", role: "tabpanel", id: `pane-${c.id}`, "aria-labelledby": `tab-${c.id}` });
      pane.hidden = i !== 0;
      c.steps.forEach(([type, val], j) => {
        if (type === "p") pane.append(el("p", { text: val }));
        if (type === "ol") pane.append(el("ol", {}, ...val.map((t) => el("li", { text: t }))));
        if (type === "code") {
          const id = `snip-${c.id}-${j}`;
          pane.append(el("pre", { class: "code" }, el("code", { id, text: val }),
            el("button", { type: "button", class: "ghost copy", onclick: () => copy(val, "Setup") }, "Copy")));
        }
      });
      panes.append(pane);
    });
    tabs.addEventListener("keydown", (e) => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
      const ids = CLIENTS.map((c) => c.id);
      const cur = ids.indexOf(document.activeElement.id.replace("tab-", ""));
      let next = e.key === "ArrowRight" ? cur + 1 : e.key === "ArrowLeft" ? cur - 1 : e.key === "Home" ? 0 : ids.length - 1;
      next = (next + ids.length) % ids.length;
      selectClient(ids[next], true);
      e.preventDefault();
    });
  }

  function selectClient(id, focus) {
    for (const c of CLIENTS) {
      const on = c.id === id;
      const tab = $(`tab-${c.id}`);
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`pane-${c.id}`).hidden = !on;
      if (on && focus) tab.focus();
    }
  }

  /* ---------------------------------------------------------------- wiring */
  function init() {
    renderClients();
    for (const b of document.querySelectorAll("[data-copy-target]")) {
      b.addEventListener("click", () => copy($(b.dataset.copyTarget).textContent, "Command"));
    }
    $("scan-form").addEventListener("submit", (e) => { e.preventDefault(); startScan(normalise($("repo").value)); });
    $("repo").addEventListener("input", () => showFieldError(""));
    for (const b of document.querySelectorAll(".example")) {
      b.addEventListener("click", () => { $("repo").value = b.dataset.repo; startScan(b.dataset.repo); });
    }
    $("search").addEventListener("input", () => { shown = PAGE; renderItems(); });
    $("kind").addEventListener("change", () => { shown = PAGE; renderItems(); });
    window.addEventListener("popstate", route);
    route();
    loadRecent();
  }

  function route() {
    const m = window.location.pathname.match(/^\/s\/(s_[a-f0-9]{6,32})$/);
    if (m) open(m[1], false);
    else { current = null; clearTimeout(pollTimer); stopClock(); $("scan").hidden = true; $("result").hidden = true; document.title = "Justify — every line earns its place"; }
  }

  init();
})();
