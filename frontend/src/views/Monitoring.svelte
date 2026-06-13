<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { ts } from "../lib/format";

  // Editable form model mirroring MonitorConfig.
  let enabled = false;
  let interval_seconds = 300;
  let send_when_degraded = true;
  let bind_cellular = true;
  let method = "POST";
  let url = "";
  let body = "";
  let timeout_seconds = 15;
  let expectStatus = "200, 204";
  let headers: { key: string; value: string }[] = [];

  let history: any[] = [];

  const PLACEHOLDERS = "{iccid} {imei} {imsi} {operator} {signal_rssi} {signal_percent} " +
    "{ip_address} {hostname} {timestamp} {state} {profile_name} {status} {status_message}";

  async function load() {
    const c = await api.monitorConfig();
    enabled = !!c.enabled;
    interval_seconds = c.interval_seconds ?? 300;
    send_when_degraded = c.send_when_degraded ?? true;
    bind_cellular = c.bind_cellular ?? true;
    const r = c.request ?? {};
    method = r.method ?? "POST";
    url = r.url ?? "";
    body = r.body ?? "";
    timeout_seconds = r.timeout_seconds ?? 15;
    expectStatus = (r.expect_status ?? [200, 204]).join(", ");
    headers = Object.entries(r.headers ?? {}).map(([key, value]) => ({ key, value: String(value) }));
  }

  async function loadHistory() {
    history = await api.monitorHistory();
  }

  function buildConfig() {
    const hdrs: Record<string, string> = {};
    for (const h of headers) if (h.key.trim()) hdrs[h.key.trim()] = h.value;
    const expect = expectStatus.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
    const cfg: any = { enabled, interval_seconds, send_when_degraded, bind_cellular };
    if (url.trim()) {
      cfg.request = {
        method, url: url.trim(), headers: hdrs, body,
        timeout_seconds, expect_status: expect.length ? expect : [200, 204],
      };
    }
    return cfg;
  }

  async function save() {
    if (await api.saveMonitorConfig(buildConfig())) toast("monitoring saved", "ok");
  }

  async function sendNow() {
    if (await api.cmd("monitor-now")) {
      toast("heartbeat sent", "ok");
      setTimeout(loadHistory, 1500);
    }
  }

  function addHeader() { headers = [...headers, { key: "", value: "" }]; }
  function removeHeader(i: number) { headers = headers.filter((_, idx) => idx !== i); }

  onMount(() => {
    load();
    loadHistory();
    const t = setInterval(loadHistory, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Monitoring</h1>
  <button class="ui-btn ui-btn-sm" on:click={sendNow}>Send heartbeat now</button>
</div>
<p class="muted">A global heartbeat sent to your endpoint on a schedule. While connected it goes
  out the cellular interface (proving cellular egress); if cellular drops it keeps sending over
  any other route with <code>status=degraded</code>. A profile may override this if it defines
  its own enabled monitor.</p>

<section class="ui-card">
  <div class="row">
    <label><input type="checkbox" bind:checked={enabled} /> Enabled</label>
    <label class="muted">interval <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={interval_seconds} /> s</label>
    <label><input type="checkbox" bind:checked={send_when_degraded} /> keep sending while degraded</label>
    <label><input type="checkbox" bind:checked={bind_cellular} /> bind to cellular (uncheck for LAN/VPN endpoint)</label>
  </div>
</section>

<section class="ui-card">
  <h2>Request</h2>
  <div class="row">
    <select class="ui-select" style="width:auto" bind:value={method}>
      {#each ["POST", "GET", "PUT", "PATCH", "HEAD"] as m}<option>{m}</option>{/each}
    </select>
    <input class="ui-input" style="flex:1;min-width:280px" placeholder="https://your-endpoint.example.com/heartbeat" bind:value={url} />
  </div>

  <h2 style="margin-top:14px">Headers</h2>
  {#each headers as h, i}
    <div class="row" style="margin-bottom:6px">
      <input class="ui-input" style="max-width:200px" placeholder="Header" bind:value={h.key} />
      <input class="ui-input" style="flex:1" placeholder="Value" bind:value={h.value} />
      <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => removeHeader(i)}>×</button>
    </div>
  {/each}
  <button class="ui-btn ui-btn-sm" on:click={addHeader}>+ header</button>

  <h2 style="margin-top:14px">Body</h2>
  <textarea class="ui-textarea" rows="5" bind:value={body}
    placeholder={'{"iccid":"{iccid}","status":"{status}","rssi":"{signal_rssi}"}'}></textarea>
  <p class="muted">Placeholders (usable in URL, headers, body): <code>{PLACEHOLDERS}</code></p>

  <div class="row" style="margin-top:8px">
    <label class="muted">timeout <input class="ui-input" style="width:70px;display:inline-block" type="number" bind:value={timeout_seconds} /> s</label>
    <label class="muted">expect status <input class="ui-input" style="width:120px;display:inline-block" bind:value={expectStatus} /></label>
    <button class="ui-btn ui-btn-primary" on:click={save}>Save</button>
  </div>
</section>

<section class="ui-card">
  <h2>Recent heartbeats</h2>
  <table>
    <thead><tr><th>Time</th><th>Result</th><th>Status</th><th>Latency</th><th>URL</th><th>Error</th></tr></thead>
    <tbody>
      {#each history as r}
        <tr>
          <td class="nowrap">{ts(r.ts)}</td>
          <td><span class="badge {r.ok ? 'green' : 'red'}">{r.ok ? "ok" : "FAIL"}</span></td>
          <td class="mono">{r.status_code ?? "—"}</td>
          <td class="mono">{r.latency_ms != null ? Math.round(r.latency_ms) + " ms" : "—"}</td>
          <td class="break">{r.url}</td>
          <td class="break muted">{r.error ?? ""}</td>
        </tr>
      {:else}
        <tr><td colspan="6" class="muted">No heartbeats yet.</td></tr>
      {/each}
    </tbody>
  </table>
</section>
