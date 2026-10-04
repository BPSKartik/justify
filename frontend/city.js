/*
 * The code city: a repository as a skyline. One tower per file, districts are folders, streets are
 * the space between them. A tower's height is its size; its floors say what its lines are:
 *   coral — dead weight (no use anywhere)       gold — copied from somewhere else
 *   violet — written in AI-signed commits        steel blue — no AI trace
 * Coral and gold floors are drawn at least one storey tall, so a single dead import is visible.
 *
 * Built with esbuild into ../justify/hosted/static/city.js (see deploy/build-frontend.sh), so the
 * page loads one small file from its own origin and its Content-Security-Policy stays strict.
 */
import {
  ACESFilmicToneMapping, BoxGeometry, Color, DirectionalLight, EdgesGeometry, Fog, HemisphereLight, InstancedMesh,
  LineBasicMaterial, LineSegments, Mesh, MeshBasicMaterial, MeshStandardMaterial, Object3D, PCFShadowMap,
  PerspectiveCamera, PlaneGeometry, Raycaster, Scene, SRGBColorSpace, Vector2, Vector3, WebGLRenderer,
  DoubleSide,
} from "three";

export const COLORS = {
  ground: 0xf1f1f3, plate: [0xe3e3e7, 0xd8d8dd, 0xcdcdd4, 0xc2c2ca],
  body: 0xb3b3bb, notrace: 0x98a2b4, ai: 0x7a5cff, dead: 0xef5641, dup: 0xf0a82c, hover: 0x0a0a0a,
};
const LANG_COLORS = {
  Python: 0x4f8fdb, TypeScript: 0x3fbfb4, JavaScript: 0xe6c547, Go: 0x5cc8dc, Rust: 0xdc875c, Java: 0xcf7a4f,
  Kotlin: 0xa97bff, C: 0x9fb0c6, "C/C++ header": 0x8597ad, "C++": 0xd0708c, "C#": 0x72c477, Ruby: 0xe0505f,
  PHP: 0x9384d0, Swift: 0xf08a4b, Shell: 0x86c06c, Dart: 0x4ec3e0, Vue: 0x4fc08d, Svelte: 0xff6a3d,
};
export const langColor = (lang) => LANG_COLORS[lang] ?? 0x8b93a1;

const WHITE = new Color(0xffffff);
const ease = (t) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

/* ------------------------------------------------------------------ layout: a nested squarified treemap */

function buildTree(files) {
  const root = { name: "", path: "", dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    for (const d of parts.slice(0, -1)) {
      if (!node.dirs.has(d)) node.dirs.set(d, { name: d, path: node.path ? `${node.path}/${d}` : d, dirs: new Map(), files: [] });
      node = node.dirs.get(d);
    }
    node.files.push(f);
  }
  const collapse = (n) => {                       // a folder holding one folder and nothing else is one street
    for (const [k, c] of n.dirs) n.dirs.set(k, collapse(c));
    if (n !== root && n.files.length === 0 && n.dirs.size === 1) {
      const only = [...n.dirs.values()][0];
      return { ...only, name: `${n.name}/${only.name}` };
    }
    return n;
  };
  collapse(root);
  const weigh = (n) => {
    n.weight = n.files.reduce((s, f) => s + (f.w = Math.sqrt(Math.max(1, f.lines)) + 2), 0);
    for (const c of n.dirs.values()) n.weight += weigh(c) * 1.08;
    return n.weight;
  };
  weigh(root);
  return root;
}

function worst(row, side, scale) {
  let sum = 0, max = 0, min = Infinity;
  for (const n of row) { const a = n.weight * scale; sum += a; if (a > max) max = a; if (a < min) min = a; }
  return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
}

function squarify(items, rect) {
  const total = items.reduce((s, n) => s + n.weight, 0);
  if (!total || rect.w <= 0 || rect.h <= 0) return [];
  const scale = (rect.w * rect.h) / total;
  const rest = items.slice().sort((a, b) => b.weight - a.weight);
  const out = [];
  let r = { ...rect };
  while (rest.length) {
    const side = Math.min(r.w, r.h);
    let row = [], best = Infinity;
    while (rest.length) {
      const cand = row.concat([rest[0]]);
      const wv = worst(cand, side, scale);
      if (row.length && wv > best) break;
      row = cand; best = wv; rest.shift();
    }
    const area = row.reduce((s, n) => s + n.weight * scale, 0);
    if (r.w >= r.h) {
      const sw = area / r.h; let y = r.y;
      for (const n of row) { const h = (n.weight * scale) / sw; out.push({ node: n, x: r.x, y, w: sw, h }); y += h; }
      r = { x: r.x + sw, y: r.y, w: r.w - sw, h: r.h };
    } else {
      const sh = area / r.w; let x = r.x;
      for (const n of row) { const w = (n.weight * scale) / sh; out.push({ node: n, x, y: r.y, w, h: sh }); x += w; }
      r = { x: r.x, y: r.y + sh, w: r.w, h: r.h - sh };
    }
  }
  return out;
}

export function layout(files) {
  const root = buildTree(files);
  const side = Math.sqrt(root.weight) * 1.18;
  const plates = [], towers = [];
  // a handful of files would make wide blocks; fewer files, slimmer towers
  const slim = 0.16 + 0.16 * (1 - Math.min(1, files.length / 60));
  const place = (node, rect, depth) => {
    const pad = depth === 0 ? side * 0.012 : Math.max(0.35, side * 0.018 / (depth + 0.5));
    const inner = { x: rect.x + pad, y: rect.y + pad, w: rect.w - 2 * pad, h: rect.h - 2 * pad };
    if (inner.w <= 0.2 || inner.h <= 0.2) return;
    if (depth > 0) plates.push({ ...inner, depth, path: node.path, weight: node.weight, name: node.name });
    const items = [...node.dirs.values(), ...node.files.map((f) => ({ file: f, weight: f.w }))];
    for (const cell of squarify(items, inner)) {
      if (cell.node.file) {
        const m = Math.max(0.12, Math.min(cell.w, cell.h) * slim);
        towers.push({ file: cell.node.file, x: cell.x + m, y: cell.y + m, w: Math.max(0.1, cell.w - 2 * m),
                      d: Math.max(0.1, cell.h - 2 * m) });
      } else {
        place(cell.node, cell, depth + 1);
      }
    }
  };
  place(root, { x: -side / 2, y: -side / 2, w: side, h: side }, 0);
  const maxLines = Math.max(1, ...files.map((f) => f.lines));
  const hmax = side * 0.24;
  for (const t of towers) t.h = Math.max(0.3, hmax * Math.pow(t.file.lines / maxLines, 0.55));
  return { side, plates, towers };
}

/* ------------------------------------------------------------------ floors */

function floors(t, mode) {
  const f = t.file, H = t.h, n = Math.max(1, f.lines);
  const storey = Math.max(0.28, H * 0.06);
  const deadH = f.dead > 0 && mode !== "authors" && mode !== "language" ? Math.max(storey, (H * f.dead) / n) : 0;
  const dupH = f.dup > 0 && mode !== "authors" && mode !== "language" ? Math.max(storey, (H * f.dup) / n) : 0;
  const hot = Math.min(H * 0.85, deadH + dupH);
  const k = deadH + dupH > 0 ? hot / (deadH + dupH) : 0;
  const body = Math.max(0.12, H - hot);
  const out = [];
  if (mode === "language") {
    out.push({ y: 0, h: H, color: langColor(f.lang), hot: false });
    return out;
  }
  const traced = (f.ai ?? 0) + (f.human ?? 0);
  if ((mode === "all" || mode === "authors") && traced > 0) {
    // AI-signed floors at the foot of the tower: tops are what the eye sees most, and putting the
    // smaller share there would make a 20%-AI repository look mostly violet
    const aiH = (body * f.ai) / traced;
    if (aiH > 0.001) out.push({ y: 0, h: aiH, color: COLORS.ai, hot: false });
    if (body - aiH > 0.001) out.push({ y: aiH, h: body - aiH, color: COLORS.notrace, hot: false });
  } else {
    out.push({ y: 0, h: body, color: COLORS.body, hot: false });
  }
  let y = body;
  if (dupH) { out.push({ y, h: dupH * k, color: COLORS.dup, hot: true }); y += dupH * k; }
  if (deadH) out.push({ y, h: deadH * k, color: COLORS.dead, hot: true });
  return out;
}

/* ------------------------------------------------------------------ the scene */

export function mountCity(host, files, opts = {}) {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const small = Math.min(window.innerWidth, window.innerHeight) < 640;
  // a city drawn in a background tab skips the rise: nobody would see it, and frames there are throttled
  const o = { mode: "all", autoRotate: !still, beam: false, rise: !still && !document.hidden, labels: true, onHover: null, onPick: null,
              shadows: !small && files.length < 1600, ...opts };
  if (document.hidden) o.rise = false;

  const canvas = document.createElement("canvas");
  canvas.className = "city-canvas";
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", opts.label || "A 3D city of the repository: one tower per file");
  const renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, small ? 1.5 : 2));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = o.shadows;
  renderer.shadowMap.type = PCFShadowMap;
  host.append(canvas);
  const labelLayer = document.createElement("div");
  labelLayer.className = "city-labels";
  labelLayer.setAttribute("aria-hidden", "true");
  host.append(labelLayer);

  const { side, plates, towers } = layout(files);
  const scene = new Scene();
  scene.fog = new Fog(0xf3f3f5, side * 1.9, side * 4.2);
  const camera = new PerspectiveCamera(32, 1, 0.1, side * 10);
  scene.add(new HemisphereLight(0xffffff, 0xd6d6dc, 1.15));
  const rim = new DirectionalLight(0xffffff, 0.55);          // a soft light from behind outlines the skyline
  rim.position.set(-side * 0.8, side * 0.5, -side * 0.9);
  scene.add(rim);
  const sun = new DirectionalLight(0xffffff, 2.3);
  sun.position.set(side * 0.6, side * 1.1, side * 0.35);
  sun.castShadow = o.shadows;
  if (o.shadows) {
    sun.shadow.mapSize.set(2048, 2048);
    const s = side * 0.75;
    Object.assign(sun.shadow.camera, { left: -s, right: s, top: s, bottom: -s, near: 1, far: side * 3 });
    sun.shadow.bias = -0.0006;
  }
  scene.add(sun);

  const ground = new Mesh(new PlaneGeometry(side * 24, side * 24), new MeshStandardMaterial({ color: COLORS.ground, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.02;
  ground.receiveShadow = o.shadows;
  scene.add(ground);

  const box = new BoxGeometry(1, 1, 1);
  box.translate(0, 0.5, 0);                       // boxes grow up from the ground
  const dummy = new Object3D();
  const color = new Color();

  const plateMesh = new InstancedMesh(box, new MeshStandardMaterial({ roughness: 0.95, metalness: 0 }), Math.max(1, plates.length));
  plateMesh.receiveShadow = o.shadows;
  plates.forEach((p, i) => {
    dummy.position.set(p.x + p.w / 2, 0, p.y + p.h / 2);
    dummy.scale.set(p.w, 0.06 + 0.05 * Math.min(p.depth, 3), p.h);
    dummy.updateMatrix();
    plateMesh.setMatrixAt(i, dummy.matrix);
    plateMesh.setColorAt(i, color.setHex(COLORS.plate[Math.min(p.depth, 3)]));
  });
  plateMesh.count = plates.length;
  scene.add(plateMesh);

  const cap = towers.length * 4 + 1;
  const bodyMesh = new InstancedMesh(box, new MeshStandardMaterial({ roughness: 0.62, metalness: 0.08 }), cap);
  const hotMesh = new InstancedMesh(box, new MeshBasicMaterial({ toneMapped: false }), cap);
  bodyMesh.castShadow = bodyMesh.receiveShadow = o.shadows;
  hotMesh.castShadow = o.shadows;
  scene.add(bodyMesh, hotMesh);

  let segs = [];                                   // {tower, y, h, color, hot, mesh index}
  const bodyOwner = [], hotOwner = [];
  const hotBase = [];
  const plateTop = (t) => 0.06 + 0.05 * 3;
  const rise = towers.map((t) => ({ delay: (Math.hypot(t.x + t.w / 2, t.y + t.d / 2) / (side * 0.72)) * 0.7, p: o.rise ? 0 : 1 }));
  let riseStart = performance.now();

  function rebuild(mode) {
    segs = [];
    bodyOwner.length = hotOwner.length = hotBase.length = 0;
    let bi = 0, hi = 0;
    towers.forEach((t, ti) => {
      for (const s of floors(t, mode)) {
        const seg = { ti, ...s };
        if (s.hot) { seg.index = hi++; hotOwner.push(ti); hotBase.push(s.color); } else { seg.index = bi++; bodyOwner.push(ti); }
        segs.push(seg);
      }
    });
    bodyMesh.count = bi;
    hotMesh.count = hi;
    for (const s of segs) (s.hot ? hotMesh : bodyMesh).setColorAt(s.index, color.setHex(s.color));
    place();
    if (bodyMesh.instanceColor) bodyMesh.instanceColor.needsUpdate = true;
    if (hotMesh.instanceColor) hotMesh.instanceColor.needsUpdate = true;
  }

  function place() {
    for (const s of segs) {
      const t = towers[s.ti];
      const p = ease(rise[s.ti].p);
      dummy.position.set(t.x + t.w / 2, plateTop(t) + s.y * p, t.y + t.d / 2);
      dummy.scale.set(s.hot ? t.w * 1.04 : t.w, Math.max(0.0001, s.h * p), s.hot ? t.d * 1.04 : t.d);
      dummy.updateMatrix();
      (s.hot ? hotMesh : bodyMesh).setMatrixAt(s.index, dummy.matrix);
    }
    bodyMesh.instanceMatrix.needsUpdate = true;
    hotMesh.instanceMatrix.needsUpdate = true;
    bodyMesh.computeBoundingSphere();
    hotMesh.computeBoundingSphere();
  }

  /* the outline that follows the pointer, and a beam that sweeps the city looking for dead weight */
  const outline = new LineSegments(new EdgesGeometry(box), new LineBasicMaterial({ color: COLORS.hover, transparent: true, opacity: 0.95 }));
  outline.visible = false;
  scene.add(outline);
  // the scanner: a thin coral line crossing the city, with a faint sheet above it
  const beam = new Object3D();
  const line = new Mesh(new BoxGeometry(1, 1, 1), new MeshBasicMaterial({ color: 0xdc4a36, toneMapped: false }));
  line.scale.set(side * 0.006, 0.08, side * 1.02);
  line.position.y = 0.2;
  const sheet = new Mesh(new PlaneGeometry(1, 1), new MeshBasicMaterial({ color: 0xdc4a36, transparent: true, opacity: 0.045,
    depthWrite: false, side: DoubleSide }));
  sheet.scale.set(side * 1.02, side * 0.1, 1);
  sheet.rotation.y = Math.PI / 2;
  sheet.position.y = side * 0.05;
  beam.add(line, sheet);
  beam.visible = o.beam;
  scene.add(beam);

  /* ---------------------------------------------------------------- the camera and its controls */
  // seen from 36° up, the city's footprint and skyline project to about one `side` tall; this leaves margin
  const fit = () => side * (camera.aspect >= 1.15 ? 2.2 : camera.aspect >= 0.85 ? 2.35 : 2.9);
  const view = { az: Math.PI * 0.22, pol: 0.95, r: side * 2.05, target: new Vector3(0, side * 0.02, 0) };
  const goal = { ...view };
  const rMin = side * 0.45, rMax = side * 3.2;
  function aim() {
    camera.position.set(view.target.x + view.r * Math.sin(view.pol) * Math.cos(view.az), view.target.y + view.r * Math.cos(view.pol),
      view.target.z + view.r * Math.sin(view.pol) * Math.sin(view.az));
    camera.lookAt(view.target);
  }
  let idleSince = performance.now();
  const pointers = new Map();
  let dragged = false, downAt = null, pinch = 0;
  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    dragged = false; downAt = { x: e.clientX, y: e.clientY };
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = Math.hypot(a.x - b.x, a.y - b.y); }
    idleSince = Infinity; wake();
  });
  canvas.addEventListener("pointermove", (e) => {
    const prev = pointers.get(e.pointerId);
    if (!prev) { hover(e); return; }
    const dx = e.clientX - prev.x, dy = e.clientY - prev.y;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 5) dragged = true;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch) goal.r = Math.min(rMax, Math.max(rMin, goal.r * (pinch / d)));
      pinch = d;
    } else if (dragged) {
      goal.az += dx * 0.006;
      goal.pol = Math.min(1.32, Math.max(0.35, goal.pol - dy * 0.005));
    }
    wake();
  });
  const up = (e) => {
    const wasDrag = dragged;
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = 0;
    if (!pointers.size) idleSince = performance.now();
    if (!wasDrag && e.type === "pointerup" && downAt) pick(e);
    downAt = null;
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("pointerleave", () => { if (!pointers.size) setHover(null); });
  canvas.addEventListener("keydown", (e) => {
    const k = e.key;
    if (k === "ArrowLeft") goal.az -= 0.15; else if (k === "ArrowRight") goal.az += 0.15;
    else if (k === "ArrowUp") goal.pol = Math.max(0.35, goal.pol - 0.08); else if (k === "ArrowDown") goal.pol = Math.min(1.32, goal.pol + 0.08);
    else if (k === "+" || k === "=") zoom(0.85); else if (k === "-") zoom(1.18); else return;
    e.preventDefault(); idleSince = performance.now(); wake();
  });
  function zoom(f) { goal.r = Math.min(rMax, Math.max(rMin, goal.r * f)); idleSince = performance.now(); wake(); }

  /* ---------------------------------------------------------------- picking */
  const ray = new Raycaster();
  const ndc = new Vector2();
  let hovered = null, pinned = null, lastHover = 0;
  function towerAt(e) {
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hits = ray.intersectObjects([hotMesh, bodyMesh], false);
    if (!hits.length) return null;
    const h = hits[0];
    return (h.object === hotMesh ? hotOwner : bodyOwner)[h.instanceId] ?? null;
  }
  function hover(e) {
    const now = performance.now();
    if (now - lastHover < 40) return;
    lastHover = now;
    const ti = towerAt(e);
    setHover(ti, e);
  }
  function setHover(ti, e) {
    if (ti === hovered && ti !== null) { if (o.onHover && e) o.onHover(towers[ti].file, e.clientX, e.clientY); return; }
    hovered = ti;
    showOutline(ti ?? pinned);
    canvas.style.cursor = ti === null ? "grab" : "pointer";
    if (o.onHover) o.onHover(ti === null ? null : towers[ti].file, e ? e.clientX : 0, e ? e.clientY : 0);
    wake();
  }
  function showOutline(ti) {
    if (ti === null || ti === undefined) { outline.visible = false; return; }
    const t = towers[ti];
    outline.position.set(t.x + t.w / 2, plateTop(t), t.y + t.d / 2);
    outline.scale.set(t.w * 1.08, t.h * ease(rise[ti].p) + 0.02, t.d * 1.08);
    outline.visible = true;
  }
  function pick(e) {
    const ti = towerAt(e);
    if (ti === null) return;
    pinned = ti;
    showOutline(ti);
    if (o.onPick) o.onPick(towers[ti].file);
    wake();
  }

  /* ---------------------------------------------------------------- district names */
  const named = plates.filter((p) => p.depth <= 2).sort((a, b) => b.weight - a.weight).slice(0, small ? 4 : 7)
    .map((p) => {
      const span = document.createElement("span");
      span.className = "city-label";
      span.textContent = p.path.split("/").slice(-2).join("/");
      labelLayer.append(span);
      return { p, span, v: new Vector3(p.x + p.w / 2, 0.4, p.y + p.h / 2) };
    });
  function labels(w, h) {
    if (!o.labels) { labelLayer.hidden = true; return; }
    labelLayer.hidden = false;
    for (const l of named) {
      const v = l.v.clone().project(camera);
      const on = v.z < 1 && Math.abs(v.x) < 0.95 && Math.abs(v.y) < 0.95;
      l.span.hidden = !on;
      if (on) l.span.style.transform = `translate(${((v.x + 1) / 2) * w}px, ${((1 - v.y) / 2) * h}px) translate(-50%, -50%)`;
    }
  }

  /* ---------------------------------------------------------------- the loop: runs only while something moves */
  let raf = 0, visible = true, alive = true, w = 0, h = 0, lastFrame = performance.now(), fitted = false;
  function resize() {
    const r = host.getBoundingClientRect();
    w = Math.max(1, Math.floor(r.width)); h = Math.max(1, Math.floor(r.height));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    if (!fitted) { view.r = goal.r = fit(); fitted = true; }
    wake();
  }
  function frame(now) {
    raf = 0;
    if (!alive || !visible) return;
    const dt = Math.min(0.05, (now - lastFrame) / 1000);
    lastFrame = now;
    let busy = false;
    if (o.rise && rise.some((r) => r.p < 1)) {
      const el = (now - riseStart) / 1000;
      for (const r of rise) r.p = Math.min(1, Math.max(0, (el - r.delay) / 0.9));
      place();
      busy = true;
    }
    if (o.autoRotate && now - idleSince > 2500) { goal.az += dt * 0.07; busy = true; }
    for (const k of ["az", "pol", "r"]) {
      const d = goal[k] - view[k];
      if (Math.abs(d) > 1e-4) { view[k] += d * Math.min(1, dt * 8); busy = true; } else view[k] = goal[k];
    }
    if (o.beam && beam.visible) {
      const x = ((now / 1000) % 7) / 7;
      const bx = -side * 0.55 + x * side * 1.1;
      beam.position.set(bx, 0, 0);
      segs.forEach((s) => {
        if (!s.hot) return;
        const t = towers[s.ti];
        const near = Math.max(0, 1 - Math.abs(t.x + t.w / 2 - bx) / (side * 0.05));
        color.setHex(hotBase[s.index]).lerp(WHITE, near * 0.65);
        hotMesh.setColorAt(s.index, color);
      });
      if (hotMesh.instanceColor) hotMesh.instanceColor.needsUpdate = true;
      busy = true;
    }
    if (outline.visible && (hovered ?? pinned) !== null) showOutline(hovered ?? pinned);
    aim();
    renderer.render(scene, camera);
    labels(w, h);
    if (busy || pointers.size) raf = requestAnimationFrame(frame);
  }
  function wake() { if (!raf && alive && visible) { lastFrame = performance.now(); raf = requestAnimationFrame(frame); } }

  const ro = new ResizeObserver(resize);
  ro.observe(host);
  const io = new IntersectionObserver((entries) => { visible = entries[0].isIntersecting && !document.hidden; if (visible) wake(); });
  io.observe(host);
  const onVis = () => { visible = !document.hidden; if (visible) wake(); };
  document.addEventListener("visibilitychange", onVis);

  rebuild(o.mode);
  resize();
  aim();

  return {
    towers: towers.length,
    setMode(mode) { o.mode = mode; rebuild(mode); wake(); },
    zoom,
    reset() { Object.assign(goal, { az: Math.PI * 0.22, pol: 0.95, r: fit() }); pinned = null; showOutline(null); wake(); },
    highlight(path) {
      const ti = path ? towers.findIndex((t) => t.file.path === path) : -1;
      pinned = ti >= 0 ? ti : null;
      showOutline(pinned);
      wake();
      return ti >= 0;
    },
    replay() { for (const r of rise) r.p = 0; o.rise = true; riseStart = performance.now(); wake(); },
    destroy() {
      alive = false;
      cancelAnimationFrame(raf);
      ro.disconnect(); io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      renderer.dispose();
      box.dispose();
      canvas.remove(); labelLayer.remove();
    },
  };
}
