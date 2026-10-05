// labbook private notebook: live machine, receipts, agent run replay.
(function () {
  "use strict";
  const { el, fmt, richText, lineChart, swimlanes } = window.LB;
  const view = document.getElementById("view");
  const freshness = document.getElementById("freshness");
  let timer = null;
  let player = null;

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || `${response.status} ${response.statusText}`);
    return value;
  }

  function setTab(name) {
    for (const link of document.querySelectorAll("nav.tabs a")) {
      if (link.dataset.tab === name) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function line(key, value, options = {}) {
    return el("div", { class: "line" + (options.total ? " total" : ""), title: options.title || null },
      el("span", { class: "k" }, key), el("span", { class: "dots" }),
      el("span", { class: "v" + (options.mono ? " mono" : "") }, value ?? "–"));
  }

  function status(state) {
    return el("span", { class: `status ${state || ""}` }, state || "unknown");
  }

  function actor(name) {
    const labels = { claude: "Claude", codex: "Codex", gpu: "GPU job", human: "Person", system: "System" };
    return el("span", { class: `actor ${name}` }, labels[name] || name);
  }

  function tile(label, value, note) {
    return el("div", { class: "card tile" }, el("div", { class: "label" }, label),
      el("div", { class: "value" }, value), note ? el("div", { class: "note" }, note) : null);
  }

  function meter(percent, level) {
    const value = Math.max(0, Math.min(100, percent || 0));
    return el("div", { class: `meter ${level || ""}`, role: "meter", "aria-valuenow": value, "aria-valuemin": 0, "aria-valuemax": 100 },
      el("span", { style: `width:${value}%` }));
  }

  function showError(error) {
    view.replaceChildren(el("div", { class: "card error" }, `Could not load this view: ${error.message}`));
  }

  // ------------------------------------------------------------------ live

  async function live() {
    setTab("live");
    view.classList.add("refreshing");
    let data;
    try { data = await getJSON("/api/live"); } catch (error) { view.classList.remove("refreshing"); return showError(error); }
    view.classList.remove("refreshing");
    const p = data.pulse;
    const age = data.age_seconds;
    freshness.className = `status ${p && age < 30 ? "live" : "stale"}`;
    freshness.textContent = p ? `telemetry ${fmt.ago(age)}` : "no telemetry";
    if (!p) {
      view.replaceChildren(el("h1", {}, "Live"), el("div", { class: "card" },
        "System Pulse has not written a snapshot yet. Start it with ",
        el("code", {}, "systemctl --user start system-pulse-agent"), ", or run ",
        el("code", {}, "python3 pulse/agent.py --once"), "."));
      return;
    }
    const queue = p.gpu_queue || { jobs: [], gpus: [] };
    const energy = p.energy || { jobs: {}, today: {} };
    const power = p.power || {};
    const jobsById = Object.fromEntries((queue.jobs || []).map((job) => [job.id, job]));
    const running = (queue.jobs || []).filter((job) => ["starting", "running", "cancelling"].includes(job.status));
    const queued = (queue.jobs || []).filter((job) => job.status === "queued");

    const outlet = power.measured_ac_w != null ? `${fmt.num(power.measured_ac_w)}`
      : power.estimate_low_w != null ? `${fmt.num(power.estimate_low_w)}–${fmt.num(power.estimate_high_w)}` : null;
    const hero = el("div", { class: "card" },
      el("h3", {}, power.measured_ac_w != null ? "Drawing now (measured at the UPS)" : "Drawing now (outlet estimate)"),
      outlet ? el("div", { class: "hero" }, outlet, el("small", {}, "W"))
        : el("div", { class: "hero" }, fmt.num(power.gpu_total_w), el("small", {}, "W on GPUs")),
      el("div", { class: "secondary" },
        `GPUs ${fmt.num(power.gpu_total_w)} W · CPU package ${power.cpu_package_w != null ? fmt.num(power.cpu_package_w) + " W" : "not readable"}`),
      el("div", { class: "muted" }, `Profile: ${power.profile || "generic"}${power.psu ? " · " + power.psu : ""}`));
    const today = energy.today || {};
    const tiles = el("div", { class: "grid tiles" },
      tile("GPU energy today", fmt.energy(today.gpu_wh), `${fmt.energy(today.job_gpu_wh)} attributed to jobs`),
      tile("CPU package today", energy.cpu_measured ? fmt.energy(today.cpu_package_wh) : "not measured",
        energy.cpu_measured ? `${fmt.energy(today.job_cpu_wh)} attributed` : "RAPL needs read access"),
      tile("Queue", `${running.length} running`, `${queued.length} waiting · worker ${queue.worker_state || "unknown"}`),
      tile("CPU / RAM", `${fmt.num(p.cpu?.total)}% · ${fmt.num(p.memory?.percent)}%`,
        `load ${fmt.num(p.load?.[0], 2)} · disk ${fmt.num(p.disk?.percent)}%`));

    const gpuCards = el("div", { class: "grid gpus" }, (p.gpus || []).map((gpu) => {
      const reservation = (queue.gpus || []).find((g) => g.uuid === gpu.uuid) || {};
      const job = reservation.job_id ? jobsById[reservation.job_id] : null;
      const jobEnergy = job ? energy.jobs?.[String(job.id)] : null;
      const vram = gpu.vram_total ? (100 * gpu.vram_used) / gpu.vram_total : null;
      const tempLevel = gpu.temperature >= 84 ? "critical" : gpu.temperature >= 78 ? "warning" : "";
      return el("div", { class: "card" },
        el("div", { class: "row" }, el("strong", {}, `GPU ${gpu.index} · ${gpu.name}`), el("span", { class: "spacer" }),
          status(reservation.state === "reserved" ? "running" : reservation.state || "unknown")),
        el("div", { class: "row", style: "margin-top:10px" }, el("span", { class: "muted" }, "Utilization"), el("span", { class: "spacer" }), el("span", { class: "num" }, `${fmt.num(gpu.utilization)}%`)),
        meter(gpu.utilization),
        el("div", { class: "row", style: "margin-top:8px" }, el("span", { class: "muted" }, "Memory"), el("span", { class: "spacer" }), el("span", { class: "num" }, `${fmt.bytes(gpu.vram_used)} of ${fmt.bytes(gpu.vram_total)}`)),
        meter(vram),
        el("dl", { class: "kv", style: "margin-top:10px" },
          el("dt", {}, "Temperature"), el("dd", {}, `${fmt.num(gpu.temperature)} °C `, el("span", { class: `status ${tempLevel || "good"}` }, tempLevel || "normal")),
          el("dt", {}, "Board power"), el("dd", {}, `${fmt.num(gpu.power_w)} W`),
          el("dt", {}, "Job"), el("dd", {}, job ? el("a", { href: `#/receipt/${job.id}` }, `#${job.id} ${job.project} / ${job.name}`) : "–"),
          el("dt", {}, "Job energy"), el("dd", {}, jobEnergy ? `${fmt.energy(jobEnergy.gpu_wh)} GPU so far` : "–")));
    }));

    const queueRows = [...running, ...queued].map((job) => {
      const jobEnergy = energy.jobs?.[String(job.id)];
      const now = Date.now() / 1000;
      return el("tr", { class: "clickable", onclick: () => { location.hash = `#/receipt/${job.id}`; } },
        el("td", { class: "num" }, `#${job.id}`), el("td", {}, status(job.status)),
        el("td", {}, `${job.project} / ${job.name}`), el("td", { class: "right num" }, job.gpu_count),
        el("td", { class: "right num" }, job.started_at ? fmt.duration(now - job.started_at) : `waiting ${fmt.duration(now - job.created_at)}`),
        el("td", { class: "right num" }, jobEnergy ? fmt.energy(jobEnergy.gpu_wh + (jobEnergy.cpu_wh || 0)) : "–"),
        el("td", { class: "muted" }, job.detail || ""));
    });
    const queueTable = queueRows.length ? el("div", { class: "card" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Job", "State", "Project / name", "GPUs", "Time", "Energy", "Scheduler note"].map((h, i) =>
        el("th", { class: i === 3 || i === 4 || i === 5 ? "right" : null }, h)))),
      el("tbody", {}, queueRows))) : el("div", { class: "card empty" }, "The queue is empty.");

    const alerts = (p.monitoring?.active_alerts || []);
    const temps = (p.sensors?.temperatures || []).slice(0, 12);
    const tempCard = el("div", { class: "card" }, el("h3", {}, alerts.length ? `${alerts.length} temperature alert(s)` : "Temperatures"),
      el("dl", { class: "kv" }, temps.flatMap((t) => {
        const alert = alerts.find((a) => a.sensor === t.name);
        return [el("dt", {}, t.name), el("dd", {}, alert ? status(alert.level) : null, ` ${fmt.num(t.celsius, 1)} °C`)];
      })));
    const components = power.modeled_components || [];
    const powerCard = el("div", { class: "card" }, el("h3", {}, "How the outlet estimate is built"),
      components.length ? el("dl", { class: "kv" }, components.flatMap((c) => [el("dt", {}, c.label),
        el("dd", { title: c.detail }, `${fmt.num(c.low_w, 1)}–${fmt.num(c.high_w, 1)} W`)]),
      el("dt", {}, "PSU loss"), el("dd", {}, `${fmt.num(power.psu_loss_low_w, 1)}–${fmt.num(power.psu_loss_high_w, 1)} W`))
        : el("div", { class: "muted" }, "The estimate needs CPU package power (RAPL)."));

    view.replaceChildren(
      el("h1", {}, p.hostname || "Live"),
      el("p", { class: "lede" }, `Up ${fmt.duration(p.uptime_seconds)} · ${(p.warnings || []).join(" · ") || "all readings available"}`),
      el("div", { class: "grid", style: "grid-template-columns:minmax(260px,1.2fr) 2fr" }, hero, tiles),
      el("h2", {}, "GPUs"), gpuCards,
      el("h2", {}, "Queue"), queueTable,
      el("div", { class: "grid", style: "grid-template-columns:1fr 1fr;margin-top:12px" }, tempCard, powerCard));
  }

  // ------------------------------------------------------------------ receipts

  let receiptFilter = { project: "", status: "", agent: false, text: "" };

  function agentPill(agent) {
    if (!agent) return el("span", { class: "muted" }, "–");
    if (agent.run) return el("span", { class: "pill", title: agent.run }, `Codex · turn ${agent.turn ?? "?"} · ${agent.link}`);
    return el("span", { class: "pill" }, `Claude session · ${agent.link}`);
  }

  async function receipts() {
    setTab("receipts");
    let data;
    try { data = await getJSON("/api/receipts?limit=5000"); } catch (error) { return showError(error); }
    const rows = data.receipts;
    const projects = [...new Set(rows.map((r) => r.project))].sort();
    const body = el("tbody");
    const count = el("span", { class: "muted" });
    const render = () => {
      const f = receiptFilter;
      const text = f.text.toLowerCase();
      const shown = rows.filter((r) => (!f.project || r.project === f.project) && (!f.status || r.status === f.status)
        && (!f.agent || r.agent) && (!text || `${r.id} ${r.name}`.toLowerCase().includes(text)));
      count.textContent = `${shown.length} of ${rows.length} jobs`;
      body.replaceChildren(...shown.slice(0, 400).map((r) => el("tr", { class: "clickable", onclick: () => { location.hash = `#/receipt/${r.id}`; } },
        el("td", { class: "num" }, `#${r.id}`), el("td", {}, el("div", {}, r.name), el("div", { class: "muted" }, r.project)),
        el("td", {}, status(r.status)), el("td", { class: "num" }, fmt.time(r.finished || r.started || r.submitted)),
        el("td", { class: "right num" }, fmt.duration(r.duration_seconds)), el("td", { class: "right num" }, r.gpus),
        el("td", { class: "right num" }, r.gpu_wh != null ? fmt.energy(r.gpu_wh + (r.cpu_wh || 0)) : "–"),
        el("td", {}, agentPill(r.agent)))));
    };
    const select = (name, options, label) => el("select", { "aria-label": label, onchange: (e) => { receiptFilter[name] = e.target.value; render(); } },
      el("option", { value: "" }, label), options.map((o) => el("option", { value: o, selected: receiptFilter[name] === o }, o)));
    view.replaceChildren(
      el("h1", {}, "Receipts"),
      el("p", { class: "lede" }, "Every gpuq job, with the code, image and hardware it ran on, the energy it used, and the agent turn that asked for it."),
      el("div", { class: "filters" },
        select("project", projects, "All projects"),
        select("status", ["succeeded", "failed", "cancelled", "skipped", "running", "queued"], "Any status"),
        el("label", { class: "row secondary" }, el("input", { type: "checkbox", checked: receiptFilter.agent, onchange: (e) => { receiptFilter.agent = e.target.checked; render(); } }), "Agent-submitted"),
        el("input", { type: "search", placeholder: "Job id or name", value: receiptFilter.text, oninput: (e) => { receiptFilter.text = e.target.value; render(); } }),
        el("span", { class: "spacer" }), count),
      el("div", { class: "card" }, el("table", {}, el("thead", {}, el("tr", {}, ["Job", "Name", "Status", "When", "Duration", "GPUs", "Energy", "Asked by"].map((h, i) =>
        el("th", { class: i === 4 || i === 5 || i === 6 ? "right" : null }, h)))), body)));
    render();
  }

  async function receipt(id) {
    setTab("receipts");
    let r;
    try { r = await getJSON(`/api/receipts/${id}`); } catch (error) { return showError(error); }
    const job = r.job, when = r.times, code = r.code, env = r.environment, energy = r.energy, agent = r.agent;
    const chart = el("div", { style: "margin-top:10px" });
    const sections = [
      el("div", { class: "head" }, el("span", { class: "title" }, "RECEIPT"), el("span", { class: "mono muted" }, r.id)),
      el("div", { class: "job" }, job.name), el("div", { class: "row" }, el("span", { class: "secondary" }, job.project), status(job.status),
        ...Object.entries(job.labels || {}).map(([k, v]) => el("span", { class: "pill" }, `${k}=${v}`))),
      el("hr", { class: "sep" }),
      line("Submitted", fmt.time(when.submitted)), line("Started", fmt.time(when.started)), line("Finished", fmt.time(when.finished)),
      line("Waited in queue", fmt.duration(when.queue_wait_seconds)), line("Ran for", fmt.duration(when.duration_seconds)),
      job.exit_code != null ? line("Exit code", String(job.exit_code)) : null,
      el("hr", { class: "sep" }), el("div", { class: "sec" }, "CODE"),
      line("Git commit", code.git_commit ? code.git_commit.slice(0, 12) + (code.git_dirty ? " (uncommitted changes)" : "") : "not recorded", { mono: true }),
      code.snapshot && code.snapshot.tree_sha256 ? line("Frozen source", `${fmt.num(code.snapshot.files)} files · ${fmt.bytes(code.snapshot.bytes)}`) : null,
      code.snapshot && code.snapshot.tree_sha256 ? line("Source tree", code.snapshot.tree_sha256.slice(0, 16), { mono: true, title: code.snapshot.tree_sha256 }) : null,
      el("div", { class: "sec", style: "margin-top:14px" }, "ENVIRONMENT"),
      line("Runtime", env.backend === "docker" ? "Docker container" : "native systemd service"),
      env.image_id ? line("Image", env.image_id.replace("sha256:", "").slice(0, 16), { mono: true, title: env.image_id }) : null,
      el("details", {}, el("summary", {}, "Command"), el("pre", { class: "block" }, (env.command || []).join(" "))),
      el("div", { class: "sec", style: "margin-top:14px" }, "HARDWARE"),
      ...(r.hardware.gpus.length ? r.hardware.gpus.map((g) => line(`GPU ${g.index}`, `${g.name}`, { title: g.uuid })) : [line("GPU", "none (CPU job)")]),
      line("Reserved", `${r.hardware.cpus_reserved} CPUs · ${fmt.bytes(r.hardware.memory_reserved_bytes)} RAM`),
      el("hr", { class: "sep" }), el("div", { class: "sec" }, "ENERGY"),
    ];
    if (energy) {
      sections.push(line("GPU (measured)", fmt.energy(energy.gpu_wh)),
        line("CPU (attributed)", energy.cpu_wh != null ? fmt.energy(energy.cpu_wh) : "not measured"),
        line("Total", fmt.energy(energy.total_wh), { total: true }),
        line("Run time metered", energy.coverage != null ? `${fmt.num(energy.coverage * 100)}%` : "–"));
      const pricing = r.pricing || {};
      if (pricing.cost) sections.push(line("Electricity", `${fmt.num(pricing.cost.amount, 4)} ${pricing.cost.currency}`, { title: `at ${pricing.cost.price_per_kwh}/kWh` }));
      if (pricing.co2_grams != null) sections.push(line("CO₂", `${fmt.num(pricing.co2_grams)} g`, { title: `${pricing.grid_gco2_per_kwh} g/kWh ${pricing.grid_source || ""}` }));
      sections.push(chart, el("details", {}, el("summary", {}, "How this was measured"),
        el("p", { class: "secondary" }, `GPU: ${energy.method.gpu}.`), el("p", { class: "secondary" }, `CPU: ${energy.method.cpu}.`)));
    } else {
      sections.push(el("div", { class: "muted" }, "Not recorded: System Pulse was not metering while this job ran."));
    }
    sections.push(el("hr", { class: "sep" }), el("div", { class: "sec" }, "ASKED FOR BY"));
    if (agent && agent.run) {
      const tokens = agent.turn_tokens;
      sections.push(line("Agent run", el("a", { href: `#/run/${agent.run}` }, agent.run_name || agent.run)),
        line("Turn", `${agent.turn ?? "?"}${agent.turn_status ? " · " + agent.turn_status : ""}`),
        line("Link", agent.link, { title: agent.evidence }),
        el("div", { class: "muted", style: "font-size:12px;text-align:right" }, agent.evidence),
        agent.model ? line("Model", `${agent.model}${agent.effort ? " · " + agent.effort : ""}`) : null,
        tokens ? line("Tokens that turn", fmt.compact(tokens.total_tokens), { title: `input ${fmt.num(tokens.input_tokens)} (cached ${fmt.num(tokens.cached_input_tokens)}), output ${fmt.num(tokens.output_tokens)} (reasoning ${fmt.num(tokens.reasoning_output_tokens)})` }) : null);
      sections.push(el("div", { class: "sec", style: "margin-top:14px" }, "VERIFICATION"));
      if ((agent.notes || []).length) {
        for (const note of agent.notes) sections.push(el("div", { class: "note-row" }, actor(note.by), el("span", {}, el("strong", {}, `${note.kind}: `), note.text)));
      } else sections.push(el("div", { class: "muted" }, "No verdict or decision recorded for this turn."));
    } else if (agent && agent.claude_session) {
      sections.push(line("Claude session", agent.claude_session, { mono: true }), el("div", { class: "muted" }, agent.evidence));
    } else {
      sections.push(el("div", { class: "muted" }, "Submitted directly (no agent run recorded)."));
    }
    if (r.artifacts) {
      sections.push(el("hr", { class: "sep" }), el("div", { class: "sec" }, "OUTPUTS"),
        line("Files", `${fmt.num(r.artifacts.output_files)} · ${fmt.bytes(r.artifacts.output_bytes)}`),
        line("Console log", fmt.bytes(r.artifacts.log_bytes)));
    }
    sections.push(el("hr", { class: "sep" }), el("div", { class: "sec", style: "text-align:center" }, "DIGEST (SHA-256)"),
      el("div", { class: "digest" }, r.digest || "Issued when the job finishes"),
      r.digest ? el("div", { class: "muted", style: "font-size:12px;text-align:center;margin-top:4px" },
        `Covers ${(r.digest_scope || []).join(", ")}. Check a copy with: labbook verify receipt.json`) : null);
    const download = () => {
      const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
      const link = el("a", { href: URL.createObjectURL(blob), download: `receipt-${r.id}.json` });
      document.body.append(link); link.click(); link.remove();
    };
    view.replaceChildren(el("p", {}, el("a", { href: "#/receipts" }, "← All receipts")),
      el("article", { class: "receipt" }, sections,
        el("div", { class: "actions" }, el("button", { class: "btn", onclick: download }, "Download JSON"),
          r.digest ? el("button", { class: "btn", onclick: () => navigator.clipboard?.writeText(r.digest) }, "Copy digest") : null)));
    if (energy && energy.power_minutes.length) {
      lineChart(chart, energy.power_minutes.map(([minute, watts]) => [minute * 1000, watts]),
        { unit: "W", label: "Job power per minute", height: 130 });
    }
  }

  // ------------------------------------------------------------------ runs

  async function runs() {
    setTab("runs");
    let data;
    try { data = await getJSON("/api/runs"); } catch (error) { return showError(error); }
    view.replaceChildren(el("h1", {}, "Agent runs"),
      el("p", { class: "lede" }, "Each run is one Codex thread supervised by Claude through codex-task. Open one to replay it: briefs, Codex turns, the GPU jobs they launched, verdicts and decisions."),
      data.runs.length ? el("div", { class: "card" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Run", "Project", "Turns", "State", "Last report", "Started", "Notes"].map((h) => el("th", {}, h)))),
        el("tbody", {}, data.runs.map((run) => el("tr", { class: "clickable", onclick: () => { location.hash = `#/run/${run.id}`; } },
          el("td", {}, el("strong", {}, run.name), el("div", { class: "muted mono" }, run.id)), el("td", {}, run.project),
          el("td", { class: "num" }, run.turns), el("td", {}, status(run.state)),
          el("td", {}, run.status ? el("span", { class: "pill" }, run.status) : "–"), el("td", { class: "num" }, fmt.time(run.created)),
          el("td", { class: "num" }, run.notes || "–"))))))
        : el("div", { class: "card empty" }, "No codex-task runs yet."));
  }

  // Consecutive GPU job events (queued and finished, interleaved) fold into one row.
  function groupEvents(events) {
    const groups = [];
    for (const event of events) {
      const last = groups[groups.length - 1];
      if (event.actor === "gpu" && last && last.actor === "gpu" && event.at - last.items[last.items.length - 1].at < 1800) {
        last.items.push(event);
      } else groups.push({ actor: event.actor, kind: event.kind, at: event.at, items: [event] });
    }
    return groups;
  }

  function eventItem(group, runId, finals) {
    const first = group.items[0];
    const node = el("li", { class: group.actor });
    if (group.items.length > 1) {
      const jobs = new Map();
      for (const e of group.items) {
        // Each job's final state, even when it finished in a later group.
        jobs.set(e.job, Object.assign({ job: e.job, name: e.name, link: e.link }, finals[e.job] || {}));
      }
      const counts = {};
      for (const entry of jobs.values()) counts[entry.status || "running"] = (counts[entry.status || "running"] || 0) + 1;
      const ids = [...jobs.keys()].sort((a, b) => a - b);
      const last = group.items[group.items.length - 1];
      node.append(el("div", { class: "when" }, `${fmt.time(first.at)} – ${fmt.time(last.at, false)} · `, actor("gpu")),
        el("div", { class: "what" }, `${jobs.size} GPU job${jobs.size === 1 ? "" : "s"}`, " ",
          ...Object.entries(counts).map(([k, v]) => el("span", { class: "pill" }, `${v} ${k}`))),
        el("details", {}, el("summary", {}, `Jobs #${ids[0]}–#${ids[ids.length - 1]}`),
          el("div", {}, [...jobs.values()].map((e) => el("div", { class: "row" }, el("a", { href: `#/receipt/${e.job}` }, `#${e.job}`),
            el("span", {}, e.name), e.status ? status(e.status) : null, e.duration_seconds ? el("span", { class: "muted" }, fmt.duration(e.duration_seconds)) : null,
            e.energy_wh != null ? el("span", { class: "muted" }, fmt.energy(e.energy_wh)) : null,
            el("span", { class: "muted" }, e.link === "inferred" ? "(inferred link)" : ""))))));
      return node;
    }
    const e = first;
    const meta = [];
    if (e.turn) meta.push(`turn ${e.turn}`);
    if (e.duration_seconds) meta.push(fmt.duration(e.duration_seconds));
    if (e.activity) meta.push(`${e.activity.commands} commands · ${e.activity.edits} edits`);
    if (e.tokens) meta.push(`${fmt.compact(e.tokens.total_tokens)} tokens`);
    if (e.energy_wh != null) meta.push(fmt.energy(e.energy_wh));
    if (e.job) meta.push(e.link === "inferred" ? "inferred link" : "exact link");
    node.append(el("div", { class: "when" }, fmt.time(e.at), " · ", actor(e.actor === "gpu" ? "gpu" : e.actor)),
      el("div", { class: "what" }, e.job ? el("a", { href: `#/receipt/${e.job}` }, e.title) : e.title, " ", e.status ? el("span", { class: "pill" }, e.status) : null),
      e.text ? el("div", { class: "body" }, richText(e.text)) : null,
      el("div", { class: "meta" }, meta.join(" · ")));
    if (e.kind === "brief" || e.kind === "instruction" || e.kind === "report") {
      const which = e.kind === "report" ? "report" : "prompt";
      const holder = el("pre", { class: "block" }, "Loading…");
      const details = el("details", {}, el("summary", {}, which === "report" ? "Full report" : "Full text"), holder);
      details.addEventListener("toggle", async () => {
        if (details.open && holder.dataset.loaded !== "1") {
          const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/turns/${e.turn}/${which}`);
          holder.textContent = await response.text();
          holder.dataset.loaded = "1";
        }
      }, { once: false });
      node.append(details);
    }
    return node;
  }

  async function run(id) {
    setTab("runs");
    let t;
    try { t = await getJSON(`/api/runs/${encodeURIComponent(id)}`); } catch (error) { return showError(error); }
    const events = t.events;
    const totals = t.totals;
    const jobs = Object.entries(totals.jobs).map(([k, v]) => `${v} ${k}`).join(" · ") || "none";
    const tiles = el("div", { class: "grid tiles" },
      tile("Wall time", fmt.duration(totals.wall_seconds)), tile("Codex turns", totals.turns),
      tile("GPU jobs", Object.values(totals.jobs).reduce((a, b) => a + b, 0), jobs),
      tile("GPU hours", fmt.num(totals.gpu_hours, 1), totals.energy_wh ? `${fmt.energy(totals.energy_wh)} metered` : "energy not metered"),
      tile("Codex tokens", fmt.compact(totals.tokens)),
      tile("Verdicts · decisions", `${totals.verdicts} · ${totals.decisions}`, "recorded with codex-task note"));

    // Swimlanes
    const lanesBox = el("div", { class: "card", style: "padding:12px 16px" });
    const start = events.length ? events[0].at : Date.now() / 1000, end = events.length ? events[events.length - 1].at : start + 1;
    const marks = [];
    const turnStart = {};
    for (const e of events) {
      if (e.actor === "claude" && (e.kind === "brief" || e.kind === "instruction")) turnStart[e.turn] = e.at;
    }
    const jobStart = {};
    for (const e of events) {
      if (e.kind === "brief" || e.kind === "instruction" || e.kind === "steer") {
        marks.push({ lane: "claude", start: e.at, color: "var(--claude)", title: e.title, lines: [fmt.time(e.at)] });
      } else if (e.kind === "report" || e.kind === "working") {
        marks.push({ lane: "codex", start: turnStart[e.turn] || e.at, end: e.at, color: "var(--codex)", faint: e.kind === "working",
          title: `Turn ${e.turn}${e.status ? " · " + e.status : e.kind === "working" ? " · in progress" : ""}`,
          lines: [e.duration_seconds ? fmt.duration(e.duration_seconds) : "running", e.tokens ? `${fmt.compact(e.tokens.total_tokens)} tokens` : ""].filter(Boolean) });
      } else if (e.kind === "job") {
        jobStart[e.job] = e;
      } else if (e.kind === "job-end") {
        const begin = jobStart[e.job];
        marks.push({ lane: "gpu", start: begin ? begin.at : e.at, end: e.at, color: "var(--gpu)", faint: e.status !== "succeeded", job: e.job,
          title: `Job #${e.job} · ${e.status}`, lines: [e.name, e.energy_wh != null ? fmt.energy(e.energy_wh) : ""].filter(Boolean) });
        delete jobStart[e.job];
      } else if (e.actor === "human") {
        marks.push({ lane: "people", start: e.at, color: "var(--person)", shape: "diamond", title: e.title, lines: [e.text.slice(0, 80)] });
      } else if (e.actor === "system") {
        marks.push({ lane: "people", start: e.at, color: "var(--system)", shape: "ring", title: e.title, lines: [e.text.slice(0, 80)] });
      } else if (e.actor === "claude") {
        marks.push({ lane: "claude", start: e.at, color: "var(--claude)", title: e.title, lines: [e.text.slice(0, 80)] });
      }
    }
    for (const begin of Object.values(jobStart)) {
      marks.push({ lane: "gpu", start: begin.at, end: Date.now() / 1000, color: "var(--gpu)", faint: true, job: begin.job, title: `Job #${begin.job} · running`, lines: [begin.name] });
    }
    const lanes = [{ key: "claude", label: "Claude" }, { key: "codex", label: "Codex" }, { key: "gpu", label: "GPU jobs" }, { key: "people", label: "People · system" }];
    const legend = el("div", { class: "legend" }, actor("claude"), actor("codex"), actor("gpu"), actor("human"), actor("system"));

    // Event list and player
    const finals = {};
    for (const e of events) if (e.kind === "job-end") finals[e.job] = { status: e.status, energy_wh: e.energy_wh, duration_seconds: e.duration_seconds };
    const groups = groupEvents(events);
    const list = el("ol", { class: "timeline" }, groups.map((g) => eventItem(g, id, finals)));
    const items = [...list.children];
    const slider = el("input", { type: "range", min: 0, max: Math.max(0, groups.length - 1), value: groups.length - 1, "aria-label": "Replay position" });
    const position = el("span", { class: "muted num" });
    const playButton = el("button", { class: "btn primary" }, "▶ Replay");
    let lanesApi = null;
    const seek = (index, scroll) => {
      index = Number(index);
      items.forEach((item, i) => { item.classList.toggle("future", i > index); item.classList.toggle("current", i === index); });
      position.textContent = `${index + 1} / ${groups.length} · ${fmt.time(groups[index]?.at)}`;
      lanesApi?.setCursor(groups[index]?.at || start);
      if (scroll) items[index]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    slider.addEventListener("input", () => { stopPlayer(); seek(slider.value, true); });
    playButton.addEventListener("click", () => {
      if (player) { stopPlayer(); return; }
      let index = Number(slider.value) >= groups.length - 1 ? 0 : Number(slider.value);
      playButton.textContent = "❚❚ Pause";
      player = setInterval(() => {
        slider.value = index; seek(index, true);
        if (++index >= groups.length) stopPlayer();
      }, 650);
      player.button = playButton;
    });

    view.replaceChildren(
      el("p", {}, el("a", { href: "#/runs" }, "← All runs")),
      el("h1", {}, t.run.name), el("p", { class: "lede" }, `${t.run.project} · ${t.run.model || "Codex default model"}${t.run.effort ? " · " + t.run.effort : ""} · ${t.run.wakes}/${t.run.max_wakes ?? "–"} unattended wakes used`),
      tiles, el("h2", {}, "Who did what, when"), lanesBox,
      el("div", { class: "player" }, playButton, slider, position),
      el("h2", {}, "Timeline"), list);
    lanesApi = swimlanes(lanesBox, lanes, marks, { start, end, onPick: (mark) => { if (mark.job) location.hash = `#/receipt/${mark.job}`; } });
    lanesBox.append(legend);
    if (groups.length) seek(groups.length - 1, false);
  }

  function stopPlayer() {
    if (player) {
      clearInterval(player);
      if (player.button) player.button.textContent = "▶ Replay";
      player = null;
    }
  }

  // ------------------------------------------------------------------ public preview

  async function publicPreview() {
    setTab("public");
    let data;
    try { data = await getJSON("/api/public-preview"); } catch (error) { return showError(error); }
    const box = el("div");
    view.replaceChildren(el("h1", {}, "Public window preview"),
      el("p", { class: "lede" }, "Exactly what `labbook public` serves to anyone. It is built from an allowlist: no hostnames, paths, commands, job names, logs or transcripts. Map project names to public aliases in ~/.config/labbook/config.json."),
      box);
    window.renderPublic(box, data);
  }

  // ------------------------------------------------------------------ router

  function route() {
    clearInterval(timer);
    stopPlayer();
    const [name, arg] = (location.hash || "#/live").slice(2).split("/");
    window.scrollTo(0, 0);
    if (name === "receipts") receipts();
    else if (name === "receipt" && arg) receipt(arg);
    else if (name === "runs") runs();
    else if (name === "run" && arg) run(decodeURIComponent(arg));
    else if (name === "public") publicPreview();
    else { live(); timer = setInterval(live, 5000); }
  }

  // The header badge tracks telemetry freshness on every page.
  async function badge() {
    try {
      const data = await getJSON("/api/live");
      const age = data.age_seconds;
      freshness.className = `status ${data.pulse && age < 30 ? "live" : "stale"}`;
      freshness.textContent = data.pulse ? `telemetry ${fmt.ago(age)}` : "no telemetry";
    } catch (error) {
      freshness.className = "status stale";
      freshness.textContent = "notebook unreachable";
    }
  }

  window.addEventListener("hashchange", route);
  route();
  badge();
  setInterval(() => { if (!location.hash.startsWith("#/live") && location.hash) badge(); }, 10000);
})();
