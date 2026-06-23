<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import { ts } from "./format";

  // One line per interface for a single metric over time. Points with a null
  // value break the line (gap), so a 100%-loss cycle shows as a hole rather
  // than a misleading interpolation. Hover for a crosshair + per-interface
  // readout at the nearest sample; drag horizontally to zoom (dispatches a
  // `zoom` event with {from, to} epoch seconds).
  export let series: Record<string, { ts: number; value: number | null }[]> = {};
  export let colorOf: (iface: string) => string = () => "var(--color-primary)";
  export let cellular: string | null = null;
  export let windowStart = 0;
  export let windowEnd = 1;
  export let unit = "";
  export let valueFloor: number | null = null; // force y-axis lower bound (e.g. 0)
  export let valueCeil: number | null = null;   // force y-axis upper bound (e.g. 100)

  const dispatch = createEventDispatcher();

  // Wide, short aspect so `height:auto` stays a sane height at full card width.
  const W = 1100;
  const H = 200;
  const PAD = { l: 46, r: 14, t: 12, b: 22 };
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;

  $: ifaces = Object.keys(series);
  $: allVals = ifaces.flatMap((i) => series[i].map((p) => p.value).filter((v): v is number => v != null));
  $: lo = valueFloor != null ? valueFloor : allVals.length ? Math.min(...allVals) : 0;
  $: hiRaw = valueCeil != null ? valueCeil : allVals.length ? Math.max(...allVals) : 1;
  $: hi = hiRaw <= lo ? lo + 1 : hiRaw;
  $: spanX = Math.max(1, windowEnd - windowStart);

  function xOf(t: number) { return PAD.l + ((t - windowStart) / spanX) * plotW; }
  function yOf(v: number) { return PAD.t + (1 - (v - lo) / (hi - lo)) * plotH; }

  function pathFor(points: { ts: number; value: number | null }[]) {
    let d = "";
    let pen = false;
    for (const p of points) {
      if (p.value == null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${xOf(p.ts).toFixed(1)},${yOf(p.value).toFixed(1)} `;
      pen = true;
    }
    return d.trim();
  }

  $: yTicks = [lo, (lo + hi) / 2, hi];
  function fmt(v: number) { return v >= 100 ? v.toFixed(0) : v.toFixed(v < 10 ? 1 : 0); }
  function fmtVal(v: number) { return unit === "%" ? v.toFixed(2) + "%" : v.toFixed(1) + " ms"; }

  // ── hover + zoom interaction ──────────────────────────────────────────────
  $: tsList = Array.from(new Set(ifaces.flatMap((i) => series[i].map((p) => p.ts)))).sort((a, b) => a - b);
  $: seriesMap = Object.fromEntries(
    ifaces.map((i) => [i, new Map(series[i].map((p) => [p.ts, p.value]))]),
  ) as Record<string, Map<number, number | null>>;

  let svgEl: SVGSVGElement;
  let wrapW = 0;
  let hover: { ts: number; px: number; items: { iface: string; value: number; color: string }[] } | null = null;
  let drag: { x0: number; x1: number } | null = null;

  function vbX(e: MouseEvent) {
    const r = svgEl.getBoundingClientRect();
    return r.width ? ((e.clientX - r.left) / r.width) * W : 0;
  }
  function timeAt(x: number) {
    const t = windowStart + ((x - PAD.l) / plotW) * spanX;
    return Math.min(windowEnd, Math.max(windowStart, t));
  }
  function nearestTs(t: number) {
    let best = tsList[0], bd = Infinity;
    for (const x of tsList) { const d = Math.abs(x - t); if (d < bd) { bd = d; best = x; } }
    return best;
  }

  function onMove(e: MouseEvent) {
    if (!tsList.length) return;
    const x = vbX(e);
    if (drag) drag = { ...drag, x1: x };
    const nt = nearestTs(timeAt(x));
    const items = ifaces
      .map((i) => ({ iface: i, value: seriesMap[i].get(nt) as number, color: colorOf(i) }))
      .filter((it) => it.value != null);
    const r = svgEl.getBoundingClientRect();
    hover = { ts: nt, px: e.clientX - r.left, items };
  }
  function onLeave() { hover = null; drag = null; }
  function onDown(e: MouseEvent) { const x = vbX(e); drag = { x0: x, x1: x }; }
  function onUp() {
    if (drag) {
      const a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
      drag = null;
      if (b - a > 8) { // distinguish a zoom-drag from a click
        const from = timeAt(a), to = timeAt(b);
        if (to - from >= 1) dispatch("zoom", { from, to });
      }
    }
  }

  $: selX = drag ? Math.min(drag.x0, drag.x1) : 0;
  $: selW = drag ? Math.abs(drag.x1 - drag.x0) : 0;
  $: tipRight = hover ? hover.px < wrapW * 0.6 : true;
</script>

{#if allVals.length >= 1}
  <div class="wrap" bind:clientWidth={wrapW}>
    <svg
      class="chart" class:dragging={!!drag}
      viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet"
      bind:this={svgEl}
      on:mousemove={onMove} on:mouseleave={onLeave}
      on:mousedown={onDown} on:mouseup={onUp}
      role="img" aria-label="latency chart"
    >
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

      {#if hover}
        <line class="cross" x1={xOf(hover.ts)} x2={xOf(hover.ts)} y1={PAD.t} y2={H - PAD.b} />
        {#each hover.items as it}
          <circle cx={xOf(hover.ts)} cy={yOf(it.value)} r="3" fill={it.color}
            stroke="var(--color-bg, #111)" stroke-width="1" />
        {/each}
      {/if}

      {#if drag && selW > 0}
        <rect class="sel" x={selX} y={PAD.t} width={selW} height={plotH} />
      {/if}

      <!-- transparent capture layer so empty plot area still gets pointer events -->
      <rect x={PAD.l} y={PAD.t} width={plotW} height={plotH} fill="transparent" />

      <text class="xlab" x={PAD.l} y={H - 6} text-anchor="start">{ts(windowStart)}</text>
      <text class="xlab" x={W - PAD.r} y={H - 6} text-anchor="end">{ts(windowEnd)}</text>
    </svg>

    {#if hover && hover.items.length}
      <div class="tip" class:left={!tipRight}
        style="left:{hover.px}px">
        <div class="tip-ts">{ts(hover.ts)}</div>
        {#each hover.items as it}
          <div class="tip-row">
            <span class="sw" style="background:{it.color}"></span>
            <span class="tip-if">{it.iface}{it.iface === cellular ? " ·cell" : ""}</span>
            <span class="tip-v">{fmtVal(it.value)}</span>
          </div>
        {/each}
      </div>
    {/if}
  </div>
{:else}
  <p class="muted">no data in this window yet</p>
{/if}

<style>
  .wrap { position: relative; }
  .chart { width: 100%; height: auto; max-height: 260px; display: block; cursor: crosshair; }
  .chart.dragging { cursor: ew-resize; }
  .grid { stroke: var(--color-border, #2a2a2a); stroke-width: 1; opacity: 0.5; }
  .ylab, .xlab { fill: var(--color-text-muted, #888); font-size: 11px; font-family: var(--font-mono, monospace); }
  .cross { stroke: var(--color-text-muted, #888); stroke-width: 1; stroke-dasharray: 3 3; opacity: .7; }
  .sel { fill: var(--color-primary, #84cc16); opacity: .14; stroke: var(--color-primary, #84cc16); stroke-opacity: .5; }

  .tip {
    position: absolute; top: 4px; transform: translateX(10px);
    pointer-events: none; z-index: 5; min-width: 140px;
    background: var(--color-surface, #1b1b1f); border: 1px solid var(--color-border, #333);
    border-radius: 6px; padding: 6px 8px; box-shadow: 0 4px 14px rgba(0,0,0,.35);
    font-size: var(--fs-xs, 11px);
  }
  .tip.left { transform: translateX(-100%) translateX(-10px); }
  .tip-ts { font-family: var(--font-mono, monospace); color: var(--color-text-muted); margin-bottom: 4px; white-space: nowrap; }
  .tip-row { display: flex; align-items: center; gap: 6px; line-height: 1.5; }
  .tip .sw { width: 9px; height: 9px; border-radius: 2px; flex: none; }
  .tip-if { font-family: var(--font-mono, monospace); flex: 1; }
  .tip-v { font-variant-numeric: tabular-nums; font-weight: 600; }
</style>
