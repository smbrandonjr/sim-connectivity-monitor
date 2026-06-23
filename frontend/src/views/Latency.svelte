<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";
  import { toast } from "../lib/toast";
  import LatencyChart from "../lib/LatencyChart.svelte";

  // ── settings (UI-managed, hot-reloaded on the next probe cycle) ──────────
  let showSettings = false;
  let saving = false;
  let cfg = {
    enabled: false,
    interval_seconds: 60,
    packet_count: 5,
    timeout_seconds: 2,
    raw_retention_days: 7,
    rollup_retention_days: 30,
    interface_colors: {} as Record<string, string>,
  };
  let targetsText = "1.1.1.1\n1.0.0.1\n8.8.8.8\n8.8.4.4\n9.9.9.9";
  let interfacesText = ""; // empty = auto-enumerate every up interface
  let excludeText = "";

  const splitList = (s: string) =>
    s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

  async function loadConfig() {
    try {
      const c = await api.latencyConfig();
      cfg = {
        enabled: !!c.enabled,
        interval_seconds: c.interval_seconds ?? 60,
        packet_count: c.packet_count ?? 5,
        timeout_seconds: c.timeout_seconds ?? 2,
        raw_retention_days: c.raw_retention_days ?? 7,
        rollup_retention_days: c.rollup_retention_days ?? 30,
        interface_colors: { ...(c.interface_colors ?? {}) },
      };
      targetsText = (c.targets ?? []).join("\n");
      interfacesText = (c.interfaces ?? []).join(", ");
      excludeText = (c.exclude_interfaces ?? []).join(", ");
    } catch {
      /* keep defaults */
    }
  }

  function buildConfig() {
    return {
      ...cfg,
      interval_seconds: Number(cfg.interval_seconds),
      packet_count: Number(cfg.packet_count),
      timeout_seconds: Number(cfg.timeout_seconds),
      raw_retention_days: Number(cfg.raw_retention_days),
      rollup_retention_days: Number(cfg.rollup_retention_days),
      targets: splitList(targetsText),
      interfaces: splitList(interfacesText),
      exclude_interfaces: splitList(excludeText),
    };
  }

  async function saveConfig() {
    saving = true;
    const ok = await api.saveLatencyConfig(buildConfig());
    saving = false;
    if (ok) {
      toast("Latency settings saved — applies on the next cycle", "ok");
      load();
    }
  }

  const PRESETS = [
    { label: "1h", s: 3600 },
    { label: "6h", s: 21600 },
    { label: "24h", s: 86400 },
    { label: "7d", s: 604800 },
    { label: "30d", s: 2592000 },
  ];
  const PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ef4444", "#14b8a6", "#ec4899"];

  let presetS: number | null = 86400;
  let fromStr = "";
  let toStr = "";
  let ifaceFilter = ""; // "" = all interfaces
  let data: any = null;
  let loading = false;
  let timer: any;

  const toEpoch = (s: string) => (s ? new Date(s).getTime() / 1000 : null);
  function toLocalInput(epoch: number) {
    const d = new Date(epoch * 1000);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // Zoom: an explicit epoch window (set by drag-zoom) overrides the preset /
  // custom range. A stack lets "zoom out" step back through prior windows.
  let zoomWindow: { from: number; to: number } | null = null;
  let zoomStack: { from: number; to: number }[] = [];

  async function load() {
    loading = true;
    try {
      let from: number | null, to: number | null;
      if (zoomWindow) {
        from = zoomWindow.from; to = zoomWindow.to;
      } else if (presetS != null) {
        to = Date.now() / 1000;
        from = to - presetS;
      } else {
        from = toEpoch(fromStr);
        to = toEpoch(toStr);
        if (from == null || to == null) { loading = false; return; }
      }
      data = await api.latency(from, to, ifaceFilter || undefined);
    } catch {
      /* keep last */
    }
    loading = false;
  }

  function clearZoom() { zoomWindow = null; zoomStack = []; }
  function pickPreset(s: number) { clearZoom(); presetS = s; load(); }
  function useCustom() {
    if (data) { fromStr = toLocalInput(data.window_start); toStr = toLocalInput(data.window_end); }
    clearZoom();
    presetS = null;
  }

  function onZoom(e: CustomEvent<{ from: number; to: number }>) {
    if (data) zoomStack = [...zoomStack, { from: data.window_start, to: data.window_end }];
    zoomWindow = { from: e.detail.from, to: e.detail.to };
    load();
  }
  function zoomOut() {
    if (zoomStack.length) {
      zoomWindow = zoomStack[zoomStack.length - 1];
      zoomStack = zoomStack.slice(0, -1);
    } else if (zoomWindow) {
      // widen 2× around the centre so you can always keep zooming out
      const c = (zoomWindow.from + zoomWindow.to) / 2;
      const half = zoomWindow.to - zoomWindow.from;
      zoomWindow = { from: c - half, to: c + half };
    } else { return; }
    load();
  }
  function resetZoom() { clearZoom(); load(); }

  onMount(() => {
    load();
    loadConfig();
    // Live-refresh only while showing a "now"-anchored preset (not zoomed/custom).
    timer = setInterval(() => { if (presetS != null && !zoomWindow) load(); }, 15000);
  });
  onDestroy(() => clearInterval(timer));

  // Aggregate the per-(interface,target) series into one point-per-cycle per
  // interface: all targets in a cycle share a timestamp, so we average their
  // RTT and loss to get a clean per-interface line.
  function perInterface(metric: "rtt" | "loss") {
    const out: Record<string, { ts: number; value: number | null }[]> = {};
    if (!data?.series) return out;
    const acc: Record<string, Record<number, { sum: number; n: number }>> = {};
    for (const [key, points] of Object.entries<any>(data.series)) {
      const iface = key.split("|")[0];
      acc[iface] ||= {};
      for (const p of points) {
        const v = metric === "rtt" ? p.rtt_avg_ms : p.loss_pct;
        const slot = (acc[iface][p.ts] ||= { sum: 0, n: 0 });
        if (v != null) { slot.sum += v; slot.n += 1; }
        else if (metric === "rtt") { /* leave as gap */ }
        else { slot.sum += 100; slot.n += 1; } // null rtt path still counts loss=100
      }
    }
    for (const iface of Object.keys(acc)) {
      out[iface] = Object.entries(acc[iface])
        .map(([t, s]) => ({ ts: Number(t), value: s.n ? s.sum / s.n : null }))
        .sort((a, b) => a.ts - b.ts);
    }
    return out;
  }

  $: interfaces = (data?.interfaces ?? []) as string[];
  // Deterministic default colour from the interface name, so the same interface
  // looks identical on every device without any config. A saved override (from
  // settings / config.yaml) takes precedence.
  function defaultColor(iface: string) {
    let h = 0;
    for (let i = 0; i < iface.length; i++) h = (h * 31 + iface.charCodeAt(i)) >>> 0;
    return PALETTE[h % PALETTE.length];
  }
  // Reactive so editing a colour in settings recolours the charts live.
  $: colorOf = (iface: string) => cfg.interface_colors?.[iface] || defaultColor(iface);
  $: cellular = data?.cellular_interface ?? null;

  function setColor(iface: string, hex: string) {
    cfg = { ...cfg, interface_colors: { ...cfg.interface_colors, [iface]: hex } };
  }
  function onColorInput(iface: string, e: Event) {
    setColor(iface, (e.currentTarget as HTMLInputElement).value);
  }
  function resetColor(iface: string) {
    const m = { ...cfg.interface_colors };
    delete m[iface];
    cfg = { ...cfg, interface_colors: m };
  }
  // Interfaces we can colour: those seen in the data plus any already pinned.
  $: colorableIfaces = Array.from(
    new Set([...(data?.interfaces ?? []), ...Object.keys(cfg.interface_colors ?? {})]),
  ).sort();
  $: rttSeries = data ? perInterface("rtt") : {};
  $: lossSeries = data ? perInterface("loss") : {};
  $: headline = data?.headline ?? {};

  // Per (interface, target) digest over the window, computed from the series.
  function buildSummary(d: any) {
    if (!d?.series) return [];
    const rows: any[] = [];
    for (const [key, pts] of Object.entries<any>(d.series)) {
      const [iface, target] = key.split("|");
      const rtts = pts.map((p: any) => p.rtt_avg_ms).filter((v: any) => v != null);
      const mins = pts.map((p: any) => p.rtt_min_ms).filter((v: any) => v != null);
      const maxs = pts.map((p: any) => p.rtt_max_ms).filter((v: any) => v != null);
      const losses = pts.map((p: any) => p.loss_pct).filter((v: any) => v != null);
      rows.push({
        iface, target, samples: pts.length,
        avg: rtts.length ? rtts.reduce((a: number, b: number) => a + b, 0) / rtts.length : null,
        min: mins.length ? Math.min(...mins) : null,
        max: maxs.length ? Math.max(...maxs) : null,
        loss: losses.length ? losses.reduce((a: number, b: number) => a + b, 0) / losses.length : null,
      });
    }
    rows.sort((a, b) => (a.iface === b.iface ? a.target.localeCompare(b.target) : a.iface.localeCompare(b.iface)));
    return rows;
  }
  $: summaryRows = buildSummary(data);

  function exportCsv() {
    if (!data) return;
    const url = api.latencyCsvUrl(data.window_start, data.window_end, ifaceFilter || undefined);
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function pct(v: number | null | undefined) { return v == null ? "—" : v.toFixed(v >= 99.95 ? 1 : 2) + "%"; }
  function ms(v: number | null | undefined) { return v == null ? "—" : Math.round(v) + " ms"; }
  const SOURCE_LABEL: Record<string, string> = { raw: "per-probe", hour: "hourly avg", day: "daily avg" };
</script>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Latency &amp; packet loss</h2>
    <div class="seg">
      {#each PRESETS as p}
        <button class="chip" class:on={presetS === p.s} on:click={() => pickPreset(p.s)}>{p.label}</button>
      {/each}
      <button class="chip" class:on={presetS === null} on:click={useCustom}>custom</button>
      <button class="ui-btn ui-btn-sm" class:on={showSettings} title="Monitor settings"
        on:click={() => { showSettings = !showSettings; }}>
        <i class="ri-settings-3-line"></i>
      </button>
    </div>
  </div>

  {#if showSettings}
    <div class="settings">
      <label class="toggle"><input type="checkbox" bind:checked={cfg.enabled} /> <span>Enabled</span></label>

      <div class="nums">
        <label>interval (s)
          <input class="ui-input" type="number" min="10" bind:value={cfg.interval_seconds} /></label>
        <label>pings / target
          <input class="ui-input" type="number" min="1" max="20" bind:value={cfg.packet_count} /></label>
        <label>timeout (s)
          <input class="ui-input" type="number" min="1" max="30" bind:value={cfg.timeout_seconds} /></label>
        <label>keep raw (days)
          <input class="ui-input" type="number" min="1" max="90" bind:value={cfg.raw_retention_days} /></label>
        <label>keep rollups (days)
          <input class="ui-input" type="number" min="1" max="400" bind:value={cfg.rollup_retention_days} /></label>
      </div>

      <div class="two">
        <label>targets <span class="hint">(one per line, or comma/space separated)</span>
          <textarea class="ui-input ta" rows="5" bind:value={targetsText}
            placeholder="1.1.1.1&#10;8.8.8.8"></textarea></label>
        <div class="ifaces">
          <label>interfaces <span class="hint">(empty = auto: every up interface)</span>
            <input class="ui-input" bind:value={interfacesText} placeholder="auto" /></label>
          <label>exclude interfaces
            <input class="ui-input" bind:value={excludeText} placeholder="e.g. docker0" /></label>
        </div>
      </div>

      {#if colorableIfaces.length}
        <div class="colors">
          <span class="lbl">interface colors <span class="hint">(consistent across devices)</span></span>
          <div class="swatches">
            {#each colorableIfaces as iface}
              <div class="cpick" title={cfg.interface_colors?.[iface] ? "custom" : "auto (from name)"}>
                <input type="color" value={colorOf(iface)}
                  on:input={(e) => onColorInput(iface, e)} />
                <span class="mono">{iface}</span>
                {#if cfg.interface_colors?.[iface]}
                  <button class="x" title="reset to auto" on:click={() => resetColor(iface)}>×</button>
                {/if}
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <div class="actions">
        <span class="muted hint">Changes apply on the next probe cycle — no restart needed.</span>
        <button class="ui-btn ui-btn-primary ui-btn-sm" on:click={saveConfig} disabled={saving}>
          {saving ? "Saving…" : "Save settings"}
        </button>
      </div>
    </div>
  {/if}

  {#if presetS === null}
    <div class="row" style="margin:4px 0 10px">
      <label class="muted">from <input class="ui-input" type="datetime-local" bind:value={fromStr} /></label>
      <label class="muted">to <input class="ui-input" type="datetime-local" bind:value={toStr} /></label>
      <button class="ui-btn ui-btn-sm" on:click={load}>Apply</button>
    </div>
  {/if}

  {#if data}
    {#if interfaces.length}
      <div class="legend">
        {#each interfaces as iface}
          <button
            class="lg"
            class:dim={ifaceFilter && ifaceFilter !== iface}
            on:click={() => { ifaceFilter = ifaceFilter === iface ? "" : iface; load(); }}
            title="click to isolate this interface"
          >
            <span class="sw" style="background:{colorOf(iface)}"></span>
            <span class="mono">{iface}{iface === cellular ? " ·cell" : ""}</span>
            <span class="muted">{ms(headline[iface]?.rtt_avg_ms)} / {pct(headline[iface]?.loss_pct)} loss</span>
          </button>
        {/each}
      </div>

      <div class="row charthdr">
        <h3 class="ttl" style="flex:1;margin:0">Latency <span class="muted">({SOURCE_LABEL[data.source] ?? data.source})</span></h3>
        <span class="muted hint">drag to zoom</span>
        {#if zoomWindow}
          <button class="chip" on:click={zoomOut} title="Zoom out one step">
            <i class="ri-zoom-out-line"></i> out
          </button>
          <button class="chip" on:click={resetZoom} title="Back to the selected range">reset</button>
        {/if}
      </div>
      <LatencyChart series={rttSeries} {colorOf} {cellular}
        windowStart={data.window_start} windowEnd={data.window_end} unit="ms" valueFloor={0}
        on:zoom={onZoom} />

      <h3 class="ttl">Packet loss</h3>
      <LatencyChart series={lossSeries} {colorOf} {cellular}
        windowStart={data.window_start} windowEnd={data.window_end} unit="%" valueFloor={0} valueCeil={100}
        on:zoom={onZoom} />

      <div class="row" style="margin-top:18px">
        <h3 class="ttl" style="flex:1;margin:0">Summary <span class="muted">({SOURCE_LABEL[data.source] ?? data.source} over window)</span></h3>
        <button class="ui-btn ui-btn-sm" on:click={exportCsv} title="Download this window as CSV">
          <i class="ri-download-2-line"></i> Export CSV
        </button>
      </div>
      <div class="tablewrap">
        <table class="summary">
          <thead>
            <tr>
              <th>Interface</th><th>Target</th><th class="num">Samples</th>
              <th class="num">Avg</th><th class="num">Min</th><th class="num">Max</th><th class="num">Loss</th>
            </tr>
          </thead>
          <tbody>
            {#each summaryRows as r}
              <tr>
                <td><span class="sw" style="background:{colorOf(r.iface)}"></span><span class="mono">{r.iface}{r.iface === cellular ? " ·cell" : ""}</span></td>
                <td class="mono">{r.target}</td>
                <td class="num">{r.samples}</td>
                <td class="num">{ms(r.avg)}</td>
                <td class="num">{ms(r.min)}</td>
                <td class="num">{ms(r.max)}</td>
                <td class="num" class:bad={r.loss != null && r.loss >= 1}>{pct(r.loss)}</td>
              </tr>
            {:else}
              <tr><td colspan="7" class="muted">no rows in this window</td></tr>
            {/each}
          </tbody>
        </table>
      </div>

      <p class="muted foot">
        {ts(data.window_start)} → {ts(data.window_end)} · {loading ? "refreshing…" : zoomWindow ? "zoomed" : "live"}
      </p>
    {:else}
      <p class="muted">No latency samples in this window.
        {#if !cfg.enabled}
          Open <button class="linkish" on:click={() => (showSettings = true)}>settings</button>
          and enable the monitor, then give it a few cycles.
        {:else}
          The monitor is enabled — give it a few cycles to accumulate data.
        {/if}</p>
    {/if}
  {:else}
    <p class="muted">{loading ? "loading…" : "no data"}</p>
  {/if}
</section>

<style>
  .seg { display: flex; gap: 4px; flex-wrap: wrap; }
  .ttl { font-size: var(--fs-sm); margin: 16px 0 4px; color: var(--color-text-muted); }
  .charthdr { align-items: center; gap: 10px; margin: 16px 0 4px; }
  .charthdr .hint { font-size: var(--fs-xs, 11px); opacity: .65; }
  .legend { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
  .lg {
    display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    background: var(--color-surface-2, rgba(127,127,127,.08)); border: 1px solid var(--color-border, #333);
    border-radius: 6px; padding: 3px 8px; font-size: var(--fs-xs, 11px); color: inherit;
  }
  .lg.dim { opacity: 0.4; }
  .sw { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .foot { font-size: var(--fs-xs, 11px); margin: 6px 0 0; }

  .settings {
    border: 1px solid var(--color-border, #333); border-radius: 8px;
    padding: 14px; margin: 10px 0 4px; display: flex; flex-direction: column; gap: 14px;
    background: var(--color-surface-2, rgba(127,127,127,.05));
  }
  .settings label {
    display: flex; flex-direction: column; gap: 4px;
    font-size: var(--fs-xs, 11px); color: var(--color-text-muted);
  }
  .settings .toggle {
    flex-direction: row; align-items: center; gap: 8px;
    width: max-content; font-size: var(--fs-sm, 13px); color: var(--color-text);
  }
  .settings .nums { display: flex; flex-wrap: wrap; gap: 14px; }
  .settings .nums label { width: 120px; }
  .settings .nums .ui-input { width: 100%; }
  .settings .two { display: flex; flex-wrap: wrap; gap: 18px; }
  .settings .two > label { flex: 1; min-width: 240px; }
  .settings .ifaces { flex: 1; min-width: 240px; display: flex; flex-direction: column; gap: 12px; }
  .settings .ta { font-family: var(--font-mono, monospace); resize: vertical; width: 100%; }
  .settings .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .settings .hint { font-size: var(--fs-xs, 11px); opacity: .7; font-weight: 400; }

  .settings .colors { display: flex; flex-direction: column; gap: 6px; }
  .settings .colors .lbl { font-size: var(--fs-xs, 11px); color: var(--color-text-muted); }
  .settings .swatches { display: flex; flex-wrap: wrap; gap: 10px; }
  .settings .cpick {
    display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-xs, 11px);
    border: 1px solid var(--color-border, #333); border-radius: 6px; padding: 3px 6px 3px 4px;
  }
  .settings .cpick input[type="color"] {
    width: 22px; height: 22px; padding: 0; border: none; background: none; cursor: pointer; border-radius: 4px;
  }
  .settings .cpick .x {
    background: none; border: none; color: var(--color-text-muted); cursor: pointer;
    font-size: 14px; line-height: 1; padding: 0 2px;
  }
  .linkish {
    background: none; border: none; padding: 0; cursor: pointer;
    color: var(--color-primary); text-decoration: underline; font: inherit;
  }

  .tablewrap { overflow-x: auto; margin-top: 6px; }
  table.summary { width: 100%; border-collapse: collapse; font-size: var(--fs-sm, 13px); }
  table.summary th, table.summary td {
    text-align: left; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--color-border, #2a2a2a);
    white-space: nowrap;
  }
  table.summary th { color: var(--color-text-muted); font-weight: 600; font-size: var(--fs-xs, 11px); text-transform: uppercase; letter-spacing: .03em; }
  table.summary td .sw { width: 9px; height: 9px; border-radius: 2px; display: inline-block; margin-right: 6px; }
  table.summary .num { text-align: right; font-variant-numeric: tabular-nums; }
  table.summary td.bad { color: var(--status-red, #ef4444); font-weight: 600; }
</style>
