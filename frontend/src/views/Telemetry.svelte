<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import Sparkline from "../lib/Sparkline.svelte";

  let latest: any = {};
  let history: any[] = [];

  const SERIES = [
    { key: "rsrp", label: "RSRP", unit: "dBm" },
    { key: "rsrq", label: "RSRQ", unit: "dB" },
    { key: "sinr", label: "SINR", unit: "dB" },
    { key: "rssi", label: "RSSI", unit: "dBm" },
  ];

  async function load() {
    const data = await api.telemetry();
    latest = data.latest ?? {};
    history = data.history ?? [];
  }

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });

  $: seriesValues = (key: string) => history.map((r) => r[key]);
</script>

<h1>Telemetry</h1>
<p class="muted">Deep link metrics sampled while connected ({history.length} samples). RSRP/RSRQ/SINR
  predict LTE reliability better than signal bars.</p>

<div class="cards">
  <section class="ui-card">
    <h2>Serving cell</h2>
    <dl>
      <dt>RAT / band</dt><dd>{latest.rat ?? "—"} {latest.band ? `· B${latest.band}` : ""}</dd>
      <dt>Operator</dt><dd>{latest.operator_numeric ?? "—"}</dd>
      <dt>Cell ID / PCI</dt><dd>{latest.cell_id ?? "—"} / {latest.pci ?? "—"}</dd>
      <dt>EARFCN / TAC</dt><dd>{latest.earfcn ?? "—"} / {latest.tac ?? "—"}</dd>
    </dl>
  </section>
  {#each SERIES as c}
    <section class="ui-card">
      <h2>{c.label}</h2>
      <div class="metric-now">{latest[c.key] ?? "—"} <span class="muted">{c.unit}</span></div>
      <Sparkline values={seriesValues(c.key)} />
    </section>
  {/each}
</div>
