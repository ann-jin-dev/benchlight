// The public window: renders /api/public. Also used by the private preview tab.
(function () {
  "use strict";
  const { el, fmt, stackedColumns } = window.LB;

  function tile(label, value, note) {
    return el("div", { class: "card tile" }, el("div", { class: "label" }, label),
      el("div", { class: "value" }, value), note ? el("div", { class: "note" }, note) : null);
  }

  function meter(percent) {
    const value = Math.max(0, Math.min(100, percent || 0));
    return el("div", { class: "meter", role: "meter", "aria-valuenow": value, "aria-valuemin": 0, "aria-valuemax": 100 },
      el("span", { style: `width:${value}%` }));
  }

  function renderPublic(container, data) {
    const live = data.live, totals = data.totals;
    const statusLine = el("div", { class: `row ${live.stale ? "stale" : ""}` }, el("span", { class: "pulse-dot", "aria-hidden": "true" }),
      el("span", { class: "secondary" }, live.stale
        ? (live.age_seconds ? `Offline · last reading ${fmt.ago(live.age_seconds)}` : "Waiting for the first reading")
        : `Live · updated ${fmt.ago(live.age_seconds)}`));

    const gpus = el("div", { class: "grid gpus" }, live.gpus.map((gpu, index) => el("div", { class: "card" },
      el("div", { class: "row" }, el("strong", {}, `GPU ${index + 1} · ${gpu.name || "GPU"}`), el("span", { class: "spacer" }),
        el("span", { class: `status ${gpu.reserved ? "running" : ""}` }, gpu.reserved ? "running a job" : "idle")),
      el("div", { class: "row", style: "margin-top:10px" }, el("span", { class: "muted" }, "Utilization"), el("span", { class: "spacer" }),
        el("span", { class: "num" }, `${fmt.num(gpu.utilization)}%`)),
      meter(gpu.utilization),
      el("div", { class: "row", style: "margin-top:10px" },
        el("span", { class: "secondary" }, `${fmt.num(gpu.power_w)} W`), el("span", { class: "secondary" }, `${fmt.num(gpu.temperature)} °C`),
        el("span", { class: "secondary" }, `${fmt.num(gpu.vram_percent)}% memory`)))));

    const p = live.power;
    const outlet = p.measured_w != null ? `${fmt.num(p.measured_w)} W` : p.outlet_low_w != null ? `${fmt.num(p.outlet_low_w)}–${fmt.num(p.outlet_high_w)} W` : `${fmt.num(p.gpu_w)} W`;
    const today = live.energy_today || {};
    const running = live.queue.running;
    const nowTiles = el("div", { class: "grid tiles" },
      tile("Drawing now", outlet, p.measured_w != null ? "measured at the UPS" : p.outlet_low_w != null ? "outlet estimate" : "GPU boards only"),
      tile("GPU energy today", fmt.energy(today.gpu_wh), today.job_gpu_wh != null ? `${fmt.energy(today.job_gpu_wh)} by experiments` : null),
      tile("Jobs", `${running.length} running`, `${live.queue.queued} waiting in the queue`),
      tile("CPU · RAM", `${fmt.num(live.cpu_percent)}% · ${fmt.num(live.memory_percent)}%`));

    const since = totals.since ? new Date(totals.since * 1000).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" }) : "the start";
    const rate = totals.jobs ? Math.round((100 * totals.succeeded) / Math.max(1, totals.jobs)) : 0;
    const totalTiles = el("div", { class: "grid tiles" },
      tile("Experiments run", fmt.num(totals.jobs), `${rate}% succeeded`),
      tile("GPU hours", fmt.num(totals.gpu_hours, 1)),
      tile("Agent runs", fmt.num(totals.agent_runs), `${fmt.num(totals.agent_turns)} Codex turns supervised by Claude`),
      tile("Verdicts & decisions", fmt.num(totals.recorded_verdicts_and_decisions), "recorded on run timelines"));

    const chart = el("div");
    const days = [...data.energy_days].reverse();

    const receipts = data.recent_receipts.length ? el("div", { class: "card" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Finished", "Project", "Status", "GPUs", "Ran for", "GPU energy", "Asked by"].map((h, i) =>
        el("th", { class: i >= 3 && i <= 5 ? "right" : null }, h)))),
      el("tbody", {}, data.recent_receipts.map((r) => el("tr", {},
        el("td", { class: "num" }, fmt.time(r.finished)), el("td", {}, r.project),
        el("td", {}, el("span", { class: `status ${r.status}` }, r.status)), el("td", { class: "right num" }, r.gpus),
        el("td", { class: "right num" }, fmt.duration(r.duration_seconds)), el("td", { class: "right num" }, fmt.energy(r.gpu_wh)),
        el("td", {}, r.agent ? el("span", { class: "pill" }, `AI agent · ${r.agent_link} link`) : el("span", { class: "muted" }, "person")))))))
      : el("div", { class: "card empty" }, "No finished experiments yet.");

    container.replaceChildren(
      el("h1", {}, data.title || "Live lab"),
      data.subtitle ? el("p", { class: "lede" }, data.subtitle) : null,
      statusLine,
      el("h2", {}, "Right now"), gpus, el("div", { style: "height:12px" }), nowTiles,
      running.length ? el("p", { class: "secondary" }, "Running: ", running.map((job) => `${job.project} on ${job.gpus ? `${job.gpus} GPU${job.gpus === 1 ? "" : "s"}` : "CPU only"} for ${fmt.duration(job.elapsed_seconds)}`).join(" · ")) : null,
      el("h2", {}, `Since ${since}`), totalTiles,
      el("h2", {}, "GPU energy per day"), el("div", { class: "card" }, chart),
      el("h2", {}, "Latest experiment receipts"), receipts,
      el("p", { class: "muted", style: "margin-top:24px" },
        "People and AI coding agents share this workstation's GPUs through one queue. Every experiment gets a receipt: the exact code and container it ran, measured GPU energy, and the agent turn that asked for it. ",
        "This page shows aggregates only; commands, file paths, logs and agent transcripts never leave the machine.",
        data.repo_url ? el("span", {}, " ", el("a", { href: data.repo_url }, "How it works →")) : null));

    stackedColumns(chart, days.map((d) => ({
      label: d.day, short: d.day.slice(5),
      values: { jobs: d.attributed_gpu_kwh, idle: Math.max(0, d.gpu_kwh - d.attributed_gpu_kwh) },
    })), [
      { key: "jobs", label: "Used by experiments", color: "var(--energy-used)" },
      { key: "idle", label: "Idle or unattributed", color: "var(--energy-idle)" },
    ], { unit: "kWh", label: "GPU energy per day", empty: "Energy metering has just started." });
  }

  window.renderPublic = renderPublic;

  if (document.body.dataset.page === "public") {
    const root = document.getElementById("public");
    const refresh = async () => {
      try {
        const response = await fetch("/api/public", { headers: { Accept: "application/json" } });
        if (response.ok) renderPublic(root, await response.json());
      } catch (error) {
        if (!root.childElementCount) root.replaceChildren(el("div", { class: "card error" }, "The lab is not reachable right now."));
      }
    };
    refresh();
    setInterval(refresh, 10000);
  }
})();
