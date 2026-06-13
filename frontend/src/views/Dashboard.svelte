<script lang="ts">
  import { status } from "../lib/stores";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import Copyable from "../lib/Copyable.svelte";

  let fallbackSeconds = 900;

  async function cmd(name: string, body?: Record<string, unknown>, ok?: string) {
    if (await api.cmd(name, body)) toast(ok ?? "requested", "ok");
  }

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
      <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => cmd("fallback-abort", {}, "aborting")}>Abort</button>
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
        <dt>Default route</dt><dd>{s.routing_ok == null ? "—" : s.routing_ok ? "cellular" : "not cellular"}</dd>
        <dt>RSRP / SINR</dt><dd>{t.rsrp ?? "—"} / {t.sinr ?? "—"}</dd>
        <dt>Last error</dt><dd style="color:var(--status-red)">{s.last_error ?? "—"}</dd>
      </dl>
    </section>

    <section class="ui-card">
      <h2>Heartbeat</h2>
      <dl>
        <dt>Scheduled</dt><dd>{s.monitor_paused ? "paused" : "running"}</dd>
        <dt>Unread SMS</dt><dd>{s.sms_unread ?? 0}</dd>
      </dl>
    </section>
  </div>

  <section class="ui-card">
    <h2>Actions</h2>
    <div class="row">
      <button class="ui-btn" on:click={() => cmd("reconnect", {}, "reconnecting")}>Reconnect</button>
      <button class="ui-btn ui-btn-danger" on:click={() => cmd("reset-modem", {}, "resetting modem")}>Reset modem</button>
      <button class="ui-btn" on:click={() => cmd("monitor-now", {}, "sending heartbeat")}>Send heartbeat</button>
      {#if s.monitor_paused}
        <button class="ui-btn" on:click={() => cmd("monitor-resume", {}, "resumed")}>Resume heartbeats</button>
      {:else}
        <button class="ui-btn" on:click={() => cmd("monitor-pause", {}, "paused")}>Pause heartbeats</button>
      {/if}
    </div>
    <div class="row" style="margin-top:10px">
      <button class="ui-btn ui-btn-danger" on:click={() => cmd("fallback-test", { duration_seconds: fallbackSeconds }, "fallback test started")}>
        Start fallback test
      </button>
      <label class="muted">for <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={fallbackSeconds} /> s</label>
    </div>
  </section>
{/if}
