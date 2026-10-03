/* Justify — choosing how to sign in. */
(() => {
  "use strict";
  const J = window.Justify;
  const { $, el } = J;
  const ICONS = {
    github: "M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.53-1.33-1.3-1.69-1.3-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.78 1.2 1.78 1.2 1.03 1.77 2.71 1.26 3.37.96.1-.75.4-1.26.73-1.55-2.56-.29-5.25-1.28-5.25-5.7 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.43-2.7 5.4-5.27 5.69.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5Z",
  };
  function icon(key) {
    if (key === "microsoft") {
      return J.svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
        J.svg("rect", { x: "1", y: "1", width: "10", height: "10", fill: "#f25022" }), J.svg("rect", { x: "13", y: "1", width: "10", height: "10", fill: "#7fba00" }),
        J.svg("rect", { x: "1", y: "13", width: "10", height: "10", fill: "#00a4ef" }), J.svg("rect", { x: "13", y: "13", width: "10", height: "10", fill: "#ffb900" }));
    }
    if (ICONS[key]) return J.svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, J.svg("path", { d: ICONS[key], fill: "currentColor" }));
    return null;
  }
  async function init() {
    const q = new URLSearchParams(location.search);
    const next = q.get("next") || "/dashboard";
    const safe = next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
    if (q.get("error")) { $("signin-error").textContent = q.get("error"); $("signin-error").hidden = false; }
    if (safe.startsWith("/oauth/consent")) {
      $("signin-title").textContent = "Sign in to connect your AI app";
      $("signin-why").textContent = "An AI app asked to use Justify for you. Sign in, then you will be asked to approve it.";
    }
    const [cfg, me] = await Promise.all([J.loadConfig(), J.loadMe()]);
    if (me && !q.get("error")) { location.replace(safe); return; }
    const box = $("providers");
    $("no-accounts").hidden = cfg.providers.length > 0;
    for (const p of cfg.providers) {
      const href = `/auth/${encodeURIComponent(p.key)}/start?next=${encodeURIComponent(safe)}`;
      box.append(el("a", { class: `provider provider-${p.key}`, href }, icon(p.key), `Continue with ${p.label}`));
    }
  }
  init();
})();
