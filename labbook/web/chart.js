// Shared DOM helpers, formatters and three small SVG charts.
// Every data string goes in through textContent, never innerHTML.
(function () {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      // CSSOM writes are allowed under the page's style-src 'self' policy;
      // a style attribute would be blocked.
      else if (key === "style") node.style.cssText = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const fmt = {
    num(value, digits = 0) {
      if (value === null || value === undefined || Number.isNaN(value)) return "–";
      return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
    },
    compact(value) {
      if (value === null || value === undefined) return "–";
      return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
    },
    duration(seconds) {
      if (seconds === null || seconds === undefined) return "–";
      seconds = Math.round(seconds);
      if (seconds < 60) return `${seconds} s`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 60) return `${minutes} min`;
      const hours = Math.floor(minutes / 60);
      if (hours < 48) return `${hours} h ${minutes % 60} min`;
      return `${Math.floor(hours / 24)} d ${hours % 24} h`;
    },
    energy(wh) {
      if (wh === null || wh === undefined) return "–";
      return wh >= 1000 ? `${fmt.num(wh / 1000, 2)} kWh` : `${fmt.num(wh, wh < 10 ? 2 : 1)} Wh`;
    },
    bytes(value) {
      if (!value) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
      return `${fmt.num(value / 1024 ** index, index ? 1 : 0)} ${units[index]}`;
    },
    time(epoch, withDate = true) {
      if (!epoch) return "–";
      const date = new Date(epoch * 1000);
      return withDate
        ? date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
        : date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    },
    ago(seconds) {
      if (seconds === null || seconds === undefined) return "never";
      return seconds < 90 ? `${Math.round(seconds)} s ago` : `${fmt.duration(seconds)} ago`;
    },
  };

  // A clean axis: step of 1/2/2.5/5 × 10^k, 3-5 ticks, top at a multiple of it.
  function niceScale(value) {
    if (!value || value <= 0) return { max: 1, step: 0.25 };
    const raw = value / 4, power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((s) => s * power).find((s) => s >= raw);
    let digits = 0;
    while (digits < 6 && Math.abs(step * 10 ** digits - Math.round(step * 10 ** digits)) > 1e-9) digits++;
    return { max: Math.ceil(value / step - 1e-9) * step, step, digits };
  }

  function tooltip(container) {
    let tip = container.querySelector(":scope > .tooltip");
    if (!tip) {
      tip = el("div", { class: "tooltip", role: "status", hidden: true });
      container.append(tip);
    }
    return {
      show(x, y, ...rows) {
        tip.replaceChildren(...rows);
        tip.hidden = false;
        const width = tip.offsetWidth, box = container.clientWidth;
        tip.style.left = `${Math.max(0, Math.min(box - width, x + 12))}px`;
        tip.style.top = `${Math.max(0, y - tip.offsetHeight - 10)}px`;
      },
      hide() { tip.hidden = true; },
    };
  }

  // Single-series line over time: 2px line, 10% area wash, crosshair that
  // snaps to the nearest point, end dot with its value.
  function lineChart(container, points, options = {}) {
    const width = container.clientWidth || 560, height = options.height || 160;
    const pad = { left: 44, right: 54, top: 12, bottom: 24 };
    container.classList.add("chart");
    container.replaceChildren();
    if (!points.length) {
      container.append(el("div", { class: "empty" }, options.empty || "No readings yet"));
      return;
    }
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1;
    const scale = niceScale(Math.max(...ys) * 1.05), yMax = scale.max;
    const sx = (x) => pad.left + ((x - x0) / Math.max(1, x1 - x0)) * (width - pad.left - pad.right);
    const sy = (y) => height - pad.bottom - (y / yMax) * (height - pad.top - pad.bottom);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": options.label || "Line chart" });
    for (let value = 0; value <= yMax + 1e-9; value += scale.step) {
      const y = sy(value);
      root.append(svg("line", { class: value ? "gridline" : "baseline", x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
      root.append(svg("text", { class: "tick", x: pad.left - 6, y: y + 4, "text-anchor": "end" }, fmt.num(value, scale.digits)));
    }
    const ticks = Math.min(4, points.length);
    for (let i = 0; i < ticks; i++) {
      const t = x0 + ((x1 - x0) * i) / Math.max(1, ticks - 1);
      root.append(svg("text", { class: "tick", x: sx(t), y: height - 6, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle" },
        fmt.time(t / 1000, false)));
    }
    const path = points.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    root.append(svg("path", { class: "series-area", d: `${path}L${sx(x1)},${sy(0)}L${sx(x0)},${sy(0)}Z` }));
    root.append(svg("path", { class: "series-line", d: path }));
    const last = points[points.length - 1];
    root.append(svg("circle", { class: "dot", cx: sx(last[0]), cy: sy(last[1]), r: 4, fill: "var(--claude)" }));
    root.append(svg("text", { class: "tick", x: sx(last[0]) + 8, y: sy(last[1]) + 4 }, `${fmt.num(last[1])} ${options.unit || ""}`));
    const cross = svg("line", { class: "crosshair", y1: pad.top, y2: height - pad.bottom, visibility: "hidden" });
    const focus = svg("circle", { class: "dot", r: 4, fill: "var(--claude)", visibility: "hidden" });
    root.append(cross, focus);
    const hit = svg("rect", { x: pad.left, y: 0, width: width - pad.left - pad.right, height, fill: "transparent", tabindex: 0 });
    root.append(hit);
    container.append(root);
    const tip = tooltip(container);
    const pick = (clientX) => {
      const box = root.getBoundingClientRect();
      const x = ((clientX - box.left) / box.width) * width;
      let best = points[0];
      for (const p of points) if (Math.abs(sx(p[0]) - x) < Math.abs(sx(best[0]) - x)) best = p;
      cross.setAttribute("x1", sx(best[0])); cross.setAttribute("x2", sx(best[0]));
      focus.setAttribute("cx", sx(best[0])); focus.setAttribute("cy", sy(best[1]));
      cross.setAttribute("visibility", "visible"); focus.setAttribute("visibility", "visible");
      tip.show((sx(best[0]) / width) * box.width, (sy(best[1]) / height) * box.height,
        el("strong", {}, `${fmt.num(best[1])} ${options.unit || ""}`), el("br"),
        el("span", { class: "muted" }, fmt.time(best[0] / 1000)));
    };
    hit.addEventListener("pointermove", (event) => pick(event.clientX));
    hit.addEventListener("pointerleave", () => { tip.hide(); cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden"); });
  }

  // Stacked columns: <=24px wide, 4px rounded top, 2px surface gap between
  // segments, one tooltip per segment, legend always shown for 2+ series.
  function stackedColumns(container, rows, series, options = {}) {
    const width = container.clientWidth || 560, height = options.height || 180;
    const pad = { left: 44, right: 8, top: 12, bottom: 24 };
    container.classList.add("chart");
    container.replaceChildren();
    if (!rows.length) {
      container.append(el("div", { class: "empty" }, options.empty || "Nothing recorded yet"));
      return;
    }
    const totals = rows.map((row) => series.reduce((sum, s) => sum + (row.values[s.key] || 0), 0));
    const scale = niceScale(Math.max(...totals) * 1.05), yMax = scale.max;
    const band = (width - pad.left - pad.right) / rows.length;
    const barWidth = Math.min(24, band * 0.6);
    const sy = (y) => (y / yMax) * (height - pad.top - pad.bottom);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.label || "Column chart" });
    for (let value = 0; value <= yMax + 1e-9; value += scale.step) {
      const y = height - pad.bottom - sy(value);
      root.append(svg("line", { class: value ? "gridline" : "baseline", x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
      root.append(svg("text", { class: "tick", x: pad.left - 6, y: y + 4, "text-anchor": "end" }, fmt.num(value, scale.digits)));
    }
    const tip = tooltip(container);
    rows.forEach((row, index) => {
      const x = pad.left + band * index + (band - barWidth) / 2;
      let base = height - pad.bottom;
      const drawn = series.filter((s) => (row.values[s.key] || 0) > 0);
      drawn.forEach((s, k) => {
        const h = sy(row.values[s.key]);
        const top = k === drawn.length - 1;
        const gap = k > 0 ? 2 : 0;
        const y = base - h, r = top ? Math.min(4, h / 2) : 0;
        const d = `M${x},${base - gap}V${y + r}` + (r ? `Q${x},${y} ${x + r},${y}H${x + barWidth - r}Q${x + barWidth},${y} ${x + barWidth},${y + r}` : `H${x + barWidth}`) + `V${base - gap}Z`;
        const mark = svg("path", { d, fill: s.color, tabindex: 0 });
        const show = () => {
          const box = root.getBoundingClientRect();
          tip.show(((x + barWidth / 2) / width) * box.width, (y / height) * box.height,
            el("strong", {}, `${fmt.num(row.values[s.key], 2)} ${options.unit || ""}`), el("br"),
            el("span", { class: "muted" }, `${s.label} · ${row.label}`));
        };
        mark.addEventListener("pointerenter", show);
        mark.addEventListener("focus", show);
        mark.addEventListener("pointerleave", () => tip.hide());
        mark.addEventListener("blur", () => tip.hide());
        root.append(mark);
        base = y;
      });
      if (rows.length <= 16 || index % Math.ceil(rows.length / 8) === 0) {
        root.append(svg("text", { class: "tick", x: x + barWidth / 2, y: height - 6, "text-anchor": "middle" }, row.short || row.label));
      }
    });
    container.append(root);
    if (series.length > 1) {
      container.append(el("div", { class: "legend" }, series.map((s) =>
        el("span", {}, el("span", { class: "sw", style: `background:${s.color}` }), s.label))));
    }
  }

  // Swimlanes for a run replay: one lane per actor, spans as bars and points
  // as dots; setCursor(t) draws the replay position.
  function swimlanes(container, lanes, marks, options = {}) {
    const width = container.clientWidth || 900;
    const laneHeight = 30, pad = { left: 86, right: 12, top: 6, bottom: 24 };
    const height = pad.top + lanes.length * laneHeight + pad.bottom;
    container.classList.add("chart", "lanes");
    container.replaceChildren();
    const t0 = options.start, t1 = Math.max(options.end, t0 + 1);
    const sx = (t) => pad.left + ((t - t0) / (t1 - t0)) * (width - pad.left - pad.right);
    const laneY = Object.fromEntries(lanes.map((lane, i) => [lane.key, pad.top + i * laneHeight + laneHeight / 2]));
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Run timeline by actor" });
    lanes.forEach((lane) => {
      const y = laneY[lane.key];
      root.append(svg("line", { class: "gridline", x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
      root.append(svg("text", { class: "lane-label", x: 0, y: y + 4 }, lane.label));
    });
    for (let i = 0; i < 5; i++) {
      const t = t0 + ((t1 - t0) * i) / 4;
      root.append(svg("text", { class: "tick", x: sx(t), y: height - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, fmt.time(t)));
    }
    const tip = tooltip(container);
    for (const mark of marks) {
      const y = laneY[mark.lane];
      if (y === undefined) continue;
      let shape;
      if (mark.end && sx(mark.end) - sx(mark.start) >= 3) {
        shape = svg("rect", { x: sx(mark.start), y: y - 5, width: Math.max(3, sx(mark.end) - sx(mark.start)), height: 10, rx: 3, fill: mark.color, "fill-opacity": mark.faint ? 0.45 : 1 });
      } else if (mark.shape === "diamond") {
        const x = sx(mark.start);
        shape = svg("path", { d: `M${x},${y - 6}L${x + 6},${y}L${x},${y + 6}L${x - 6},${y}Z`, fill: mark.color, class: "dot" });
      } else if (mark.shape === "ring") {
        shape = svg("circle", { cx: sx(mark.start), cy: y, r: 4, fill: "var(--surface-1)", stroke: mark.color, "stroke-width": 2 });
      } else {
        shape = svg("circle", { class: "dot", cx: sx(mark.start), cy: y, r: 4.5, fill: mark.color });
      }
      root.append(shape);
      const x = sx(mark.start), w = mark.end ? Math.max(12, sx(mark.end) - x) : 12;
      const hit = svg("rect", { class: "hit", x: mark.end ? x : x - 6, y: y - 12, width: w, height: 24, tabindex: 0 });
      const show = () => {
        const box = root.getBoundingClientRect();
        tip.show((x / width) * box.width, ((y - 8) / height) * box.height,
          el("strong", {}, mark.title), ...(mark.lines || []).flatMap((line) => [el("br"), el("span", { class: "muted" }, line)]));
      };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", () => tip.hide());
      hit.addEventListener("blur", () => tip.hide());
      if (options.onPick) hit.addEventListener("click", () => options.onPick(mark));
      root.append(hit);
    }
    const cursor = svg("line", { class: "crosshair", y1: pad.top, y2: height - pad.bottom, visibility: "hidden", "stroke-width": 2 });
    root.append(cursor);
    container.append(root);
    return {
      setCursor(t) {
        cursor.setAttribute("x1", sx(t)); cursor.setAttribute("x2", sx(t));
        cursor.setAttribute("visibility", "visible");
      },
    };
  }

  // Just enough Markdown for briefs and reports: headings, **bold**, `code`.
  // Builds nodes directly, so text from agents can never become markup.
  function richText(text) {
    const fragment = document.createDocumentFragment();
    String(text || "").split("\n").forEach((raw, index) => {
      if (index) fragment.append(document.createElement("br"));
      const heading = raw.match(/^#{1,6}\s+(.*)$/);
      const target = heading ? el("strong") : fragment;
      for (const part of (heading ? heading[1] : raw).split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
        if (!part) continue;
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) target.append(el("strong", {}, part.slice(2, -2)));
        else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) target.append(el("code", {}, part.slice(1, -1)));
        else target.append(document.createTextNode(part));
      }
      if (heading) fragment.append(target);
    });
    return fragment;
  }

  window.LB = { el, svg, fmt, richText, lineChart, stackedColumns, swimlanes };
})();
