<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { confirmDialog } from "../lib/confirm";
  import { ts } from "../lib/format";

  let messages: any[] = [];
  let status:
    | { enabled: boolean; egress: string; interface: string | null; ports: number[]; errors: string[] }
    | null = null;

  // ── listener config ───────────────────────────────────────────────────────
  let enabled = false;
  let egress = "auto";
  let ports: number[] = [];
  let portInput = "";
  let saving = false;

  // ── auto-reply rules ──────────────────────────────────────────────────────
  type Rule = {
    name: string;
    enabled: boolean;
    match: "contains" | "exact" | "prefix" | "regex";
    pattern: string;
    case_sensitive: boolean;
    reply: string;
  };
  let showRules = false;
  let rules: Rule[] = [];

  const blankRule = (): Rule => ({
    name: "", enabled: true, match: "contains",
    pattern: "", case_sensitive: false, reply: "",
  });

  async function loadConfig() {
    try {
      const c = await api.udpConfig();
      enabled = !!c.enabled;
      egress = c.egress ?? "auto";
      ports = (c.ports ?? []).slice();
      rules = (c.rules ?? []).map((r: any) => ({ ...blankRule(), ...r }));
      status = c.status ?? null;
    } catch {
      /* keep defaults */
    }
  }

  function addPort() {
    const p = parseInt(portInput.trim(), 10);
    if (!Number.isInteger(p) || p < 1 || p > 65535) {
      toast("Enter a port between 1 and 65535", "error");
      return;
    }
    if (!ports.includes(p)) ports = [...ports, p].sort((a, b) => a - b);
    portInput = "";
  }
  function removePort(p: number) {
    ports = ports.filter((x) => x !== p);
  }

  function addRule() {
    rules = [...rules, blankRule()];
    showRules = true;
  }
  function removeRule(i: number) {
    rules = rules.filter((_, idx) => idx !== i);
  }

  async function save() {
    // Drop blank rule rows so an empty editor row can't fail validation.
    const clean = rules.filter((r) => r.pattern.trim() && r.reply.trim());
    saving = true;
    const ok = await api.saveUdpConfig({
      enabled,
      ports,
      egress,
      rules: clean,
    });
    saving = false;
    if (ok) {
      rules = clean.map((r) => ({ ...r }));
      toast("UDP listener saved", "ok");
      setTimeout(loadConfig, 800);  // pick up the new runtime status
    }
  }

  async function load() {
    messages = await api.udp();
  }

  async function clearAll() {
    const ok = await confirmDialog({
      title: "Clear capture log",
      message: "Delete ALL captured UDP messages from the device? This can't be undone.",
      confirmLabel: "Clear all", danger: true,
    });
    if (!ok) return;
    await api.clearUdp();
    setTimeout(load, 400);
  }

  onMount(() => {
    loadConfig();
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>UDP</h1>
  {#if status}
    {#if status.enabled && status.ports.length}
      <span class="badge green">listening: {status.ports.join(", ")} on {status.interface ?? "all interfaces"}</span>
    {:else if status.enabled}
      <span class="badge amber">enabled, no ports bound</span>
    {:else}
      <span class="badge">off</span>
    {/if}
    {#each status.errors ?? [] as e}<span class="badge red">{e}</span>{/each}
  {/if}
  <span style="flex:1"></span>
  <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={clearAll}>Clear log</button>
</div>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Listener</h2>
    <label class="toggle">
      <input type="checkbox" bind:checked={enabled} /> <span>Enabled</span>
    </label>
  </div>
  <p class="muted hint">
    Bind one or more UDP ports, capture every inbound datagram, and optionally
    auto-reply to the sender. Datagrams are decoded as UTF-8 for display and
    matching; non-text payloads are shown as hex and never auto-replied.
  </p>

  <div class="row" style="margin-top:8px">
    <input class="ui-input" style="max-width:140px" placeholder="port (e.g. 9999)"
      bind:value={portInput}
      on:keydown={(e) => { if (e.key === "Enter") addPort(); }} />
    <button class="ui-btn ui-btn-sm" on:click={addPort}><i class="ri-add-line"></i> Add port</button>
    <div class="chips">
      {#each ports as p (p)}
        <span class="chip">{p}<button title="remove" on:click={() => removePort(p)}><i class="ri-close-line"></i></button></span>
      {:else}
        <span class="muted">No ports yet.</span>
      {/each}
    </div>
  </div>

  <div class="row" style="margin-top:8px">
    <label class="toggle" style="gap:8px">
      <span>Listen on</span>
      <select class="ui-select" style="width:auto" title="interface" bind:value={egress}>
        <option value="wlan">Wi-Fi</option>
        <option value="cellular">Cellular</option>
        <option value="auto">Any</option>
      </select>
    </label>
    <span class="muted hint" style="flex:1">
      Which interface to bind to (SO_BINDTODEVICE). "Any" listens on all
      interfaces; falls back to all if the chosen one isn't up.
    </span>
  </div>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Auto-reply</h2>
    <button class="ui-btn ui-btn-sm" class:on={showRules} on:click={() => (showRules = !showRules)}>
      {showRules ? "Hide" : "Show"} rules ({rules.length})
    </button>
  </div>
  <p class="muted hint">
    When an inbound datagram matches a rule, the device replies to the sender on
    the same port. Rules are tried top-to-bottom; the first match wins. A per-peer
    rate cap prevents reply loops.
  </p>

  {#if showRules}
    <div class="rules">
      {#each rules as r, i (i)}
        <div class="rule" class:off={!r.enabled}>
          <div class="rline">
            <label class="toggle sm" title="enable this rule">
              <input type="checkbox" bind:checked={r.enabled} />
            </label>
            <input class="ui-input name" placeholder="label (optional)" bind:value={r.name} />
            <select class="ui-input match" bind:value={r.match}>
              <option value="contains">contains</option>
              <option value="exact">exact</option>
              <option value="prefix">starts with</option>
              <option value="regex">regex</option>
            </select>
            <label class="toggle sm" title="case sensitive">
              <input type="checkbox" bind:checked={r.case_sensitive} /> <span>Aa</span>
            </label>
            <button class="ui-btn ui-btn-sm ui-btn-danger" title="remove rule"
              on:click={() => removeRule(i)}><i class="ri-delete-bin-line"></i></button>
          </div>
          <div class="rline">
            <input class="ui-input pat" placeholder="pattern to match in the datagram" bind:value={r.pattern} />
          </div>
          <div class="rline">
            <textarea class="ui-input rep" rows="2" placeholder="reply to send back" bind:value={r.reply}></textarea>
          </div>
        </div>
      {:else}
        <p class="muted">No rules yet. Add one to start auto-replying.</p>
      {/each}
    </div>

    <div class="row" style="margin-top:10px">
      <button class="ui-btn ui-btn-sm" on:click={addRule}><i class="ri-add-line"></i> Add rule</button>
    </div>
  {/if}

  <div class="row" style="margin-top:10px">
    <span style="flex:1"></span>
    <button class="ui-btn ui-btn-primary ui-btn-sm" on:click={save} disabled={saving}>
      {saving ? "Saving…" : "Save listener"}
    </button>
  </div>
</section>

<table>
  <thead><tr><th>Time</th><th>Dir</th><th>Port</th><th>Peer</th><th>Payload</th><th>Rule</th></tr></thead>
  <tbody>
    {#each messages as m (m.id)}
      <tr>
        <td class="nowrap">{ts(m.ts)}</td>
        <td>
          {#if m.direction === "in"}<span class="badge green">in</span>
          {:else}<span class="badge">reply</span>{/if}
        </td>
        <td class="mono">{m.port}</td>
        <td class="nowrap mono">{m.peer}</td>
        <td class="break">
          {#if m.body != null}{m.body}
          {:else}<span class="mono">{m.body_hex}</span><span class="badge amber">binary</span>{/if}
          <span class="badge">{m.length}B</span>
        </td>
        <td class="break">{m.matched_rule ?? "—"}</td>
      </tr>
    {:else}
      <tr><td colspan="6" class="muted">No datagrams captured yet.</td></tr>
    {/each}
  </tbody>
</table>
<p class="muted">Non-UTF-8 payloads are shown as hex and are never auto-replied.</p>

<style>
  .hint { font-size: var(--fs-xs, 11px); margin: 2px 0 0; }
  .toggle {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: var(--fs-sm, 13px); color: var(--color-text); white-space: nowrap;
  }
  .toggle.sm { gap: 4px; font-size: var(--fs-xs, 11px); color: var(--color-text-muted); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .chip {
    display: inline-flex; align-items: center; gap: 4px;
    border: 1px solid var(--color-border, #333); border-radius: 6px;
    padding: 2px 4px 2px 8px; font-family: var(--font-mono, monospace);
    font-size: var(--fs-sm, 13px); background: var(--color-surface-2, rgba(127,127,127,.05));
  }
  .chip button {
    border: none; background: none; cursor: pointer; color: var(--color-text-muted);
    display: inline-flex; padding: 0;
  }
  .chip button:hover { color: var(--color-danger, #e55); }
  .rules { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
  .rule {
    border: 1px solid var(--color-border, #333); border-radius: 8px; padding: 10px;
    display: flex; flex-direction: column; gap: 6px;
    background: var(--color-surface-2, rgba(127,127,127,.05));
  }
  .rule.off { opacity: 0.55; }
  .rline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .rule .name { flex: 1; min-width: 120px; }
  .rule .match { width: 110px; flex: none; }
  .rule .pat { flex: 1; font-family: var(--font-mono, monospace); }
  .rule .rep { flex: 1; width: 100%; resize: vertical; }
</style>
