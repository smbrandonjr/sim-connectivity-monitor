<script lang="ts">
  import { onMount } from "svelte";
  import { status } from "../lib/stores";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { ts } from "../lib/format";

  let commands = "";

  const REFERENCE = [
    { group: "Forbidden network list (FPLMN)", items: [
      { cmd: "AT+CRSM=176,28539,0,0,12", desc: "Read the SIM's blacklist of rejected networks (FFFFFF = empty slot)." },
      { cmd: 'AT+CRSM=214,28539,0,0,12,"FFFFFFFFFFFFFFFFFFFFFFFF"\nAT+CFUN=1,1', desc: "Clear the blacklist then reboot the modem to rescan." },
    ]},
    { group: "Network selection", items: [
      { cmd: "AT+COPS?", desc: "Current operator and selection mode (0 auto / 1 manual)." },
      { cmd: "AT+COPS=?", desc: "Scan visible networks — takes 1–2 min, run alone. 1=available 2=current 3=forbidden." },
      { cmd: 'AT+COPS=1,2,"310260"', desc: "Force a network by PLMN (310260 T-Mobile, 310410 AT&T, 311480 Verizon). Persists!" },
      { cmd: "AT+COPS=0", desc: "Back to automatic network selection." },
    ]},
    { group: "Quick status", items: [
      { cmd: "AT+CSQ", desc: "Signal (0–31; 99 = no signal). dBm ≈ −113 + 2n." },
      { cmd: "AT+CEREG?", desc: "LTE registration (1 home, 5 roaming, 2 searching, 3 denied)." },
      { cmd: "AT+QNWINFO", desc: "Serving network / band (Quectel)." },
    ]},
  ];

  function add(cmd: string) {
    commands = commands ? `${commands}\n${cmd}` : cmd;
  }

  async function runStandard() {
    if (await api.cmd("run-diagnostics", { commands: [] })) toast("diagnostics queued", "ok");
  }
  async function runCustom() {
    const list = commands.split("\n").map((c) => c.trim()).filter(Boolean);
    if (await api.cmd("run-diagnostics", { commands: list })) toast("diagnostics queued", "ok");
  }

  $: report = $status?.diagnostics;
</script>

<h1>Diagnostics</h1>
<p class="muted">Run AT commands over the app's serial port — safe while everything is running.
  Results appear below within a few seconds.</p>

<section class="ui-card">
  <div class="row">
    <button class="ui-btn ui-btn-primary" on:click={runStandard}>Run standard diagnostics</button>
    <span class="muted">signal, registration, operator, PDP contexts + vendor checks</span>
  </div>
</section>

<section class="ui-card">
  <h2>Custom AT commands</h2>
  <textarea class="ui-textarea" rows="5" placeholder="One AT command per line" bind:value={commands}></textarea>
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
  <p class="muted">Click a command to add it to the box above.</p>
  {#each REFERENCE as g}
    <h3 style="font-size:var(--fs-sm);margin:12px 0 4px;color:var(--color-text-muted)">{g.group}</h3>
    <table>
      <tbody>
        {#each g.items as it}
          <tr>
            <td class="nowrap"><a href={"#"} on:click|preventDefault={() => add(it.cmd)}><code>{it.cmd.split("\n")[0]}{it.cmd.includes("\n") ? " …" : ""}</code></a></td>
            <td class="muted">{it.desc}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/each}
</section>
