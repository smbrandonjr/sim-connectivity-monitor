<script lang="ts">
  import { onMount } from "svelte";
  import { status } from "../lib/stores";
  import { api } from "../lib/api";
  import Copyable from "../lib/Copyable.svelte";
  import Sparkline from "../lib/Sparkline.svelte";

  let history: any[] = [];
  const SERIES = [
    { key: "rsrp", label: "RSRP", unit: "dBm" },
    { key: "rsrq", label: "RSRQ", unit: "dB" },
    { key: "sinr", label: "SINR", unit: "dB" },
    { key: "rssi", label: "RSSI", unit: "dBm" },
  ];

  async function loadTelemetry() {
    history = (await api.telemetry()).history ?? [];
  }

  onMount(() => {
    loadTelemetry();
    const t = setInterval(loadTelemetry, 5000);
    return () => clearInterval(t);
  });

  $: s = $status;
  $: t = s?.telemetry ?? {};
  $: fb = s?.fallback;
</script>

{#if !s}
  <p class="muted">connecting…</p>
{:else}
  <h1>Status</h1>

  {#if fb?.active}
    <div class="ui-card alert">
      <strong>Fallback test in progress</strong> — radio off until the SIM applet switches profiles.
    </div>
  {/if}

  <div class="cards">
    <section class="ui-card">
      <h2>Modem</h2>
      <dl>
        <dt>Vendor / model</dt><dd>{s.vendor ?? "—"} {s.model ?? ""}</dd>
        <dt>Firmware</dt><dd><Copyable value={s.firmware} /></dd>
        <dt>IMEI</dt><dd><Copyable value={s.imei} /></dd>
        <dt>Operator</dt><dd>{s.operator ?? "—"}</dd>
        <dt>Registration</dt><dd>{s.registration ?? "—"}</dd>
        <dt>Signal</dt><dd>{s.signal_rssi != null ? `${s.signal_rssi} dBm (${s.signal_percent}%)` : "—"}</dd>
      </dl>
    </section>

    <section class="ui-card">
      <h2>SIM</h2>
      <dl>
        <dt>Present</dt><dd>{s.sim_present ? "yes" : "no"}</dd>
        <dt>ICCID</dt><dd><Copyable value={s.iccid} /></dd>
        <dt>IMSI</dt><dd><Copyable value={s.imsi} /></dd>
        <dt>Profile</dt><dd>{s.active_profile ?? "—"}{#if s.forced_profile} <span class="badge amber">forced</span>{/if}</dd>
      </dl>
    </section>

    <section class="ui-card">
      <h2>Network</h2>
      <dl>
        <dt>Interface</dt><dd>{s.interface ?? "—"}</dd>
        <dt>IP address</dt><dd><Copyable value={s.ip_address} /></dd>
        <dt>APN</dt><dd>{s.apn ?? "—"}</dd>
        <dt>Default route</dt><dd>{s.routing_ok == null ? "—" : s.routing_ok ? "cellular" : "not cellular"}</dd>
        <dt>Last error</dt><dd style="color:var(--status-red)">{s.last_error ?? "—"}</dd>
      </dl>
    </section>

    <section class="ui-card">
      <h2>Serving cell</h2>
      <dl>
        <dt>RAT / band</dt><dd>{t.rat ?? "—"} {t.band ? `· B${t.band}` : ""}</dd>
        <dt>Operator (MCC/MNC)</dt><dd>{t.operator_numeric ?? "—"}</dd>
        <dt>Cell ID / PCI</dt><dd>{t.cell_id ?? "—"} / {t.pci ?? "—"}</dd>
        <dt>EARFCN / TAC</dt><dd>{t.earfcn ?? "—"} / {t.tac ?? "—"}</dd>
      </dl>
    </section>
  </div>

  <div class="cards">
    {#each SERIES as c}
      <section class="ui-card">
        <h2>{c.label}</h2>
        <div class="metric-now">{t[c.key] ?? "—"} <span class="muted">{c.unit}</span></div>
        <Sparkline values={history.map((r) => r[c.key])} />
      </section>
    {/each}
  </div>
{/if}
