<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { ts, dur, bytes } from "../lib/format";

  // ── window + filters ────────────────────────────────────────────────────
  const RANGES = [
    { id: "1h", label: "1 h", seconds: 3600 },
    { id: "6h", label: "6 h", seconds: 6 * 3600 },
    { id: "24h", label: "24 h", seconds: 86400 },
    { id: "7d", label: "7 d", seconds: 7 * 86400 },
    { id: "30d", label: "30 d", seconds: 30 * 86400 },
    { id: "all", label: "all", seconds: null as number | null },
  ];
  let range = "24h";
  let ipFilter = "";
  let portFilter = "";
  let proto = "";
  let direction = "";
  let liveOnly = false;

  let flows: any[] = [];
  let total = 0;
  let page = 0;
  const PAGE_SIZE = 50;
  let summary: any = null;
  let loading = false;

  function windowFrom(): number | undefined {
    const r = RANGES.find((x) => x.id === range);
    return r?.seconds != null ? Date.now() / 1000 - r.seconds : undefined;
  }

  async function load() {
    loading = true;
    const from = windowFrom();
    const port = parseInt(portFilter.trim(), 10);
    try {
      const [f, s] = await Promise.all([
        api.trafficFlows({
          from,
          ip: ipFilter.trim() || undefined,
          port: Number.isInteger(port) ? port : undefined,
          proto: proto || undefined,
          direction: direction || undefined,
          active: liveOnly ? true : undefined,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        }),
        api.trafficSummary(from),
      ]);
      flows = f.flows;
      total = f.total;
      summary = s;
    } catch {
      /* keep last data */
    }
    loading = false;
  }

  // Text filters debounce; selects apply immediately. Every change resets paging.
  let ready = false;
  let debounce: ReturnType<typeof setTimeout>;
  function filtersChanged(..._args: unknown[]) {
    if (!ready) return;
    page = 0;
    clearTimeout(debounce);
    debounce = setTimeout(load, 350);
  }
  $: filtersChanged(range, ipFilter, portFilter, proto, direction, liveOnly);

  function goPage(p: number) {
    page = Math.max(0, Math.min(p, Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)));
    load();
  }
  $: pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $: rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  $: rangeEnd = Math.min(total, (page + 1) * PAGE_SIZE);

  // Device-level totals: what this device sent/received (in + out flows);
  // forwarded traffic (routed for someone else) is called out separately.
  $: totals = summary?.totals ?? {};
  $: deviceSent = (totals.out?.bytes_sent ?? 0) + (totals.in?.bytes_sent ?? 0);
  $: deviceRecv = (totals.out?.bytes_recv ?? 0) + (totals.in?.bytes_recv ?? 0);
  $: flowCount = Object.values(totals).reduce((a: number, t: any) => a + (t.flows ?? 0), 0);
  $: fwdBytes = (totals.fwd?.bytes_sent ?? 0) + (totals.fwd?.bytes_recv ?? 0);
  $: st = summary?.status ?? null;

  // ── audit settings (auto-saved) ─────────────────────────────────────────
  let cfg: any = null;
  let lastSaved = "";
  let saveState: "idle" | "saving" | "saved" | "error" = "idle";
  let saveTimer: ReturnType<typeof setTimeout>;

  async function loadConfig() {
    try {
      cfg = await api.trafficConfig();
      lastSaved = JSON.stringify(cfg);
    } catch {
      /* keep defaults */
    }
  }
  function scheduleSave(..._deps: unknown[]) {
    if (!cfg) return;
    if (JSON.stringify(cfg) === lastSaved) return;
    saveState = "saving";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      const cur = JSON.stringify(cfg);
      const ok = await api.saveTrafficConfig(cfg);
      if (ok) {
        lastSaved = cur;
        saveState = "saved";
      } else {
        saveState = "error";
      }
    }, 700);
  }
  $: scheduleSave(cfg?.enabled, cfg?.snapshot_interval_seconds,
     cfg?.retention_days, cfg?.max_flows, cfg?.include_local);

  function dirClass(d: string): string {
    if (d === "out") return "blue";
    if (d === "in") return "green";
    if (d === "fwd") return "amber";
    return "";
  }

  onMount(() => {
    loadConfig();
    load().then(() => (ready = true));
    // Only auto-reload page 0 so paging back doesn't jump under the user.
    const t = setInterval(() => { if (page === 0) load(); }, 10000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Traffic</h1>
  {#if st}
    {#if st.running}
      <span class="badge green">auditing ({st.backend})</span>
    {:else if st.error}
      <span class="badge red" title={st.error}>unavailable</span>
    {:else}
      <span class="badge">off</span>
    {/if}
    {#each st.warnings ?? [] as w}<span class="badge amber" title={w}>partial</span>{/each}
  {/if}
  <span style="flex:1"></span>
  <span class="save-status {saveState}">
    {#if saveState === "saving"}● saving…{:else if saveState === "saved"}✓ saved{:else if saveState === "error"}✕ save failed{/if}
  </span>
</div>
<p class="muted" style="margin-top:-4px">
  Every connection through this device — inbound, outbound, or forwarded, on any
  interface — recorded from the kernel connection tracker with endpoints,
  protocol, and byte counts. Flow accounting, not packet capture: payloads are
  never stored.
</p>

{#if st?.error}
  <section class="ui-card">
    <p style="margin:0"><span class="badge red">not auditing</span> {st.error}</p>
  </section>
{/if}

<div class="cards">
  <section class="ui-card stat">
    <div class="label">Sent</div>
    <div class="value">{bytes(deviceSent)}</div>
  </section>
  <section class="ui-card stat">
    <div class="label">Received</div>
    <div class="value">{bytes(deviceRecv)}</div>
  </section>
  <section class="ui-card stat">
    <div class="label">Flows</div>
    <div class="value">{flowCount}</div>
    <div class="sub muted">{summary?.distinct_remotes ?? 0} remote hosts</div>
  </section>
  <section class="ui-card stat">
    <div class="label">Live now</div>
    <div class="value">{summary?.active_flows ?? 0}</div>
    {#if fwdBytes > 0}<div class="sub muted">{bytes(fwdBytes)} forwarded</div>{/if}
  </section>
</div>

<section class="ui-card">
  <div class="row filters">
    <input class="ui-input" style="max-width:190px" placeholder="IP (exact or 10.0.0.*)"
      bind:value={ipFilter} />
    <input class="ui-input" style="max-width:110px" placeholder="port" bind:value={portFilter} />
    <select class="ui-select" style="width:auto" bind:value={proto} title="protocol">
      <option value="">any proto</option>
      <option value="tcp">tcp</option>
      <option value="udp">udp</option>
      <option value="icmp">icmp</option>
    </select>
    <select class="ui-select" style="width:auto" bind:value={direction} title="direction">
      <option value="">any direction</option>
      <option value="out">outbound</option>
      <option value="in">inbound</option>
      <option value="fwd">forwarded</option>
      <option value="local">local</option>
    </select>
    <label class="toggle"><input type="checkbox" bind:checked={liveOnly} /> <span>live only</span></label>
    <span style="flex:1"></span>
    <div class="range-tabs">
      {#each RANGES as r (r.id)}
        <button class="ui-btn ui-btn-sm" class:on={range === r.id} on:click={() => (range = r.id)}>
          {r.label}
        </button>
      {/each}
    </div>
  </div>

  <div class="row" style="margin-top:10px">
    <h2 style="flex:1">Flows</h2>
    {#if loading}<span class="muted">loading…</span>{/if}
    {#if total > 0}
      <span class="muted">{rangeStart}–{rangeEnd} of {total}</span>
      <button class="ui-btn ui-btn-sm" disabled={page === 0} on:click={() => goPage(page - 1)}>‹ newer</button>
      <button class="ui-btn ui-btn-sm" disabled={page >= pages - 1} on:click={() => goPage(page + 1)}>older ›</button>
    {/if}
  </div>

  <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Last seen</th><th>Dir</th><th>Proto</th><th>Remote</th><th>Local</th>
          <th>Sent</th><th>Recv</th><th>Pkts</th><th>Duration</th>
        </tr>
      </thead>
      <tbody>
        {#each flows as f (f.id)}
          <tr>
            <td class="nowrap">{ts(f.last_seen)}</td>
            <td>
              <span class="badge {dirClass(f.direction)}">{f.direction}</span>
              {#if f.active}<span class="badge lime" title="flow still open">live</span>{/if}
            </td>
            <td class="mono">{f.proto}</td>
            <td class="mono nowrap">{f.remote_ip}{f.remote_port != null ? `:${f.remote_port}` : ""}</td>
            <td class="mono nowrap">
              {#if f.direction === "fwd" || f.direction === "local"}
                {f.local_ip}{f.local_port != null ? `:${f.local_port}` : ""}
              {:else}
                {f.local_port != null ? `:${f.local_port}` : "—"}
              {/if}
            </td>
            <td class="nowrap">{bytes(f.bytes_sent)}</td>
            <td class="nowrap">{bytes(f.bytes_recv)}</td>
            <td class="nowrap">{f.packets_sent + f.packets_recv}</td>
            <td class="nowrap">{dur(f.last_seen - f.first_seen)}</td>
          </tr>
        {:else}
          <tr><td colspan="9" class="muted">No flows match. Traffic appears here as connections close (live ones within {cfg?.snapshot_interval_seconds ?? 30}s).</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

<div class="cards two">
  <section class="ui-card">
    <h2>Top remote hosts</h2>
    <p class="muted hint">By total volume in the selected window.</p>
    <table>
      <thead><tr><th>Remote IP</th><th>Flows</th><th>Sent</th><th>Recv</th></tr></thead>
      <tbody>
        {#each summary?.top_remotes ?? [] as r (r.remote_ip)}
          <tr>
            <td class="mono clicky" title="filter to this IP"
                on:click={() => (ipFilter = r.remote_ip)}>{r.remote_ip}</td>
            <td>{r.flows}</td>
            <td class="nowrap">{bytes(r.bytes_sent)}</td>
            <td class="nowrap">{bytes(r.bytes_recv)}</td>
          </tr>
        {:else}
          <tr><td colspan="4" class="muted">Nothing yet.</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <section class="ui-card">
    <h2>Top ports</h2>
    <p class="muted hint">Service port: ours for inbound flows, theirs for outbound.</p>
    <table>
      <thead><tr><th>Port</th><th>Proto</th><th>Flows</th><th>Sent</th><th>Recv</th></tr></thead>
      <tbody>
        {#each summary?.top_ports ?? [] as p (`${p.proto}:${p.port}`)}
          <tr>
            <td class="mono clicky" title="filter to this port"
                on:click={() => (portFilter = String(p.port))}>{p.port}</td>
            <td class="mono">{p.proto}</td>
            <td>{p.flows}</td>
            <td class="nowrap">{bytes(p.bytes_sent)}</td>
            <td class="nowrap">{bytes(p.bytes_recv)}</td>
          </tr>
        {:else}
          <tr><td colspan="5" class="muted">Nothing yet.</td></tr>
        {/each}
      </tbody>
    </table>
  </section>
</div>

{#if cfg}
  <section class="ui-card">
    <div class="row">
      <h2 style="flex:1">Audit settings</h2>
      <label class="toggle">
        <input type="checkbox" bind:checked={cfg.enabled} /> <span>Enabled</span>
      </label>
    </div>
    <div class="row" style="margin-top:8px; flex-wrap:wrap">
      <label class="field">
        <span class="muted">live snapshot every</span>
        <input class="ui-input num" type="number" min="5" max="600" bind:value={cfg.snapshot_interval_seconds} />
        <span class="muted">s</span>
      </label>
      <label class="field">
        <span class="muted">keep flows for</span>
        <input class="ui-input num" type="number" min="1" max="365" bind:value={cfg.retention_days} />
        <span class="muted">days</span>
      </label>
      <label class="field">
        <span class="muted">max stored flows</span>
        <input class="ui-input num wide" type="number" min="1000" step="1000" bind:value={cfg.max_flows} />
      </label>
      <label class="toggle" title="also record loopback / host-internal flows">
        <input type="checkbox" bind:checked={cfg.include_local} /> <span>include local chatter</span>
      </label>
    </div>
  </section>
{/if}

<style>
  .save-status { font-size: var(--fs-sm, 13px); min-width: 84px; }
  .save-status.saving { color: var(--color-text-muted); }
  .save-status.saved { color: var(--status-green); }
  .save-status.error { color: var(--status-red); }
  .hint { font-size: var(--fs-xs, 11px); margin: 2px 0 8px; }
  .toggle {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: var(--fs-sm, 13px); color: var(--color-text); white-space: nowrap;
  }
  .cards { margin-bottom: 14px; }
  .cards.two { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin-top: 14px; }
  .stat .label {
    font-size: var(--fs-xs, 11px); text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--color-text-muted);
  }
  .stat .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
  .stat .sub { font-size: var(--fs-xs, 11px); margin-top: 2px; }
  .filters { flex-wrap: wrap; }
  .range-tabs { display: inline-flex; gap: 4px; }
  .range-tabs .ui-btn.on { border-color: var(--color-accent, #7c6); color: var(--color-accent, #7c6); }
  .field { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-sm, 13px); }
  .field .num { width: 76px; }
  .field .num.wide { width: 110px; }
  .clicky { cursor: pointer; }
  .clicky:hover { text-decoration: underline; }
</style>
