<script lang="ts">
  import { status } from "../lib/stores";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { ts } from "../lib/format";
  import ModemSetup from "../lib/ModemSetup.svelte";

  let commands = "";
  let fallbackSeconds = 900;

  // Forceable radio access technologies (newest → oldest).
  const RATS = [
    { key: "5g_sa", label: "5G SA" },
    { key: "5g_nsa", label: "5G NSA" },
    { key: "lte", label: "LTE" },
    { key: "lte_m", label: "LTE-M" },
    { key: "nb_iot", label: "NB-IoT" },
    { key: "3g", label: "3G" },
    { key: "2g", label: "2G" },
  ];
  $: ratSupported = $status?.rat_supported ?? [];
  $: currentRat = $status?.telemetry?.rat ?? null;
  async function setRat(rat: string, label: string) {
    if (await api.cmd("set-rat", { rat })) toast(`network mode → ${label}`, "ok");
  }

  // Full AT reference (function + sample response), click any to queue it.
  const REFERENCE: { group: string; rows: { cmd: string; fn: string; sample: string }[] }[] = [
    { group: "Identity & SIM", rows: [
      { cmd: "AT", fn: "Attention; verifies command path", sample: "OK" },
      { cmd: "ATI", fn: "Module number", sample: "EC25EFAR06A11M4G" },
      { cmd: "AT+CGMI", fn: "Manufacturer identity", sample: "Quectel" },
      { cmd: "AT+CGMM", fn: "Model identity", sample: "EC25" },
      { cmd: "AT+CPIN?", fn: "SIM PIN state (Hologram needs none)", sample: "+CPIN: READY" },
      { cmd: "AT+CCID", fn: "SIM ICCID (profile identifier)", sample: "+CCID: 891234…" },
      { cmd: "AT+CFUN?", fn: "Module functionality", sample: "+CFUN: 1,0" },
    ]},
    { group: "Signal & registration", rows: [
      { cmd: "AT+CSQ", fn: "Signal quality", sample: "+CSQ: 22,4" },
      { cmd: "AT+CREG?", fn: "GSM/SMS registration", sample: "+CREG: 0,5" },
      { cmd: "AT+CGREG?", fn: "3G registration", sample: "+CGREG: 0,5" },
      { cmd: "AT+CEREG?", fn: "LTE/EPS registration", sample: '+CEREG: 2,5,"MI9S","25SS404",8' },
      { cmd: "AT+QNWINFO", fn: "Serving network / band (Quectel)", sample: '+QNWINFO: "FDD LTE","310410","LTE BAND 2",2150' },
    ]},
    { group: "Data context", rows: [
      { cmd: "AT+CGDCONT?", fn: "PDP context parameters (APN)", sample: '+CGDCONT: 1,"IP","hologram","0.0.0.0",0,0' },
      { cmd: "AT+CGACT?", fn: "PDP context activation", sample: "+CGACT: 1,1" },
    ]},
    { group: "Network selection", rows: [
      { cmd: "AT+COPS?", fn: "Operator selection status", sample: '+COPS: 1,2,"310260",2' },
      { cmd: "AT+COPS=?", fn: "Networks in reach (scan, 1–2 min)", sample: '+COPS: (1,"AT&T","AT&T","310410",2)' },
      { cmd: 'AT+COPS=1,2,"310260"', fn: "Force a network by PLMN (persists!)", sample: "OK" },
      { cmd: "AT+COPS=0", fn: "Back to automatic selection", sample: "OK" },
    ]},
    { group: "Forbidden networks (FPLMN)", rows: [
      { cmd: "AT+CRSM=176,28539,0,0,12", fn: "Read FPLMN (FFFF… = empty)", sample: '+CRSM: 144,0,"FFFFFFFFFFFFFFFFFFFFFFFF"' },
      { cmd: 'AT+CRSM=214,28539,0,0,12,"FFFFFFFFFFFFFFFFFFFFFFFF"\nAT+CFUN=1,1', fn: "Clear FPLMN + reboot modem", sample: "+CRSM: 144,0" },
    ]},
  ];

  function add(cmd: string) { commands = commands ? `${commands}\n${cmd}` : cmd; }

  async function runStandard() {
    if (await api.cmd("run-diagnostics", { commands: [] })) toast("diagnostics queued", "ok");
  }
  async function runCustom() {
    const list = commands.split("\n").map((c) => c.trim()).filter(Boolean);
    if (await api.cmd("run-diagnostics", { commands: list })) toast("diagnostics queued", "ok");
  }

  async function act(name: string, body: any, msg: string) {
    if (await api.cmd(name, body)) toast(msg, "ok");
  }

  $: report = $status?.diagnostics;
  $: paused = $status?.monitor_paused;
</script>

<h1>Diagnostics</h1>

<ModemSetup />

<section class="ui-card">
  <h2>Actions</h2>
  <div class="row">
    <button class="ui-btn" on:click={() => act("reconnect", {}, "reconnecting")}>Reconnect</button>
    <button class="ui-btn ui-btn-danger" on:click={() => act("reset-modem", {}, "resetting modem")}>Reset modem</button>
    <button class="ui-btn" on:click={() => act("monitor-now", {}, "heartbeat sent")}>Send heartbeat</button>
    {#if paused}
      <button class="ui-btn" on:click={() => act("monitor-resume", {}, "resumed")}>Resume heartbeats</button>
    {:else}
      <button class="ui-btn" on:click={() => act("monitor-pause", {}, "paused")}>Pause heartbeats</button>
    {/if}
  </div>
  <div class="row" style="margin-top:10px">
    {#if $status?.fallback?.active}
      <button class="ui-btn ui-btn-danger" on:click={() => act("fallback-abort", {}, "aborting fallback test")}>Abort fallback test</button>
    {:else}
      <button class="ui-btn ui-btn-danger" on:click={() => act("fallback-test", { duration_seconds: fallbackSeconds }, "fallback test started")}>Start fallback test</button>
      <label class="muted">for <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={fallbackSeconds} /> s</label>
    {/if}
  </div>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Network mode (RAT)</h2>
    <span class="muted">current: <strong>{currentRat ?? "—"}</strong></span>
  </div>
  <p class="muted">Force the modem onto a specific radio access technology. Options your modem
    doesn't support are disabled. Forcing a mode briefly drops the connection while it
    re-attaches; the setting is saved on the modem and persists across reboots.</p>
  {#if ratSupported.length === 0}
    <p class="muted">No modem connected.</p>
  {:else}
    <div class="row" style="flex-wrap:wrap">
      {#each RATS as r}
        <button class="ui-btn ui-btn-sm" disabled={!ratSupported.includes(r.key)}
          title={ratSupported.includes(r.key) ? `Force ${r.label}` : "Not supported by this modem"}
          on:click={() => setRat(r.key, r.label)}>{r.label}</button>
      {/each}
    </div>
    <div class="row" style="margin-top:10px">
      <button class="ui-btn" on:click={() => setRat("auto", "Automatic")}>Reset to factory default (Automatic)</button>
      <span class="muted">restores automatic selection of all supported technologies</span>
    </div>
  {/if}
</section>

<section class="ui-card">
  <h2>AT console</h2>
  <p class="muted">Runs over the app's serial port — safe while everything is running. Results
    appear below within a few seconds.</p>
  <div class="row">
    <button class="ui-btn ui-btn-primary" on:click={runStandard}>Run standard diagnostics</button>
    <span class="muted">the full sweep: identity, SIM, signal, registration, PDP, operator</span>
  </div>
  <textarea class="ui-textarea" rows="5" style="margin-top:10px" placeholder="One AT command per line" bind:value={commands}></textarea>
  <div class="row" style="margin-top:8px"><button class="ui-btn" on:click={runCustom}>Run</button></div>
</section>

{#if report}
  <section class="ui-card">
    <h2>Results — {ts(report.ran_at)}</h2>
    {#if report.note}<p style="color:var(--status-red)">{report.note}</p>{/if}
    <div class="code-block">{#each report.entries as e}&gt; {e.command}
{e.output}

{/each}</div>
  </section>
{/if}

<section class="ui-card">
  <h2>Command reference</h2>
  <p class="muted">Click a command to add it to the console above.</p>
  {#each REFERENCE as g}
    <h3 style="font-size:var(--fs-sm);margin:12px 0 4px;color:var(--color-text-muted)">{g.group}</h3>
    <table>
      <thead><tr><th>Command</th><th>Function</th><th>Sample response</th></tr></thead>
      <tbody>
        {#each g.rows as r}
          <tr>
            <td class="nowrap"><a href={"#"} on:click|preventDefault={() => add(r.cmd)}><code>{r.cmd.split("\n")[0]}{r.cmd.includes("\n") ? " …" : ""}</code></a></td>
            <td class="muted">{r.fn}</td>
            <td class="mono break">{r.sample}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/each}
</section>
