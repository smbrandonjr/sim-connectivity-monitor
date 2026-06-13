<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { ts } from "../lib/format";

  let events: any[] = [];
  let kind = "";
  const KINDS = ["state", "modem", "sim", "profile", "pdp", "connection",
    "routing", "recovery", "fallback", "monitor", "sms", "ota", "identity", "urc", "command"];

  async function load() {
    events = await api.events();
  }
  $: shown = kind ? events.filter((e) => e.kind === kind) : events;

  const levelColor = (l: string) =>
    l === "error" ? "red" : l === "warning" ? "amber" : "";

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Events</h1>
  <select class="ui-select" style="width:auto" bind:value={kind}>
    <option value="">all kinds</option>
    {#each KINDS as k}<option value={k}>{k}</option>{/each}
  </select>
</div>

<table>
  <thead><tr><th>Time</th><th>Level</th><th>Kind</th><th>Message</th></tr></thead>
  <tbody>
    {#each shown as e}
      <tr>
        <td class="nowrap">{ts(e.ts)}</td>
        <td><span class="badge {levelColor(e.level)}">{e.level}</span></td>
        <td class="mono">{e.kind}</td>
        <td class="break">{e.message}</td>
      </tr>
    {:else}
      <tr><td colspan="4" class="muted">No events.</td></tr>
    {/each}
  </tbody>
</table>
