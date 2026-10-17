/* LLMBench railway report. Inlined into every page by report.py; renders the
 * page from the JSON in #data. All data-derived text goes in through
 * textContent (transcripts and code are untrusted). */
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const app = document.getElementById("app");
  app.textContent = "";

  // ------------------------------------------------------------ helpers
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "style") e.style.cssText = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const k of kids.flat()) {
      if (k == null || k === false) continue;
      e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return e;
  }
  const SVGNS = "http://www.w3.org/2000/svg";
  function sv(tag, attrs, ...kids) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
    for (const k of kids.flat()) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return e;
  }
  const isNum = (x) => typeof x === "number" && isFinite(x);
  function fmt(x, d = 1) { return isNum(x) ? x.toFixed(d) : "–"; }
  function fmtInt(x) { return isNum(x) ? Math.round(x).toLocaleString("en-US") : "–"; }
  function compact(x) {
    if (!isNum(x)) return "–";
    const a = Math.abs(x);
    if (a >= 1e9) return (x / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
    if (a >= 1e6) return (x / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (x / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return a >= 10 || x === Math.round(x) ? String(Math.round(x)) : x.toFixed(1);
  }
  function fmtUs(us) {
    if (!isNum(us) || us <= 0) return "–";
    if (us >= 1e6) return (us / 1e6).toFixed(2) + " s";
    if (us >= 1e3) return (us / 1e3).toFixed(us >= 1e4 ? 0 : 1) + " ms";
    return us.toFixed(us >= 100 ? 0 : 1) + " µs";
  }
  function fmtMin(m) {
    if (!isNum(m)) return "–";
    const h = Math.floor(m / 60), mm = Math.floor(m % 60), ss = Math.round((m * 60) % 60);
    return h ? `${h}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
  }
  function fmtUsd(x) { return isNum(x) ? "$" + (x >= 100 ? x.toFixed(0) : x.toFixed(2)) : "–"; }
  const slotVar = (slot) => (slot >= 1 && slot <= 8 ? `var(--s${slot})` : "var(--s-other)");
  const SCEN = { daily: "日常", spring: "春运", refund_wave: "退票潮" };
  const FUNC = {
    q_direct: "查询：直达", q_transfer1: "查询：一次换乘", q_transfer2: "查询：两次换乘", q_crossline: "查询：跨线车次",
    q_overnight: "查询：跨日", q_none: "查询：无方案", q_invalid: "查询：非法请求", b_invalid: "订票：非法请求",
    b_capacity: "订票：容量", b_interval: "订票：区间复用", b_multileg: "订票：多程原子性", b_seats: "订票：座位合法性",
    b_crossline: "订票：跨线车次", refund: "退票", refund_multileg: "退票：多程订单",
  };
  const keyLabel = (k) => { const [sc, t] = k.split("/"); return `${SCEN[sc] || sc} · ${t.slice(1)} 线程`; };
  const STATUS = {
    ok: ["✓", "正常", "var(--st-ok)"], timeout: ["⏱", "超时", "var(--st-warn)"],
    wrong: ["✗", "判错", "var(--st-crit)"], crash: ["⚠", "崩溃", "var(--st-crash)"],
    memory: ["⚠", "超内存", "var(--st-crash)"], fatal: ["⛔", "致命", "var(--st-crit)"],
    gate: ["◐", "满足率不足", "var(--st-warn)"],
    missing: ["–", "无数据", "var(--st-ok)"],
  };
  function statusChip(st) {
    const [icon, label, color] = STATUS[st] || STATUS.missing;
    return el("span", { class: "status" }, el("span", { class: "dot", style: `background:${color}` }), `${icon} ${label}`);
  }
  const END = { submitted: "提前交卷", deadline: "到点收卷", agent_stalled: "卡死", resume_limit: "续跑次数用尽", server_died: "服务退出" };
  const FATAL = { data_race: "TSan 报告数据竞争", crash_under_tsan: "TSan 下崩溃", init_too_slow: "init 超过 30 秒", compile_error: "编译失败" };

  function section(id, title, ...kids) { return el("section", { id }, el("h2", {}, title), ...kids); }
  function card(...kids) { return el("div", { class: "card" }, ...kids); }
  function note(...kids) { return el("p", { class: "note" }, ...kids); }
  function tile(label, value, detail, hero) {
    return el("div", { class: "tile" + (hero ? " hero" : "") }, el("div", { class: "label" }, label),
      el("div", { class: "value" }, value), detail ? el("div", { class: "detail" }, detail) : null);
  }

  // ------------------------------------------------------------ theme
  function themeButton() {
    const b = el("button", { class: "btn", type: "button" });
    const current = () => document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const label = () => { b.textContent = current() === "dark" ? "☀ 浅色" : "☾ 深色"; };
    b.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("llmbench-theme", next); } catch (e) { /* storage may be blocked */ }
      label();
    });
    label();
    return b;
  }

  // ------------------------------------------------------------ tables
  function table(headers, rows, opts = {}) {
    // headers: [{label, num, sort: (row) => value}], rows: [{cells: [...], data}]
    const thead = el("tr", {}, headers.map((h, i) => {
      const th = el("th", { class: (h.num ? "num " : "") + (h.sort ? "sortable" : ""), title: h.title }, h.label);
      if (h.sort) th.addEventListener("click", () => sortBy(i, th));
      return th;
    }));
    const tbody = el("tbody");
    const draw = (rs) => { tbody.textContent = ""; rs.forEach((r) => tbody.append(el("tr", {}, r.cells.map((c, i) =>
      el("td", { class: headers[i].num ? "num" : headers[i].cls }, c))))); };
    let current = rows.slice();
    function sortBy(i, th) {
      const dir = th.dataset.dir === "desc" ? "asc" : "desc";
      thead.querySelectorAll("th").forEach((t) => delete t.dataset.dir);
      th.dataset.dir = dir;
      const f = headers[i].sort;
      current.sort((a, b) => {
        const x = f(a.data), y = f(b.data);
        const nx = x == null || Number.isNaN(x), ny = y == null || Number.isNaN(y);
        if (nx || ny) return nx - ny;
        return (x < y ? -1 : x > y ? 1 : 0) * (dir === "asc" ? 1 : -1);
      });
      draw(current);
    }
    draw(current);
    return el("div", { class: "scroll-x" }, el("table", { class: opts.cls }, el("thead", {}, thead), tbody));
  }

  // ------------------------------------------------------------ charts
  function niceTicks(lo, hi, n = 5) {
    if (!(hi > lo)) return [lo];
    const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function logTicks(lo, hi) {
    const out = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      const v = Math.pow(10, e);
      if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v);
    }
    if (out.length < 3) {
      for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
        for (const m of [2, 5]) { const v = m * Math.pow(10, e); if (v >= lo && v <= hi) out.push(v); }
      out.sort((a, b) => a - b);
    }
    return out;
  }
  function scale(lo, hi, r0, r1, log) {
    if (log) {
      const a = Math.log(lo), b = Math.log(hi);
      const f = (v) => r0 + (Math.log(Math.max(v, lo)) - a) / (b - a || 1) * (r1 - r0);
      f.inv = (p) => Math.exp(a + (p - r0) / (r1 - r0) * (b - a));
      return f;
    }
    const f = (v) => r0 + (v - lo) / (hi - lo || 1) * (r1 - r0);
    f.inv = (p) => lo + (p - r0) / (r1 - r0) * (hi - lo);
    return f;
  }
  const charts = [];
  function chartBox(draw) {
    const box = el("div", { class: "chart" });
    const tip = el("div", { class: "tooltip", style: "display:none" });
    box.append(tip);
    const redraw = () => {
      const w = Math.max(280, Math.floor(box.clientWidth || box.parentNode?.clientWidth || 600));
      if (box._w === w) return;
      box._w = w;
      box.querySelectorAll("svg").forEach((s) => s.remove());
      draw(box, w, tip);
    };
    charts.push(redraw);
    requestAnimationFrame(redraw);
    return box;
  }
  let resizeTimer = null;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => charts.forEach((c) => c()), 150); });

  function showTip(tip, box, x, y, title, rows) {
    tip.textContent = "";
    if (title) tip.append(el("div", { class: "tt-title" }, title));
    for (const r of rows) {
      tip.append(el("div", { class: "tt-row" },
        r.color ? el("span", { class: "line-key", style: `background:${r.color}` }) : null,
        el("b", {}, r.value), el("span", { class: "name" }, r.name || "")));
    }
    tip.style.display = "block";
    const bw = box.clientWidth, tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(0, x + 14), bw - tw - 4) + "px";
    tip.style.top = Math.max(0, y - 10) + "px";
  }
  const hideTip = (tip) => { tip.style.display = "none"; };

  /* Line chart. opts: {series: [{name, color, points: [[x, y, extra]], step, markers, hollow(p)}],
   *  x: {min, max, log, fmt, label}, y: {min, max, log, fmt, label}, refs: [{y, label}],
   *  vlines: [{x, label}], height, fmtTip: (series, point) => string} */
  function lineChart(opts) {
    const box = chartBox((box, W, tip) => {
      const H = opts.height || 240, m = { l: 52, r: 16, t: opts.vlines?.length ? 30 : 22, b: 34 };
      const valid = (p) => isNum(p[0]) && isNum(p[1]) && (!opts.x.log || p[0] > 0) && (!opts.y.log || p[1] > 0);
      const all = opts.series.flatMap((s) => s.points.filter(valid));
      if (!all.length) { box.append(el("p", { class: "note" }, "无数据")); return; }
      let xlo = opts.x.min ?? Math.min(...all.map((p) => p[0])), xhi = opts.x.max ?? Math.max(...all.map((p) => p[0]));
      let ylo = opts.y.min ?? Math.min(...all.map((p) => p[1])), yhi = opts.y.max ?? Math.max(...all.map((p) => p[1]));
      if (opts.refs) for (const r of opts.refs) { yhi = Math.max(yhi, r.y); ylo = Math.min(ylo, r.y); }
      if (opts.y.log) { ylo /= 1.3; yhi *= 1.3; }
      else { if (yhi <= ylo) yhi = ylo + 1; if (!opts.y.tight) yhi += (yhi - ylo) * 0.06; }
      if (xhi <= xlo) xhi = xlo + 1;
      const X = scale(xlo, xhi, m.l, W - m.r, opts.x.log), Y = scale(ylo, yhi, H - m.b, m.t, opts.y.log);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
      const yt = opts.y.log ? logTicks(ylo, yhi) : niceTicks(ylo, yhi, 4);
      for (const v of yt) {
        svg.append(sv("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
        svg.append(sv("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, (opts.y.fmt || compact)(v)));
      }
      const xt = opts.x.log ? logTicks(xlo, xhi) : niceTicks(xlo, xhi, Math.max(3, Math.floor(W / 110)));
      for (const v of xt) svg.append(sv("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, (opts.x.fmt || compact)(v)));
      svg.append(sv("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
      if (opts.x.label) svg.append(sv("text", { x: W - m.r, y: H - 4, "text-anchor": "end" }, opts.x.label));
      if (opts.y.label) svg.append(sv("text", { x: 4, y: 11, "text-anchor": "start" }, opts.y.label));
      for (const r of opts.refs || []) {
        svg.append(sv("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(r.y), y2: Y(r.y) }));
        svg.append(sv("text", { x: W - m.r - 2, y: Y(r.y) - 4, "text-anchor": "end" }, r.label));
      }
      eventLines(svg, (opts.vlines || []).filter((v) => isNum(v.x) && v.x >= xlo && v.x <= xhi), X, m.t, H - m.b, m.t - 6);
      for (const s of opts.series) {
        const pts = s.points.filter(valid);
        if (!pts.length) continue;
        let d = "";
        pts.forEach((p, i) => {
          const x = X(p[0]), y = Y(p[1]);
          if (i === 0) d += `M${x},${y}`;
          else if (s.step) d += `H${x}V${y}`;
          else d += `L${x},${y}`;
        });
        if (s.step && s.extendTo != null) d += `H${X(s.extendTo)}`;
        if (pts.length > 1 || s.step) svg.append(sv("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        if (s.markers || pts.length === 1) for (const p of pts) {
          const hollow = s.hollow && s.hollow(p);
          svg.append(sv("circle", { cx: X(p[0]), cy: Y(p[1]), r: 4, fill: hollow ? "var(--surface)" : s.color,
            stroke: hollow ? s.color : "var(--surface)", "stroke-width": 2 }));
        }
      }
      const cross = sv("line", { class: "crosshair", y1: m.t, y2: H - m.b, style: "display:none" });
      svg.append(cross);
      const hit = sv("rect", { class: "hit", x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b });
      svg.append(hit);
      const xs = [...new Set(all.map((p) => p[0]))].sort((a, b) => a - b);
      hit.addEventListener("pointermove", (ev) => {
        const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
        const xv = X.inv(px);
        let best = xs[0];
        for (const x of xs) if (Math.abs(X(x) - px) < Math.abs(X(best) - px)) best = x;
        const at = opts.snap === false ? xv : best;
        cross.setAttribute("x1", X(at)); cross.setAttribute("x2", X(at)); cross.style.display = "";
        const rows = [];
        for (const s of opts.series) {
          const pts = s.points.filter(valid);
          if (!pts.length) continue;
          let p = null;
          if (s.step) { for (const q of pts) if (q[0] <= at + 1e-9) p = q; }
          else p = pts.reduce((a, q) => (Math.abs(q[0] - at) < Math.abs(a[0] - at) ? q : a), pts[0]);
          if (p) rows.push({ color: s.color, name: s.name, value: opts.fmtTip ? opts.fmtTip(s, p) : (opts.y.fmt || compact)(p[1]) });
        }
        showTip(tip, box, X(at) * r.width / W, (ev.clientY - r.top), (opts.x.fmt || compact)(at) + (opts.x.unit || ""), rows);
      });
      hit.addEventListener("pointerleave", () => { cross.style.display = "none"; hideTip(tip); });
      box.prepend(svg);
    });
    const wrap = el("div", {}, opts.legend !== false && opts.series.length > 1 ?
      el("div", { class: "legend" }, opts.series.map((s) => el("span", {}, el("span", { class: "line-key", style: `background:${s.color}` }), s.name))) : null, box);
    if (opts.table) wrap.append(dataTable(opts.table));
    return wrap;
  }

  function dataTable(t) {
    const d = el("details", { class: "data-table" }, el("summary", {}, "数据表"));
    let built = false;
    d.addEventListener("toggle", () => {
      if (!built && d.open) { built = true; d.append(table(t.headers.map((h) => ({ label: h, num: true })), t.rows.map((r) => ({ cells: r, data: r })))); }
    });
    return d;
  }

  /* Vertical event markers; labels closer than ~40px are merged. */
  function eventLines(svg, evs, X, y1, y2, ly) {
    const labels = [];
    for (const v of evs.slice().sort((a, b) => a.x - b.x)) {
      svg.append(sv("line", { class: "event-line", x1: X(v.x), x2: X(v.x), y1, y2 }));
      if (!v.label) continue;
      const last = labels[labels.length - 1];
      if (last && X(v.x) - last.x < 40) { if (!last.parts.includes(v.label)) last.parts.push(v.label); }
      else labels.push({ x: X(v.x), parts: [v.label] });
    }
    for (const l of labels) svg.append(sv("text", { x: l.x + 3, y: ly, "font-size": 10 }, l.parts.join("·")));
  }

  /* The leaderboard chart: per group a track, the runs as ticks (hollow when the
   * run failed a correctness gate), the mean as a dot, min-max as a whisker. */
  function dotWhisker(groups) {
    return chartBox((box, W, tip) => {
      // Narrow screens put each name above its track instead of beside it.
      const narrow = W < 640;
      const rowH = narrow ? 46 : 34, m = narrow ? { l: 16, r: 16, t: 24, b: 26 } : { l: Math.min(300, Math.max(150, W * 0.32)), r: 130, t: 24, b: 26 };
      const H = m.t + m.b + rowH * groups.length;
      const xmax = Math.max(110, ...groups.map((g) => g.max)) * 1.04;
      const xmin = Math.min(0, ...groups.map((g) => g.min)) * 1.04;  // totals can be negative
      const X = scale(xmin, xmax, m.l, W - m.r);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "总分：各参赛单位的平均分、最差到最好、每次运行" });
      for (const v of niceTicks(xmin, xmax, Math.max(3, Math.floor((W - m.l - m.r) / 90)))) {
        svg.append(sv("line", { class: "grid", x1: X(v), x2: X(v), y1: m.t, y2: H - m.b }));
        svg.append(sv("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, compact(v)));
      }
      for (const [v, label] of [[0, "0"], [DATA.ref_total, `${fmt(DATA.ref_total)} 强参考`]]) {
        svg.append(sv("line", { class: "ref", x1: X(v), x2: X(v), y1: m.t - 6, y2: H - m.b, "stroke-width": 1.5 }));
        svg.append(sv("text", { x: X(v), y: m.t - 10, "text-anchor": v || xmin < 0 ? "middle" : "start", class: "label-ink" }, label));
      }
      groups.forEach((g, i) => {
        const cy = m.t + rowH * i + (narrow ? rowH - 12 : rowH / 2), color = slotVar(g.slot);
        const name = g.label.length > 46 ? g.label.slice(0, 45) + "…" : g.label;
        if (narrow) svg.append(sv("text", { x: m.l, y: cy - 16, "text-anchor": "start", class: "label-ink", "font-size": 12 }, name));
        else svg.append(sv("text", { x: m.l - 10, y: cy + 4, "text-anchor": "end", class: "label-ink", "font-size": 12 }, name));
        svg.append(sv("rect", { x: X(xmin), y: cy - 3, width: X(xmax) - X(xmin), height: 6, rx: 3, fill: "var(--wash)" }));
        if (g.n > 1) {
          svg.append(sv("line", { x1: X(g.min), x2: X(g.max), y1: cy, y2: cy, stroke: color, "stroke-width": 2 }));
          for (const x of [g.min, g.max]) svg.append(sv("line", { x1: X(x), x2: X(x), y1: cy - 6, y2: cy + 6, stroke: color, "stroke-width": 2 }));
        }
        for (const r of g.runs) {
          svg.append(sv("rect", { x: X(r.total) - 1.5, y: cy - 11, width: 3, height: 7, rx: 1,
            fill: r.failed ? "var(--surface)" : color, stroke: color, "stroke-width": r.failed ? 1 : 0 }));
        }
        svg.append(sv("circle", { cx: X(g.mean), cy, r: 6, fill: color, stroke: "var(--surface)", "stroke-width": 2 }));
        svg.append(sv("text", narrow ? { x: W - m.r, y: cy - 16, "text-anchor": "end", class: "label-strong", "font-size": 12 }
          : { x: W - m.r + 10, y: cy + 4, class: "label-strong", "font-size": 12 },
          `${fmt(g.mean)}${g.n > 1 ? `  (${fmt(g.min, 0)}–${fmt(g.max, 0)})` : ""}`));
        const hit = sv("rect", { class: "hit", x: 0, y: m.t + rowH * i, width: W, height: rowH });
        hit.addEventListener("pointermove", (ev) => {
          const r = svg.getBoundingClientRect();
          showTip(tip, box, (ev.clientX - r.left), (ev.clientY - r.top), g.label, [
            { color, value: fmt(g.mean), name: `平均（n=${g.n}${g.manual ? "，人工" : ""}）` },
            { value: `${fmt(g.min)} – ${fmt(g.max)}`, name: "最差 – 最好" },
            { value: g.runs.map((x) => fmt(x.total, 0) + (x.failed ? "✗" : "")).join(" / "), name: "各次" }]);
        });
        hit.addEventListener("pointerleave", () => hideTip(tip));
        svg.append(hit);
      });
      box.prepend(svg);
    });
  }

  /* Score versus cost: one labelled dot per group (labels carry identity). */
  function scatter(points) {
    return chartBox((box, W, tip) => {
      const H = 300, m = { l: 48, r: 24, t: 22, b: 36 };
      const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
      const xlo = Math.min(...xs) / 1.6, xhi = Math.max(...xs) * 1.6;
      const yhi = Math.max(110, ...ys) * 1.05;
      const X = scale(xlo, xhi, m.l, W - m.r, true), Y = scale(0, yhi, H - m.b, m.t);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "平均分与平均费用" });
      for (const v of niceTicks(0, yhi, 4)) {
        svg.append(sv("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
        svg.append(sv("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, compact(v)));
      }
      for (const v of logTicks(xlo, xhi)) svg.append(sv("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, fmtUsd(v)));
      svg.append(sv("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
      svg.append(sv("text", { x: W - m.r, y: H - 4, "text-anchor": "end" }, "每次运行的平均费用（对数）"));
      svg.append(sv("text", { x: 4, y: 11 }, "平均分"));
      svg.append(sv("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(100), y2: Y(100) }));
      for (const p of points) {
        const cx = X(p.x), cy = Y(p.y), color = slotVar(p.slot);
        svg.append(sv("circle", { cx, cy, r: 5, fill: color, stroke: "var(--surface)", "stroke-width": 2 }));
        const textW = [...p.label.slice(0, 40)].reduce((w, ch) => w + (ch.charCodeAt(0) > 255 ? 12 : 6.5), 0);
        const right = cx + 9 + textW < W - 4;
        svg.append(sv("text", { x: cx + (right ? 9 : -9), y: cy + 4, "text-anchor": right ? "start" : "end", class: "label-ink" },
          p.label.length > 40 ? p.label.slice(0, 39) + "…" : p.label));
        const hit = sv("circle", { class: "hit", cx, cy, r: 14 });
        hit.addEventListener("pointermove", (ev) => {
          const r = svg.getBoundingClientRect();
          showTip(tip, box, ev.clientX - r.left, ev.clientY - r.top, p.label, [
            { color, value: fmt(p.y), name: "平均分" }, { value: fmtUsd(p.x), name: "平均费用" },
            { value: p.per ? fmt(p.per) : "–", name: "分 / 美元" }]);
        });
        hit.addEventListener("pointerleave", () => hideTip(tip));
        svg.append(hit);
      }
      box.prepend(svg);
    });
  }

  /* Agent activity: one lane per kind of work, a block per call. */
  function swimlane(items, events, xmax) {
    const LANES = [["model", "模型生成"], ["check", "make check"], ["shell", "其他命令"], ["edit", "编辑文件"], ["read", "查看文件"], ["other", "其他工具"]];
    const used = LANES.filter(([k]) => items.some((i) => i.lane === k));
    return chartBox((box, W, tip) => {
      const laneH = 22, m = { l: 92, r: 12, t: 22, b: 28 };
      const H = m.t + m.b + laneH * used.length;
      const X = scale(0, xmax, m.l, W - m.r);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Agent 活动时间线" });
      for (const v of niceTicks(0, xmax, Math.max(3, Math.floor(W / 110)))) {
        svg.append(sv("line", { class: "grid", x1: X(v), x2: X(v), y1: m.t, y2: H - m.b }));
        svg.append(sv("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, `${v} 分`));
      }
      used.forEach(([k, label], i) => {
        const y = m.t + i * laneH;
        svg.append(sv("text", { x: m.l - 8, y: y + laneH / 2 + 4, "text-anchor": "end", class: "label-ink" }, label));
        svg.append(sv("rect", { x: m.l, y: y + 3, width: W - m.l - m.r, height: laneH - 6, fill: "var(--wash)", rx: 2 }));
      });
      const laneIndex = Object.fromEntries(used.map(([k], i) => [k, i]));
      const colorOf = (it) => it.lane === "check" ? (it.ok ? "var(--st-good)" : "var(--st-crit)") :
        it.lane === "model" ? "var(--s1)" : it.ok === false ? "var(--st-crit)" : "var(--ink-2)";
      for (const it of items) {
        const i = laneIndex[it.lane];
        if (i == null || !isNum(it.s)) continue;
        const x1 = X(Math.max(0, it.s)), x2 = X(Math.min(xmax, Math.max(it.e ?? it.s, it.s)));
        svg.append(sv("rect", { x: x1, y: m.t + i * laneH + 4, width: Math.max(1.5, x2 - x1), height: laneH - 8, fill: colorOf(it), rx: 1, opacity: it.lane === "model" ? 0.55 : 0.85 }));
      }
      eventLines(svg, events.filter((ev) => isNum(ev.t_min)).map((ev) => ({ x: ev.t_min, label: ev.label })), X, m.t - 4, H - m.b, m.t - 8);
      const cross = sv("line", { class: "crosshair", y1: m.t, y2: H - m.b, style: "display:none" });
      svg.append(cross);
      const hit = sv("rect", { class: "hit", x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b });
      hit.addEventListener("pointermove", (ev) => {
        const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width), py = (ev.clientY - r.top) * (H / r.height);
        const t = X.inv(px), lane = used[Math.floor((py - m.t) / laneH)];
        cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.style.display = "";
        const tol = (xmax / (W - m.l - m.r)) * 3;
        const hits = items.filter((it) => lane && it.lane === lane[0] && it.s - tol <= t && (it.e ?? it.s) + tol >= t).slice(0, 4);
        showTip(tip, box, ev.clientX - r.left, ev.clientY - r.top, `${fmtMin(t)}${lane ? " · " + lane[1] : ""}`,
          hits.length ? hits.map((h) => ({ color: colorOf(h), value: fmt(((h.e ?? h.s) - h.s) * 60, 1) + " s", name: h.label || "" })) : [{ value: "", name: "（空闲）" }]);
      });
      hit.addEventListener("pointerleave", () => { cross.style.display = "none"; hideTip(tip); });
      svg.append(hit);
      box.prepend(svg);
    });
  }

  function heatColor(score) {
    const bins = [10, 25, 40, 60, 80, 100];
    let i = bins.findIndex((b) => score < b);
    if (i < 0) i = 6;
    const vars = ["--q100", "--q200", "--q300", "--q400", "--q500", "--q600", "--q700"];
    return [`var(${vars[i]})`, i >= 4 ? "#ffffff" : "#0b0b0b"];
  }

  // ------------------------------------------------------------ code
  const KW = new Set("alignas alignof auto bool break case catch char class const constexpr consteval constinit continue decltype default delete do double else enum explicit extern false float for friend goto if inline int long mutable namespace new noexcept nullptr operator private protected public register return short signed sizeof static static_assert static_cast struct switch template this thread_local throw true try typedef typename union unsigned using virtual void volatile while uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t size_t std".split(" "));
  const TOKEN = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(^[ \t]*#[^\n]*)|(\b\d[\w.']*)|([A-Za-z_]\w*)/gm;
  function highlightLine(line, state) {
    // state.comment: inside a /* */ block carried over from previous lines.
    const frag = document.createDocumentFragment();
    let rest = line;
    if (state.comment) {
      const end = rest.indexOf("*/");
      if (end < 0) { frag.append(el("span", { class: "tok-com" }, rest)); return frag; }
      frag.append(el("span", { class: "tok-com" }, rest.slice(0, end + 2)));
      rest = rest.slice(end + 2);
      state.comment = false;
    }
    let last = 0, mt;
    TOKEN.lastIndex = 0;
    while ((mt = TOKEN.exec(rest))) {
      if (mt.index > last) frag.append(rest.slice(last, mt.index));
      const [tok, com, str, pp, num, word] = mt;
      if (com) {
        if (com.startsWith("/*") && !com.endsWith("*/")) state.comment = true;
        frag.append(el("span", { class: "tok-com" }, com));
      } else if (str) frag.append(el("span", { class: "tok-str" }, str));
      else if (pp) frag.append(el("span", { class: "tok-pp" }, pp));
      else if (num) frag.append(el("span", { class: "tok-num" }, num));
      else if (word && KW.has(word)) frag.append(el("span", { class: "tok-kw" }, word));
      else frag.append(tok);
      last = mt.index + tok.length;
    }
    if (last < rest.length) frag.append(rest.slice(last));
    if (/\/\*(?![\s\S]*\*\/)/.test(rest.replace(/"(?:[^"\\]|\\.)*"/g, ""))) state.comment = true;
    return frag;
  }
  function codeBlock(text, highlight) {
    const box = el("div", { class: "code" });
    const state = { comment: false };
    text.split("\n").forEach((line, i) => box.append(el("div", { class: "ln" }, el("span", { class: "n" }, i + 1),
      el("span", { class: "t" }, highlight ? highlightLine(line, state) : line))));
    return box;
  }
  function diffBlock(lines) {
    // lines: unified diff strings, or [sign, text] pairs
    const box = el("div", { class: "code" });
    for (const l of lines) {
      const [sign, text] = Array.isArray(l) ? l : [l.startsWith("+++") || l.startsWith("---") ? "h" : l[0] || " ", l];
      const cls = sign === "+" ? "add" : sign === "-" ? "del" : sign === "@" || sign === "h" ? "hunk" : "";
      box.append(el("div", { class: "ln " + cls }, el("span", { class: "n" }, Array.isArray(l) ? sign : ""), el("span", { class: "t" }, text)));
    }
    return box;
  }
  function lazyDetails(summary, build, open) {
    const d = el("details", { open: open ? "" : null }, el("summary", {}, summary));
    let built = false;
    const go = () => { if (!built && d.open) { built = true; d.append(build()); } };
    d.addEventListener("toggle", go);
    if (open) requestAnimationFrame(go);
    return d;
  }

  // ------------------------------------------------------------ index page
  function renderIndex() {
    const S = DATA.season, groups = DATA.groups;
    app.append(el("header", { class: "top" },
      el("div", {}, el("h1", {}, `LLMBench 铁路订票 · 赛季 ${S.name}`),
        el("div", { class: "sub" }, `${groups.length} 个参赛单位 · ${groups.reduce((a, g) => a + g.n, 0)} 次正式运行 · 赛季准备于 ${S.created || "–"} · 指纹 ${S.fingerprint}`)),
      el("div", { class: "spacer" }), themeButton()));
    app.append(el("nav", { class: "toc" }, [["board", "排行"], ["table", "汇总表"], ["heat", "分项"], ["cost", "分数与费用"], ["time", "分数随时间"], ["others", "其他运行"], ["season", "赛季信息"]]
      .map(([id, t]) => el("a", { href: "#" + id }, t))));

    if (!groups.length) app.append(el("div", { class: "banner info" }, el("strong", {}, "还没有正式运行。"),
      `正式运行需要：正式模式（--strict）、时限 ${DATA.standard_minutes} 分钟、用本赛季评分、任务包和提示词版本与赛季一致。其他运行列在“其他运行”里。`));

    app.append(section("board", "排行",
      note(`点 = 平均分，横线 = 最差到最好，短竖线 = 每次运行（空心表示该次有场景判错、崩溃或超内存）。总分 = 性能 65% + 功能 10% + 周转量 12% + 偏好 8% + 内存 5%；和强参考完全一样的实现得 ${fmt(DATA.ref_total)} 分，不封顶。`),
      card(groups.length ? dotWhisker(groups) : note("无数据"))));

    const anyCost = groups.some((g) => g.cost);
    const ends = (g) => Object.entries(g.ends).map(([k, v]) => `${END[k] || k}×${v}`).join(" ");
    app.append(section("table", "汇总表", card(table([
      { label: "#", num: true, sort: (g) => g.rank }, { label: "参赛单位", sort: (g) => g.label },
      { label: "n", num: true, sort: (g) => g.n }, { label: "平均", num: true, sort: (g) => g.mean },
      { label: "最差", num: true, sort: (g) => g.min }, { label: "最好", num: true, sort: (g) => g.max },
      { label: "性能", num: true, sort: (g) => g.perf }, { label: "功能", num: true, sort: (g) => g.functional },
      { label: "周转量", num: true, sort: (g) => g.transport }, { label: "偏好", num: true, sort: (g) => g.pref },
      { label: "内存", num: true, sort: (g) => g.memory },
      { label: "通过率", num: true, sort: (g) => g.pass_rate, title: "9 组测量中没有失败（超时、判错、门槛）的比例" },
      { label: "平均用时", num: true, sort: (g) => g.elapsed }, { label: "结束方式" },
      { label: "平均费用", num: true, sort: (g) => g.cost }, { label: "分 / 美元", num: true, sort: (g) => g.per_usd },
      { label: "平均 token", num: true, sort: (g) => g.tokens }, { label: "工具版本" }],
      groups.map((g, i) => (g.rank = i + 1, {
        data: g, cells: [i + 1,
          el("span", {}, el("span", { class: "key", style: `background:${slotVar(g.slot)}` }), g.label, g.manual ? el("span", { class: "chip", style: "margin-left:6px" }, "人工") : null),
          g.n, fmt(g.mean), fmt(g.min), fmt(g.max), fmt(g.perf), fmt(g.functional), fmt(g.transport), fmt(g.pref), fmt(g.memory), fmt(g.pass_rate * 100, 0) + "%",
          fmtMin(g.elapsed), ends(g), fmtUsd(g.cost), g.per_usd ? fmt(g.per_usd) : "–", compact(g.tokens),
          el("span", {}, g.versions.join(", ") || "–", g.versions.length > 1 ? el("span", { class: "chip fail", title: "这一组的运行用了不同版本的工具", style: "margin-left:4px" }, "多版本") : null)],
      }))),
      note("总分 = 性能 65% + 功能 10% + 周转量 12% + 偏好 8% + 内存 5%。性能：0 = 基线，100 = 强参考，比基线慢为负，失败的测量 −100。功能：功能测试集各类别通过率的平均。周转量：与强参考持平 70 分，少 7% 为 0，多 3% 为满分 100。内存：不超过强参考 100 分，4 倍及以上 0 分。通过率 = 9 组测量中没有被清零的比例（判错、崩溃、超内存，或订票满足率低于强参考的 85%）。分 / 美元 = 平均分 ÷ 平均费用。点击表头排序。"))));

    // heatmap
    const keys = DATA.keys;
    const heat = el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "参赛单位"), keys.map((k) => {
      const [sc, t] = k.split("/");
      return el("th", { style: "text-align:center" }, SCEN[sc] || sc, el("br"), el("span", { class: "muted" }, `${t.slice(1)} 线程`));
    }))),
      el("tbody", {}, groups.map((g) => el("tr", {}, el("td", { class: "nowrap" }, el("span", { class: "key", style: `background:${slotVar(g.slot)}` }), g.label),
        keys.map((k) => {
          const [bg, fg] = heatColor(g.heat[k] || 0);
          const failed = g.heat_failed[k];
          return el("td", { class: "heat", style: `background:${bg};color:${fg}`, title: `${g.label} · ${keyLabel(k)}：平均 ${fmt(g.heat[k])}${failed ? `，${failed} 次被清零` : ""}` },
            (failed ? "✗ " : "") + fmt(g.heat[k], 0));
        })))));
    app.append(section("heat", "分项得分", card(el("div", { class: "scroll-x" }, heat),
      note("每格是该组测量的平均性能分（0 = 基线，100 = 强参考，比基线慢为负，最低 −100）。✗ 表示至少一次运行在该组记了最低分（超时、判错、崩溃、超内存、致命错误，或订票满足率低于强参考的 85%）。颜色越深分数越高，100 以上同色。"))));

    const pts = groups.filter((g) => g.cost > 0).map((g) => ({ x: g.cost, y: g.mean, label: g.label, slot: g.slot, per: g.per_usd }));
    app.append(section("cost", "分数与费用", card(pts.length ? scatter(pts) : note("没有费用数据（OpenCode 自定义提供商未配置价格，或人工模式未填写费用）。"),
      pts.length ? note("横轴是每次运行的平均费用（对数刻度），越靠左上越划算。") : null)));

    app.append(section("time", "分数随时间", card(lineChart({
      series: groups.map((g) => ({ name: g.label, color: slotVar(g.slot), points: g.curve, step: true })),
      x: { min: 0, max: DATA.standard_minutes, fmt: (v) => `${v}`, label: "分钟", unit: " 分钟" }, y: { min: Math.min(0, ...groups.flatMap((g) => g.curve.map((p) => p[1]))), label: "平均分", fmt: (v) => compact(v) },
      refs: [{ y: DATA.ref_total, label: "强参考" }], height: 280, fmtTip: (s, p) => fmt(p[1]),
      table: { headers: ["分钟", ...groups.map((g) => g.label)], rows: (groups[0]?.curve || []).map((p, i) => [p[0], ...groups.map((g) => fmt(g.curve[i][1]))]) },
    }), note("每个参赛单位各次运行在同一时刻的快照得分取平均。中间快照用精简评测（只测 4 线程、不跑 TSan），最后一张是完整评测，两者不完全可比。"))));

    const others = DATA.others;
    const cat = { nonstandard: "非标准运行", reference: "参考实现", unscored: "未评分" };
    app.append(section("others", "其他运行", card(others.length ? table([
      { label: "类别", sort: (r) => r.category }, { label: "运行", sort: (r) => r.label }, { label: "目录" },
      { label: "总分", num: true, sort: (r) => r.final?.total }, { label: "模式" }, { label: "开始", sort: (r) => r.start }, { label: "原因" }],
      others.map((r) => ({ data: r, cells: [cat[r.category] || r.category, el("a", { href: r.page }, r.label), r.dir,
        r.final ? fmt(r.final.total) : "–", r.manual ? `人工 · ${r.mode}` : r.mode || "–", r.start || "–",
        r.category === "unscored" ? "还没有评分结果" : r.reasons.join("；") || "–"] }))) : note("无"))));

    const w = S.world || {}, mach = S.machine || {};
    app.append(section("season", "赛季信息", card(
      el("div", { class: "tiles" },
        tile("车站", fmtInt(w.stations)), tile("线路", fmtInt(w.lines)), tile("车次", fmtInt(w.trains)),
        ...Object.entries(S.intents || {}).map(([k, v]) => tile(`${SCEN[k] || k} 意图数`, compact(v)))),
      el("p", { class: "note" }, `评测机：${mach.cpu || "–"}，${mach.logical_cpus || "–"} 个逻辑核；${mach.distro || mach.os || "–"}；${mach.compiler || "–"}；沙箱 ${S.sandbox || "–"}；测量用 CPU ${S.cpus || "–"}；任务版本 ${S.task_version || "–"}。`),
      el("h3", {}, "锚点"),
      table([{ label: "测量" }, { label: "基线吞吐", num: true }, { label: "基线查询 p99", num: true }, { label: "基线订票 p99", num: true }, { label: "基线用时", num: true },
        { label: "强参考吞吐", num: true }, { label: "强参考查询 p99", num: true }, { label: "强参考订票 p99", num: true }],
      S.anchors.map((a) => ({ data: a, cells: [keyLabel(a.key), compact(a.baseline_throughput) + "/s", fmtUs(a.baseline_query_p99_ns / 1e3), fmtUs(a.baseline_book_p99_ns / 1e3),
        fmt(a.baseline_wall_s, a.baseline_wall_s < 10 ? 1 : 0) + " s", compact(a.strong_throughput) + "/s", fmtUs(a.strong_query_p99_ns / 1e3), fmtUs(a.strong_book_p99_ns / 1e3)] }))),
      note("吞吐 = 每秒完成的调用数。分数按与强参考之比 r 的幂曲线计：基线 0 分、强参考 100 分，r = 0.5 约 65 分，单项最多 150 分；比基线慢为负分，最低 −100，超时、判错的测量也记 −100。"))));
  }

  // ------------------------------------------------------------ run page
  function renderRun() {
    const R = DATA.run, F = R.final, A = DATA.activity, color = slotVar(R.slot || 1);
    app.append(el("header", { class: "top" },
      el("div", {}, el("div", { class: "sub" }, el("a", { href: DATA.index }, `← 赛季 ${DATA.season.name}`)),
        el("h1", {}, R.label), el("div", { class: "sub" }, `${R.dir} · ${R.manual ? "人工 · " : ""}${R.mode === "strict" ? "正式模式" : R.mode === "trial" ? "试跑模式" : R.mode || ""} · 开始于 ${R.start || "–"}`)),
      el("div", { class: "spacer" }), themeButton()));
    const toc = [["overview", "概览"], ["curve", "得分曲线"], ["measure", "测量"], ["dist", "延迟与吞吐"], ["correct", "正确性"], ["activity", "Agent 活动"], ["code", "代码"], ["transcript", "会话记录"]];
    app.append(el("nav", { class: "toc" }, toc.map(([id, t]) => el("a", { href: "#" + id }, t))));

    const banners = [];
    if (F?.fatal) banners.push(el("div", { class: "banner crit" }, el("strong", {}, "总分 0："), FATAL[F.fatal] || F.fatal));
    if (F?.needs_review) banners.push(el("div", { class: "banner warn" }, el("strong", {}, "需要人工复核："), "静态扫描发现可疑用法，见“正确性”一节。"));
    if (R.category === "nonstandard") banners.push(el("div", { class: "banner info" }, el("strong", {}, "非标准运行，不参与排名："), R.reasons.join("；")));
    if (R.category === "unscored") banners.push(el("div", { class: "banner info" }, el("strong", {}, "还没有评分结果。"), "用 railway_eval.py snapshots 评分后重新生成报告。"));
    if (R.category === "reference") banners.push(el("div", { class: "banner info" }, el("strong", {}, "参考实现空跑："), "用来核对整条流程，不参与排名。"));
    app.append(...banners);

    const u = R.usage || {};
    const st = A?.stats || {};
    app.append(section("overview", "概览", el("div", { class: "tiles" },
      tile("总分", F ? fmt(F.total) : "–", F ? `性能 ${fmt(F.performance)} · 功能 ${fmt(F.functional)} · 周转量 ${fmt(F.transport)} · 偏好 ${fmt(F.preference)} · 内存 ${fmt(F.memory)}` : null, true),
      tile("结束方式", END[R.end_reason] || R.end_reason || "–", `用时 ${fmtMin(R.elapsed_min)}`),
      tile("续跑次数", R.resumes ?? "–"),
      tile("费用", fmtUsd(u.cost), u.source === "manual" ? "人工填写" : null),
      tile("token", compact((u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0) + (u.output || 0)),
        `输入 ${compact(u.input)} · 缓存读 ${compact(u.cache_read)} · 输出 ${compact(u.output)}`),
      tile("首次通过 make check", isNum(st.first_pass_min) ? fmtMin(st.first_pass_min) : "–", A ? `${st.checks_passed}/${st.checks} 次通过` : "无会话记录"),
      tile("失败的测量", F ? `${F.gated} / 9` : "–"),
      tile("工具", `${R.tool_name || "–"}`, `${R.tool_version || ""} · ${R.model || ""}${R.variant ? ` (${R.variant})` : ""}`))));

    // score curve
    const evs = DATA.events || [];
    const EVL = { notice: "提醒", resume: "续跑", submitted: "交卷", deadline: "收卷" };
    const vlines = evs.filter((e) => EVL[e.kind]).map((e) => ({ x: e.t_min, label: EVL[e.kind] }));
    const curve = R.curve || [];
    const curveCard = card(curve.length ? lineChart({
      series: [{ name: "快照得分（隐藏负载）", color, points: curve.map((p) => [p.t, p.total, p]), step: true, markers: true, extendTo: Math.max(R.elapsed_min || 0, ...curve.map((p) => p.t)),
        hollow: (p) => p[2].mode !== "full" }],
      x: { min: 0, max: Math.max(R.minutes || DATA.standard_minutes, R.elapsed_min || 0), label: "分钟", unit: " 分钟", fmt: (v) => `${Math.round(v)}` },
      y: { min: Math.min(0, ...curve.map((p) => p.total)), label: "总分" }, refs: [{ y: DATA.ref_total, label: "强参考" }], vlines, height: 240, legend: false,
      fmtTip: (s, p) => `${fmt(p[1])}（${p[2].name}，${p[2].mode === "full" ? "完整评测" : "精简评测"}）`,
      table: { headers: ["快照", "分钟", "总分", "评测"], rows: curve.map((p) => [p.name, fmt(p.t), fmt(p.total), p.mode]) },
    }) : note("没有评分结果"), note("实心点 = 完整评测，空心点 = 精简评测（只测 4 线程、不跑 TSan），两者不完全可比。竖线是时间提醒、续跑和交卷 / 收卷。"));
    const checks = A?.checks || [];
    let checkCard = null;
    if (checks.length) {
      const sc = ["daily", "spring", "refund_wave"];
      checkCard = card(el("h3", {}, "Agent 自测：make check 的公开场景吞吐"), lineChart({
        series: sc.map((s, i) => ({ name: SCEN[s], color: `var(--s${i + 1})`, markers: true,
          points: checks.filter((c) => c.scenarios[s]).map((c) => [c.t, c.scenarios[s].throughput, c]), hollow: (p) => !p[2].scenarios[s].ok })),
        x: { min: 0, max: Math.max(R.minutes || DATA.standard_minutes, R.elapsed_min || 0), label: "分钟", unit: " 分钟", fmt: (v) => `${Math.round(v)}` },
        y: { log: true, label: "调用/秒（对数）" }, height: 220, fmtTip: (s, p) => `${compact(p[1])}/s${p[2].passed ? "" : "（本次未全部通过）"}`,
        table: { headers: ["分钟", "通过", ...sc.map((s) => SCEN[s] + " 吞吐")], rows: checks.map((c) => [fmt(c.t), c.passed ? "是" : "否", ...sc.map((s) => c.scenarios[s] ? fmtInt(c.scenarios[s].throughput) : "–")]) },
      }), note("来自 Agent 在工作区里运行的 make check（公开负载、4 线程），只用来解释过程，从不参与评分；数据由 Agent 的工作区产生，理论上可被伪造。空心点 = 该场景判错。"),
      el("p", { class: "note" }, `共 ${checks.length} 次 make check，通过 ${checks.filter((c) => c.passed).length} 次。`));
    }
    app.append(section("curve", "得分曲线", curveCard, checkCard));

    // measurements
    const M = DATA.measurements;
    if (M) {
      const maxScore = Math.max(110, ...M.map((r) => r.score));
      app.append(section("measure", "测量明细", card(table([
        { label: "测量", sort: (r) => r.key }, { label: "状态", sort: (r) => r.status }, { label: "得分", num: true, sort: (r) => r.score },
        { label: "", cls: "bar-cell" }, { label: "吞吐分", num: true, sort: (r) => r.thr_score }, { label: "延迟分", num: true, sort: (r) => r.lat_score },
        { label: "吞吐", num: true, sort: (r) => r.throughput }, { label: "基线 / 强参考", num: true },
        { label: "查询 p99", num: true, sort: (r) => r.query_p99_us }, { label: "订票 p99", num: true, sort: (r) => r.book_p99_us },
        { label: "init", num: true, sort: (r) => r.init_s }, { label: "峰值内存", num: true, sort: (r) => r.maxrss_mb }, { label: "重复", num: true }],
      M.map((r) => {
        const reps = r.repeats || [];
        const thrRange = reps.length > 1 ? `${compact(Math.min(...reps.map((x) => x.throughput)))}–${compact(Math.max(...reps.map((x) => x.throughput)))}` : "";
        return { data: r, cells: [keyLabel(r.key), statusChip(r.status), fmt(r.score),
          el("div", { class: "bar-track", title: `${fmt(r.score)} 分` }, el("div", { class: "bar-fill", style: `width:${Math.min(100, Math.max(0, r.score) / maxScore * 100)}%;background:${color}` }),
            el("div", { class: "bar-ref", style: `left:${100 / maxScore * 100}%` })),
          fmt(r.thr_score), fmt(r.lat_score),
          el("span", { title: thrRange ? `各次重复：${thrRange}/s` : "" }, isNum(r.throughput) ? compact(r.throughput) + "/s" : "–"),
          el("span", { class: "muted" }, `${compact(r.base.throughput)} / ${compact(r.strong.throughput)}`),
          el("span", { title: `基线 ${fmtUs(r.base.query_p99_us)}，强参考 ${fmtUs(r.strong.query_p99_us)}` }, fmtUs(r.query_p99_us)),
          el("span", { title: `基线 ${fmtUs(r.base.book_p99_us)}，强参考 ${fmtUs(r.strong.book_p99_us)}` }, fmtUs(r.book_p99_us)),
          isNum(r.init_s) ? fmt(r.init_s, 2) + " s" : "–",
          el("span", { title: r.mem_limit_mb ? `上限 ${fmtInt(r.mem_limit_mb)} MB` : "" }, isNum(r.maxrss_mb) ? `${fmtInt(r.maxrss_mb)} MB` : "–"),
          el("span", { title: thrRange ? `吞吐 ${thrRange}/s` : "" }, r.runs ?? "–")] };
      })), note("得分条的竖线是 100 分（强参考）。吞吐、p99 取多次重复的中位数；悬停可看各次重复的范围和参考值。峰值内存包含压测器自身的占用。"),
        DATA.build ? el("p", { class: "note" }, `编译 ${fmt(DATA.build.compile_s, 1)} s，二进制 ${compact(DATA.build.binary_bytes)}B（含压测器）。`) : null)));

      // distributions
      const sel = el("select", { "aria-label": "选择测量" }, DATA.keys.map((k) => el("option", { value: k }, keyLabel(k))));
      sel.value = "daily/t4";
      const holder = el("div");
      const drawDist = () => {
        holder.textContent = "";
        const row = M.find((r) => r.key === sel.value), d = row.dist;
        const runName = "本次运行";
        const ser = (getter) => [["sub", runName, color], ["base", "基线", "var(--s-other)"], ["strong", "强参考", "var(--ink-2)"]]
          .map(([k, name, c]) => ({ name, color: c, points: (d[k] && getter(d[k])) || [] })).filter((s) => s.points.length);
        if (!d.sub) { holder.append(note(row.status === "ok" ? "这组测量没有分布数据（旧版评测）。" : "这组测量没有成功完成，没有分布数据。")); return; }
        const cdf = (op) => lineChart({ series: ser((x) => x[op]), x: { log: true, label: "延迟（对数）", fmt: (v) => fmtUs(v) },
          y: { min: 0, max: 1, tight: true, label: "累计比例", fmt: (v) => `${Math.round(v * 100)}%` }, height: 220, snap: false,
          refs: [{ y: 0.99, label: "p99" }], fmtTip: (s, p) => `${(p[1] * 100).toFixed(1)}% ≤ ${fmtUs(p[0])}` });
        holder.append(el("div", { class: "grid2" }, card(el("h3", {}, "查询延迟分布"), cdf("query")), card(el("h3", {}, "订票延迟分布"), cdf("book"))));
        holder.append(card(el("h3", {}, "压测期间的吞吐"), lineChart({ series: ser((x) => x.timeline),
          x: { min: 0, max: 100, label: "主阶段进度 %", fmt: (v) => `${Math.round(v)}%` }, y: { min: 0, label: "调用/秒" }, height: 220,
          fmtTip: (s, p) => compact(p[1]) + "/s" }),
          note("把并发阶段切成 50 段，每段的完成速度。曲线越往后越低，通常说明锁竞争或数据结构随订单增多而变慢。")));
        if (!d.base && !d.strong) holder.append(note("赛季锚点没有分布数据（用旧版 prepare 准备的赛季），只显示本次运行。"));
      };
      sel.addEventListener("change", drawDist);
      app.append(section("dist", "延迟与吞吐", el("div", { class: "flex" }, el("span", { class: "note" }, "测量："), sel), holder));
      drawDist();

      // correctness & preference
      const C = DATA.correctness, P = DATA.preference;
      const corr = card(table([{ label: "场景" }, { label: "结论" }, { label: "错误统计" }],
        C.scenarios.map((s) => ({ data: s, cells: [SCEN[s.scenario] || s.scenario,
          s.failure ? el("span", { style: "color:var(--st-crit)" }, "✗ " + s.failure) : (F.fatal ? "–" : el("span", {}, "✓ 通过")),
          s.error_counts ? Object.entries(s.error_counts).map(([k, v]) => `${k}×${v}`).join("，") : "–"] }))));
      for (const s of C.scenarios) if (s.errors?.length) corr.append(lazyDetails(`${SCEN[s.scenario]}：前 ${s.errors.length} 条错误`, () => el("pre", { class: "out" }, s.errors.join("\n"))));
      for (const [k, v] of Object.entries(C.logs)) corr.append(lazyDetails(k === "compile_log" ? "编译输出" : k === "tsan_report" ? "TSan 报告" : k, () => el("pre", { class: "out" }, v)));
      if (C.flags.length) corr.append(el("h3", {}, "静态扫描"), table([{ label: "位置" }, { label: "原因" }, { label: "代码" }],
        C.flags.map((f) => ({ data: f, cells: [`${f.file}:${f.line}`, f.reason, el("code", {}, f.text)] }))));
      const pref = card(el("h3", {}, `座位偏好（${P.threads} 线程运行的静默阶段）`), table([{ label: "场景" }, { label: "靠窗满足率", num: true }, { label: "强参考", num: true }, { label: "同行满足率", num: true }, { label: "强参考", num: true }],
        P.rows.map((r) => ({ data: r, cells: [SCEN[r.scenario], pct(r.window), pct(r.strong_window), pct(r.together), pct(r.strong_together)] }))));
      const Q = DATA.quality;
      const ratio = (a, b) => (isNum(a) && isNum(b) && b > 0 ? a / b : null);
      const qual = Q && card(el("h3", {}, `订票质量（${Q.threads} 线程运行的主阶段）`), table([
        { label: "场景" }, { label: "满足率", num: true, title: "买到票的购票请求 ÷ 查询到方案的购票请求" }, { label: "强参考", num: true },
        { label: "门槛", title: `不低于强参考的 ${fmt(Q.gate * 100, 0)}%，否则整个场景清零` },
        { label: "周转量（人·分钟）", num: true }, { label: "÷ 强参考", num: true }, { label: "周转量分", num: true },
        { label: "利用率", num: true, title: "拒绝过订票的车次-日期-席别上，卖出的座位分钟 ÷ 运力（仅展示）" }, { label: "强参考", num: true },
        { label: "拒售", num: true, title: "返回 Withheld 的订票次数" }],
        Q.rows.map((r) => {
          const q = r.q || {}, sq = r.strong || {}, sr = ratio(q.satisfaction, sq.satisfaction);
          const gate = r.gate ? (r.gate.passed ? el("span", { class: "status" }, `✓ ${pct(r.gate.ratio)}`) : el("span", { class: "status", style: "color:var(--st-crit)" }, `✗ ${pct(r.gate.ratio)}`)) : (isNum(sr) ? pct(sr) : "–");
          return { data: r, cells: [SCEN[r.scenario], pct(q.satisfaction), pct(sq.satisfaction), gate,
            compact(q.passenger_minutes), pct(isNum(r.transport_ratio) ? r.transport_ratio : ratio(q.passenger_minutes, sq.passenger_minutes)),
            fmt(r.transport), pct(q.utilization), pct(sq.utilization), isNum(q.withheld) ? q.withheld : "–"] };
        })),
        note(`周转量 = 买到票的旅客人数 × 所查询第一个方案的乘车分钟。与强参考持平得 70 分，少 ${fmt((1 - Q.floor) * 100, 0)}% 为 0 分，多 ${fmt((Q.ceil - 1) * 100, 0)}% 为满分 100。利用率只展示，不计分。`));
      const FN = DATA.functional;
      const func = FN && card(el("h3", {}, `功能测试（${Object.values(FN.categories).reduce((a, c) => a + c.total, 0)} 题${FN.crashes ? `，引擎崩溃 ${FN.crashes} 次` : ""}）`),
        table([{ label: "类别" }, { label: "通过", num: true }, { label: "通过率", num: true }, { label: "第一个失败" }],
          Object.entries(FN.categories).map(([k, c]) => ({ data: c, cells: [FUNC[k] || k, `${c.passed} / ${c.total}`,
            c.passed === c.total ? el("span", {}, "✓ 100%") : el("span", { style: "color:var(--st-crit)" }, pct(c.passed / c.total)),
            c.failures.length ? el("code", { class: "failmsg" }, c.failures[0].split("\n")[0]) : "–"] }))),
        note("每题单线程运行，只检查一条规则；查询结果和参考实现逐字比对，订票按规则校验（拒售总是允许）。功能分 = 各类别通过率的平均。"));
      const mem = Q && card(el("h3", {}, "内存"), table([{ label: "场景" }, { label: "引擎峰值内存", num: true, title: "该场景各次运行中最高的进程内存，减去压测器自身的占用" }, { label: "强参考", num: true }, { label: "倍数", num: true }, { label: "内存分", num: true }],
        Q.rows.map((r) => {
          const m = r.memory_mb || {};
          return { data: r, cells: [SCEN[r.scenario], isNum(m.engine) ? `${fmt(m.engine, 0)} MB` : "–", isNum(m.strong) ? `${fmt(m.strong, 0)} MB` : "–",
            isNum(m.engine) && m.strong ? `${fmt(m.engine / m.strong, 2)}×` : "–", fmt(r.memory)] };
        })), note(`不超过强参考得 100 分，每多一倍扣 50 分，${fmt(DATA.memory_zero, 0)} 倍及以上 0 分。`));
      app.append(section("correct", "正确性、功能与质量", corr, ...(func ? [func] : []), ...(qual ? [qual] : []), ...(mem ? [mem] : []), pref));
    }

    // activity
    if (A) {
      const xmax = Math.max(R.minutes || 0, R.elapsed_min || 0, ...A.swim.map((i) => i.e || 0)) || 1;
      const cum = A.cumulative;
      app.append(section("activity", "Agent 活动",
        el("div", { class: "tiles" },
          tile("模型生成时间", isNum(st.gen_pct) ? fmt(st.gen_pct, 0) + "%" : "–", "占总用时"),
          tile("工具执行时间", isNum(st.tool_pct) ? fmt(st.tool_pct, 0) + "%" : "–", "占总用时（含编译、压测）"),
          tile("模型调用", fmtInt(st.steps), `工具调用 ${fmtInt(st.tool_calls)}`),
          tile("最大上下文", compact(st.max_context), "单步输入 token（含缓存）"),
          tile("缓存命中率", isNum(st.cache_hit_pct) ? fmt(st.cache_hit_pct, 0) + "%" : "–"),
          tile("编译失败", fmtInt(st.compile_fail_calls), "出现编译错误的命令次数")),
        card(el("h3", {}, "时间线"), swimlane(A.swim, vlines.map((v) => ({ t_min: v.x, label: v.label })), xmax),
          note("模型生成 = 每一步从开始到结束；make check 绿色 = 通过，红色 = 失败；其他工具红色 = 出错。悬停查看每次调用。")),
        el("div", { class: "grid2" },
          card(el("h3", {}, "上下文大小"), lineChart({ series: [{ name: "单步输入 token", color, points: A.context }],
            x: { min: 0, max: xmax, label: "分钟", unit: " 分钟", fmt: (v) => `${Math.round(v)}` }, y: { min: 0, label: "token" }, height: 200, legend: false }),
            note("每次调用模型时输入的 token 数（含缓存）。突然下降通常是上下文被压缩。")),
          A.has_cost ? card(el("h3", {}, "累计费用"), lineChart({ series: [{ name: "累计费用", color, points: cum.map((c) => [c[0], c[1]]) }],
            x: { min: 0, max: xmax, label: "分钟", unit: " 分钟", fmt: (v) => `${Math.round(v)}` }, y: { min: 0, label: "美元", fmt: (v) => "$" + compact(v) }, height: 200, legend: false, fmtTip: (s, p) => fmtUsd(p[1]) }))
            : card(el("h3", {}, "累计输出 token"), lineChart({ series: [{ name: "累计输出 token", color, points: cum.map((c) => [c[0], c[2]]) }],
              x: { min: 0, max: xmax, label: "分钟", unit: " 分钟", fmt: (v) => `${Math.round(v)}` }, y: { min: 0, label: "token" }, height: 200, legend: false }))),
        el("div", { class: "grid2" },
          card(el("h3", {}, "工具调用次数"), table([{ label: "工具" }, { label: "次数", num: true }], st.tool_counts.map(([n, c]) => ({ data: [n, c], cells: [n, c] })))),
          card(el("h3", {}, "最常见的编译错误"), st.top_errors.length ? table([{ label: "错误" }, { label: "次数", num: true }],
            st.top_errors.map(([m2, c]) => ({ data: [m2, c], cells: [el("code", {}, m2), c] }))) : note("没有编译错误")))));
    } else {
      app.append(section("activity", "Agent 活动", card(note(R.manual ? "没有会话记录。人工模式可以用 run_manual.py attach --transcript 导入。" : "没有会话记录。"))));
    }

    // code
    const CODE = DATA.code;
    const fpRows = CODE.snapshots.map((s) => ({ data: s, cells: [s.name, fmtMin(s.t), s.files, fmtInt(s.lines), s.fp.labels.join("、") || "–",
      Object.entries(s.fp.counts).filter(([, v]) => v).map(([k, v]) => `${k}×${v}`).join(" ") || "–"] }));
    const codeCard = card(table([{ label: "快照" }, { label: "时刻", num: true }, { label: "文件", num: true }, { label: "行数", num: true }, { label: "并发策略（启发式）" }, { label: "同步原语计数" }], fpRows),
      note("并发策略由源码中的同步原语推断，只作参考：例如“互斥锁”可能是一把全局锁，也可能是少量粗粒度锁。"));
    for (const d of CODE.diffs) codeCard.append(lazyDetails(`${d.from} → ${d.to}：+${d.added} −${d.removed}${d.note ? "（" + d.note + "）" : ""}`, () => diffBlock(d.lines)));
    const srcCard = card(el("h3", {}, "最终提交的源码"));
    for (const f of CODE.source) srcCard.append(lazyDetails(`${f.path}（${fmtInt(f.text.split("\n").length)} 行${f.note ? "，" + f.note : ""}）`, () => codeBlock(f.text, true), CODE.source.length === 1));
    if (!CODE.source.length) srcCard.append(note("最终快照里没有源文件。"));
    app.append(section("code", "代码", codeCard, srcCard));

    // transcript
    const T = A?.entries || [];
    const txCard = card();
    if (!T.length) txCard.append(note("没有会话记录。"));
    else {
      const CHUNK = 150;
      let shown = 0;
      const list = el("div");
      const more = el("button", { class: "btn", type: "button" });
      const renderMore = () => {
        for (const e of T.slice(shown, shown + CHUNK)) list.append(txEntry(e));
        shown = Math.min(T.length, shown + CHUNK);
        more.textContent = `再显示 ${Math.min(CHUNK, T.length - shown)} 条（共 ${T.length} 条）`;
        more.style.display = shown < T.length ? "" : "none";
      };
      more.addEventListener("click", renderMore);
      txCard.append(note("工具输出只保留前后各 20 行（生成报告时加 --full-transcript 可保留全部）。密钥已替换为 ***。"), list, more);
      renderMore();
    }
    app.append(section("transcript", "会话记录", txCard));

    const mach = R.manual || DATA.machine ? DATA.machine : null;
    if (mach || DATA.eval_machine) app.append(section("machine", "运行环境", card(
      mach ? el("p", { class: "note" }, `Agent 机器：${mach.cpu || "–"}，${mach.logical_cpus || "–"} 个逻辑核；${mach.distro || mach.os || "–"}；${mach.compiler || "–"}`) : null,
      DATA.eval_machine ? el("p", { class: "note" }, `评测机器：${DATA.eval_machine.cpu || "–"}，${DATA.eval_machine.logical_cpus || "–"} 个逻辑核；${DATA.eval_machine.distro || DATA.eval_machine.os || "–"}；${DATA.eval_machine.compiler || "–"}`) : null)));
  }
  function pct(x) { return isNum(x) ? (x * 100).toFixed(1) + "%" : "–"; }

  function txEntry(e) {
    const time = el("span", {}, fmtMin(e.t));
    if (e.k === "user") return el("div", { class: "tx user" }, el("div", { class: "meta" }, time, " · 用户 / 运行器"), el("div", { class: "text" }, e.text));
    if (e.k === "assistant") {
      const box = el("div", { class: "tx assistant" }, el("div", { class: "meta" }, time, " · 模型"));
      if (e.reasoning) box.append(lazyDetails("推理过程", () => el("div", { class: "text", style: "color:var(--ink-2);font-size:13px" }, e.reasoning)));
      if (e.text) box.append(el("div", { class: "text" }, e.text));
      return box;
    }
    const head = [time, " · ", e.name || "工具", e.dur_s != null ? ` · ${e.dur_s} s` : "", " ",
      e.ok ? null : el("span", { class: "chip fail" }, e.exit != null ? `退出码 ${e.exit}` : "出错")];
    const summary = e.cmd ? e.cmd.split("\n")[0].slice(0, 160) : e.path || (e.args || "").slice(0, 160);
    return el("div", { class: "tx tool" }, el("div", { class: "meta" }, ...head),
      lazyDetails(summary || "（无参数）", () => {
        const kids = [];
        if (e.desc) kids.push(note(e.desc));
        if (e.cmd) kids.push(el("pre", { class: "out" }, "$ " + e.cmd));
        if (e.args) kids.push(el("pre", { class: "out" }, e.args));
        if (e.diff) kids.push(diffBlock(e.diff));
        if (e.out) kids.push(el("pre", { class: "out" }, e.out));
        return el("div", {}, ...kids);
      }));
  }

  if (DATA.page === "index") renderIndex(); else renderRun();
})();
