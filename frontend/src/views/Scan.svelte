<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { api } from "../lib/api";

  let tool = "discovery"; // discovery | ports | reachability | traceroute
  let ifaces: any[] = [];
  let status: any = { running: false, kind: null, results: [], summary: null };
  let poll: number | undefined;

  // form fields
  let cidr = "";
  let ports = "common";
  let host = "";
  let target = "";
  let iface = "";

  async function loadStatus() {
    status = await api.scanStatus();
  }

  function ensurePolling() {
    if (!poll) poll = window.setInterval(loadStatus, 800);
  }

  async function start() {
    let ok = false;
    if (tool === "discovery") ok = await api.scanStart("discovery", { cidr, ports });
    else if (tool === "ports") ok = await api.scanStart("ports", { host, ports });
    else if (tool === "reachability") ok = await api.scanStart("reachability", { target, interface: iface || null });
    else if (tool === "traceroute") ok = await api.scanStart("traceroute", { target, interface: iface || null });
    if (ok) { ensurePolling(); loadStatus(); }
  }
  async function stop() { await api.scanStop(); setTimeout(loadStatus, 300); }

  function useCidr(c: string) { cidr = c; }

  onMount(async () => {
    ifaces = await api.scanInterfaces();
    if (ifaces[0]) { cidr = ifaces[0].cidr; iface = ifaces.find((i) => i.name === "wwan0")?.name ?? ifaces[0].name; }
    await loadStatus();
    ensurePolling();
  });
  onDestroy(() => poll && clearInterval(poll));

  $: pct = status.total ? Math.round((status.progress / status.total) * 100) : 0;
  const TOOLS = [
    { id: "discovery", label: "Host discovery" },
    { id: "ports", label: "Port scan" },
    { id: "reachability", label: "Reachability" },
    { id: "traceroute", label: "Traceroute" },
  ];
</script>

<h1>Scan</h1>
<p class="muted">Network tools that run from this device. Host discovery and port scans use TCP
  connect (a host is "up" if it answers or actively refuses). Reachability and traceroute can be
  bound to an interface — handy for comparing the cellular path vs. wifi/ethernet.</p>

<div class="chips" style="margin-bottom:12px">
  {#each TOOLS as t}
    <button class="chip" class:on={tool === t.id} on:click={() => (tool = t.id)}>{t.label}</button>
  {/each}
</div>

<section class="ui-card">
  {#if tool === "discovery"}
    <h2>Host discovery</h2>
    <div class="row">
      <input class="ui-input" style="max-width:220px" bind:value={cidr} placeholder="192.168.1.0/24" />
      <input class="ui-input" style="max-width:200px" bind:value={ports} placeholder="common | 22,80,443 | 1-1024" />
    </div>
    <div class="row" style="margin-top:6px">
      {#each ifaces as i}
        <button class="chip" on:click={() => useCidr(i.cidr)} title={`${i.name} ${i.ip}`}>{i.name}: {i.cidr}</button>
      {/each}
    </div>
  {:else if tool === "ports"}
    <h2>Port scan</h2>
    <div class="row">
      <input class="ui-input" style="max-width:220px" bind:value={host} placeholder="host or IP" />
      <input class="ui-input" style="max-width:240px" bind:value={ports} placeholder="common | 1-1024 | 22,80,443" />
    </div>
  {:else}
    <h2>{tool === "reachability" ? "Reachability" : "Traceroute"}</h2>
    <div class="row">
      <input class="ui-input" style="max-width:260px" bind:value={target} placeholder={tool === "reachability" ? "host, IP, or URL" : "host or IP"} />
      <label class="muted">via
        <select class="ui-select" bind:value={iface}>
          <option value="">default route</option>
          {#each ifaces as i}<option value={i.name}>{i.name}</option>{/each}
        </select>
      </label>
    </div>
  {/if}

  <div class="row" style="margin-top:10px">
    {#if status.running}
      <button class="ui-btn ui-btn-danger" on:click={stop}>Stop</button>
      <div class="scan-progress"><div class="scan-progress-bar" style="width:{pct}%"></div></div>
      <span class="muted">{status.progress}/{status.total} ({pct}%)</span>
    {:else}
      <button class="ui-btn ui-btn-primary" on:click={start}>Start scan</button>
    {/if}
  </div>
  {#if status.error}<p style="color:var(--status-red);margin-top:8px">{status.error}</p>{/if}
</section>

{#if status.kind === "discovery"}
  <section class="ui-card">
    <h2>Hosts {status.summary ? `· ${status.summary.alive} alive of ${status.summary.scanned}` : ""}</h2>
    <table>
      <thead><tr><th>IP</th><th>Hostname</th><th>Open ports</th></tr></thead>
      <tbody>
        {#each status.results as r}
          <tr><td class="mono">{r.ip}</td><td class="mono">{r.host ?? "—"}</td>
            <td class="mono">{r.open_ports?.length ? r.open_ports.join(", ") : "—"}</td></tr>
        {:else}<tr><td colspan="3" class="muted">{status.running ? "scanning…" : "no live hosts found"}</td></tr>{/each}
      </tbody>
    </table>
  </section>
{:else if status.kind === "ports"}
  <section class="ui-card">
    <h2>Open ports {status.summary ? `· ${status.summary.open} of ${status.summary.scanned}` : ""}</h2>
    <div class="chips">
      {#each status.results as r}<span class="chip on">{r.port}</span>{:else}
        <span class="muted">{status.running ? "scanning…" : "no open ports"}</span>{/each}
    </div>
  </section>
{:else if status.kind === "reachability" && status.summary}
  {@const s = status.summary}
  <div class="cards">
    <section class="ui-card"><h2>Ping {status.interface ? `(${status.interface})` : ""}</h2>
      <dl><dt>Loss</dt><dd>{s.ping.loss_pct}%</dd><dt>Avg RTT</dt><dd>{s.ping.avg_ms ?? "—"} ms</dd>
        <dt>Received</dt><dd>{s.ping.received}/{s.ping.sent}</dd></dl></section>
    <section class="ui-card"><h2>DNS</h2>
      <dl><dt>Resolved</dt><dd>{s.dns.ok ? "yes" : "no"}</dd><dt>Time</dt><dd>{s.dns.ms} ms</dd>
        <dt>Addresses</dt><dd class="break">{s.dns.addresses?.join(", ") || "—"}</dd></dl></section>
    <section class="ui-card"><h2>HTTP</h2>
      <dl><dt>Status</dt><dd>{s.http.status ?? "—"}</dd><dt>Latency</dt><dd>{s.http.latency_ms} ms</dd>
        <dt>OK</dt><dd>{s.http.ok ? "yes" : (s.http.error ?? "no")}</dd></dl></section>
    <section class="ui-card"><h2>TCP</h2>
      <dl>{#each Object.entries(s.tcp) as [p, st]}<dt>:{p}</dt><dd>{st}</dd>{/each}</dl></section>
  </div>
{:else if status.kind === "traceroute"}
  <section class="ui-card">
    <h2>Hops {status.summary ? `· ${status.summary.reached ? "reached target" : "did not reach"}` : ""}</h2>
    <table>
      <thead><tr><th>#</th><th>IP</th><th>Host</th><th>RTT</th></tr></thead>
      <tbody>
        {#each status.results as h}
          <tr><td class="mono">{h.ttl}</td><td class="mono">{h.ip ?? "*"}</td>
            <td class="mono">{h.host ?? "—"}</td><td class="mono">{h.rtt_ms != null ? h.rtt_ms + " ms" : "*"}</td></tr>
        {:else}<tr><td colspan="4" class="muted">{status.running ? "tracing…" : "no hops"}</td></tr>{/each}
      </tbody>
    </table>
  </section>
{/if}
