<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";

  let rows: any[] = [];
  let source = "all";
  let kind = "all";

  async function load() {
    rows = await api.timeline();
  }

  const badge = (src: string) => (src === "urc" ? "amber" : src === "identity" ? "green" : "");

  $: kinds = ["all", ...Array.from(new Set(rows.map((r) => r.kind))).sort()];
  $: shown = rows.filter(
    (r) => (source === "all" || r.source === source) && (kind === "all" || r.kind === kind),
  );

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Timeline</h1>
  <select class="ui-select" style="width:auto" bind:value={source}>
    <option value="all">all sources</option>
    <option value="event">events</option>
    <option value="urc">URCs</option>
    <option value="identity">identity</option>
  </select>
  <select class="ui-select" style="width:auto" bind:value={kind}>
    {#each kinds as k}<option value={k}>{k === "all" ? "all kinds" : k}</option>{/each}
  </select>
  <span class="nav-spacer"></span>
  <a class="ui-btn ui-btn-sm" href="/api/bundle.json">Download diagnostic bundle</a>
</div>
<p class="muted">Everything that happened, time-ordered: state changes and events, unsolicited
  modem messages (URCs), and SIM identity changes. The diagnostic bundle is a secret-free JSON
  snapshot you can share for side-by-side comparison between devices.</p>

<table>
  <thead><tr><th>Time</th><th>Source</th><th>Kind</th><th>Detail</th></tr></thead>
  <tbody>
    {#each shown as r}
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
