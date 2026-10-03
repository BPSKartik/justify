/* Justify — the audit page. Shared helpers live in common.js (window.Justify). */
(() => {
  "use strict";
  const J = window.Justify;
  const { $, el, fmt, compact, short, toast, copy, api } = J;
  const ORIGIN = window.location.origin;
  const MCP_URL = `${ORIGIN}/mcp`;
  const STAGES = ["clone", "ingest", "facts", "candidates", "copies", "attribution", "metrics"];
  const VERDICTS = ["ALL", "REMOVE", "SIMPLIFY", "AMBIGUOUS"];
  const PAGE = 80;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let config = { accounts: false, providers: [], quotas: {}, mcp_auth: false };
  let me = null;

  /* ---------------------------------------------------------------- what to audit: a repository, or your own code */
  function selectSource(which, focus) {
    const repo = which === "repo";
    $("tab-repo").setAttribute("aria-selected", String(repo));
    $("tab-upload").setAttribute("aria-selected", String(!repo));
    $("tab-repo").tabIndex = repo ? 0 : -1;
    $("tab-upload").tabIndex = repo ? -1 : 0;
    $("scan-form").hidden = !repo;
    $("upload-form").hidden = repo;
    if (!repo) {
      const needs = !me;
      $("upload-signin").hidden = !needs || !config.accounts;
      $("drop").hidden = needs && config.accounts;
      $("upload-signin-link").href = `/signin?next=${encodeURIComponent("/?upload=1")}`;
      if (!config.accounts) showUploadError("Uploads need accounts, which are not switched on for this server.");
    }
    if (focus) (repo ? $("tab-repo") : $("tab-upload")).focus();
  }

  function renderAllowance() {
    const q = config.quotas || {};
    const box = $("allowance");
    box.replaceChildren();
    if (!config.accounts) return;
    if (me) {
      const u = me.usage;
      box.append(u.limit == null ? "No daily limit on your account." :
        `${fmt(Math.max(0, u.limit - u.used))} of ${fmt(u.limit)} audits left today. Results already audited are always free to open.`);
    } else {
      box.append(`${fmt(q.anonymous)} free audits a day without an account — `,
        el("a", { href: `/signin?next=${encodeURIComponent(location.pathname)}` }, `sign in for ${fmt(q.member)}`),
        ", a history and your own code.");
    }
  }

  /* ---------------------------------------------------------------- scanning */
  let current = null, pollTimer = null, clockTimer = null, started = 0;

  function normalise(raw) {
    const s = raw.trim();
    if (!s) return "";
    if (/^(https?:\/\/)?(www\.)?github\.com\//i.test(s)) return s;
    return s.replace(/^\/+/, "");
  }

  function showFieldError(msg, link) {
    const e = $("repo-error");
    e.replaceChildren(msg || "");
    if (link) e.append(" ", el("a", { href: link.href }, link.text));
    e.hidden = !msg;
    $("repo").setAttribute("aria-invalid", msg ? "true" : "false");
  }
  function showUploadError(msg, link) {
    const e = $("upload-error");
    e.replaceChildren(msg || "");
    if (link) e.append(" ", el("a", { href: link.href }, link.text));
    e.hidden = !msg;
  }

  function quotaLink(body) {
    return body && body.signin ? { href: `/signin?next=${encodeURIComponent(location.pathname)}`, text: "Sign in" } : null;
  }

  async function startScan(repo) {
    showFieldError("");
    if (!repo) { showFieldError("Type a repository, like owner/name."); $("repo").focus(); return; }
    setBusy(true);
    const { status, body, retryAfter } = await api("/api/scans", { method: "POST", body: JSON.stringify({ repo }) });
    setBusy(false);
    if (status === 429 && body && body.code === "quota") { showFieldError(body.error, quotaLink(body)); return; }
    if (status === 429) { countdown(retryAfter || 60); return; }
    if (status === 403 && body && body.code === "csrf") { await J.loadMe(true); showFieldError("Your session changed. Press Audit it again."); return; }
    if (!body || status >= 400 || status === 0) { showFieldError((body && body.error) || "Something went wrong. Try again."); return; }
    history.pushState({ id: body.id }, "", `/s/${body.id}`);
    follow(body);
  }

  function countdown(seconds) {
    let left = seconds;
    const tick = () => {
      showFieldError(`That is a lot of requests from one address. You can start another in ${Math.ceil(left / 60)} min.`);
      if (left-- <= 0) { showFieldError(""); return; }
      setTimeout(tick, 1000);
    };
    tick();
  }

  function setBusy(on) {
    $("scan-btn").disabled = on;
    $("scan-btn").textContent = on ? "Starting…" : "Audit it";
  }

  /* ---------------------------------------------------------------- uploads */
  const SKIP_DIRS = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next", "target",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".tox", "coverage", ".gradle", "Pods", "vendor"]);
  const BINARY = /\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|whl|so|dylib|dll|exe|bin|pyc|db|sqlite3?|woff2?|ttf|otf|mp[34]|mov|onnx|pt|pkl|npy|parquet|lock|jar|class)$/i;

  async function uploadZip(file) {
    if (!me) { selectSource("upload"); return; }
    if (file.size > 25 * 1024 * 1024) { showUploadError("That zip is larger than 25 MB. Leave out build output and dependencies."); return; }
    showUploadError("");
    setUploadBusy(`Uploading ${file.name}…`);
    const key = J.vault.newKey();
    const { status, body } = await api(`/api/uploads?name=${encodeURIComponent(file.name)}`,
      { method: "POST", body: file, headers: { "Content-Type": "application/zip", "X-Justify-Result-Key": key } });
    afterUpload(status, body, key);
  }

  async function uploadFiles(entries, name) {
    if (!me) { selectSource("upload"); return; }
    const keep = entries.filter(({ path, file }) => !path.split("/").some((p) => SKIP_DIRS.has(p)) && !BINARY.test(path) && file.size < 1024 * 1024);
    if (!keep.length) { showUploadError("Nothing in that folder looks like source code."); return; }
    if (keep.length > 2000) { showUploadError(`That folder has ${fmt(keep.length)} source files; the limit is 2,000. Zip it instead.`); return; }
    let total = 0;
    setUploadBusy(`Reading ${fmt(keep.length)} files…`);
    const files = [];
    for (const { path, file } of keep) {
      total += file.size;
      if (total > 8 * 1024 * 1024) { setUploadBusy(""); showUploadError("Those files add up to more than 8 MB. Zip the folder instead."); return; }
      files.push({ path, content: await file.text() });
    }
    setUploadBusy(`Uploading ${fmt(files.length)} files…`);
    const key = J.vault.newKey();
    const { status, body } = await api("/api/uploads", { method: "POST", body: JSON.stringify({ name, files }),
      headers: { "X-Justify-Result-Key": key } });
    afterUpload(status, body, key);
  }

  function afterUpload(status, body, key) {
    setUploadBusy("");
    if (body && body.id && status < 400) J.vault.set(body.id, key);   // the only copy of the key
    if (status === 401) { selectSource("upload"); return; }
    if (status === 429 && body && body.code === "quota") { showUploadError(body.error); return; }
    if (!body || status >= 400 || status === 0) { showUploadError((body && body.error) || "The upload did not go through. Try again."); return; }
    history.pushState({ id: body.id }, "", `/s/${body.id}`);
    follow(body);
  }

  function setUploadBusy(msg) {
    const d = $("drop");
    d.classList.toggle("busy", !!msg);
    d.querySelector(".drop-title").replaceChildren(...(msg ? [msg] : ["Drop a ", el("b", { text: ".zip" }), " or a folder here"]));
  }

  async function walk(entry, prefix, out) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ path: prefix + entry.name, file });
    } else if (entry.isDirectory && !SKIP_DIRS.has(entry.name)) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e, `${prefix}${entry.name}/`, out);
      } while (batch.length);
    }
  }

  function wireUpload() {
    const drop = $("drop");
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const items = [...(e.dataTransfer.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
      const files = [...(e.dataTransfer.files || [])];
      if (files.length === 1 && /\.zip$/i.test(files[0].name)) { uploadZip(files[0]); return; }
      if (items.length) {
        const out = [];
        for (const it of items) await walk(it, "", out);
        uploadFiles(out, items.length === 1 && items[0].isDirectory ? items[0].name : "code");
      }
    });
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("pick-zip").click(); } });
    $("pick-zip").addEventListener("change", (e) => { if (e.target.files[0]) uploadZip(e.target.files[0]); e.target.value = ""; });
    $("pick-dir").addEventListener("change", (e) => {
      const list = [...e.target.files].map((f) => ({ path: f.webkitRelativePath || f.name, file: f }));
      const name = list.length ? list[0].path.split("/")[0] : "code";
      uploadFiles(list, name);
      e.target.value = "";
    });
  }

  /* ---------------------------------------------------------------- following a scan */
  function follow(scan, scroll = "smooth") {
    current = scan.id;
    clearTimeout(pollTimer);
    if (scan.status === "done" && (scan.result || scan.sealed)) { openDone(scan, scroll); return; }
    renderScan(scan);
    $("scan").scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
    const poll = async () => {
      if (current !== scan.id) return;
      const { status, body } = await api(`/api/scans/${encodeURIComponent(scan.id)}`);
      if (current !== scan.id) return;
      if (status === 404 || (!body && status !== 0)) { renderFailed({ error: "That scan no longer exists. Start it again." }); return; }
      if (status === 0 || !body) { pollTimer = setTimeout(poll, 3000); return; }
      if (body.status === "done") { openDone(body, "smooth"); return; }
      if (body.status === "failed") { renderFailed(body); return; }
      renderScan(body);
      pollTimer = setTimeout(poll, 1500);
    };
    pollTimer = setTimeout(poll, 1200);
  }

  async function openDone(scan, scroll, keyText) {
    if (!scan.sealed) { $("sealed-box").hidden = true; renderResult(scan, scroll); return; }
    const key = keyText || J.vault.get(scan.id);
    if (key) {
      try {
        const result = await J.unseal(scan.id, key);
        if (keyText) J.vault.set(scan.id, keyText);
        $("sealed-box").hidden = true;
        renderResult({ ...scan, result, key }, scroll);
        return;
      } catch {
        if (keyText) { $("sealed-error").textContent = "That key does not open this audit."; $("sealed-error").hidden = false; return; }
      }
    }
    showSealed(scan);
  }

  function showSealed(scan) {
    stopClock();
    $("scan").hidden = true;
    $("result").hidden = false;
    $("sealed-box").hidden = false;
    $("sealed-error").hidden = true;
    for (const id of ["balance", "madeof", "cityview", "authors", "no-git", "findings"]) $(id).hidden = true;
    $("result-kind").textContent = "Private audit";
    $("private-badge").hidden = false;
    $("result-title").textContent = scan.repo.replace(/^upload\//, "");
    $("result-meta").textContent = "uploaded code · sealed";
    for (const id of ["copy-plan", "copy-link", "download-md", "save-key"]) $(id).hidden = true;
    $("sealed-form").onsubmit = (e) => { e.preventDefault(); openDone(scan, "auto", $("sealed-key").value); };
    $("result").scrollIntoView({ behavior: "auto", block: "start" });
  }

  function renderScan(scan) {
    $("result").hidden = true;
    $("scan").hidden = false;
    $("scan-failed").hidden = true;
    $("scan-eyebrow").textContent = scan.status === "queued" ? "Waiting in line" : "Auditing";
    $("scan-repo").textContent = scan.repo;
    $("scan-meta").textContent = scan.sha ? `${scan.ref || "default branch"} · commit ${short(scan.sha)}` : scan.kind === "upload" ? "your uploaded code · private" : "";
    $("rail").querySelector('[data-stage="clone"]').hidden = scan.kind === "upload";
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
    $("fail-local").hidden = !["too_large", "scan_timeout", "scan_failed", "clone_timeout"].includes(scan.error_code);
    $("fail-signin").hidden = scan.error_code !== "signin";
  }

  /* ---------------------------------------------------------------- the statement */
  let findings = [], filterVerdict = "ALL", filterFile = "", shown = PAGE, blobBase = "", cityCtl = null, scanNow = null;

  function renderResult(scan, scroll = "smooth") {
    stopClock();
    scanNow = scan;
    $("scan").hidden = true;
    $("result").hidden = false;
    const r = scan.result;
    const m = r.metrics || {};
    const a = m.attribution;
    const upload = scan.kind === "upload";
    document.title = `${scan.repo} — Justify`;
    $("result-kind").textContent = upload ? "Private audit" : "Audit statement";
    $("private-badge").hidden = !scan.private;
    $("result-title").textContent = upload ? scan.repo.replace(/^upload\//, "") : scan.repo;
    const langs = r.languages || [];
    const code = langs.filter((l) => l.audit === "full" || l.audit === "copies");
    const codeLines = code.reduce((s, l) => s + l.lines, 0);
    const codeFiles = code.reduce((s, l) => s + l.files, 0);
    $("result-meta").textContent = [
      upload ? "uploaded code" : `${scan.ref || "default branch"} · commit ${short(scan.sha)}`,
      langs.length ? `${J.plural(codeFiles, "code file")} · ${J.plural(codeLines, "line")}` : `${J.plural(r.files, "Python file")} · ${J.plural(r.lines, "line")}`,
      scan.duration_s ? `audited in ${fmt(scan.duration_s, 1)} s` : null,
    ].filter(Boolean).join(" · ");

    // the headline: a ratio when there is Python, and a plain account of what was checked when there is not
    const noPy = m.jlr_percent == null;
    $("jlr-box").hidden = noPy;
    $("nopy-box").hidden = !noPy;
    if (!noPy) {
      $("jlr").textContent = fmt(m.jlr_percent, 2);
      $("jlr-sub").textContent = m.dead_weight_lines
        ? `of every Python line justifies itself: ${fmt(m.dead_weight_lines)} lines in ${fmt(m.dead_weight_units)} units have no use anywhere.`
        : "of every Python line justifies itself: nothing without a use was found.";
    } else {
      const top = code[0];
      $("nopy-lang").textContent = top ? `${top.name}` : "No source code";
      $("nopy-sub").textContent = top
        ? `Justify finds dead code in Python, where it knows the language's rules well enough to be sure. ${code.map((l) => l.name).join(", ")} ${code.length === 1 ? "was" : "were"} checked for copied blocks — ${m.duplicate_lines ? `${fmt(m.duplicate_lines)} duplicate lines found` : "none found"}.`
        : "No file here is in a language Justify reads. It looks for Python, and copies in 25+ other languages.";
    }

    const ledger = $("ledger");
    ledger.replaceChildren();
    const row = (label, value, unit) => el("div", {}, el("dt", { text: label }), el("dd", {}, value, unit ? el("small", { text: unit }) : null));
    if (!noPy) ledger.append(row("Dead weight", fmt(m.per_1000_lines, 2), "per 1k lines"), row("Candidates to remove", fmt(m.dead_weight_units), "units"));
    ledger.append(row("Copied code", fmt(m.duplicate_lines), "lines"));
    if (!noPy) ledger.append(row("Need judgement", fmt(countBy(r.findings, "AMBIGUOUS")), "units"));
    ledger.append(row("Languages", fmt(langs.length), langs.length === 1 ? "language" : "languages"));

    renderLanguages(langs);
    renderCity(r);
    renderAuthors(a, upload);

    findings = (r.findings || []).map((f) => ({ ...f, v: f.verdict }));
    findings.sort((x, y) => VERDICTS.indexOf(x.v) - VERDICTS.indexOf(y.v) || x.file.localeCompare(y.file) || x.line - y.line);
    filterVerdict = "ALL";
    filterFile = "";
    shown = PAGE;
    $("search").value = "";
    $("kind").value = "";
    blobBase = r.github_blob_base || "";
    renderFilters();
    renderItems();

    const md = `/api/scans/${encodeURIComponent(scan.id)}/report.md`;
    $("download-md").href = md;
    $("download-md").setAttribute("download", `justify-${scan.repo.replace(/\//g, "-")}.md`);
    for (const id of ["balance", "findings", "copy-plan", "download-md"]) $(id).hidden = false;
    $("copy-plan").onclick = () => copy(fixPlan(scan), "Fix plan");
    $("copy-link").onclick = () => copy(`${ORIGIN}/s/${scan.id}`, "Link");
    $("copy-link").hidden = scan.private;
    $("save-key").hidden = !scan.sealed;
    if (scan.sealed) {
      // nothing readable is on the server: downloads are made here, from what this page decrypted
      const name = scan.repo.replace(/^upload\//, "");
      $("download-md").removeAttribute("href");
      $("download-md").textContent = "Download .json";
      $("download-md").onclick = (e) => { e.preventDefault(); J.saveFile(`justify-${name}.json`, JSON.stringify(scan.result, null, 2), "application/json"); };
      $("save-key").onclick = () => J.saveFile(`justify-${name}-key.txt`,
        `Justify — key for the sealed audit ${scan.id} (${name})\n\n${scan.key}\n\nOpen ${ORIGIN}/s/${scan.id} and paste this key. ` +
        "Justify does not keep it and cannot recover it.\n");
    } else {
      $("download-md").textContent = "Download .md";
      $("download-md").onclick = null;
    }
    if (scroll) $("result").scrollIntoView({ behavior: still || scroll === "auto" ? "auto" : "smooth", block: "start" });
    loadRecent();
  }

  const countBy = (list, v) => (list || []).filter((f) => f.verdict === v).length;

  function renderLanguages(langs) {
    $("madeof").hidden = !langs.length;
    if (!langs.length) return;
    const total = langs.reduce((s, l) => s + l.lines, 0) || 1;
    const bar = $("langbar");
    const list = $("langlist");
    bar.replaceChildren();
    list.replaceChildren();
    const depth = { full: "Full audit", copies: "Copies checked", prose: "Counted", data: "Counted", none: "Counted" };
    langs.slice(0, 8).forEach((l) => {
      const seg = el("span", { class: "lang-seg", title: `${l.name}: ${fmt(l.lines)} lines` });
      seg.style.width = `${Math.max(0.6, (100 * l.lines) / total)}%`;
      seg.style.background = J.langColor(l.name);
      bar.append(seg);
      const sw = el("i", { class: "sw" });
      sw.style.background = J.langColor(l.name);
      list.append(el("li", {}, sw, el("b", { text: l.name }),
        el("span", { class: "meta", text: `${J.plural(l.lines, "line")} · ${J.plural(l.files, "file")}` }),
        el("span", { class: `depth depth-${l.audit}`, text: depth[l.audit] || "Counted" })));
    });
    bar.setAttribute("aria-label", langs.slice(0, 8).map((l) => `${l.name} ${fmt((100 * l.lines) / total, 0)}%`).join(", "));
    $("madeof-meta").textContent = `${J.plural(total, "line")} in ${J.plural(langs.reduce((s, l) => s + l.files, 0), "file")}`;
  }

  /* ---------------------------------------------------------------- the city of this repository */
  const tip = J.cityTip();
  function cityFiles(r) {
    return (r.files_detail || []).map((f) => ({ path: f.path, lines: f.lines, dead: f.dead || 0, dup: f.dup || 0,
      ai: f.ai, human: f.human, lang: f.lang }));
  }
  async function renderCity(r) {
    if (cityCtl) { cityCtl.destroy(); cityCtl = null; }
    const files = cityFiles(r);
    $("cityview").hidden = files.length < 2;
    if (files.length < 2) return;
    const traced = files.some((f) => (f.ai || 0) + (f.human || 0) > 0);
    $("city-meta").textContent = `${J.plural(files.length, "file")}${files.length >= 3000 ? " (the largest 3,000)" : ""} · height is size · click a tower to see its findings`;
    for (const b of $("city-modes").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b.dataset.mode === "all"));
      b.hidden = b.dataset.mode === "authors" && !traced;
    }
    cityLegend("all", traced);
    try {
      cityCtl = await J.city($("result-city"), files, {
        mode: "all", autoRotate: false, rise: true, labels: true,
        label: `A 3D city of ${files.length} files. Coral floors are dead weight, gold floors are copied code.`,
        onHover: tip,
        onPick: (f) => { setFileFilter(f.path); $("findings").scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" }); },
      });
    } catch {
      $("cityview").hidden = true;
    }
  }
  function cityLegend(mode, traced) {
    const box = $("city-legend");
    const sw = (cls, text) => el("span", {}, el("i", { class: `sw ${cls}` }), text);
    box.replaceChildren();
    if (mode === "language") { box.append(el("span", { text: "Each colour is a language — see “What it is made of” above." })); return; }
    if (mode === "all" || mode === "authors") {
      if (traced) box.append(sw("human", "No AI trace"), sw("ai", "AI-signed commits"));
      else box.append(sw("body", "Code"));
    } else box.append(sw("body", "Code"));
    if (mode !== "authors") box.append(sw("dup", "Copied"), sw("dead", "Dead weight"),
      el("span", { class: "meta", text: "Coral and gold floors are at least one storey tall, so a single line shows." }));
  }

  /* ---------------------------------------------------------------- who wrote it */
  function renderAuthors(a, upload) {
    const code = a && (a.all_code || { ai_lines: a.ai_lines, human_lines: a.human_lines });
    const total = code ? (code.ai_lines || 0) + (code.human_lines || 0) : 0;
    $("authors").hidden = !a || !total;
    $("no-git").hidden = !!(a && total);
    if (!a || !total) {
      $("no-git").textContent = upload
        ? "Uploaded code has no history, so who wrote it cannot be traced. Zip the folder with its .git folder to include it."
        : "Authorship could not be traced for this repository.";
      return;
    }
    const aiShare = (100 * (code.ai_lines || 0)) / total;
    $("authors-meta").textContent = `${fmt(a.ai_commits)} of ${J.plural(a.commits, "commit")} ${a.ai_commits === 1 ? "carries" : "carry"} an assistant's signature`;
    const aiBar = el("span", { class: "ai" });
    const humanBar = el("span", { class: "human" });
    aiBar.style.width = `${aiShare}%`;
    humanBar.style.width = `${100 - aiShare}%`;
    $("split").replaceChildren(aiBar, humanBar);
    $("split").setAttribute("aria-label", `${fmt(aiShare, 1)}% of lines from AI-signed commits, ${fmt(100 - aiShare, 1)}% with no AI trace`);

    const tools = $("tools");
    tools.replaceChildren();
    const entries = Object.entries(a.tools || {});
    if (entries.length) {
      tools.append(el("span", { class: "tools-label", text: "Signed by" }),
        ...entries.map(([name, n]) => el("span", { class: "tool-chip" }, name, el("b", { text: fmt(n) }))));
    } else {
      tools.append(el("span", { class: "tools-label", text: "No commit here carries an assistant's signature." }));
    }
    const h = a.history || {};
    $("thin").hidden = !h.thin;
    $("split").classList.toggle("unknown", !!h.thin);
    if (h.thin) {
      $("thin").textContent = h.commits <= 2
        ? `The whole history is ${fmt(h.commits)} commit${h.commits === 1 ? "" : "s"}: the code arrived all at once, so its history cannot show how it was written — by a person or an assistant. Treat the split above as unknown, not as human.`
        : `One commit wrote ${fmt(h.largest_commit_percent, 0)}% of the lines that exist today, so the history cannot show how most of this code was written. Treat the split above with care.`;
    }

    const cmp = $("compare");
    cmp.replaceChildren();
    const card = (title, ai, human, unit) => {
      const verdict = ai == null || human == null ? "" :
        ai === human ? "Even." : ai < human ? "AI-signed code does better here." : "AI-signed code costs more here.";
      return el("div", { class: "cmp" },
        el("h4", { text: title }),
        el("div", { class: "row" }, el("span", { class: "who ai", text: "AI-signed" }), el("span", { text: ai == null ? "—" : `${fmt(ai, unit === "%" ? 1 : 2)}${unit}` })),
        el("div", { class: "row" }, el("span", { class: "who", text: "No AI trace" }), el("span", { text: human == null ? "—" : `${fmt(human, unit === "%" ? 1 : 2)}${unit}` })),
        verdict ? el("p", { class: "verdict", text: verdict }) : null);
    };
    if ((a.ai_lines || 0) + (a.human_lines || 0) > 0) {
      cmp.append(card("Unused Python, per 1,000 lines", a.ai_dead_per_1000, a.human_dead_per_1000, ""),
        card("Duplicate Python, per 1,000 lines", a.ai_dup_per_1000, a.human_dup_per_1000, ""));
      const rw = a.rework;
      if (rw && rw.ai && rw.ai.rewritten_percent != null) {
        cmp.append(card(`Lines later rewritten (since ${rw.since})`, rw.ai.rewritten_percent, rw.human ? rw.human.rewritten_percent : null, "%"));
      }
    }
  }

  /* ---------------------------------------------------------------- line items */
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

  function setFileFilter(path) {
    filterFile = path || "";
    $("file-filter").hidden = !filterFile;
    $("file-filter-name").textContent = filterFile ? `Showing ${filterFile}` : "";
    if (cityCtl) cityCtl.highlight(filterFile || null);
    shown = PAGE;
    renderItems();
  }

  function renderItems() {
    const q = $("search").value.trim().toLowerCase();
    const kind = $("kind").value;
    const list = findings.filter((f) => (filterVerdict === "ALL" || f.v === filterVerdict) && (!filterFile || f.file === filterFile) &&
      (!kind || f.kind === kind) && (!q || f.file.toLowerCase().includes(q) || f.name.toLowerCase().includes(q)));
    $("findings-count").textContent = `${fmt(list.length)} of ${fmt(findings.length)}`;
    const ol = $("items");
    ol.replaceChildren();
    for (const f of list.slice(0, shown)) {
      const loc = `${f.file}:${f.line}`;
      const link = blobBase ? el("a", { class: "item-loc", href: `${blobBase}${f.file.split("/").map(encodeURIComponent).join("/")}#L${f.line}`, target: "_blank", rel: "noopener" }, loc)
        : el("span", { class: "item-loc" }, loc);
      const who = f.authored_by === "ai" ? "AI-signed" : f.authored_by === "human" ? "No AI trace" : f.authored_by === "n/a" ? "" : "Unknown";
      ol.append(el("li", { class: "item", onmouseenter: () => cityCtl && !filterFile && cityCtl.highlight(f.file), onmouseleave: () => cityCtl && !filterFile && cityCtl.highlight(null) },
        el("span", { class: `stamp ${f.v}`, text: f.v }),
        el("div", { class: "item-main" },
          el("p", { class: "item-title" }, el("span", { class: "kind", text: `${f.kind === "duplicate" ? "copy" : f.kind} ` }), el("span", { class: "name", text: f.name })),
          link,
          el("p", { class: "item-reason", text: f.reason })),
        who ? el("span", { class: `chip ${f.authored_by || ""}`, text: who }) : el("span")));
    }
    if (list.length > shown) {
      ol.append(el("li", { class: "more" }, el("button", { type: "button", class: "ghost", onclick: () => { shown += PAGE; renderItems(); } },
        `Show ${fmt(Math.min(PAGE, list.length - shown))} more`)));
    }
    const empty = $("items-empty");
    empty.hidden = list.length > 0;
    empty.textContent = findings.length === 0
      ? "Nothing to change: every line Justify could check has a reason to be here."
      : "Nothing matches these filters.";
  }

  /* the same plan the MCP tool hands an assistant, for pasting into any chat */
  function fixPlan(scan) {
    const r = scan.result;
    const act = findings.filter((f) => f.v === "REMOVE" || f.v === "SIMPLIFY" || f.v === "AMBIGUOUS");
    const lines = [`Justify audit of ${scan.repo}${scan.sha ? ` at ${short(scan.sha)}` : ""} — fix plan`, "",
      "Work through these one file at a time. Show me each change before you make it, and run the tests afterwards.",
      "REMOVE means no use was found anywhere in the repository; check for dynamic uses (getattr, strings, plugins) first.", ""];
    const byFile = new Map();
    for (const f of act) { if (!byFile.has(f.file)) byFile.set(f.file, []); byFile.get(f.file).push(f); }
    if (!byFile.size) lines.push("Nothing to change — every line Justify could check has a reason to be here.");
    for (const [file, list] of byFile) {
      lines.push(`## ${file}`);
      for (const f of list.sort((x, y) => x.line - y.line)) {
        const span = f.end_line && f.end_line !== f.line ? `lines ${f.line}–${f.end_line}` : `line ${f.line}`;
        if (f.v === "REMOVE") lines.push(`- Delete ${f.kind} \`${f.name}\` (${span}): ${f.reason}`);
        else if (f.v === "SIMPLIFY") lines.push(`- Merge \`${f.name}\` (${span}): ${f.reason}`);
        else lines.push(`- Ask me about ${f.kind} \`${f.name}\` (${span}) — ${f.reason}`);
      }
      lines.push("");
    }
    lines.push(`Full report: ${ORIGIN}/s/${scan.id}`);
    if (r.metrics && r.metrics.jlr_percent != null) lines.splice(1, 0, `Justified Line Ratio ${fmt(r.metrics.jlr_percent, 2)}%.`);
    return lines.join("\n");
  }

  /* ---------------------------------------------------------------- recent */
  async function loadRecent() {
    const { body } = await api("/api/recent");
    const scans = (body && body.scans) || [];
    $("recent-wrap").hidden = scans.length === 0;
    const ul = $("recent");
    ul.replaceChildren();
    for (const s of scans) {
      const langs = (s.languages || []).filter((l) => l.name !== "Markdown").slice(0, 2).map((l) => l.name).join(", ");
      const meta = [s.jlr_percent != null ? `JLR ${fmt(s.jlr_percent, 2)}%` : "no Python", `${compact(s.code_lines || s.lines)} lines`, langs,
        s.ai_share_percent ? `${fmt(s.ai_share_percent, 0)}% AI-signed` : null].filter(Boolean).join(" · ");
      ul.append(el("li", {}, el("a", { href: `/s/${s.id}`, onclick: (e) => { e.preventDefault(); openScan(s.id, true); } },
        el("span", { class: "r-name", text: s.repo }), el("span", { class: "r-meta", text: meta }))));
    }
  }

  async function openScan(id, push) {
    if (push) history.pushState({ id }, "", `/s/${id}`);
    const { status, body } = await api(`/api/scans/${encodeURIComponent(id)}`);
    if (status !== 200 || !body) {
      renderFailed({ error: me ? "That audit does not exist, or it belongs to someone else." : "That audit does not exist — or it is private. Sign in if it is yours.",
        error_code: me ? "" : "signin" });
      return;
    }
    if (body.status === "failed") renderFailed(body); else follow(body, push ? "smooth" : "auto");
  }

  /* ---------------------------------------------------------------- the hero city */
  async function heroCity() {
    const host = $("hero-city");
    try {
      const res = await fetch("/static/sample-city.json");
      const data = await res.json();
      const files = data.files.map(([path, lines, dead, dup, ai, human]) => ({ path, lines, dead, dup, ai, human, lang: "Python" }));
      await J.city(host, files, { mode: "all", autoRotate: !still, rise: !still, beam: !still, labels: false,
        label: `A 3D city of ${files.length} files of ${data.repo}, as Justify audited it.`, onHover: tip });
      $("hero-loading").remove();
      const tools = Object.keys(data.tools || {}).slice(0, 4).join(", ");
      $("hero-cap-text").textContent = `One tower per file of ${data.repo} — ${fmt(files.length)} files, audited at commit ${short(data.sha)}. ` +
        `Gold floors are its ${fmt(data.dup_lines)} copied lines; violet came from commits signed by ${tools}. Drag to look around.`;
    } catch {
      host.classList.add("no-city");
      $("hero-loading").remove();
      host.append(el("p", { class: "no-city-note", text: "This browser cannot draw the 3D city. Every number is in the audit below." }));
    }
  }

  /* ---------------------------------------------------------------- connect your AI */
  function clients(auth) {
    const signin = auth ? "Justify asks you to sign in once and approve the app; after that it calls Justify as you." : "No account and no key needed.";
    return [
      { id: "claude-code", name: "Claude Code", steps: [
        ["code", `claude mcp add --transport http justify ${MCP_URL}`],
        ["p", auth ? "Then run /mcp inside Claude Code and choose Authenticate — your browser opens to approve it. Or skip the browser with a personal token from your dashboard:" : "Then ask: “Audit https://github.com/owner/repo with justify.”"],
        ...(auth ? [["code", `claude mcp add --transport http justify ${MCP_URL} --header "Authorization: Bearer jst_YOUR_TOKEN"`]] : [])] },
      { id: "vscode", name: "Copilot in VS Code", steps: [
        ["p", "Add this to .vscode/mcp.json (or run “MCP: Add Server”), then use Copilot Chat in Agent mode." + (auth ? " VS Code opens the sign-in for you." : "")],
        ["code", JSON.stringify({ servers: { justify: { type: "http", url: MCP_URL } } }, null, 2)]] },
      { id: "claude", name: "Claude app", steps: [
        ["ol", ["Open Settings → Connectors and choose “Add custom connector”.", `Name it Justify and paste ${MCP_URL}.`,
          auth ? "Claude sends you to Justify to sign in and approve it." : "Leave authentication off.",
          "In a chat, turn it on from the + menu → Connectors. Works on claude.ai, Claude Desktop and mobile."]]] },
      { id: "chatgpt", name: "ChatGPT", steps: [
        ["ol", ["On the web, open Settings → Apps & Connectors → Advanced and turn on Developer mode.",
          `Create a connector with the MCP server URL ${MCP_URL}${auth ? " and authentication OAuth" : " and no authentication"}.`,
          auth ? "ChatGPT opens Justify's sign-in; approve it once." : "Developer mode is on paid plans; a workspace admin may need to allow it.",
          "Ask: “Use Justify to audit my code” — you can share files in the chat, and Justify audits them privately."]]] },
      { id: "cursor", name: "Cursor", steps: [
        ["p", "Add this to ~/.cursor/mcp.json (or .cursor/mcp.json in a project)." + (auth ? " Cursor shows a Login button for Justify." : "")],
        ["code", JSON.stringify({ mcpServers: { justify: { url: MCP_URL } } }, null, 2)]] },
      { id: "gemini", name: "Gemini CLI", steps: [
        ["code", `gemini mcp add --transport http justify ${MCP_URL}`],
        ["p", "In settings.json, use \"httpUrl\" — in Gemini CLI a plain \"url\" means the older SSE transport." + (auth ? " Run /mcp auth justify to sign in." : "")]] },
      { id: "copilot-studio", name: "Copilot Studio", steps: [
        ["ol", ["In your agent, open Tools → Add a tool → New tool → Model Context Protocol.",
          `Server URL: ${MCP_URL}. Authentication: ${auth ? "OAuth 2.0, dynamic discovery" : "None"}.`, "Generative orchestration must be on for the agent to call it."]]] },
    ].map((c) => ({ ...c, signin }));
  }

  function renderClients() {
    const auth = !!config.mcp_auth;
    $("endpoint").textContent = MCP_URL;
    $("connect-auth").textContent = auth
      ? "Sign in once from your AI app (standard OAuth), or use a personal token from your dashboard. Audits count against your daily allowance and land in your history."
      : "No account, no key: paste the URL.";
    const list = clients(auth);
    const tabs = $("client-tabs");
    const panes = $("client-panes");
    tabs.replaceChildren();
    panes.replaceChildren();
    list.forEach((c, i) => {
      tabs.append(el("button", { type: "button", role: "tab", id: `tab-${c.id}`, "aria-controls": `pane-${c.id}`,
        "aria-selected": String(i === 0), tabindex: i === 0 ? "0" : "-1", onclick: () => selectClient(list, c.id) }, c.name));
      const pane = el("div", { class: "pane", role: "tabpanel", id: `pane-${c.id}`, "aria-labelledby": `tab-${c.id}` });
      pane.hidden = i !== 0;
      c.steps.forEach(([type, val]) => {
        if (type === "p") pane.append(el("p", { text: val }));
        if (type === "ol") pane.append(el("ol", {}, ...val.map((t) => el("li", { text: t }))));
        if (type === "code") pane.append(el("pre", { class: "code" }, el("code", { text: val }),
          el("button", { type: "button", class: "ghost copy", onclick: () => copy(val, "Setup") }, "Copy")));
      });
      panes.append(pane);
    });
    tabs.onkeydown = (e) => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
      const ids = list.map((c) => c.id);
      const cur = ids.indexOf(document.activeElement.id.replace("tab-", ""));
      let next = e.key === "ArrowRight" ? cur + 1 : e.key === "ArrowLeft" ? cur - 1 : e.key === "Home" ? 0 : ids.length - 1;
      next = (next + ids.length) % ids.length;
      selectClient(list, ids[next], true);
      e.preventDefault();
    };
  }

  function selectClient(list, id, focus) {
    for (const c of list) {
      const on = c.id === id;
      const tab = $(`tab-${c.id}`);
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`pane-${c.id}`).hidden = !on;
      if (on && focus) tab.focus();
    }
  }

  /* ---------------------------------------------------------------- measured data */
  const STUDY = [            // measured 3 Oct 2026, each repository at its latest commit
    { repo: "jmorrison-juniper/MistHelper", lines: 629811, ai: 7.14, human: 0.99 },
    { repo: "PrefectHQ/fastmcp", lines: 250830, ai: 1.59, human: 3.26 },
    { repo: "Azure/azure-functions-agents-runtime", lines: 54240, ai: 2.38, human: 2.09 },
    { repo: "judeper/FSI-CopilotGov", lines: 41188, ai: 0.56, human: 0.0 },
    { repo: "MasterworkTools/openforge-catalog", lines: 37123, ai: 3.23, human: 8.81 },
  ];
  // the jury's live run, graded by --prove: 13 units of face-attendance, 84 calls (3 Oct 2026)
  const JURY = [
    { model: "GPT-5.6 Sol", maker: "OpenAI · Azure AI Foundry", right: 11, wrong: 0 },
    { model: "gpt-oss-120b", maker: "OpenAI · Azure AI Foundry", right: 11, wrong: 0 },
    { model: "Claude Opus 5.5", maker: "Anthropic · Claude Code", right: 9, wrong: 2 },
    { model: "Phi-4", maker: "Microsoft · Azure AI Foundry", right: 7, wrong: 4 },
    { model: "Phi-4-reasoning", maker: "Microsoft · Azure AI Foundry", right: 4, wrong: 0 },
    { model: "Llama-3.3-70B", maker: "Meta · Azure AI Foundry", right: 4, wrong: 7 },
  ];

  function renderDupChart() {
    const box = $("dup-chart");
    const max = Math.max(...STUDY.flatMap((r) => [r.ai, r.human]));
    const table = $("dup-table");
    table.append(el("tr", {}, el("th", { text: "Repository" }), el("th", { text: "AI-signed" }), el("th", { text: "No AI trace" })));
    for (const r of STUDY) {
      const bar = (cls, v) => {
        const i = el("i");
        i.style.width = `${Math.max(0.5, (100 * v) / max)}%`;
        return el("span", { class: `bar ${cls}` }, i, fmt(v, 2));
      };
      box.append(el("div", { class: `bar-row${r.ai > 2 * Math.max(r.human, 0.01) && r.ai > 5 ? " hot" : ""}` },
        el("div", { class: "bar-name" }, el("span", { class: "owner", text: `${r.repo.split("/")[0]}/` }),
          r.repo.split("/")[1], el("small", { text: `${fmt(r.lines)} lines` })),
        el("div", { class: "bar-pair" }, bar("ai", r.ai), bar("human", r.human))));
      table.append(el("tr", {}, el("td", { text: r.repo }), el("td", { text: fmt(r.ai, 2) }), el("td", { text: fmt(r.human, 2) })));
    }
  }

  function renderBoard() {
    const box = $("board");
    for (const j of JURY) {
      const n = j.right + j.wrong;
      const right = el("i", { class: "right" });
      const wrong = el("i", { class: "wrong" });
      right.style.width = `${(100 * j.right) / 11}%`;
      wrong.style.width = `${(100 * j.wrong) / 11}%`;
      box.append(el("div", { class: "bar-row" },
        el("div", { class: "bar-name" }, j.model, el("small", { text: j.maker })),
        el("div", { class: "board-bar", role: "img", "aria-label": `${j.model}: ${j.right} right, ${j.wrong} wrong of ${n} valid votes` },
          el("span", { class: "board-track" }, right, wrong), el("span", { class: "board-num", text: `${j.right}/${n}` }))));
    }
  }

  /* ---------------------------------------------------------------- wiring */
  async function init() {
    renderDupChart();
    renderBoard();
    for (const b of document.querySelectorAll("[data-copy-target]")) {
      b.addEventListener("click", () => copy($(b.dataset.copyTarget).textContent, "Command"));
    }
    $("scan-form").addEventListener("submit", (e) => { e.preventDefault(); startScan(normalise($("repo").value)); });
    $("repo").addEventListener("input", () => showFieldError(""));
    for (const b of document.querySelectorAll(".example")) {
      b.addEventListener("click", () => { $("repo").value = b.dataset.repo; startScan(b.dataset.repo); });
    }
    $("tab-repo").addEventListener("click", () => selectSource("repo"));
    $("tab-upload").addEventListener("click", () => selectSource("upload"));
    $("tab-repo").parentElement.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { selectSource($("tab-repo").getAttribute("aria-selected") === "true" ? "upload" : "repo", true); e.preventDefault(); }
    });
    wireUpload();
    $("repos-owner").addEventListener("submit", (e) => { e.preventDefault(); yourRepos($("repos-owner-input").value); });
    $("repos-filter").addEventListener("input", drawRepos);
    $("search").addEventListener("input", () => { shown = PAGE; renderItems(); });
    $("kind").addEventListener("change", () => { shown = PAGE; renderItems(); });
    $("file-filter-clear").addEventListener("click", () => setFileFilter(""));
    for (const b of $("city-modes").querySelectorAll("button")) {
      b.addEventListener("click", () => {
        for (const o of $("city-modes").querySelectorAll("button")) o.setAttribute("aria-pressed", String(o === b));
        if (cityCtl) cityCtl.setMode(b.dataset.mode);
        const traced = !$("city-modes").querySelector('[data-mode="authors"]').hidden;
        cityLegend(b.dataset.mode, traced);
      });
    }
    $("city-in").addEventListener("click", () => cityCtl && cityCtl.zoom(0.8));
    $("city-out").addEventListener("click", () => cityCtl && cityCtl.zoom(1.25));
    $("city-reset").addEventListener("click", () => cityCtl && cityCtl.reset());
    window.addEventListener("popstate", route);

    heroCity();
    reveal();
    const [cfg, user] = await Promise.all([J.loadConfig(), J.renderAuth()]);
    config = cfg;
    me = user;
    renderAllowance();
    renderClients();
    yourRepos();
    const qs = new URLSearchParams(location.search);
    if (qs.get("upload") === "1") selectSource("upload");
    if (qs.get("repos") === "1" && me) setTimeout(() => $("repos-wrap").scrollIntoView({ behavior: "auto" }), 50);
    route();
    loadRecent();
  }

  /* ---------------------------------------------------------------- sections rise into view */
  function reveal() {
    if (still || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }, { rootMargin: "0px 0px -8% 0px" });
    for (const n of document.querySelectorAll(".numbers .num, .tile, .pipeline li, .rules li, .panel, .data-map, .qa details, .cta-box")) {
      n.classList.add("reveal");
      io.observe(n);
    }
  }

  /* ---------------------------------------------------------------- the signed-in person's repositories */
  let repoList = [], repoOwner = "", privateList = [];
  async function yourRepos(owner) {
    if (!me) return;
    const gh = (me.providers || []).includes("github") ? me.user.login : "";
    repoOwner = (owner || repoOwner || gh || "").trim();
    $("repos-wrap").hidden = false;
    $("repos-owner-input").value = repoOwner;
    await privateRepos(gh);
    if (!repoOwner) {
      repoList = [];
      drawRepos();
      $("repos-note").textContent = "Type a GitHub user or organisation to list its public repositories. Signing in with GitHub lists yours.";
      return;
    }
    $("repos-note").textContent = "Loading from GitHub…";
    try {
      repoList = await J.githubRepos(repoOwner);
      $("repos-note").textContent = `${J.plural(repoList.length, "public repository", "public repositories")} of ${repoOwner}` +
        (privateList.length ? ` and ${J.plural(privateList.length, "private one")} you connected` : "") +
        " — public ones listed by GitHub to this browser directly.";
    } catch (err) {
      repoList = [];
      $("repos-note").textContent = err.message;
    }
    drawRepos();
  }

  /* private repositories, through the Justify GitHub App the person installed on their own account */
  async function privateRepos(gh) {
    const box = $("repos-actions");
    box.replaceChildren();
    privateList = [];
    if (!config.github_app) return;
    if (!gh) {
      box.append(el("span", { text: "Sign in with GitHub to audit your private repositories." }));
      return;
    }
    const { status, body } = await api("/api/me/private-repos");
    if (status !== 200 || !body || !body.enabled) return;
    privateList = body.repos || [];
    if (body.connected) {
      box.append(el("span", { class: "chip private", text: `${fmt(privateList.length)} private connected` }),
        el("a", { class: "btn-ghost small", href: "/github/connect" }, "Add repositories"));
    } else {
      box.append(el("a", { class: "btn-primary small", href: "/github/connect" }, "Connect private repositories"),
        el("span", { text: "Read-only, only the repositories you pick. Results are sealed." }));
    }
  }

  async function startPrivate(fullName) {
    const key = J.vault.newKey();
    const { status, body } = await api("/api/scans", { method: "POST", body: JSON.stringify({ repo: fullName, private: true }),
      headers: { "X-Justify-Result-Key": key } });
    if (!body || status >= 400 || status === 0) { $("repos-note").textContent = (body && body.error) || "That did not start. Try again."; return; }
    J.vault.set(body.id, key);
    history.pushState({ id: body.id }, "", `/s/${body.id}`);
    follow(body);
  }

  function drawRepos() {
    const q = $("repos-filter").value.trim().toLowerCase();
    const grid = $("repo-grid");
    grid.replaceChildren();
    const seen = new Set(privateList.map((r) => r.full_name.toLowerCase()));
    const all = [...privateList, ...repoList.filter((r) => !seen.has(r.full_name.toLowerCase()))];
    const list = all.filter((r) => !q || r.full_name.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q));
    for (const r of list.slice(0, 30)) {
      const dot = el("span", { class: "rc-dot" });
      dot.style.background = J.langColor(r.language);
      const tag = r.private ? el("span", { class: "chip private", text: "private" })
        : r.fork ? el("span", { class: "chip", text: "fork" }) : r.archived ? el("span", { class: "chip", text: "archived" }) : null;
      const go = () => {
        if (r.private) startPrivate(r.full_name);
        else { $("repo").value = r.full_name; startScan(r.full_name); }
      };
      grid.append(el("li", {}, el("div", { class: "repo-card" },
        el("div", { class: "rc-top" }, el("span", { class: "rc-name", text: r.private ? r.full_name : r.name }), tag),
        el("p", { class: "rc-desc", text: r.description || "No description." }),
        el("div", { class: "rc-meta" }, r.language ? el("span", {}, dot, r.language) : null,
          r.stars ? el("span", { text: `★ ${fmt(r.stars)}` }) : null,
          r.pushed_at ? el("span", { text: `pushed ${J.ago(r.pushed_at)}` }) : null),
        el("button", { type: "button", class: "btn-primary small", onclick: go }, r.private ? "Audit privately" : "Audit"))));
    }
    if (!list.length && all.length) grid.append(el("li", { class: "meta", text: "Nothing matches." }));
  }

  function route() {
    const m = window.location.pathname.match(/^\/s\/(s_[a-f0-9]{6,32})$/);
    if (m) openScan(m[1], false);
    else {
      current = null; clearTimeout(pollTimer); stopClock(); $("scan").hidden = true; $("result").hidden = true;
      if (cityCtl) { cityCtl.destroy(); cityCtl = null; }
      document.title = "Justify — every line earns its place";
    }
  }

  init();
})();
