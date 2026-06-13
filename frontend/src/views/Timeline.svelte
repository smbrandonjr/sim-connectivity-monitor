<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";

  let rows: any[] = [];

  async function load() {
    rows = await api.timeline();
  }

  const badge = (src: string) => (src === "urc" ? "amber" : src === "identity" ? "green" : "");

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Timeline</h1>
  <a class="ui-btn ui-btn-sm" href="/api/bundle.json">Download diagnostic bundle</a>
</div>
<p class="muted">Events, unsolicited modem messages (URCs), and SIM identity changes in one
  time-ordered view. The diagnostic bundle is a secret-free JSON snapshot you can share for
  side-by-side comparison between devices.</p>

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
      <tr><td colspan="4" class="muted">Nothing recorded yet.</td></tr>
    {/each}
  </tbody>
</table>
