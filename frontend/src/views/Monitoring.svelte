<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { status } from "../lib/stores";
  import { toast } from "../lib/toast";
  import { ts } from "../lib/format";

  // ── global config ──────────────────────────────────────────────────────────
  let enabled = false;
  let send_when_degraded = true;

  const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
    "Pacific/Honolulu", "UTC",
  ];

  // ── destinations (each: where + over which interface + on its own cadence) ──
  type Sched = { enabled: boolean; timezone: string; days: number[]; start: string; end: string; override: string };
  type Dest = {
    name: string; enabled: boolean; egress: string; method: string; url: string;
    headers: { key: string; value: string }[];
    expectStatus: string; interval_seconds: number; timeout_seconds: number;
    schedule: Sched; showSched: boolean;
  };
  const newSchedule = (): Sched => ({
    enabled: false, timezone: "America/New_York",
    days: [0, 1, 2, 3, 4], start: "09:00", end: "18:00", override: "auto",
  });
  const newDest = (egress = "wlan"): Dest => ({
    name: "", enabled: true, egress, method: "POST", url: "", headers: [],
    expectStatus: "200, 204", interval_seconds: 300, timeout_seconds: 15,
    schedule: newSchedule(), showSched: false,
  });
  let destinations: Dest[] = [];

  function addDest() { destinations = [...destinations, newDest()]; }
  function removeDest(i: number) { destinations = destinations.filter((_, idx) => idx !== i); }
  function toggleDay(i: number, d: number) {
    const days = destinations[i].schedule.days;
    destinations[i].schedule.days = days.includes(d)
      ? days.filter((x) => x !== d) : [...days, d].sort((a, b) => a - b);
    destinations = destinations;
  }
  function addHeader(i: number) {
    destinations[i].headers = [...destinations[i].headers, { key: "", value: "" }];
    destinations = destinations;
  }
  function removeHeader(i: number, hi: number) {
    destinations[i].headers = destinations[i].headers.filter((_, idx) => idx !== hi);
    destinations = destinations;
  }

  // body: structured builder (default) or raw template — SHARED across destinations
  let useRawBody = false;
  let rawBody = "";
  let fields: { path: string; value: string; kind: string }[] = [];
  let custom = { path: "", value: "", kind: "static" };

  let phValues: Record<string, any> = {};
  let history: any[] = [];
  let total = 0;
  let page = 0;
  const PAGE_SIZE = 25;

  // Catalog of clickable fields → JSON path + placeholder. Grouped to match a
  // typical ingest schema (top level / signal.* / meta.*).
  const CATALOG: { group: string; label: string; path: string; value: string }[] = [
    { group: "Top level", label: "iccid", path: "iccid", value: "iccid" },
    { group: "Top level", label: "status", path: "status", value: "status" },
    { group: "Top level", label: "rat", path: "rat", value: "rat" },
    { group: "Top level", label: "network (operator)", path: "network", value: "operator" },
    { group: "signal", label: "rssi_dbm", path: "signal.rssi_dbm", value: "rssi" },
    { group: "signal", label: "rsrp_dbm", path: "signal.rsrp_dbm", value: "rsrp" },
    { group: "signal", label: "rsrq_db", path: "signal.rsrq_db", value: "rsrq" },
    { group: "signal", label: "sinr_db", path: "signal.sinr_db", value: "sinr" },
    { group: "signal", label: "bars (%)", path: "signal.bars", value: "signal_percent" },
    { group: "signal", label: "band", path: "signal.band", value: "band" },
    { group: "signal", label: "earfcn", path: "signal.earfcn", value: "earfcn" },
    { group: "signal", label: "cell_id", path: "signal.cell_id", value: "cell_id" },
    { group: "signal", label: "tac", path: "signal.tac", value: "tac" },
    { group: "signal", label: "pci", path: "signal.pci", value: "pci" },
    { group: "signal", label: "mcc", path: "signal.mcc", value: "mcc" },
    { group: "signal", label: "mnc", path: "signal.mnc", value: "mnc" },
    { group: "signal", label: "operator", path: "signal.operator", value: "operator" },
    { group: "meta", label: "probe_id (SIM name)", path: "meta.probe_id", value: "sim_name" },
    { group: "meta", label: "fw", path: "meta.fw", value: "firmware" },
    { group: "meta", label: "modem_model", path: "meta.modem_model", value: "modem_model" },
    { group: "meta", label: "imei", path: "meta.imei", value: "imei" },
    { group: "meta", label: "imsi", path: "meta.imsi", value: "imsi" },
    { group: "meta", label: "apn", path: "meta.apn", value: "apn" },
    { group: "meta", label: "ip (cellular)", path: "meta.ip", value: "ip_address" },
    { group: "meta", label: "wlan0_ip", path: "meta.wlan0_ip", value: "wlan0_ip" },
    { group: "meta", label: "eth0_ip", path: "meta.eth0_ip", value: "eth0_ip" },
    { group: "meta", label: "gateway", path: "meta.gateway", value: "gateway" },
    { group: "meta", label: "public_ip", path: "meta.public_ip", value: "public_ip" },
    { group: "meta", label: "interface (cellular)", path: "meta.interface", value: "interface" },
    { group: "meta", label: "egress_interface (heartbeat path)", path: "meta.egress_interface", value: "egress_interface" },
    { group: "meta", label: "registration", path: "meta.registration", value: "registration" },
    { group: "meta", label: "status_message", path: "meta.status_message", value: "status_message" },
    { group: "meta", label: "last_error", path: "meta.last_error", value: "last_error" },
    { group: "meta", label: "uptime_s", path: "meta.uptime_s", value: "uptime_s" },
    { group: "meta", label: "cpu_load", path: "meta.cpu_load", value: "cpu_load" },
    { group: "meta", label: "mem_free_mb", path: "meta.mem_free_mb", value: "mem_free_mb" },
    { group: "meta", label: "temperature_c", path: "meta.temperature_c", value: "temperature_c" },
    { group: "meta", label: "sampled_at", path: "meta.sampled_at", value: "sampled_at" },
  ];

  // Cellular-path stats from the two probe monitors (last cycle + 1h/3h/6h/24h
  // windows), each exposing avg/min/max latency + loss/fail. Generated so the
  // full set stays selectable here. Ping nests under meta.latency.*, web checks
  // under meta.web.*; the `value` is the placeholder key the server fills in.
  function probeFields(prefix: string, group: string, base: string, latWord: string, lossWord: string) {
    const out: { group: string; label: string; path: string; value: string }[] = [];
    out.push(
      { group, label: `${latWord} last avg (ms)`, path: `${base}.last_ms`, value: `${prefix}latency_ms` },
      { group, label: `${latWord} last min (ms)`, path: `${base}.last_min_ms`, value: `${prefix}latency_min_ms` },
      { group, label: `${latWord} last max (ms)`, path: `${base}.last_max_ms`, value: `${prefix}latency_max_ms` },
      { group, label: `${lossWord} last (%)`, path: `${base}.last_loss_pct`, value: `${prefix}loss_pct` },
    );
    for (const w of ["1h", "3h", "6h", "24h"]) {
      out.push(
        { group, label: `${latWord} ${w} avg (ms)`, path: `${base}.avg_ms_${w}`, value: `${prefix}latency_${w}` },
        { group, label: `${latWord} ${w} min (ms)`, path: `${base}.min_ms_${w}`, value: `${prefix}latency_min_${w}` },
        { group, label: `${latWord} ${w} max (ms)`, path: `${base}.max_ms_${w}`, value: `${prefix}latency_max_${w}` },
        { group, label: `${lossWord} ${w} (%)`, path: `${base}.loss_pct_${w}`, value: `${prefix}loss_${w}` },
      );
    }
    return out;
  }
  CATALOG.push(
    ...probeFields("", "latency", "meta.latency", "latency", "loss"),
    ...probeFields("http_", "web", "meta.web", "response", "fail"),
  );
  const GROUPS = ["Top level", "signal", "meta", "latency", "web"];
  const RECOMMENDED = ["iccid", "status", "signal.rssi_dbm", "signal.rsrp_dbm", "signal.sinr_db",
    "signal.band", "meta.imei", "meta.fw", "meta.ip", "meta.sampled_at"];

  function selected(path: string) { return fields.some((f) => f.path === path); }
  function toggle(item: { path: string; value: string }) {
    fields = selected(item.path)
      ? fields.filter((f) => f.path !== item.path)
      : [...fields, { path: item.path, value: item.value, kind: "placeholder" }];
  }
  function removeField(path: string) { fields = fields.filter((f) => f.path !== path); }
  function addCustom() {
    if (!custom.path.trim() || !custom.value.trim()) return;
    fields = [...fields, { ...custom, path: custom.path.trim() }];
    custom = { path: "", value: "", kind: "static" };
  }
  function selectRecommended() {
    fields = CATALOG.filter((c) => RECOMMENDED.includes(c.path))
      .map((c) => ({ path: c.path, value: c.value, kind: "placeholder" }));
  }
  function isCustom(f: { path: string }) { return !CATALOG.some((c) => c.path === f.path); }

  // Live preview mirroring the server's render_body_fields (omit unknowns, keep types).
  function buildPreview(flds: typeof fields, ph: Record<string, any>) {
    const out: any = {};
    for (const f of flds) {
      let v: any;
      if (f.kind === "placeholder") {
        v = ph[f.value];
        if (v === null || v === undefined) continue;
      } else v = f.value;
      const parts = f.path.split(".");
      let node = out;
      for (const p of parts.slice(0, -1)) node = node[p] ??= {};
      node[parts[parts.length - 1]] = v;
    }
    return out;
  }

  $: selectedPaths = new Set(fields.map((f) => f.path));
  $: preview = JSON.stringify(buildPreview(fields, phValues), null, 2);

  async function load() {
    const c = await api.monitorConfig();
    enabled = !!c.enabled;
    send_when_degraded = c.send_when_degraded ?? true;
    fields = (c.body_fields ?? []).map((f: any) => ({ ...f }));
    rawBody = c.body ?? "";
    useRawBody = !fields.length && !!rawBody;
    destinations = (c.destinations ?? []).map((d: any) => ({
      name: d.name ?? "", enabled: d.enabled ?? true, egress: d.egress ?? "wlan",
      method: d.method ?? "POST", url: d.url ?? "",
      headers: Object.entries(d.headers ?? {}).map(([key, value]) => ({ key, value: String(value) })),
      expectStatus: (d.expect_status ?? [200, 204]).join(", "),
      interval_seconds: d.interval_seconds ?? 300,
      timeout_seconds: d.timeout_seconds ?? 15,
      schedule: { ...newSchedule(), ...(d.schedule ?? {}) },
      showSched: false,
    }));
    lastSaved = JSON.stringify(buildConfig());  // baseline so auto-save won't re-save on load
    ready = true;
  }

  async function loadPlaceholders() { phValues = await api.placeholders(); }

  async function loadHistory() {
    const data = await api.monitorHistory(PAGE_SIZE, page * PAGE_SIZE);
    history = data.results; total = data.total;
  }
  function goPage(p: number) {
    page = Math.max(0, Math.min(p, Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)));
    loadHistory();
  }
  $: pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $: rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  $: rangeEnd = Math.min(total, (page + 1) * PAGE_SIZE);

  function buildConfig() {
    const cfg: any = { enabled, send_when_degraded };
    if (useRawBody) cfg.body = rawBody;
    else cfg.body_fields = fields;
    cfg.destinations = destinations
      .filter((d) => d.url.trim())  // skip half-typed rows (URL is required)
      .map((d) => {
        const hdrs: Record<string, string> = {};
        for (const h of d.headers) if (h.key.trim()) hdrs[h.key.trim()] = h.value;
        const expect = d.expectStatus.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
        const s = d.schedule;
        return {
          name: d.name, enabled: d.enabled, egress: d.egress, method: d.method,
          url: d.url.trim(), headers: hdrs,
          timeout_seconds: Number(d.timeout_seconds),
          expect_status: expect.length ? expect : [200, 204],
          interval_seconds: Number(d.interval_seconds),
          schedule: {
            enabled: s.enabled, timezone: s.timezone, days: s.days,
            start: s.start, end: s.end, override: s.override,
          },
        };
      });
    return cfg;
  }

  // ── auto-save (debounced) ───────────────────────────────────────────────────
  let ready = false;
  let lastSaved = "";
  let saveState: "idle" | "saving" | "saved" | "error" = "idle";
  let saveTimer: ReturnType<typeof setTimeout>;

  function scheduleSave() {
    if (!ready) return;
    if (JSON.stringify(buildConfig()) === lastSaved) {
      clearTimeout(saveTimer);
      return;
    }
    saveState = "saving";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(doSave, 700);
  }
  async function doSave() {
    const cur = JSON.stringify(buildConfig());
    const ok = await api.saveMonitorConfig(buildConfig());
    if (ok) { lastSaved = cur; saveState = "saved"; }
    else { saveState = "error"; }
  }
  // Binding to destinations[i].* invalidates `destinations`, so nested edits
  // re-run this and persist.
  $: scheduleSave(enabled, send_when_degraded, useRawBody, rawBody, fields, destinations);

  async function sendNow() {
    if (await api.cmd("monitor-now")) { toast("heartbeats sent", "ok"); setTimeout(loadHistory, 1500); }
  }

  onMount(() => {
    load(); loadPlaceholders(); loadHistory();
    const t = setInterval(() => { if (page === 0) loadHistory(); }, 5000);
    const p = setInterval(loadPlaceholders, 5000);
    return () => { clearInterval(t); clearInterval(p); };
  });
</script>

<div class="row">
  <h1>Monitoring</h1>
  {#if $status}
    <span class="badge {$status.monitor_active ? 'green' : 'amber'}" title="Whether a heartbeat would fire right now">
      {$status.monitor_active ? "sending now" : "not sending"}
    </span>
  {/if}
  <span class="save-status {saveState}">
    {#if saveState === "saving"}● saving…{:else if saveState === "saved"}✓ saved{:else if saveState === "error"}✕ save failed{/if}
  </span>
  <button class="ui-btn ui-btn-sm" on:click={sendNow}>Send heartbeat now</button>
</div>
<p class="muted" style="margin-top:-4px">Changes save automatically.</p>
<p class="muted">One shared payload is delivered to each destination below over its own interface, on its own
  interval/schedule — e.g. a LAN endpoint over Wi-Fi and a public endpoint over cellular, since neither
  path can reach the other. <code>{'{'}status{'}'}</code> reports cellular health regardless of the path used.
  A profile may override this if it defines its own enabled monitor.</p>

<section class="ui-card">
  <div class="row">
    <label><input type="checkbox" bind:checked={enabled} /> Enabled</label>
    <label><input type="checkbox" bind:checked={send_when_degraded} /> keep sending while degraded</label>
  </div>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Destinations</h2>
    <button class="ui-btn ui-btn-sm" on:click={addDest}><i class="ri-add-line"></i> Add destination</button>
  </div>
  {#each destinations as dest, i (i)}
    <div class="dest" class:off={!dest.enabled}>
      <div class="row">
        <label class="toggle" title="enable this destination"><input type="checkbox" bind:checked={destinations[i].enabled} /></label>
        <input class="ui-input" style="max-width:150px" placeholder="name (optional)" bind:value={destinations[i].name} />
        <select class="ui-select" style="width:auto" title="interface" bind:value={destinations[i].egress}>
          <option value="wlan">Wi-Fi</option>
          <option value="cellular">Cellular</option>
          <option value="auto">Any</option>
        </select>
        <select class="ui-select" style="width:auto" bind:value={destinations[i].method}>
          {#each ["POST", "GET", "PUT", "PATCH", "HEAD"] as m}<option>{m}</option>{/each}
        </select>
        <input class="ui-input" style="flex:1;min-width:220px" placeholder="https://endpoint/ingest" bind:value={destinations[i].url} />
        <button class="ui-btn ui-btn-sm ui-btn-danger" title="remove destination" on:click={() => removeDest(i)}>
          <i class="ri-delete-bin-line"></i>
        </button>
      </div>
      <div class="row" style="margin-top:6px">
        <label class="muted">interval <input class="ui-input" style="width:80px;display:inline-block" type="number" min="10" bind:value={destinations[i].interval_seconds} /> s</label>
        <label class="muted">timeout <input class="ui-input" style="width:64px;display:inline-block" type="number" bind:value={destinations[i].timeout_seconds} /> s</label>
        <label class="muted">expect <input class="ui-input" style="width:96px;display:inline-block" bind:value={destinations[i].expectStatus} /></label>
        <span style="flex:1"></span>
        <button class="ui-btn ui-btn-sm" class:on={destinations[i].showSched}
          on:click={() => (destinations[i].showSched = !destinations[i].showSched)}>
          <i class="ri-time-line"></i> {dest.schedule.enabled ? "window" : "24/7"}
        </button>
      </div>

      <div class="hdrs">
        {#each dest.headers as h, hi}
          <div class="row" style="margin-bottom:4px">
            <input class="ui-input" style="max-width:180px" placeholder="Header" bind:value={destinations[i].headers[hi].key} />
            <input class="ui-input" style="flex:1" placeholder="Value" bind:value={destinations[i].headers[hi].value} />
            <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => removeHeader(i, hi)}>×</button>
          </div>
        {/each}
        <button class="ui-btn ui-btn-sm" on:click={() => addHeader(i)}>+ header</button>
      </div>

      {#if destinations[i].showSched}
        <div class="schedwrap">
          <label class="muted"><input type="checkbox" bind:checked={destinations[i].schedule.enabled} /> Limit to a weekly window</label>
          <fieldset class="sched" disabled={!destinations[i].schedule.enabled}>
            <div class="row">
              <span class="muted">Days</span>
              {#each DAY_LABELS as d, di}
                <button type="button" class="chip" class:on={dest.schedule.days.includes(di)} on:click={() => toggleDay(i, di)}>{d}</button>
              {/each}
            </div>
            <div class="row" style="margin-top:8px">
              <label class="muted">from <input class="ui-input" style="width:120px;display:inline-block" type="time" bind:value={destinations[i].schedule.start} /></label>
              <label class="muted">to <input class="ui-input" style="width:120px;display:inline-block" type="time" bind:value={destinations[i].schedule.end} /></label>
              <label class="muted">tz
                <select class="ui-select" style="width:auto;display:inline-block" bind:value={destinations[i].schedule.timezone}>
                  {#each TIMEZONES as tz}<option>{tz}</option>{/each}
                  {#if !TIMEZONES.includes(dest.schedule.timezone)}<option>{dest.schedule.timezone}</option>{/if}
                </select>
              </label>
            </div>
          </fieldset>
          <div class="row" style="margin-top:8px">
            <span class="muted">Override</span>
            {#each [["auto", "Follow"], ["on", "Force on"], ["off", "Force off"]] as [val, lbl]}
              <label><input type="radio" bind:group={destinations[i].schedule.override} value={val} /> {lbl}</label>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {:else}
    <p class="muted">No destinations yet. Add one to start sending heartbeats.</p>
  {/each}
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Payload <span class="muted" style="font-weight:400">(shared by all destinations)</span></h2>
    <label class="muted"><input type="checkbox" bind:checked={useRawBody} /> raw template instead</label>
  </div>

  {#if useRawBody}
    <textarea class="ui-textarea" rows="6" bind:value={rawBody}
      placeholder={'{"iccid":"{iccid}","status":"{status}"}'}></textarea>
    <p class="muted">Tokens in <code>{'{'}braces{'}'}</code> are substituted. Prefer the field
      builder — it always produces valid JSON and correct number/string types.</p>
  {:else}
    <div class="row">
      <button class="ui-btn ui-btn-sm" on:click={selectRecommended}>Use recommended set</button>
      <button class="ui-btn ui-btn-sm" on:click={() => (fields = [])}>Clear</button>
      <span class="muted">Click fields to include them. Unknown values are dropped automatically.</span>
    </div>
    {#each GROUPS as g}
      <h3 style="font-size:var(--fs-sm);margin:12px 0 4px;color:var(--color-text-muted)">{g === "Top level" ? "Top level" : g + ".*"}</h3>
      <div class="chips">
        {#each CATALOG.filter((c) => c.group === g) as item}
          <button class="chip" class:on={selectedPaths.has(item.path)} on:click={() => toggle(item)}
                  title={"= {" + item.value + "} → " + (phValues[item.value] ?? "—")}>
            {selectedPaths.has(item.path) ? "✓ " : "+ "}{item.label}
          </button>
        {/each}
      </div>
    {/each}

    <h3 style="font-size:var(--fs-sm);margin:12px 0 4px;color:var(--color-text-muted)">Custom field</h3>
    <div class="row">
      <input class="ui-input" style="max-width:200px" placeholder="path e.g. meta.tags" bind:value={custom.path} />
      <select class="ui-select" style="width:auto" bind:value={custom.kind}>
        <option value="static">static value</option>
        <option value="placeholder">placeholder</option>
      </select>
      <input class="ui-input" style="flex:1" placeholder={custom.kind === "static" ? "literal value" : "placeholder name e.g. rsrp"} bind:value={custom.value} />
      <button class="ui-btn ui-btn-sm" on:click={addCustom}>+ add</button>
    </div>
    {#if fields.some(isCustom)}
      <div style="margin-top:6px">
        {#each fields.filter(isCustom) as f}
          <span class="chip on">{f.path} = {f.kind === "static" ? f.value : "{" + f.value + "}"}
            <button class="chip-x" on:click={() => removeField(f.path)}>×</button></span>
        {/each}
      </div>
    {/if}

    <details class="preview">
      <summary>Live preview (what would send now)</summary>
      <div class="code-block" style="margin-top:6px">{preview}</div>
    </details>
  {/if}
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Recent heartbeats</h2>
    {#if total > 0}
      <span class="muted">{rangeStart}–{rangeEnd} of {total}</span>
      <button class="ui-btn ui-btn-sm" disabled={page === 0} on:click={() => goPage(page - 1)}>‹ newer</button>
      <button class="ui-btn ui-btn-sm" disabled={page >= pages - 1} on:click={() => goPage(page + 1)}>older ›</button>
    {/if}
  </div>
  <table>
    <thead><tr><th>Time</th><th>Result</th><th>Iface</th><th>Status</th><th>Latency</th><th>URL</th><th>Error</th></tr></thead>
    <tbody>
      {#each history as r}
        <tr>
          <td class="nowrap">{ts(r.ts)}</td>
          <td><span class="badge {r.ok ? 'green' : 'red'}">{r.ok ? "ok" : "FAIL"}</span></td>
          <td class="mono">{r.interface ?? "—"}</td>
          <td class="mono">{r.status_code ?? "—"}</td>
          <td class="mono">{r.latency_ms != null ? Math.round(r.latency_ms) + " ms" : "—"}</td>
          <td class="break">{r.url}</td>
          <td class="break muted">{r.error ?? ""}</td>
        </tr>
      {:else}
        <tr><td colspan="7" class="muted">No heartbeats yet.</td></tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  fieldset.sched { border: 0; padding: 0; margin: 0; min-width: 0; }
  fieldset.sched:disabled { opacity: 0.45; }

  .dest {
    border: 1px solid var(--color-border, #333); border-radius: 8px; padding: 12px;
    margin-bottom: 10px; background: var(--color-surface-2, rgba(127,127,127,.05));
  }
  .dest.off { opacity: 0.6; }
  .dest .toggle { display: inline-flex; align-items: center; }
  .hdrs { margin-top: 8px; }
  .schedwrap {
    margin-top: 10px; padding-top: 10px;
    border-top: 1px solid var(--color-border, #2a2a2a);
    display: flex; flex-direction: column; gap: 8px;
  }

  .save-status { font-size: var(--fs-sm, 13px); min-width: 84px; }
  .save-status.saving { color: var(--color-text-muted); }
  .save-status.saved { color: var(--status-green); }
  .save-status.error { color: var(--status-red); }

  details.preview > summary {
    cursor: pointer; user-select: none; list-style: none;
    font-size: var(--fs-sm, 13px); color: var(--color-text-muted);
    margin: 12px 0 0; display: inline-flex; align-items: center; gap: 6px;
  }
  details.preview > summary::-webkit-details-marker { display: none; }
  details.preview > summary::before { content: "▸"; display: inline-block; transition: transform .12s ease; }
  details.preview[open] > summary::before { transform: rotate(90deg); }
</style>
