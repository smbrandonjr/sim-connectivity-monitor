<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";
  import LatencyChart from "../lib/LatencyChart.svelte";

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
    </div>
  </div>

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
      <p class="muted">No latency samples in this window. Enable the monitor
        (<span class="mono">latency.enabled: true</span> in config.yaml) and give it a few cycles.</p>
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
</style>
