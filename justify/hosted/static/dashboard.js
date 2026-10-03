/* Justify — a signed-in person's dashboard: allowance, totals, activity, repositories and their
   trends, what changed between audits, history, connected AI apps and personal tokens. */
(() => {
  "use strict";
  const J = window.Justify;
  const { $, el, svg, fmt, compact, short, ago, toast, copy, api } = J;
  const HISTORY_PAGE = 25;
  let historyShown = 0;

  function greet(name) {
    const h = new Date().getHours();
    const part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
    return `${part}, ${(name || "").split(/\s+/)[0] || "there"}`;
  }

  /* ---------------------------------------------------------------- the top: who, allowance, a quick audit */
  function renderHello(me) {
    const u = me.user;
    $("me-avatar").replaceChildren(J.avatar(u, "lg"));
    $("hello").textContent = greet(u.name || u.login);
    $("me-plan").textContent = u.plan === "admin" ? "Owner · no daily limit" : "Your dashboard";
    $("me-since").textContent = `${u.login ? `@${u.login} · ` : ""}member since ${new Date(u.created * 1000).toLocaleDateString("en-IN", { month: "long", year: "numeric" })}`;
    const usage = me.usage;
    const ring = $("ring-fill");
    const C = 2 * Math.PI * 50;
    ring.style.strokeDasharray = `${C}`;
    if (usage.limit == null) {
      $("allow-left").textContent = "∞";
      $("allow-label").textContent = "no daily limit";
      ring.style.strokeDashoffset = "0";
    } else {
      const left = Math.max(0, usage.limit - usage.used);
      $("allow-left").textContent = fmt(left);
      $("allow-label").textContent = `of ${fmt(usage.limit)} audits left today`;
      ring.style.strokeDashoffset = `${C * (usage.used / Math.max(1, usage.limit))}`;
      ring.classList.toggle("low", left <= Math.ceil(usage.limit * 0.15));
    }
    const reset = new Date(usage.resets_at);
    const mins = Math.max(0, Math.round((reset - Date.now()) / 60000));
    $("allow-reset").textContent = `resets in ${Math.floor(mins / 60)} h ${mins % 60} min · cached results are free`;
  }

  async function quickAudit(e) {
    e.preventDefault();
    const v = $("quick-repo").value.trim();
    const err = $("quick-error");
    err.hidden = true;
    if (!v) { err.textContent = "Type a repository, like owner/name."; err.hidden = false; return; }
    $("quick-btn").disabled = true;
    const { status, body } = await api("/api/scans", { method: "POST", body: JSON.stringify({ repo: v }) });
    $("quick-btn").disabled = false;
    if (!body || status >= 400 || status === 0) { err.textContent = (body && body.error) || "Something went wrong."; err.hidden = false; return; }
    location.href = `/s/${body.id}`;
  }

  /* ---------------------------------------------------------------- totals and charts */
  function renderStats(s) {
    const t = s.totals;
    const card = (label, value, note, cls = "") => el("div", { class: `stat ${cls}` }, el("span", { class: "stat-label", text: label }),
      el("b", { class: "stat-value", text: value }), note ? el("span", { class: "stat-note", text: note }) : null);
    $("stats").replaceChildren(
      card("Audits", fmt(t.audits), `${fmt(t.finished)} finished`),
      card("Repositories", fmt(t.repositories), "latest audit of each"),
      card("Code lines audited", compact(t.code_lines), "across those repositories"),
      card("Dead weight found", fmt(t.dead_lines), "lines with no use", "coral"),
      card("Copies found", fmt(t.duplicate_lines), "duplicate lines", "gold"),
      card("Mean JLR", t.mean_jlr == null ? "—" : `${fmt(t.mean_jlr, 2)}%`, "Python repositories"),
      card("AI-signed", t.ai_share == null ? "—" : `${fmt(t.ai_share, 1)}%`, "of traced lines", "violet"));
  }

  function renderActivity(days) {
    const box = $("activity");
    const max = Math.max(1, ...days.map((d) => d.audits));
    const W = 600, H = 120, gap = 4, bw = (W - gap * (days.length - 1)) / days.length;
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H + 22}`, class: "act-svg", preserveAspectRatio: "none" });
    days.forEach((d, i) => {
      const h = d.audits ? Math.max(4, (H * d.audits) / max) : 2;
      const r = svg("rect", { x: (i * (bw + gap)).toFixed(1), y: (H - h).toFixed(1), width: bw.toFixed(1), height: h.toFixed(1), rx: "2",
        class: d.audits ? "act-bar" : "act-empty" });
      r.append(svg("title", {}, document.createTextNode(`${d.day}: ${d.audits} audit${d.audits === 1 ? "" : "s"}`)));
      chart.append(r);
    });
    const first = days[0], last = days[days.length - 1];
    const label = (x, text, anchor) => { const t = svg("text", { x, y: H + 17, class: "act-label", "text-anchor": anchor }); t.textContent = text; return t; };
    chart.append(label(0, new Date(first.day).toLocaleDateString("en-IN", { day: "numeric", month: "short" }), "start"),
      label(W, "today", "end"));
    box.replaceChildren(chart);
    const total = days.reduce((s, d) => s + d.audits, 0);
    box.setAttribute("aria-label", `${total} audits in the last 30 days, at most ${max} in one day`);
  }

  function renderLangs(langs, assistants) {
    const box = $("langs");
    box.replaceChildren();
    const total = langs.reduce((s, l) => s + l.lines, 0) || 1;
    if (!langs.length) box.append(el("p", { class: "empty", text: "Nothing yet." }));
    for (const l of langs) {
      const bar = el("i");
      bar.style.width = `${Math.max(1, (100 * l.lines) / total)}%`;
      bar.style.background = J.langColor(l.name);
      box.append(el("div", { class: "lrow" }, el("span", { class: "lname", text: l.name }), el("span", { class: "ltrack" }, bar),
        el("span", { class: "lnum", text: compact(l.lines) })));
    }
    const a = $("assistants");
    a.replaceChildren(...(assistants.length ? assistants.map((x) => el("span", { class: "tool-chip" }, x.name, el("b", { text: fmt(x.commits) })))
      : [el("span", { class: "tools-label", text: "No assistant signatures in your audited repositories yet." })]));
  }

  /* ---------------------------------------------------------------- repositories */
  function sparkline(series) {
    const pts = series.filter((p) => p.jlr != null);
    const W = 110, H = 30;
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", "aria-hidden": "true" });
    if (pts.length < 2) { s.append(svg("line", { x1: 0, y1: H / 2, x2: W, y2: H / 2, class: "spark-flat" })); return s; }
    const lo = Math.min(...pts.map((p) => p.jlr)), hi = Math.max(...pts.map((p) => p.jlr));
    const span = Math.max(0.05, hi - lo);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${((W - 4) * i) / (pts.length - 1) + 2},${H - 3 - ((H - 6) * (p.jlr - lo)) / span}`).join(" ");
    s.append(svg("path", { d, class: "spark-line" }));
    const lastY = H - 3 - ((H - 6) * (pts[pts.length - 1].jlr - lo)) / span;
    s.append(svg("circle", { cx: W - 2, cy: lastY, r: 2.6, class: "spark-dot" }));
    return s;
  }

  function delta(now, before, digits, goodWhenUp) {
    if (now == null || before == null) return null;
    const d = now - before;
    if (Math.abs(d) < Math.pow(10, -digits) / 2) return el("span", { class: "delta even", text: "±0" });
    const good = goodWhenUp ? d > 0 : d < 0;
    return el("span", { class: `delta ${good ? "good" : "bad"}`, text: `${d > 0 ? "+" : "−"}${fmt(Math.abs(d), digits)}` });
  }

  function renderRepos(repos) {
    const tb = $("repo-table").querySelector("tbody");
    tb.replaceChildren();
    $("repos-empty").hidden = repos.length > 0;
    $("repo-table").hidden = repos.length === 0;
    for (const r of repos) {
      const L = r.latest, P = r.previous;
      const name = r.kind === "upload" ? r.repo.replace(/^upload\//, "") : r.repo;
      tb.append(el("tr", {},
        el("th", { scope: "row" }, el("a", { href: `/s/${L.id}`, class: "repo-link" }, name),
          r.kind === "upload" ? el("span", { class: "badge-private", text: "Private" }) : null,
          el("span", { class: "row-sub", text: `${L.sha ? `commit ${L.sha} · ` : ""}${ago(L.at)}` })),
        el("td", { class: "num" }, L.jlr == null ? el("span", { class: "muted", text: "no Python" }) : `${fmt(L.jlr, 2)}%`, P ? delta(L.jlr, P.jlr, 2, true) : null),
        el("td", {}, sparkline(r.series)),
        el("td", { class: "num" }, fmt(L.dead_lines), P ? delta(L.dead_lines, P.dead_lines, 0, false) : null),
        el("td", { class: "num" }, fmt(L.dup_lines), P ? delta(L.dup_lines, P.dup_lines, 0, false) : null),
        el("td", { class: "num" }, L.ai_share == null ? "—" : `${fmt(L.ai_share, 0)}%`),
        el("td", { class: "num" }, fmt(r.audits)),
        el("td", { class: "acts" }, r.audits > 1 ? el("button", { type: "button", class: "ghost", onclick: () => {
          history.replaceState(null, "", `#compare=${encodeURIComponent(r.repo)}`);
          openCompare(r.repo, name);
        } }, "What changed") : null)));
    }
  }

  async function openCompare(repo, name) {
    const { status, body } = await api(`/api/me/compare?repo=${encodeURIComponent(repo)}`);
    if (status !== 200 || !body) { toast("Could not load the comparison"); return; }
    const panel = $("compare");
    panel.hidden = false;
    $("cmp-title").textContent = `What changed in ${name}`;
    const d = body.diff;
    const nums = $("cmp-numbers");
    nums.replaceChildren();
    const [a0, a1] = [body.audits[1], body.audits[0]];
    if (!d) {
      nums.append(el("p", { class: "meta", text: "The earlier audit's details are no longer available to compare." }));
    } else {
      const pair = (label, before, after, digits, goodUp, unit = "") => el("div", { class: "cmp-num" }, el("span", { class: "stat-label", text: label }),
        el("b", {}, before == null ? "—" : `${fmt(before, digits)}${unit}`, el("span", { class: "arrow", text: " → " }), after == null ? "—" : `${fmt(after, digits)}${unit}`),
        delta(after, before, digits, goodUp));
      nums.append(
        el("p", { class: "meta cmp-span", text: `${a0 && a0.sha ? `commit ${short(a0.sha)}` : "earlier"} (${ago(a0 && a0.finished)}) → ${a1 && a1.sha ? `commit ${short(a1.sha)}` : "latest"} (${ago(a1 && a1.finished)})` }),
        pair("Justified Line Ratio", d.jlr_before, d.jlr_after, 2, true, "%"),
        pair("Dead weight", d.dead_before, d.dead_after, 0, false, " lines"),
        pair("Copies", d.dup_before, d.dup_after, 0, false, " lines"),
        el("div", { class: "cmp-num good" }, el("span", { class: "stat-label", text: "Fixed" }), el("b", { text: `${fmt(d.resolved_total)} units` }),
          el("span", { class: "stat-note", text: `${fmt(d.resolved_lines)} lines` })));
    }
    const list = (box, items, total) => {
      box.replaceChildren();
      if (!items || !items.length) { box.append(el("li", { class: "muted", text: "Nothing." })); return; }
      for (const f of items) box.append(el("li", {}, el("span", { class: `stamp ${f.verdict}`, text: f.verdict }),
        el("span", { class: "cmp-what" }, el("b", { text: f.name }), ` ${f.kind === "duplicate" ? "copy" : f.kind} · ${f.file}:${f.line}`)));
      if (total > items.length) box.append(el("li", { class: "muted", text: `and ${fmt(total - items.length)} more` }));
    };
    list($("cmp-resolved"), d && d.resolved, d ? d.resolved_total : 0);
    list($("cmp-new"), d && d.new, d ? d.new_total : 0);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------------------------------------------------------------- history */
  const VIA = { web: "Website", api: "API", mcp: "AI app", "mcp-upload": "AI app · shared files", upload: "Upload" };
  async function loadHistory(more) {
    const { body } = await api(`/api/me/scans?limit=${HISTORY_PAGE}&offset=${more ? historyShown : 0}`);
    if (!body) return;
    const tb = $("history-table").querySelector("tbody");
    if (!more) { tb.replaceChildren(); historyShown = 0; }
    for (const s of body.scans) {
      const sum = s.summary || {};
      const result = s.status === "done"
        ? [sum.jlr != null ? `JLR ${fmt(sum.jlr, 2)}%` : "no Python", `${fmt(sum.dead_lines)} dead`, `${fmt(sum.dup_lines)} copied`].join(" · ")
        : s.status === "failed" ? (s.error || "failed") : "running…";
      const name = s.kind === "upload" ? s.repo.replace(/^upload\//, "") : s.repo;
      const row = el("tr", { class: s.status === "failed" ? "failed" : "" },
        el("th", { scope: "row" }, el("a", { href: `/s/${s.id}`, class: "repo-link" }, name), s.private ? el("span", { class: "badge-private", text: "Private" }) : null),
        el("td", { text: ago(s.asked) }),
        el("td", { text: VIA[s.via] || s.via || "Website" }),
        el("td", { class: "res", text: result }),
        el("td", { class: "acts" }, el("button", { type: "button", class: "ghost", "aria-label": `Remove ${name} from history`,
          onclick: async () => {
            const sure = !s.private || window.confirm(`Delete the private audit of ${name}? Its result is removed for good.`);
            if (!sure) return;
            const { status } = await api(`/api/me/scans/${encodeURIComponent(s.id)}`, { method: "DELETE" });
            if (status === 200) { row.remove(); toast(s.private ? "Audit deleted" : "Removed from your history"); } else toast("Could not remove it");
          } }, s.private ? "Delete" : "Remove")));
      tb.append(row);
    }
    historyShown += body.scans.length;
    $("history-meta").textContent = `${fmt(body.total)} audit${body.total === 1 ? "" : "s"} · removing a public audit hides it from your history; deleting a private one erases its result`;
    $("history-more").hidden = historyShown >= body.total;
  }

  /* ---------------------------------------------------------------- AI apps and tokens */
  async function loadApps() {
    const { body } = await api("/api/me/apps");
    const ul = $("apps");
    ul.replaceChildren();
    const apps = (body && body.apps) || [];
    if (!apps.length) ul.append(el("li", { class: "muted", text: "No AI app is connected yet." }));
    for (const a of apps) {
      ul.append(el("li", {}, el("div", {}, el("b", { text: a.name }), el("span", { class: "row-sub", text: `${a.redirect_host || "app"} · connected ${ago(a.since)}${a.last_used ? ` · used ${ago(a.last_used)}` : ""}` })),
        el("button", { type: "button", class: "ghost", onclick: async (e) => {
          const { status } = await api(`/api/me/apps/${encodeURIComponent(a.client_id)}`, { method: "DELETE" });
          if (status === 200) { e.target.closest("li").remove(); toast(`${a.name} disconnected`); }
        } }, "Disconnect")));
    }
  }

  async function loadTokens() {
    const { body } = await api("/api/me/tokens");
    const ul = $("token-list");
    ul.replaceChildren();
    for (const t of (body && body.tokens) || []) {
      ul.append(el("li", {}, el("div", {}, el("b", { text: t.name }), el("span", { class: "row-sub", text: `${t.prefix}… · made ${ago(t.created)} · ${t.last_used ? `used ${ago(t.last_used)}` : "never used"}` })),
        el("button", { type: "button", class: "ghost", onclick: async (e) => {
          if (!window.confirm(`Revoke “${t.name}”? Anything using it stops working.`)) return;
          const { status } = await api(`/api/me/tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
          if (status === 200) { e.target.closest("li").remove(); toast("Token revoked"); }
        } }, "Revoke")));
    }
  }

  async function makeToken(e) {
    e.preventDefault();
    const { status, body } = await api("/api/me/tokens", { method: "POST", body: JSON.stringify({ name: $("token-name").value }) });
    if (status !== 201 || !body) { toast((body && body.error) || "Could not make a token"); return; }
    $("token-name").value = "";
    $("token-value").textContent = body.token;
    $("token-once").hidden = false;
    $("token-copy").onclick = () => copy(body.token, "Token");
    loadTokens();
  }

  /* ---------------------------------------------------------------- start */
  async function init() {
    const me = await J.renderAuth();
    const cfg = await J.loadConfig();
    if (!cfg.accounts) { location.replace("/"); return; }
    if (!me) { location.replace(`/signin?next=${encodeURIComponent("/dashboard" + location.hash)}`); return; }
    document.querySelector(".dash-link") && document.querySelector(".dash-link").setAttribute("aria-current", "page");
    renderHello(me);
    $("quick").addEventListener("submit", quickAudit);
    $("cmp-close").addEventListener("click", () => { $("compare").hidden = true; });
    $("history-more").addEventListener("click", () => loadHistory(true));
    $("token-form").addEventListener("submit", makeToken);
    $("delete-account").addEventListener("click", async () => {
      const typed = window.prompt("This erases your profile, history, tokens and private audits for good. Type DELETE to confirm.");
      if (typed !== "DELETE") return;
      const { status } = await api("/api/me", { method: "DELETE" });
      if (status === 200) location.replace("/"); else toast("Could not delete the account. Reload and try again.");
    });
    $("mcp-url").textContent = cfg.mcp_url || `${location.origin}/mcp`;
    $("mcp-copy").addEventListener("click", () => copy($("mcp-url").textContent, "MCP URL"));
    const { body: stats } = await api("/api/me/stats");
    if (stats) {
      renderStats(stats);
      renderActivity(stats.activity);
      renderLangs(stats.languages, stats.assistants);
      renderRepos(stats.repositories);
    }
    loadHistory(false);
    loadApps();
    loadTokens();
    const deep = location.hash.match(/^#compare=(.+)$/);
    if (deep) {                                   // a link straight to "what changed" for one repository
      const repo = decodeURIComponent(deep[1]);
      openCompare(repo, repo.replace(/^upload\//, ""));
    } else if (location.hash) {
      const t = document.querySelector(location.hash);
      if (t) t.scrollIntoView();
    }
  }
  init();
})();
