<script lang="ts">
  import { status } from "./stores";
  import { api } from "./api";
  import { toast } from "./toast";
  import { ts } from "./format";

  $: setup = $status?.modem_setup;
  $: ports = setup?.ports ?? [];
  $: current = setup?.at_port ?? "auto";

  let busy: string | null = null; // device currently being tested

  function hex(v: number | null | undefined) {
    return v == null ? "—" : v.toString(16).padStart(4, "0");
  }
  // No USB VID/PID => an on-board UART (ttyS0/ttyAMA0), not a USB modem port.
  // Only relevant for modems wired to the Pi's serial header (HATs).
  function isUart(p: any) {
    return p.vid == null && p.pid == null;
  }
  function recommended(p: any) {
    return p.tested && p.responded && !p.mm_claimed && !p.is_current;
  }
  async function refresh() {
    await api.cmd("scan-serial-ports");
    toast("scanning serial ports…", "info");
  }
  async function test(device: string) {
    busy = device;
    await api.cmd("probe-at-port", { device });
    setTimeout(() => (busy = busy === device ? null : busy), 3500);
  }
  async function use(device: string) {
    if (await api.cmd("set-at-port", { device })) toast(`using ${device}`, "ok");
  }
  async function auto() {
    if (await api.cmd("set-at-port", { device: "" })) toast("auto-detect restored", "ok");
  }
</script>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Modem &amp; AT port</h2>
    <button class="ui-btn ui-btn-sm" on:click={refresh}>Rescan ports</button>
  </div>

  <p class="muted">
    sim-monitor controls the modem over one spare serial <strong>AT port</strong> that
    ModemManager isn't using. Known modems are picked automatically; for a new model,
    <strong>Test</strong> the ports below and <strong>Use</strong> the one that replies with the
    modem's name and is <em>not</em> marked “MM uses”.
  </p>

  <div class="row" style="margin-bottom:8px">
    <span class="muted">Current AT port:</span>
    <code>{current}</code>
    {#if current !== "auto"}
      <button class="ui-btn ui-btn-sm" on:click={auto}>Reset to auto-detect</button>
    {/if}
    {#if setup}
      <span class="muted" style="margin-left:auto">
        {setup.modem_present ? "modem detected by ModemManager" : "no modem seen by ModemManager"}
        {#if setup.scanned_at} · scanned {ts(setup.scanned_at)}{/if}
      </span>
    {/if}
  </div>

  {#if ports.length}
    <table>
      <thead>
        <tr><th>Port</th><th>Interface</th><th>USB ID</th><th>Status</th><th>AT test</th><th></th></tr>
      </thead>
      <tbody>
        {#each ports as p}
          <tr class:rec={recommended(p)} class:uart={isUart(p)}>
            <td class="mono nowrap">{p.device}</td>
            <td class="mono">{p.interface != null ? "if" + String(p.interface).padStart(2, "0") : "—"}</td>
            <td class="mono">{#if isUart(p)}<span class="muted">on-board UART</span>{:else}{hex(p.vid)}:{hex(p.pid)}{/if}</td>
            <td class="nowrap">
              {#if p.is_current}<span class="badge green">current</span>{/if}
              {#if p.mm_claimed}<span class="badge amber" title="ModemManager uses this port — don't take it">MM uses</span>{/if}
              {#if isUart(p)}<span class="badge" title="Not a USB modem port. Only relevant for a modem wired to the Pi's serial header (HAT); USB modems appear as ttyUSB* with a USB ID.">UART</span>{/if}
            </td>
            <td class="break">
              {#if busy === p.device}
                <span class="muted">testing…</span>
              {:else if p.tested && p.responded}
                <span style="color:var(--status-green)">✓ {p.identity}</span>
              {:else if p.tested}
                <span style="color:var(--status-red)">✕ no response</span>
              {:else}
                <span class="muted">—</span>
              {/if}
            </td>
            <td class="nowrap">
              <button class="ui-btn ui-btn-sm" on:click={() => test(p.device)} disabled={busy === p.device}>Test</button>
              <button class="ui-btn ui-btn-sm ui-btn-primary" on:click={() => use(p.device)} disabled={p.is_current}>Use</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="muted" style="margin-top:8px">
      Tip: the right port answers a Test with the modem's name (e.g. <code>SIMCOM SIM7080</code>)
      and isn't marked “MM uses”. The choice is saved on the device and survives reboots.
      Ports marked <strong>UART</strong> are the Pi's own serial pins — ignore them unless your
      modem is a serial HAT rather than USB.
    </p>
  {:else}
    <p class="muted">No serial ports found yet. Plug in the modem and click “Rescan ports”.</p>
  {/if}
</section>

<style>
  tr.rec td { background: rgba(52, 211, 153, 0.08); }
  tr.uart td { opacity: 0.55; }
</style>
