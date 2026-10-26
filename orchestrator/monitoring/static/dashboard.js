// Agent Lab dashboard. Reads the local server's JSON and draws it.
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
    renderTop();
    renderTasks();
    renderTabs();
    renderPanel();
  }
  function renderTop() {
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
    live.textContent = !S.run ? "" : S.run.active ? `live · updated ${fresh} ago` : "nothing running";
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
  async function tick() {
    clearTimeout(S.timer);
    if (!S.busy) {
      S.busy = true;
      try {
        await loadRun();
        await refreshFeed();
        render();
      } finally {
        S.busy = false;
      }
    }
    S.timer = setTimeout(tick, S.run && S.run.active ? 2000 : 10000);
  }

  async function start() {
    $("error-close").addEventListener("click", dismissError);
    $("run").addEventListener("change", (e) => {
      S.runId = e.target.value; S.run = null; S.follow = true; S.sel = { task: null, tab: null };
      S.signature = ""; tick();
    });
    window.addEventListener("hashchange", () => {
      const wanted = readHash();
      if (wanted.run && wanted.run !== S.runId) { S.runId = wanted.run; S.run = null; }
      if (wanted.task) { S.follow = false; S.sel = { task: wanted.task, tab: wanted.tab }; }
      tick();
    });
    setInterval(() => { renderTop(); tickElapsed(); }, 1000);
    try {
      S.runs = (await api("/api/runs")).runs;
    } catch (err) {
      showError(err.message);
    }
    const wanted = readHash();
    S.runId = wanted.run || (S.runs[0] && S.runs[0].run_id) || null;
    if (wanted.task) { S.follow = false; S.sel = { task: wanted.task, tab: wanted.tab }; }
    render();
    tick();
  }
  start();
})();
