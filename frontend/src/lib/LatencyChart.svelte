<script lang="ts">
  import { ts } from "./format";

  // One line per interface for a single metric over time. Points with a null
  // value break the line (gap), so a 100%-loss cycle shows as a hole rather
  // than a misleading interpolation.
  export let series: Record<string, { ts: number; value: number | null }[]> = {};
  export let colorOf: (iface: string) => string = () => "var(--color-primary)";
  export let cellular: string | null = null;
  export let windowStart = 0;
  export let windowEnd = 1;
  export let unit = "";
  export let valueFloor: number | null = null; // force y-axis lower bound (e.g. 0)
  export let valueCeil: number | null = null;   // force y-axis upper bound (e.g. 100)

  // Wide, short aspect so `height:auto` stays a sane height at full card width.
  const W = 1100;
  const H = 200;
  const PAD = { l: 46, r: 14, t: 12, b: 22 };

  $: ifaces = Object.keys(series);
  $: allVals = ifaces.flatMap((i) => series[i].map((p) => p.value).filter((v): v is number => v != null));
  $: lo = valueFloor != null ? valueFloor : allVals.length ? Math.min(...allVals) : 0;
  $: hiRaw = valueCeil != null ? valueCeil : allVals.length ? Math.max(...allVals) : 1;
  $: hi = hiRaw <= lo ? lo + 1 : hiRaw;
  $: spanX = Math.max(1, windowEnd - windowStart);

  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  function xOf(t: number) { return PAD.l + ((t - windowStart) / spanX) * plotW; }
  function yOf(v: number) { return PAD.t + (1 - (v - lo) / (hi - lo)) * plotH; }

  // Build an SVG path, breaking into subpaths at null values.
  function pathFor(points: { ts: number; value: number | null }[]) {
    let d = "";
    let pen = false;
    for (const p of points) {
      if (p.value == null) { pen = false; continue; }
      const cmd = pen ? "L" : "M";
      d += `${cmd}${xOf(p.ts).toFixed(1)},${yOf(p.value).toFixed(1)} `;
      pen = true;
    }
    return d.trim();
  }

  // Three y gridlines: lo, mid, hi.
  $: yTicks = [lo, (lo + hi) / 2, hi];
  function fmt(v: number) { return v >= 100 ? v.toFixed(0) : v.toFixed(v < 10 ? 1 : 0); }
</script>

{#if allVals.length >= 1}
  <svg class="chart" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">
    {#each yTicks as t, i}
      <line class="grid" x1={PAD.l} x2={W - PAD.r} y1={yOf(t)} y2={yOf(t)} />
      <text class="ylab" x={PAD.l - 6} y={yOf(t) + 3} text-anchor="end">
        {fmt(t)}{i === yTicks.length - 1 && unit ? " " + unit : ""}
      </text>
    {/each}
    {#each ifaces as iface}
      <path
        d={pathFor(series[iface])}
        fill="none"
        stroke={colorOf(iface)}
        stroke-width={iface === cellular ? 2.6 : 1.5}
        stroke-linejoin="round"
        stroke-linecap="round"
      />
    {/each}
    <text class="xlab" x={PAD.l} y={H - 6} text-anchor="start">{ts(windowStart)}</text>
    <text class="xlab" x={W - PAD.r} y={H - 6} text-anchor="end">{ts(windowEnd)}</text>
  </svg>
{:else}
  <p class="muted">no data in this window yet</p>
{/if}

<style>
  .chart { width: 100%; height: auto; max-height: 260px; display: block; }
  .grid { stroke: var(--color-border, #2a2a2a); stroke-width: 1; opacity: 0.5; }
  .ylab, .xlab { fill: var(--color-text-muted, #888); font-size: 11px; font-family: var(--font-mono, monospace); }
</style>
