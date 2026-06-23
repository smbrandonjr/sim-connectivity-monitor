<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { status } from "../lib/stores";
  import { toast } from "../lib/toast";
  import { ts } from "../lib/format";

  // top-level config
  let enabled = false;
  let interval_seconds = 300;
  let send_when_degraded = true;
  let bind_cellular = true;

  // schedule window (limit heartbeats to e.g. Mon-Fri 9-6 Eastern)
  const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
    "Pacific/Honolulu", "UTC",
  ];
  let sched = {
    enabled: false,
    timezone: "America/New_York",
    days: [0, 1, 2, 3, 4],
    start: "09:00",
    end: "18:00",
    override: "auto",
  };
  function toggleDay(d: number) {
    sched.days = sched.days.includes(d)
      ? sched.days.filter((x) => x !== d)
      : [...sched.days, d].sort((a, b) => a - b);
  }
  let method = "POST";
  let url = "";
  let timeout_seconds = 15;
  let expectStatus = "200, 204";
  let headers: { key: string; value: string }[] = [];

  // body: structured builder (default) or raw template
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
    { group: "meta", label: "interface", path: "meta.interface", value: "interface" },
    { group: "meta", label: "registration", path: "meta.registration", value: "registration" },
    { group: "meta", label: "status_message", path: "meta.status_message", value: "status_message" },
    { group: "meta", label: "last_error", path: "meta.last_error", value: "last_error" },
    { group: "meta", label: "uptime_s", path: "meta.uptime_s", value: "uptime_s" },
    { group: "meta", label: "cpu_load", path: "meta.cpu_load", value: "cpu_load" },
    { group: "meta", label: "mem_free_mb", path: "meta.mem_free_mb", value: "mem_free_mb" },
    { group: "meta", label: "temperature_c", path: "meta.temperature_c", value: "temperature_c" },
    { group: "meta", label: "sampled_at", path: "meta.sampled_at", value: "sampled_at" },
    // Cellular-path latency/loss from the latency monitor (last cycle + windows).
    // Nested under the meta payload object (meta.latency.*).
    { group: "latency", label: "latency last (ms)", path: "meta.latency.last_ms", value: "latency_ms" },
    { group: "latency", label: "loss last (%)", path: "meta.latency.last_loss_pct", value: "loss_pct" },
    { group: "latency", label: "latency 1h avg (ms)", path: "meta.latency.avg_ms_1h", value: "latency_1h" },
    { group: "latency", label: "loss 1h (%)", path: "meta.latency.loss_pct_1h", value: "loss_1h" },
    { group: "latency", label: "latency 3h avg (ms)", path: "meta.latency.avg_ms_3h", value: "latency_3h" },
    { group: "latency", label: "loss 3h (%)", path: "meta.latency.loss_pct_3h", value: "loss_3h" },
    { group: "latency", label: "latency 6h avg (ms)", path: "meta.latency.avg_ms_6h", value: "latency_6h" },
    { group: "latency", label: "loss 6h (%)", path: "meta.latency.loss_pct_6h", value: "loss_6h" },
    { group: "latency", label: "latency 24h avg (ms)", path: "meta.latency.avg_ms_24h", value: "latency_24h" },
    { group: "latency", label: "loss 24h (%)", path: "meta.latency.loss_pct_24h", value: "loss_24h" },
  ];
  const GROUPS = ["Top level", "signal", "meta", "latency"];
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

  // Live preview mirroring the server's render_body_fields (omit unknowns, keep
  // types). Passed fields/ph explicitly so Svelte re-runs it on every change.
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

  // Reactive derived state — these recompute whenever `fields`/`phValues` change.
  $: selectedPaths = new Set(fields.map((f) => f.path));
  $: preview = JSON.stringify(buildPreview(fields, phValues), null, 2);

  async function load() {
    const c = await api.monitorConfig();
    enabled = !!c.enabled;
    interval_seconds = c.interval_seconds ?? 300;
    send_when_degraded = c.send_when_degraded ?? true;
    bind_cellular = c.bind_cellular ?? true;
    if (c.schedule) sched = { ...sched, ...c.schedule };
    const r = c.request ?? {};
    method = r.method ?? "POST";
    url = r.url ?? "";
    timeout_seconds = r.timeout_seconds ?? 15;
    expectStatus = (r.expect_status ?? [200, 204]).join(", ");
    headers = Object.entries(r.headers ?? {}).map(([key, value]) => ({ key, value: String(value) }));
    fields = (r.body_fields ?? []).map((f: any) => ({ ...f }));
    rawBody = r.body ?? "";
    useRawBody = !fields.length && !!rawBody;
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
    const hdrs: Record<string, string> = {};
    for (const h of headers) if (h.key.trim()) hdrs[h.key.trim()] = h.value;
    const expect = expectStatus.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
    const cfg: any = { enabled, interval_seconds, send_when_degraded, bind_cellular, schedule: sched };
    if (url.trim()) {
      const req: any = {
        method, url: url.trim(), headers: hdrs,
        timeout_seconds, expect_status: expect.length ? expect : [200, 204],
      };
      if (useRawBody) req.body = rawBody;
      else req.body_fields = fields;
      cfg.request = req;
    }
    return cfg;
  }
  // ── auto-save ──────────────────────────────────────────────────────────────
  // Settings persist automatically (debounced) once the form has loaded; a
  // header indicator reports state. `lastSaved` guards against saving the
  // freshly-loaded config back, and against no-op saves when nothing changed.
  let ready = false;
  let lastSaved = "";
  let saveState: "idle" | "saving" | "saved" | "error" = "idle";
  let saveTimer: ReturnType<typeof setTimeout>;

  function scheduleSave() {
    if (!ready) return;
    if (JSON.stringify(buildConfig()) === lastSaved) {
      clearTimeout(saveTimer);
      return; // reverted to the saved state; nothing to do
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
  // Re-run whenever any persisted setting changes (listed so Svelte tracks them).
  $: scheduleSave(
    enabled, interval_seconds, send_when_degraded, bind_cellular, method, url,
    timeout_seconds, expectStatus, headers, useRawBody, rawBody, fields, sched,
  );

  async function sendNow() {
    if (await api.cmd("monitor-now")) { toast("heartbeat sent", "ok"); setTimeout(loadHistory, 1500); }
  }
  function addHeader() { headers = [...headers, { key: "", value: "" }]; }
  function removeHeader(i: number) { headers = headers.filter((_, idx) => idx !== i); }

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
    <span class="badge {$status.monitor_active ? 'green' : 'amber'}" title="Whether a scheduled heartbeat would fire right now">
      {$status.monitor_active ? "sending now" : "not sending"}
    </span>
  {/if}
  <span class="save-status {saveState}">
    {#if saveState === "saving"}● saving…{:else if saveState === "saved"}✓ saved{:else if saveState === "error"}✕ save failed{/if}
  </span>
  <button class="ui-btn ui-btn-sm" on:click={sendNow}>Send heartbeat now</button>
</div>
<p class="muted" style="margin-top:-4px">Changes save automatically.</p>
<p class="muted">A global heartbeat sent to your endpoint on a schedule. While connected it goes
  out the cellular interface (proving cellular egress); if cellular drops it keeps sending over
  any other route with <code>status=degraded</code>. A profile may override this if it defines
  its own enabled monitor.</p>

<section class="ui-card">
  <div class="row">
    <label><input type="checkbox" bind:checked={enabled} /> Enabled</label>
    <label class="muted">interval <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={interval_seconds} /> s</label>
    <label><input type="checkbox" bind:checked={send_when_degraded} /> keep sending while degraded</label>
    <label><input type="checkbox" bind:checked={bind_cellular} /> bind to cellular (uncheck for LAN/VPN endpoint)</label>
  </div>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Schedule</h2>
    <label><input type="checkbox" bind:checked={sched.enabled} /> Limit heartbeats to a weekly window</label>
  </div>
  <p class="muted">Scheduled probes only fire inside this window (manual “Send heartbeat now” always works).
    Leave unchecked to send around the clock whenever monitoring is enabled.</p>
  <fieldset class="sched" disabled={!sched.enabled}>
    <div class="row">
      <span class="muted">Days</span>
      {#each DAY_LABELS as d, i}
        <button type="button" class="chip" class:on={sched.days.includes(i)} on:click={() => toggleDay(i)}>{d}</button>
      {/each}
    </div>
    <div class="row" style="margin-top:10px">
      <label class="muted">from <input class="ui-input" style="width:120px;display:inline-block" type="time" bind:value={sched.start} /></label>
      <label class="muted">to <input class="ui-input" style="width:120px;display:inline-block" type="time" bind:value={sched.end} /></label>
      <label class="muted">timezone
        <select class="ui-select" style="width:auto;display:inline-block" bind:value={sched.timezone}>
          {#each TIMEZONES as tz}<option>{tz}</option>{/each}
          {#if !TIMEZONES.includes(sched.timezone)}<option>{sched.timezone}</option>{/if}
        </select>
      </label>
    </div>
    <p class="muted" style="margin-top:6px">A window ending earlier than it starts (e.g. 22:00→02:00) wraps past midnight.</p>
  </fieldset>
  <div class="row" style="margin-top:10px">
    <span class="muted">Override</span>
    {#each [["auto", "Follow schedule"], ["on", "Force on"], ["off", "Force off"]] as [val, lbl]}
      <label><input type="radio" bind:group={sched.override} value={val} /> {lbl}</label>
    {/each}
  </div>
</section>

<section class="ui-card">
  <h2>Endpoint</h2>
  <div class="row">
    <select class="ui-select" style="width:auto" bind:value={method}>
      {#each ["POST", "GET", "PUT", "PATCH", "HEAD"] as m}<option>{m}</option>{/each}
    </select>
    <input class="ui-input" style="flex:1;min-width:280px" placeholder="https://your-endpoint.example.com/ingest/monitor/TOKEN" bind:value={url} />
  </div>

  <h2 style="margin-top:14px">Headers</h2>
  {#each headers as h, i}
    <div class="row" style="margin-bottom:6px">
      <input class="ui-input" style="max-width:200px" placeholder="Header" bind:value={h.key} />
      <input class="ui-input" style="flex:1" placeholder="Value" bind:value={h.value} />
      <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => removeHeader(i)}>×</button>
    </div>
  {/each}
  <button class="ui-btn ui-btn-sm" on:click={addHeader}>+ header</button>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Payload</h2>
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

  <div class="row" style="margin-top:10px">
    <label class="muted">timeout <input class="ui-input" style="width:70px;display:inline-block" type="number" bind:value={timeout_seconds} /> s</label>
    <label class="muted">expect status <input class="ui-input" style="width:120px;display:inline-block" bind:value={expectStatus} /></label>
  </div>
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
    <thead><tr><th>Time</th><th>Result</th><th>Status</th><th>Latency</th><th>URL</th><th>Error</th></tr></thead>
    <tbody>
      {#each history as r}
        <tr>
          <td class="nowrap">{ts(r.ts)}</td>
          <td><span class="badge {r.ok ? 'green' : 'red'}">{r.ok ? "ok" : "FAIL"}</span></td>
          <td class="mono">{r.status_code ?? "—"}</td>
          <td class="mono">{r.latency_ms != null ? Math.round(r.latency_ms) + " ms" : "—"}</td>
          <td class="break">{r.url}</td>
          <td class="break muted">{r.error ?? ""}</td>
        </tr>
      {:else}
        <tr><td colspan="6" class="muted">No heartbeats yet.</td></tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  fieldset.sched { border: 0; padding: 0; margin: 0; min-width: 0; }
  fieldset.sched:disabled { opacity: 0.45; }

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
