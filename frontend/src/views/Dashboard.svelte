<script lang="ts">
  import { onMount } from "svelte";
  import { status } from "../lib/stores";
  import { api } from "../lib/api";
  import Copyable from "../lib/Copyable.svelte";
  import Sparkline from "../lib/Sparkline.svelte";
  import ModemSetup from "../lib/ModemSetup.svelte";
  import ConnectivityHistory from "../lib/ConnectivityHistory.svelte";
  import { METRICS, classify, tierColor } from "../lib/signal";
  import { dur } from "../lib/format";

  let history: any[] = [];
  $: nowS = $status?.server_time ?? Date.now() / 1000;
  $: stateDur = $status ? nowS - $status.state_since : 0;

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

  {#if s.state === "NO_MODEM" && s.modem_setup?.modem_present}
    <div class="ui-card alert">
      <strong>Modem detected, but no AT port is set.</strong>
      A spare serial port is needed to control this modem — test the ports below and pick the
      one that responds.
    </div>
    <ModemSetup />
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
        <dt>Gateway</dt><dd><Copyable value={s.gateway} /></dd>
        <dt>Public IP</dt><dd><Copyable value={s.public_ip} /></dd>
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

    <section class="ui-card">
      <h2>Uptime</h2>
      <dl>
        <dt>Cellular</dt>
        <dd class={s.state === "CONNECTED" ? "ok" : "bad"}>
          {#if s.state === "CONNECTED"}connected for {dur(stateDur)}
          {:else}{s.state.toLowerCase().replace("_", " ")} for {dur(stateDur)}{/if}
        </dd>
        <dt>Device (since boot)</dt><dd>{s.device_uptime_s != null ? dur(s.device_uptime_s) : "—"}</dd>
      </dl>
    </section>
  </div>

  <div class="cards">
    {#each METRICS as m}
      {@const v = t[m.key]}
      {@const tier = typeof v === "number" ? classify(m, v) : null}
      <section class="ui-card metric">
        <div class="metric-head">
          <h2>{m.label}</h2>
          <span class="info" tabindex="0" role="img" aria-label={m.label + " help"}>
            i
            <div class="tip">
              <p><strong>{m.label}</strong> — {m.what}</p>
              <table>
                {#each m.bands as b}
                  <tr class:current={tier === b.tier}>
                    <td><span class="qdot {b.tier}"></span></td>
                    <td>{b.label}</td>
                    <td class="mono">{b.range}</td>
                  </tr>
                {/each}
              </table>
            </div>
          </span>
          {#if tier}<span class="qbadge {tier}">{tier}</span>{/if}
        </div>
        <div class="metric-now" style={tier ? `color:${tierColor(tier)}` : ""}>
          {v ?? "—"} <span class="muted">{m.unit}</span>
        </div>
        <Sparkline values={history.map((r) => r[m.key])} />
      </section>
    {/each}
  </div>

  <ConnectivityHistory />
{/if}

<style>
  dd.ok { color: var(--status-green); }
  dd.bad { color: var(--status-amber); }
  .metric-head { display: flex; align-items: center; gap: 8px; }
  .metric-head h2 { margin: 0; flex: 0 0 auto; }
  .qbadge {
    margin-left: auto; font-size: var(--fs-xs, 11px); text-transform: capitalize;
    padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor;
  }
  .qbadge.excellent { color: var(--status-cyan); }
  .qbadge.good { color: var(--status-green); }
  .qbadge.fair { color: var(--status-amber); }
  .qbadge.poor { color: var(--status-red); }

  .info {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; border-radius: 50%; cursor: help;
    font-size: 11px; font-style: italic; font-weight: 700;
    color: var(--color-text-muted); border: 1px solid var(--color-border, #444);
  }
  .info .tip {
    position: absolute; top: 130%; left: 0; z-index: 20; width: 280px;
    padding: 10px 12px; border-radius: 8px; font-style: normal; font-weight: 400;
    background: var(--color-surface, #1b1f26); color: var(--color-text, #e6e6e6);
    border: 1px solid var(--color-border, #444); box-shadow: 0 8px 24px rgba(0,0,0,.4);
    opacity: 0; visibility: hidden; transition: opacity .12s ease; text-align: left;
  }
  .info:hover .tip, .info:focus .tip, .info:focus-within .tip { opacity: 1; visibility: visible; }
  .info .tip p { margin: 0 0 8px; font-size: var(--fs-sm, 13px); line-height: 1.4; }
  .info .tip table { width: 100%; border-collapse: collapse; font-size: var(--fs-sm, 13px); }
  .info .tip td { padding: 2px 6px 2px 0; }
  .info .tip tr.current { font-weight: 700; }
  .qdot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
  .qdot.excellent { background: var(--status-cyan); }
  .qdot.good { background: var(--status-green); }
  .qdot.fair { background: var(--status-amber); }
  .qdot.poor { background: var(--status-red); }
</style>
