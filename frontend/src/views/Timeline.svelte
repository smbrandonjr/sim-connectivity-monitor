<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";

  let rows: any[] = [];
  let total = 0;
  let kinds: string[] = [];
  let source = "all";
  let kind = "all";
  let page = 0;
  const PAGE_SIZE = 50;
  let poll: number | undefined;

  const badge = (src: string) => (src === "urc" ? "amber" : src === "identity" ? "green" : "");

  async function load() {
    const data = await api.timeline({
      source: source === "all" ? undefined : source,
      kind: kind === "all" ? undefined : kind,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    });
    rows = data.rows;
    total = data.total;
    kinds = data.kinds;
  }

  function changeFilter() {
    page = 0;
    load();
  }
  function goPage(p: number) {
    page = Math.max(0, Math.min(p, Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)));
    load();
  }

  $: pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $: rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  $: rangeEnd = Math.min(total, (page + 1) * PAGE_SIZE);

  onMount(() => {
    load();
    poll = window.setInterval(() => { if (page === 0) load(); }, 5000);
  });
  onDestroy(() => poll && clearInterval(poll));
</script>

<div class="row">
  <h1>Timeline</h1>
  <select class="ui-select" style="width:auto" bind:value={source} on:change={changeFilter}>
    <option value="all">all sources</option>
    <option value="event">events</option>
    <option value="urc">URCs</option>
    <option value="identity">identity</option>
  </select>
  <select class="ui-select" style="width:auto" bind:value={kind} on:change={changeFilter}>
    <option value="all">all kinds</option>
    {#each kinds as k}<option value={k}>{k}</option>{/each}
  </select>
  <span class="nav-spacer"></span>
  <a class="ui-btn ui-btn-sm" href="/api/bundle.json">Download diagnostic bundle</a>
</div>
<p class="muted">Everything that happened, time-ordered: state changes and events, unsolicited
  modem messages (URCs), and SIM identity changes. The diagnostic bundle is a secret-free JSON
  snapshot you can share for side-by-side comparison between devices.</p>

<div class="row" style="margin-bottom:8px">
  {#if total > 0}
    <span class="muted">{rangeStart}–{rangeEnd} of {total}</span>
    <button class="ui-btn ui-btn-sm" disabled={page === 0} on:click={() => goPage(page - 1)}>‹ newer</button>
    <button class="ui-btn ui-btn-sm" disabled={page >= pages - 1} on:click={() => goPage(page + 1)}>older ›</button>
  {/if}
</div>

<table>
  <thead><tr><th>Time</th><th>Source</th><th>Kind</th><th>Detail</th></tr></thead>
  <tbody>
    {#each rows as r}
      <tr>
        <td class="nowrap">{ts(r.ts)}</td>
        <td><span class="badge {badge(r.source)}">{r.source}</span></td>
        <td class="mono">{r.kind}</td>
        <td class="break">{r.detail}</td>
      </tr>
    {:else}
      <tr><td colspan="4" class="muted">Nothing matches.</td></tr>
    {/each}
  </tbody>
</table>
