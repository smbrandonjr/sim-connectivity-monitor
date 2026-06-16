<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { api } from "./api";
  import { ts, dur } from "./format";

  const PRESETS = [
    { label: "1h", s: 3600 },
    { label: "24h", s: 86400 },
    { label: "7d", s: 604800 },
    { label: "30d", s: 2592000 },
  ];

  let presetS: number | null = 86400; // null = custom range
  let fromStr = "";
  let toStr = "";
  let data: any = null;
  let loading = false;
  let timer: any;

  // Convert a datetime-local string to epoch seconds (browser-local tz).
  const toEpoch = (s: string) => (s ? new Date(s).getTime() / 1000 : null);
  // epoch -> value for <input type="datetime-local"> (local, minute precision).
  function toLocalInput(epoch: number) {
    const d = new Date(epoch * 1000);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  async function load() {
    loading = true;
    try {
      if (presetS != null) {
        const to = Date.now() / 1000;
        data = await api.connectivity(to - presetS, to);
      } else {
        const from = toEpoch(fromStr);
        const to = toEpoch(toStr);
        if (from == null || to == null) { loading = false; return; }
        data = await api.connectivity(from, to);
      }
    } catch {
      /* keep last */
    }
    loading = false;
  }

  function pickPreset(s: number) {
    presetS = s;
    load();
  }
  function useCustom() {
    // seed the custom inputs from the current window, then switch to custom mode
    if (data) {
      fromStr = toLocalInput(data.window_start);
      toStr = toLocalInput(data.window_end);
    }
    presetS = null;
  }

  onMount(() => {
    load();
    // Live-refresh only while showing a "now"-anchored preset window.
    timer = setInterval(() => { if (presetS != null) load(); }, 15000);
  });
  onDestroy(() => clearInterval(timer));

  $: episodes = (data?.episodes ?? []).slice().reverse(); // most-recent first
  $: span = data ? Math.max(1, data.window_end - data.window_start) : 1;
  function leftPct(e: any) { return ((e.start - data.window_start) / span) * 100; }
  function widthPct(e: any) { return Math.max(0.4, ((e.end - e.start) / span) * 100); }
  function pct(v: number | null) { return v == null ? "—" : v.toFixed(v >= 99.95 ? 1 : 2) + "%"; }
</script>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Connectivity history</h2>
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
    <div class="stats">
      <div class="stat"><span class="v" style="color:var(--status-green)">{pct(data.uptime_pct)}</span><span class="k">uptime</span></div>
      <div class="stat"><span class="v">{data.outage_count}</span><span class="k">outages</span></div>
      <div class="stat"><span class="v">{dur(data.down_s)}</span><span class="k">total down</span></div>
      <div class="stat"><span class="v">{dur(data.longest_outage_s)}</span><span class="k">longest outage</span></div>
    </div>

    <div class="bar" title="green = connected, red = down · {ts(data.window_start)} → {ts(data.window_end)}">
      {#each (data.episodes ?? []) as e}
        <div class="down" style="left:{leftPct(e)}%;width:{widthPct(e)}%" title={(e.detail ?? 'down') + ' · ' + dur(e.duration_s)}></div>
      {/each}
    </div>
    <div class="row muted" style="font-size:var(--fs-xs,11px);justify-content:space-between">
      <span>{ts(data.window_start)}</span><span>{ts(data.window_end)}</span>
    </div>
    {#if data.data_since}
      <p class="muted" style="font-size:var(--fs-xs,11px);margin:4px 0 0">tracking since {ts(data.data_since)}</p>
    {/if}

    <h3 style="font-size:var(--fs-sm);margin:14px 0 4px;color:var(--color-text-muted)">
      Outages {loading ? "(refreshing…)" : ""}
    </h3>
    <table>
      <thead><tr><th>Started</th><th>Ended</th><th>Duration</th><th>Reason</th></tr></thead>
      <tbody>
        {#each episodes as e}
          <tr>
            <td class="nowrap">{ts(e.start)}</td>
            <td class="nowrap">{e.ongoing ? "— ongoing —" : ts(e.end)}</td>
            <td class="mono">{dur(e.duration_s)}</td>
            <td class="break muted">{e.detail ?? "—"}</td>
          </tr>
        {:else}
          <tr><td colspan="4" class="muted">No outages in this window — connected the whole time. 🎉</td></tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">{loading ? "loading…" : "no data"}</p>
  {/if}
</section>

<style>
  .seg { display: flex; gap: 4px; flex-wrap: wrap; }
  .stats { display: flex; flex-wrap: wrap; gap: 24px; margin: 10px 0 12px; }
  .stat { display: flex; flex-direction: column; }
  .stat .v { font-size: var(--fs-lg, 20px); font-weight: 700; }
  .stat .k { font-size: var(--fs-xs, 11px); color: var(--color-text-muted); text-transform: uppercase; letter-spacing: .04em; }
  .bar {
    position: relative; height: 18px; border-radius: 4px; overflow: hidden;
    background: var(--status-green); opacity: .85;
  }
  .bar .down { position: absolute; top: 0; bottom: 0; background: var(--status-red); }
</style>
