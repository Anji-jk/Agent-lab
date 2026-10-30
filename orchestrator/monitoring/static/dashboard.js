// Agent Lab dashboard. Reads the local server's JSON and draws it: the project's page
// (its totals and every run) when the address has no run, else that run.
// Every piece of text from a run goes on screen through textContent, never as HTML:
// agent output holds whatever the agent read, and it must not run in this page.
"use strict";
(() => {
  const ROLES = ["builder", "tester", "reviewer"];
  const TABS = [...ROLES, "orchestrator"];
  const ACTIVE = new Set(["building", "testing", "reviewing", "pending"]);
  const KIND = { start: "started", say: "says", think: "thinks", finish: "finished", error: "error", step: "step" };
  const RESULT = { ok: "ok", error: "error", blocked: "blocked", warn: "warning", background: "background" };
  const S = {
    view: "project", project: null, projectSig: "", chartPage: 0, tablePage: 0, filter: "all",
    runs: [], runId: null, run: null,
    sel: { task: null, tab: null }, follow: true,
    feeds: new Map(), open: new Set(), full: new Map(), signature: "",
    lastOk: 0, busy: false, timer: null,
  };
  const $ = (id) => document.getElementById(id);

  // ---- small helpers ---------------------------------------------------------------
  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  async function api(path) {
    const response = await fetch(path, { cache: "no-store" });
    let body = null;
    try { body = await response.json(); } catch (_) { /* not JSON */ }
    if (!response.ok) throw new Error((body && body.error) || `${response.status} ${response.statusText}`);
    return body;
  }
  const money = (v) => (typeof v === "number" ? `$${v.toFixed(4)}` : "");
  const tokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));
  function clock(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false });
  }
  function ago(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  }
  const since = (iso) => (Date.now() - new Date(iso).getTime()) / 1000;

  // ---- states and tones -------------------------------------------------------------
  function agentTone(a) {
    if (!a || a.state === "not started") return "idle";
    if (a.state === "running" || a.state === "starting") return a.stale ? "warn" : "running";
    return a.status === "succeeded" ? "ok" : "bad";
  }
  function agentLabel(a) {
    if (!a || a.state === "not started") return "not started";
    if (a.kept_from != null) return `${a.status || "finished"} in attempt ${a.kept_from} (kept)`;
    if (a.state === "running" || a.state === "starting") return a.stale ? "no heartbeat" : a.state;
    return a.status || "finished";
  }
  function taskTone(status) {
    if (ACTIVE.has(status)) return "running";
    if (status === "done") return "ok";
    if (["failed", "needs_review", "cancelled"].includes(status)) return "bad";
    if (status === "paused" || status === "interrupted") return "warn";
    return "idle";
  }
  const chipTone = (tone) => (tone === "idle" ? "" : tone);
  const agentLive = (a) => !!a && (a.state === "running" || a.state === "starting");

  // ---- selection ----------------------------------------------------------------------
  const task = (id) => (S.run ? S.run.tasks.find((t) => t.id === id) : null);
  function readHash() {
    const [run, taskId, tab] = decodeURIComponent(location.hash.slice(1)).split("/");
    return { run: run || null, task: taskId || null, tab: tab || null };
  }
  function writeHash() {
    if (!S.runId) return;
    const parts = [S.runId, S.sel.task, S.sel.task === "plan" ? null : S.sel.tab].filter(Boolean);
    history.replaceState(null, "", "#" + parts.map(encodeURIComponent).join("/"));
  }
  function autoSelection() {
    const run = S.run;
    if (run.planner && agentLive(run.planner)) return { task: "plan", tab: null };
    const active = run.tasks.find((t) => t.active_role);
    if (active) return { task: active.id, tab: active.active_role };
    const valid = S.sel.task === "plan" || task(S.sel.task);
    if (valid && S.sel.task) return S.sel;
    const started = run.tasks.filter((t) => t.status !== "not started");
    if (started.length) return { task: started[started.length - 1].id, tab: "orchestrator" };
    return { task: "plan", tab: null };
  }
  function fixSelection() {
    if (S.follow) S.sel = autoSelection();
    if (S.sel.task !== "plan" && !task(S.sel.task)) S.sel = { task: "plan", tab: null };
    if (S.sel.task !== "plan" && !TABS.includes(S.sel.tab)) {
      const t = task(S.sel.task);
      S.sel.tab = (t && t.active_role) || "builder";
    }
    writeHash();
  }
  function choose(taskId, tab) {
    S.follow = false;
    S.sel = { task: taskId, tab: taskId === "plan" ? null : tab };
    fixSelection();
    render();
    refreshFeed().then(render);
  }

  // ---- event feeds --------------------------------------------------------------------
  function sourceKey() {
    return `${S.runId}|${S.sel.task}|${S.sel.task === "plan" ? "planner" : S.sel.tab}`;
  }
  function sourceUrl(kind, extra) {
    const parts = S.sel.task === "plan" ? ["planner"] : [S.sel.task, S.sel.tab];
    return `/api/${kind}/${[S.runId, ...parts, ...(extra || [])].map(encodeURIComponent).join("/")}`;
  }
  function sourceLive() {
    if (!S.run) return false;
    if (S.sel.task === "plan") return agentLive(S.run.planner);
    const t = task(S.sel.task);
    if (!t) return false;
    if (S.sel.tab === "orchestrator") return ACTIVE.has(t.status);
    return agentLive(t.agents[S.sel.tab]);
  }
  async function refreshFeed() {
    if (!S.run) return;
    const key = sourceKey();
    let feed = S.feeds.get(key);
    if (!feed) { feed = { items: [], next: 0, loaded: false, wasLive: false, busy: false }; S.feeds.set(key, feed); }
    const live = sourceLive();
    if (feed.busy || (feed.loaded && !live && !feed.wasLive)) return;
    feed.busy = true;
    try {
      const data = await api(`${sourceUrl("events")}?after=${feed.next}`);
      feed.items.length = Math.min(feed.items.length, data.from);
      feed.items.push(...data.items);
      feed.next = data.next;
      feed.loaded = true;
      feed.wasLive = live;
      clearError();
    } catch (err) {
      showError(err.message);
    } finally {
      feed.busy = false;
    }
  }

  // ---- drawing ------------------------------------------------------------------------
  function render() {
    const inRun = S.view === "run";
    $("project").hidden = inRun;
    $("run-view").hidden = !inRun;
    renderTop();
    if (!inRun) { renderProject(); return; }
    renderTasks();
    renderTabs();
    renderPanel();
  }
  function renderTop() {
    const inRun = S.view === "run";
    const name = S.project ? S.project.project.name : "";
    document.title = name ? `${name} · Agent Lab` : "Agent Lab Dashboard";
    $("project-name").textContent = inRun ? "" : name;
    $("project-name").hidden = inRun;
    $("back").hidden = !inRun;
    $("back").textContent = `← ${name || "Project"}`;
    $("run-pick").hidden = !inRun;
    $("issue").hidden = !inRun;
    if (!inRun) {
      const p = S.project;
      const running = p ? p.totals.runs_by_state.running : 0;
      const fresh = S.lastOk ? ago((Date.now() - S.lastOk) / 1000) : "";
      $("cost").textContent = p ? `${money(p.totals.cost_usd)} spent` : "";
      $("live").className = "live" + (running ? " on" : "");
      $("live").textContent = !p ? "" : running ? `${plural(running, "run")} running · updated ${fresh} ago` : "nothing running";
      return;
    }
    const select = $("run");
    const ids = S.runs.map((r) => r.run_id);
    if (S.runId && !ids.includes(S.runId)) ids.unshift(S.runId);
    if (select.dataset.ids !== ids.join(",")) {
      select.replaceChildren(...ids.map((id) => {
        const r = S.runs.find((x) => x.run_id === id);
        const label = r && r.title ? `${id} · #${r.issue} ${r.title}` : id;
        return h("option", { value: id, text: label.length > 90 ? label.slice(0, 89) + "…" : label });
      }));
      select.dataset.ids = ids.join(",");
    }
    if (S.runId) select.value = S.runId;
    const issue = $("issue");
    issue.replaceChildren();
    if (S.run && S.run.issue && S.run.issue.title) {
      const text = `#${S.run.issue.number} ${S.run.issue.title}`;
      issue.append(S.run.issue.url
        ? h("a", { href: S.run.issue.url, target: "_blank", rel: "noopener noreferrer", text })
        : text);
      issue.title = text;
    }
    $("cost").textContent = S.run ? `${money(S.run.cost_usd)} spent` : "";
    const live = $("live");
    const fresh = S.lastOk ? ago((Date.now() - S.lastOk) / 1000) : "";
    live.className = "live" + (S.run && S.run.active ? " on" : "");
    const running = S.run && S.run.running ? S.run.running.length : 0;
    const many = running > 1 ? ` · ${running} tasks running` : "";
    live.textContent = !S.run ? "" : S.run.active ? `live${many} · updated ${fresh} ago` : "nothing running";
  }
  function renderTasks() {
    const bar = $("tasks");
    bar.replaceChildren();
    if (!S.run) return;
    const planner = S.run.planner;
    bar.append(h("button", {
      class: "task", type: "button", "aria-pressed": String(S.sel.task === "plan"),
      onclick: () => choose("plan", null),
    }, h("span", { class: `dot ${planner ? agentTone(planner) : "ok"}` }),
       h("span", { class: "id", text: "Plan" }),
       h("span", { class: "tt", text: planner ? `planner ${agentLabel(planner)}` : `${S.run.tasks.length} tasks` })));
    for (const t of S.run.tasks) {
      let state = t.status;
      if (t.status === "not started" && t.waiting_for.length) state = `waits for ${t.waiting_for.join(", ")}`;
      if (t.merging) state = "merging";
      bar.append(h("button", {
        class: "task", type: "button", "aria-pressed": String(S.sel.task === t.id), title: t.title,
        onclick: () => choose(t.id, t.active_role || S.sel.tab || "builder"),
      }, h("span", { class: `dot ${taskTone(t.status)}` }),
         h("span", null, h("span", { class: "id", text: t.id }), " ", h("span", { class: "st", text: state })),
         h("span", { class: "tt", text: t.title })));
    }
  }
  function renderTabs() {
    const tabs = $("tabs");
    tabs.replaceChildren();
    tabs.hidden = !S.run || S.sel.task === "plan";
    if (tabs.hidden) return;
    const t = task(S.sel.task);
    if (!t) return;
    for (const name of TABS) {
      const agent = t.agents[name];
      const tone = name === "orchestrator" ? taskTone(t.status) : agentTone(agent);
      const label = name === "orchestrator" ? t.status : agentLabel(agent);
      tabs.append(h("button", {
        class: "tab", type: "button", role: "tab", "aria-selected": String(S.sel.tab === name),
        onclick: () => choose(t.id, name),
      }, h("span", { class: `dot ${tone}` }), h("span", { class: "name", text: cap(name) }),
         h("span", { class: "st", text: label })));
    }
  }
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function renderPanel() {
    const panel = $("panel");
    if (!S.run) {
      if (!S.runs.length) panel.replaceChildren(h("div", { class: "empty", text: "No runs yet. Plan an Issue with: python -m orchestrator ai-plan <number>" }));
      return;
    }
    let top = $("panel-top");
    let events = $("panel-events");
    if (!top || !events) {
      top = h("div", { id: "panel-top", class: "panel-top" });
      events = h("div", { id: "panel-events" });
      panel.replaceChildren(top, events);
      S.signature = "";
    }
    // The header and cards are cheap and change with every refresh while an agent runs.
    top.replaceChildren(...(S.sel.task === "plan" ? planParts() : taskParts(task(S.sel.task))));

    // The event list is rebuilt only when it changed, so open details keep their scroll.
    const key = sourceKey();
    const feed = S.feeds.get(key);
    const live = sourceLive();
    const signature = JSON.stringify([key, live, feed && feed.loaded, feed && feed.items.length, feed && feed.next,
      feed && feed.items.slice(-3).map((i) => [i.pending, i.result && i.result.status]),
      [...S.open].filter((id) => id.startsWith(key)), [...S.full.keys()].filter((id) => id.startsWith(key))]);
    if (signature === S.signature) return;
    S.signature = signature;
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 140;
    events.replaceChildren(eventList(key, feed, live));
    if (live && atBottom && S.follow) window.scrollTo(0, document.body.scrollHeight);
  }

  // ---- the project page ------------------------------------------------------------
  const CHART_PAGE = 12;
  const TABLE_PAGE = 10;  // rows per page of the runs table: the newest 10, then older pages
  const RUN_STATE = {
    complete: ["Complete", "ok"], running: ["Running", "running"], attention: ["Needs attention", "bad"],
    paused: ["Paused", "warn"], planned: ["Planned", ""],
  };
  const FILTERS = [
    ["all", "All", () => true],
    ["running", "Running", (r) => r.status === "running"],
    ["complete", "Complete", (r) => r.status === "complete"],
    ["attention", "Needs attention", (r) => r.status === "attention"],
    ["unfinished", "Not finished", (r) => r.status === "paused" || r.status === "planned"],
  ];
  const OUTCOMES = [["done", "Done"], ["failed", "Failed"], ["review", "Needs review"],
                    ["running", "Running now"], ["waiting", "Not started"]];
  const plural = (n, one) => `${n} ${n === 1 ? one : one + "s"}`;
  function duration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "—";
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  }
  function when(iso) {
    const d = new Date(iso || "");
    return Number.isNaN(d.getTime()) ? "—"
      : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  }
  function runTime(r) {
    if (r.status === "running") return r.started_at ? `${duration(since(r.started_at))} so far` : "running";
    if (!r.started_at || !r.finished_at) return "—";
    return duration((new Date(r.finished_at) - new Date(r.started_at)) / 1000);
  }
  function day(iso) {
    const d = new Date(iso || "");
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString([], { day: "numeric", month: "short" });
  }
  const issueText = (r) => (r.issue && r.issue.title ? `#${r.issue.number} ${r.issue.title}` : r.run_id);
  const runHref = (r) => `#${encodeURIComponent(r.run_id)}`;
  const githubUrl = (repository) => `https://github.com/${repository.split("/").map(encodeURIComponent).join("/")}`;

  function renderProject(force) {
    const box = $("project");
    const p = S.project;
    if (!p) {
      if (!box.firstChild) box.replaceChildren(h("div", { class: "empty", text: "Loading the project…" }));
      return;
    }
    const signature = JSON.stringify([p.project, p.totals, p.runs, S.chartPage, S.tablePage, S.filter]);
    if (!force && signature === S.projectSig) return;
    S.projectSig = signature;
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.key : null;
    const parts = [projectTitle(p.project), projectCards(p)];
    if (!p.runs.length) {
      parts.push(h("div", { class: "empty", text: "No runs yet. Plan an Issue with: python -m orchestrator ai-plan <number>" }));
    } else {
      parts.push(h("div", { class: "boxes" }, spendChart(p.runs), outcomes(p.totals)), runsTable(p.runs));
    }
    box.replaceChildren(...parts);
    const chart = box.querySelector(".chart");
    if (chart) chart.scrollLeft = chart.scrollWidth;
    if (focused) {  // the last page disables the button that led there: focus its partner
      const partner = focused.endsWith("-older") ? focused.replace(/-older$/, "-newer")
        : focused.replace(/-newer$/, "-older");
      const again = [focused, partner].map((name) => box.querySelector(`[data-key="${name}"]`))
        .find((node) => node && !node.disabled);
      if (again) again.focus();
    }
  }
  function projectTitle(info) {
    const bits = [h("h1", { text: info.name })];
    if (info.repository) {
      bits.push(h("a", { class: "mono", href: githubUrl(info.repository), target: "_blank",
        rel: "noopener noreferrer", text: info.repository }));
    }
    if (info.board) {
      const text = `GitHub board #${info.board.number}`;
      bits.push(info.board.url
        ? h("a", { class: "mono", href: info.board.url, target: "_blank", rel: "noopener noreferrer", text })
        : h("span", { class: "mono muted", text }));
    }
    bits.push(h("span", { class: "mono muted path", text: info.path }));
    return h("div", { class: "ptitle" }, bits);
  }
  function stat(label, value, ...lines) {
    return h("div", { class: "stat" }, h("span", { class: "label", text: label }),
      h("span", { class: "value", text: value }),
      lines.filter(Boolean).map((line) => h("span", { class: "sub", text: line })));
  }
  function projectCards(p) {
    const t = p.totals;
    const rs = t.runs_by_state;
    const tg = t.tasks_by_group;
    const runs = [`${rs.complete} complete`, `${rs.running} running`,
      `${rs.attention} ${rs.attention === 1 ? "needs" : "need"} attention`];
    if (rs.paused) runs.push(`${rs.paused} paused`);
    if (rs.planned) runs.push(`${rs.planned} planned`);
    const tasks = [`${tg.done} done`, `${tg.failed} failed`, `${tg.review} ${tg.review === 1 ? "needs" : "need"} review`];
    if (tg.running) tasks.push(`${tg.running} running`);
    if (tg.waiting) tasks.push(`${tg.waiting} not started`);
    const last = p.runs[0];
    return h("section", { class: "stats-wrap", "aria-label": "Totals for this project" },
      h("h2", { class: "section-title", text: "Totals for this project" }),
      h("div", { class: "stats" },
        stat("Total spent", money(t.cost_usd), `planner ${money(t.planner_cost_usd)} · tasks ${money(t.tasks_cost_usd)}`),
        stat("Run summary", plural(t.runs, "run"), runs.join(" · "), `${plural(t.tasks, "task")}: ${tasks.join(" · ")}`),
        last ? stat("Last run", runTime(last) === "—" ? RUN_STATE[last.status][0] : runTime(last),
                    `${when(last.started_at || last.planned_at)} · ${money(last.cost_usd)}`, issueText(last))
             : stat("Last run", "—", "no runs yet")));
  }
  function key(kind, label) {
    return h("span", { class: "key" }, h("span", { class: `swatch ${kind}` }), label);
  }
  // Page 0 is the newest. The chart reads oldest on the left, so older is to the left;
  // the table reads newest at the top, so its older runs are on the next page, to the right.
  function pager(page, pages, label, older, newer, prefix, olderOnRight) {
    if (pages < 2) return null;
    const olderButton = h("button", { type: "button", "data-key": `${prefix}-older`, disabled: page >= pages - 1,
      text: olderOnRight ? "Older →" : "← Older", onclick: older });
    const newerButton = h("button", { type: "button", "data-key": `${prefix}-newer`, disabled: page === 0,
      text: olderOnRight ? "← Newer" : "Newer →", onclick: newer });
    const text = h("span", { class: "muted", text: label });
    return h("div", { class: "pager" }, ...(olderOnRight ? [newerButton, text, olderButton] : [olderButton, text, newerButton]));
  }
  function spendChart(runs) {
    const pages = Math.max(1, Math.ceil(runs.length / CHART_PAGE));
    S.chartPage = Math.min(S.chartPage, pages - 1);
    const start = S.chartPage * CHART_PAGE;
    const shown = runs.slice(start, start + CHART_PAGE).reverse();
    const top = Math.max(0.0001, ...shown.map((r) => r.cost_usd));
    const columns = shown.map((r) => {
      const planner = h("span", { class: "seg spend-planner" });
      const tasks = h("span", { class: "seg spend-tasks" });
      planner.style.height = `${Math.round((r.planner_cost_usd / top) * 150)}px`;
      tasks.style.height = `${Math.round((r.tasks_cost_usd / top) * 150)}px`;
      return h("a", { class: "bar-col", href: runHref(r),
        title: `${issueText(r)}: ${money(r.cost_usd)} (planner ${money(r.planner_cost_usd)}, tasks ${money(r.tasks_cost_usd)})` },
        h("span", { class: "bar-value", text: `$${r.cost_usd.toFixed(2)}` }),
        h("span", { class: "bar", "aria-hidden": "true" }, planner, tasks),
        h("span", { class: "bar-label", text: r.issue && r.issue.number != null ? `#${r.issue.number}` : r.run_id.slice(-6) }),
        h("span", { class: "bar-date", text: day(r.started_at || r.planned_at) }));
    });
    const label = `Runs ${start + 1}–${start + shown.length} of ${runs.length}, newest first`;
    return h("section", { class: "box chart-box", "aria-label": "Spend per run" },
      h("div", { class: "box-head" }, h("h2", { text: "Spend per run" }), key("spend-planner", "Planner"), key("spend-tasks", "Tasks")),
      h("div", { class: "chart" }, columns),
      pager(S.chartPage, pages, label,
        () => { S.chartPage += 1; renderProject(true); }, () => { S.chartPage -= 1; renderProject(true); }, "chart"));
  }
  function outcomes(t) {
    const groups = t.tasks_by_group;
    const stack = h("div", { class: "stack", "aria-hidden": "true" });
    for (const [kind] of OUTCOMES) {
      if (!groups[kind]) continue;
      const seg = h("span", { class: `seg ${kind}` });
      seg.style.flexGrow = String(groups[kind]);
      stack.append(seg);
    }
    return h("section", { class: "box", "aria-label": "What happened to the tasks" },
      h("div", { class: "box-head" }, h("h2", { text: "What happened to the tasks" })), stack,
      h("ul", { class: "legend" }, OUTCOMES.map(([kind, label]) => h("li", null,
        h("span", { class: `swatch ${kind}` }), h("span", { class: "grow", text: label }),
        h("span", { class: "num", text: String(groups[kind]) })))));
  }
  function runsTable(runs) {
    const counts = Object.fromEntries(FILTERS.map(([name, , test]) => [name, runs.filter(test).length]));
    const [, , test] = FILTERS.find(([name]) => name === S.filter) || FILTERS[0];
    const chosen = runs.filter(test);
    const pages = Math.max(1, Math.ceil(chosen.length / TABLE_PAGE));
    S.tablePage = Math.min(S.tablePage, pages - 1);
    const start = S.tablePage * TABLE_PAGE;
    const shown = chosen.slice(start, start + TABLE_PAGE);
    const filters = h("div", { class: "filters", role: "group", "aria-label": "Show runs" },
      FILTERS.map(([name, label]) => h("button", {
        type: "button", class: "filter", "data-key": `filter-${name}`, "aria-pressed": String(S.filter === name),
        text: `${label} ${counts[name]}`, onclick: () => { S.filter = name; S.tablePage = 0; renderProject(true); },
      })));
    const rows = shown.map((r) => {
      const [label, tone] = RUN_STATE[r.status] || [r.status, ""];
      const fill = h("span", { class: "fill" });
      fill.style.width = `${r.tasks_total ? Math.round((r.tasks_done / r.tasks_total) * 100) : 0}%`;
      return h("tr", { onclick: () => { location.hash = runHref(r); } },
        h("td", null, h("a", { class: "mono", href: runHref(r), text: r.run_id })),
        h("td", { class: "issue-cell", text: issueText(r), title: issueText(r) }),
        h("td", null, h("span", { class: `chip ${tone}`, text: label })),
        h("td", null, h("span", { class: "progress" }, h("span", { class: "track", "aria-hidden": "true" }, fill),
          `${r.tasks_done} of ${r.tasks_total}`)),
        h("td", { class: "num", text: money(r.cost_usd) }),
        h("td", { class: "num", text: runTime(r) }),
        h("td", { class: "muted", text: when(r.started_at || r.planned_at) }));
    });
    if (!rows.length) rows.push(h("tr", null, h("td", { colspan: "7", class: "muted", text: "No runs here." })));
    const table = h("table", { class: "runs-table" },
      h("thead", null, h("tr", null, ["Run", "Issue", "Status", "Tasks", "Cost", "Time", "Started"].map((name) =>
        h("th", { scope: "col", class: ["Cost", "Time"].includes(name) ? "num" : null, text: name })))),
      h("tbody", null, rows));
    const label = `Runs ${chosen.length ? start + 1 : 0}–${start + shown.length} of ${chosen.length}, `
      + `page ${S.tablePage + 1} of ${pages}`;
    return h("section", { class: "runs", "aria-label": "Runs" },
      h("div", { class: "runs-head" }, h("h2", { class: "section-title", text: `Runs (${runs.length}), newest first` }), filters),
      h("div", { class: "table-box" }, table),
      pager(S.tablePage, pages, label,
        () => { S.tablePage += 1; renderProject(true); }, () => { S.tablePage -= 1; renderProject(true); }, "table", true));
  }
  // Elapsed times on steps still running tick every second without redrawing the list.
  function tickElapsed() {
    for (const node of document.querySelectorAll("[data-since]")) {
      node.textContent = `for ${ago(since(node.dataset.since))}`;
    }
  }

  function followButton() {
    return h("button", {
      class: "follow", type: "button", "aria-pressed": String(S.follow),
      title: "Jump to whichever agent is working, and keep following it",
      onclick: () => { S.follow = !S.follow; fixSelection(); render(); refreshFeed().then(render); },
      text: S.follow ? "Following live" : "Follow live",
    });
  }
  function metrics(a, settings) {
    const bits = [];
    if (a && a.state !== "not started") {
      bits.push(`${a.turns} turns`);
      if (!a.from_record) bits.push(`${tokens(a.tokens_read)} tokens read, ${tokens(a.tokens_written)} written`);
      if (a.cost_usd != null) bits.push(money(a.cost_usd));
      if (agentLive(a) && a.last_tool) bits.push(`last tool ${a.last_tool}`);
      if (agentLive(a) && a.heartbeat_age != null) bits.push(`heartbeat ${ago(a.heartbeat_age)} ago`);
    }
    if (settings) bits.push(settings.replace(/^\w+: /, ""));
    return h("span", { class: "metrics", text: bits.join(" · ") });
  }
  function head(title, tone, label, a, settings) {
    return h("div", { class: "head" }, h("h2", { text: title }),
      h("span", { class: `chip ${chipTone(tone)}`, text: label }), metrics(a, settings), followButton());
  }
  function list(values) {
    return h("ul", null, (values || []).map((v) => h("li", { text: String(v) })));
  }
  function kv(rows) {
    const dl = h("dl", { class: "kv" });
    for (const [label, value] of rows) {
      if (value == null || value === "" || (Array.isArray(value) && !value.length)) continue;
      dl.append(h("dt", { text: label }), h("dd", null, value instanceof Node ? value : Array.isArray(value) ? list(value) : String(value)));
    }
    return dl;
  }
  function card(title, body, tone) {
    return h("div", { class: `card ${tone || ""}` }, h("h3", { text: title }), body);
  }
  function command(check) {
    if (!check || check.exit_code === undefined) return null;
    const took = typeof check.seconds === "number" ? ` in ${check.seconds.toFixed(1)}s` : "";
    if (check.timed_out) return `timed out${took}`;
    return check.exit_code === 0 ? `passed${took}` : `failed (exit ${check.exit_code})${took}`;
  }

  const MERGE_KIND = { "fast-forward": "fast-forward", merge: "real merge (full suite run)", already: "already there" };
  function planParts() {
    const run = S.run;
    const p = run.planner;
    const parts = [head("Planner", p ? agentTone(p) : "ok", p ? agentLabel(p) : "plan stored", p, "")];
    parts.push(card("Plan", kv([
      ["Issue", run.issue && run.issue.title ? `#${run.issue.number} ${run.issue.title}` : null],
      ["Repository", run.issue && run.issue.repository],
      ["Approach", run.summary],
      ["Tasks", run.tasks.map((t) => `${t.id} · ${t.complexity || "?"} · ${t.title}` +
        (t.depends_on.length ? ` (after ${t.depends_on.join(", ")})` : ""))],
      ["Base commit", run.base_commit ? run.base_commit.slice(0, 12) : null],
      ["Run branch", run.integration ? `${run.integration.branch} at ${(run.integration.head || "").slice(0, 12)}` : null],
      ["Merges", run.integration && run.integration.merges.length
        ? run.integration.merges.map((m) => `${m.task} · ${MERGE_KIND[m.kind] || m.kind}`) : null],
      ["Running now", run.running && run.running.length ? run.running.join(", ") : null],
    ])));
    return parts;
  }

  function taskParts(t) {
    if (!t) return [h("div", { class: "empty", text: "This task is not in the run." })];
    const parts = [];
    const tab = S.sel.tab;
    const failed = ["failed", "needs_review", "cancelled"].includes(t.status);
    if (tab === "orchestrator") {
      parts.push(head("Orchestrator", taskTone(t.status), t.status, null, ""));
    } else {
      const a = t.agents[tab];
      parts.push(head(tab, agentTone(a), agentLabel(a), a, a.settings));
      if (a.stale) parts.push(card("No heartbeat", h("p", { text: `Nothing from this agent for ${ago(a.heartbeat_age)}. Its process may have stopped.` }), "warn"));
    }
    const why = { failed: "Why it failed", needs_review: "Why it needs review", cancelled: "Cancelled" };
    if (failed && t.reasons.length) parts.push(card(why[t.status], list(t.reasons), t.status === "cancelled" ? "warn" : "bad"));
    if (t.status === "interrupted" && t.interrupted) {
      parts.push(card("Interrupted", list([t.interrupted, "run-plan again, or run-task --continue, takes it up from where it stopped."]), "warn"));
    }
    parts.push(tab === "orchestrator" ? orchestratorCard(t) : roleCard(t, tab));
    return parts.filter(Boolean);
  }

  function orchestratorCard(t) {
    const criteria = h("ol", null, t.acceptance_criteria.map((c) => h("li", { text: c })));
    return card("Task", kv([
      ["Title", t.title],
      ["Status", t.status],
      ["Waits for", t.waiting_for.length && t.status === "not started" ? t.waiting_for.join(", ") + " to be done" : null],
      ["Complexity", t.complexity],
      ["Files planned", t.files],
      ["Acceptance criteria", t.acceptance_criteria.length ? criteria : null],
      ["Branch", t.branch],
      ["Worktree", t.worktree_path],
      ["Commit", t.commit],
      ["Merge", t.merging ? "merging into the run's branch now" : t.merge_text],
      ["Cost", t.cost_usd ? money(t.cost_usd) : null],
      ["Result", t.status === "done" && t.reasons.length ? t.reasons : null],
    ]));
  }

  function roleCard(t, name) {
    const a = t.agents[name];
    const f = t.findings || {};
    const c = t.checks || {};
    if (a.state === "not started") {
      const waits = t.status === "not started" && t.waiting_for.length
        ? `The task waits for ${t.waiting_for.join(", ")} to be done. ${a.waits_for}` : a.waits_for;
      return card("Not started yet", kv([["Job", a.purpose], ["Runs with", a.settings], ["When", waits]]));
    }
    if (agentLive(a)) return card("About this agent", kv([["Job", a.purpose], ["Runs with", a.settings]]));
    // a continued task does not run again the agents that finished before it stopped.
    const kept = a.kept_from != null
      ? `This agent ran in attempt ${a.kept_from}. The task stopped after it finished, so this attempt kept its result and did not run it again.`
      : null;
    let rows;
    if (name === "builder") {
      rows = [["Files changed", c.builder_changed], ["Outside the plan", f.scope_violations],
              ["Says it is blocked", f.builder_blocked || null]];
    } else if (name === "tester") {
      const uncovered = Object.entries(f.uncovered || {}).map(([n, why]) => `criterion ${n}: ${why}`);
      rows = [["Files written", c.tester_changed], ["Full suite (run by the orchestrator)", command(c.suite)],
              ["Its named tests (run by the orchestrator)", command(c.named_tests)], ["Could not automate", uncovered],
              ["Wrote outside tests/", f.tester_wrote_outside_tests], ["Weakened tests", f.weakened_tests],
              ["Suspected defects", f.suspected_defects], ["Notes", f.tester_notes]];
    } else {
      rows = [["Verdict", f.reviewer_verdict || null],
              ["Criteria not met", (f.unmet || []).length ? f.unmet.map((n) => `criterion ${n}`) : null],
              ["Problems", f.reviewer_problems], ["Notes", f.reviewer_notes]];
    }
    rows.unshift(["Job", a.purpose]);
    if (kept) rows.unshift(["Kept", kept]);
    return card("Report", kv(rows));
  }

  function eventList(key, feed, live) {
    if (!feed || !feed.loaded) return h("div", { class: "empty", text: "Loading events…" });
    if (!feed.items.length) {
      const t = task(S.sel.task);
      const notStarted = S.sel.task !== "plan" && S.sel.tab !== "orchestrator" && t && t.agents[S.sel.tab].state === "not started";
      return h("div", { class: "empty", text: notStarted ? "This agent has not started, so there are no events yet."
        : "No events recorded. Runs from before the event log was added have none." });
    }
    return h("ol", { class: "events" }, feed.items.map((it) => eventRow(key, it, live)));
  }

  function eventRow(key, it, live) {
    const id = `${key}#${it.index}`;
    const full = S.full.get(id);
    const item = full || it;
    const open = S.open.has(id);
    const hasDetail = !!(item.detail || (item.result && item.result.detail));
    const kindText = it.kind === "tool" ? (it.tool || "result") : KIND[it.kind] || it.kind;
    const li = h("li", { class: `ev ${it.kind}${hasDetail ? " clickable" : ""}` },
      h("span", { class: "time", text: it.at ? clock(it.at) : "—" }),
      h("span", { class: `kind ${it.kind === "tool" ? "tool" : it.kind}`, text: kindText, title: kindText }),
      h("span", { class: "title", text: it.title || "" }),
      hasDetail ? h("button", { class: "toggle", type: "button", "aria-expanded": String(open),
        "aria-label": open ? "Hide details" : "Show details", text: open ? "▾" : "▸" }) : h("span"));

    if (it.result) {
      li.append(h("div", { class: "res" },
        h("span", { class: `chip ${it.result.status === "ok" ? "ok" : ["blocked", "background"].includes(it.result.status) ? "warn" : "bad"}`,
          text: RESULT[it.result.status] || it.result.status }),
        h("span", { class: "sum", text: it.result.summary || "", title: it.result.summary || "" })));
    } else if (it.pending) {
      li.append(h("div", { class: "res" },
        h("span", { class: `chip ${live ? "running" : "warn"}`, text: live ? "running" : "no result" }),
        h("span", { class: "sum", "data-since": live && it.at ? it.at : null,
          text: live ? (it.at ? `for ${ago(since(it.at))}` : "") : "stopped before this finished" })));
    } else if (it.kind === "step" && it.status && it.status !== "ok") {
      li.append(h("div", { class: "res" }, h("span", { class: "chip bad", text: "problem" })));
    }

    if (open) {
      const box = h("div", { class: "detail" });
      if (item.detail) box.append(h("span", { class: "label", text: it.kind === "tool" ? "input" : "details" }), h("pre", { text: item.detail }));
      if (item.result && item.result.detail) box.append(h("span", { class: "label", text: "result" }), h("pre", { text: item.result.detail }));
      if (item.truncated && !full) {
        box.append(h("button", { class: "more", type: "button", text: "Show everything",
          onclick: async (e) => {
            e.stopPropagation();
            try { S.full.set(id, await api(sourceUrl("event", [String(it.index)]))); render(); }
            catch (err) { showError(err.message); }
          } }));
      }
      li.append(box);
    }
    if (hasDetail) {
      li.addEventListener("click", (e) => {
        if (e.target.closest(".detail") || window.getSelection().toString()) return;
        if (S.open.has(id)) S.open.delete(id); else S.open.add(id);
        render();
      });
    }
    return li;
  }

  // ---- errors and the refresh loop ---------------------------------------------------
  // A closed notice stays closed while the same failure repeats on every refresh.
  function showError(message) {
    if (message === S.dismissedError) return;
    $("error-text").textContent = message; $("error").hidden = false;
  }
  function clearError() { $("error").hidden = true; S.dismissedError = null; }
  function dismissError() { S.dismissedError = $("error-text").textContent; $("error").hidden = true; }

  async function loadRun() {
    if (!S.runId) return;
    try {
      S.run = await api(`/api/run/${encodeURIComponent(S.runId)}`);
      S.lastOk = Date.now();
      clearError();
      fixSelection();
    } catch (err) {
      showError(err.message);
    }
  }
  async function loadProject() {
    try {
      S.project = await api("/api/project");
      S.lastOk = Date.now();
      clearError();
    } catch (err) {
      showError(err.message);
    }
  }
  async function tick() {
    clearTimeout(S.timer);
    if (!S.busy) {
      S.busy = true;
      try {
        if (S.view === "project") {
          await loadProject();
        } else {
          await loadRun();
          await refreshFeed();
        }
        render();
      } finally {
        S.busy = false;
      }
    }
    const wait = S.view === "project" ? (S.project && S.project.active ? 5000 : 30000)
      : (S.run && S.run.active ? 2000 : 10000);
    S.timer = setTimeout(tick, wait);
  }
  function applyHash() {
    const wanted = readHash();
    if (!wanted.run) {
      S.view = "project";
      S.projectSig = "";
      return;
    }
    S.view = "run";
    if (wanted.run !== S.runId) {
      S.runId = wanted.run; S.run = null; S.follow = true; S.sel = { task: null, tab: null }; S.signature = "";
    }
    if (wanted.task) { S.follow = false; S.sel = { task: wanted.task, tab: wanted.tab }; }
  }

  async function start() {
    $("error-close").addEventListener("click", dismissError);
    $("run").addEventListener("change", (e) => {
      S.runId = e.target.value; S.run = null; S.follow = true; S.sel = { task: null, tab: null };
      S.signature = ""; tick();
    });
    window.addEventListener("hashchange", () => {
      const before = S.view;
      applyHash();
      if (before !== S.view) window.scrollTo(0, 0);
      render();
      tick();
    });
    setInterval(() => { renderTop(); tickElapsed(); }, 1000);
    try {
      S.runs = (await api("/api/runs")).runs;
    } catch (err) {
      showError(err.message);
    }
    applyHash();
    render();
    tick();
    if (S.view === "run") loadProject().then(renderTop);
  }
  start();
})();
