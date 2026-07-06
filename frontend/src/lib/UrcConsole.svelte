<script lang="ts">
  // Live tail of the modem's unsolicited result codes (URC log). Polls
  // /api/urcs.json?after=<last id> so each request only carries new lines.
  // URCs land in the DB once per daemon tick, so worst-case latency is
  // tick_seconds + the poll interval.
  import { onMount, onDestroy } from "svelte";
  import { api } from "./api";

  const POLL_MS = 2000;
  const MAX_LINES = 500;

  let lines: any[] = [];
  let lastId = 0;
  let paused = false;
  let poll: number | undefined;
  let body: HTMLDivElement;
  let pinned = true; // auto-scroll unless the user scrolled up

  const timeOf = (epoch: number) => {
    const d = new Date(epoch * 1000);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };

  const kindBadge = (kind: string) =>
    kind === "ring" || kind === "caller_id" ? "lime"
    : kind === "new_sms" || kind === "sms_deliver" ? "blue"
    : kind === "sim_status" ? "amber"
    : kind === "unknown" ? "" : "green";

  function append(rows: any[]) {
    if (!rows.length) return;
    lines = [...lines, ...rows].slice(-MAX_LINES);
    for (const r of rows) if (r.id > lastId) lastId = r.id;
    if (pinned) queueMicrotask(() => body?.scrollTo({ top: body.scrollHeight }));
  }

  async function tick() {
    if (paused) return;
    try {
      append(await api.urcs(lastId));
    } catch {
      /* transient poll failure; next tick retries */
    }
  }

  function onScroll() {
    if (!body) return;
    pinned = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  }

  function togglePause() {
    paused = !paused;
    if (!paused) tick(); // catch up immediately on resume
  }

  onMount(async () => {
    try {
      const seed = await api.urcs(); // newest-first
      append(seed.slice().reverse());
    } catch {
      /* seeded empty; polling will fill in */
    }
    poll = window.setInterval(tick, POLL_MS);
  });
  onDestroy(() => poll && clearInterval(poll));
</script>

<div class="ui-card">
  <div class="row" style="margin-bottom:8px">
    <h2 style="margin-bottom:0">Live URC console</h2>
    <span class="badge {paused ? 'amber' : 'green'}">
      <span class="dot {paused ? 'amber' : 'green'}"></span>{paused ? "paused" : "live"}
    </span>
    <span class="nav-spacer"></span>
    <button class="ui-btn ui-btn-sm" on:click={togglePause}>
      <i class="ri-{paused ? 'play' : 'pause'}-line"></i>{paused ? "Resume" : "Pause"}
    </button>
    <button class="ui-btn ui-btn-sm" on:click={() => (lines = [])} disabled={!lines.length}>
      <i class="ri-delete-back-2-line"></i>Clear
    </button>
  </div>
  <div class="urc-body code-block" bind:this={body} on:scroll={onScroll}>
    {#each lines as l (l.id)}
      <div class="urc-line" class:call={l.kind === "ring" || l.kind === "caller_id"}>
        <span class="muted">{timeOf(l.ts)}</span>
        <span class="badge {kindBadge(l.kind)}">{l.kind}</span>
        <span class="urc-raw">{l.raw}</span>
      </div>
    {:else}
      <div class="muted">Waiting for unsolicited modem messages — RING / +CLIP show up
        here when a call comes in to the SIM's MSISDN.</div>
    {/each}
  </div>
</div>

<style>
  .urc-body {
    height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px;
  }
  .urc-line { display: flex; gap: 10px; align-items: baseline; }
  .urc-line.call .urc-raw { color: var(--color-primary); font-weight: 700; }
  .urc-raw { word-break: break-all; }
</style>
