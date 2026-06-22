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

  async function load() {
    loading = true;
    try {
      let from: number | null, to: number | null;
      if (presetS != null) {
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

  function pickPreset(s: number) { presetS = s; load(); }
  function useCustom() {
    if (data) { fromStr = toLocalInput(data.window_start); toStr = toLocalInput(data.window_end); }
    presetS = null;
  }

  onMount(() => {
    load();
    loadConfig();
    timer = setInterval(() => { if (presetS != null) load(); }, 15000);
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
  $: colorIndex = Object.fromEntries(interfaces.map((i, idx) => [i, idx]));
  const colorOf = (iface: string) => PALETTE[(colorIndex[iface] ?? 0) % PALETTE.length];
  $: cellular = data?.cellular_interface ?? null;
  $: rttSeries = data ? perInterface("rtt") : {};
  $: lossSeries = data ? perInterface("loss") : {};
  $: headline = data?.headline ?? {};

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
      <div class="row wrap">
        <label class="sw"><input type="checkbox" bind:checked={cfg.enabled} /> Enabled</label>
        <label class="muted">interval
          <input class="ui-input n" type="number" min="10" bind:value={cfg.interval_seconds} /> s</label>
        <label class="muted">pings/target
          <input class="ui-input n" type="number" min="1" max="20" bind:value={cfg.packet_count} /></label>
        <label class="muted">timeout
          <input class="ui-input n" type="number" min="1" max="30" bind:value={cfg.timeout_seconds} /> s</label>
      </div>
      <div class="row wrap">
        <label class="muted col">targets (one per line, or comma/space separated)
          <textarea class="ui-input ta" rows="4" bind:value={targetsText}
            placeholder="1.1.1.1&#10;8.8.8.8"></textarea></label>
        <div class="col grow">
          <label class="muted">interfaces <span class="hint">(empty = auto: every up interface)</span>
            <input class="ui-input" bind:value={interfacesText} placeholder="auto" /></label>
          <label class="muted">exclude interfaces
            <input class="ui-input" bind:value={excludeText} placeholder="e.g. docker0" /></label>
        </div>
      </div>
      <div class="row wrap">
        <label class="muted">keep raw samples
          <input class="ui-input n" type="number" min="1" max="90" bind:value={cfg.raw_retention_days} /> days</label>
        <label class="muted">keep rollups
          <input class="ui-input n" type="number" min="1" max="400" bind:value={cfg.rollup_retention_days} /> days</label>
        <span style="flex:1"></span>
        <button class="ui-btn ui-btn-primary ui-btn-sm" on:click={saveConfig} disabled={saving}>
          {saving ? "Saving…" : "Save settings"}
        </button>
      </div>
      <p class="muted hint">Changes apply on the next probe cycle — no restart needed.</p>
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

      <h3 class="ttl">Latency <span class="muted">({SOURCE_LABEL[data.source] ?? data.source})</span></h3>
      <LatencyChart series={rttSeries} {colorOf} {cellular}
        windowStart={data.window_start} windowEnd={data.window_end} unit="ms" valueFloor={0} />

      <h3 class="ttl">Packet loss</h3>
      <LatencyChart series={lossSeries} {colorOf} {cellular}
        windowStart={data.window_start} windowEnd={data.window_end} unit="%" valueFloor={0} valueCeil={100} />

      <p class="muted foot">
        {ts(data.window_start)} → {ts(data.window_end)} · {loading ? "refreshing…" : "live"}
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
    padding: 12px; margin: 10px 0 4px; display: flex; flex-direction: column; gap: 10px;
    background: var(--color-surface-2, rgba(127,127,127,.05));
  }
  .settings .row.wrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
  .settings label { display: flex; flex-direction: column; gap: 3px; font-size: var(--fs-xs, 11px); }
  .settings label.sw { flex-direction: row; align-items: center; gap: 6px; }
  .settings .col { min-width: 220px; }
  .settings .col.grow { flex: 1; gap: 8px; }
  .settings .ui-input.n { width: 70px; }
  .settings .ui-input.ta { font-family: var(--font-mono, monospace); resize: vertical; }
  .settings .hint { font-size: var(--fs-xs, 11px); opacity: .7; }
  .linkish {
    background: none; border: none; padding: 0; cursor: pointer;
    color: var(--color-primary); text-decoration: underline; font: inherit;
  }
</style>
