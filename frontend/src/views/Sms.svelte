<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { confirmDialog } from "../lib/confirm";
  import { ts } from "../lib/format";

  let messages: any[] = [];
  let total = 0;
  let page = 0;
  const PAGE_SIZE = 25;
  let number = "";
  let text = "";

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
  let savingRules = false;
  let autoEnabled = false;
  let rules: Rule[] = [];

  const blankRule = (): Rule => ({
    name: "", enabled: true, match: "contains",
    pattern: "", case_sensitive: false, reply: "",
  });

  async function loadRules() {
    try {
      const c = await api.smsAutoReply();
      autoEnabled = !!c.enabled;
      rules = (c.rules ?? []).map((r: any) => ({ ...blankRule(), ...r }));
    } catch {
      /* keep defaults */
    }
  }

  function addRule() {
    rules = [...rules, blankRule()];
    showRules = true;
  }
  function removeRule(i: number) {
    rules = rules.filter((_, idx) => idx !== i);
  }

  async function saveRules() {
    // Drop blank rows so an empty editor row can't fail validation.
    const clean = rules.filter((r) => r.pattern.trim() && r.reply.trim());
    savingRules = true;
    const ok = await api.saveSmsAutoReply({ enabled: autoEnabled, rules: clean });
    savingRules = false;
    if (ok) {
      rules = clean.map((r) => ({ ...r }));
      toast("Auto-reply rules saved", "ok");
    }
  }

  async function load() {
    const data = await api.sms(PAGE_SIZE, page * PAGE_SIZE);
    messages = data.results;
    total = data.total;
  }
  function goPage(p: number) {
    page = Math.max(0, Math.min(p, Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)));
    load();
  }
  $: pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $: rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  $: rangeEnd = Math.min(total, (page + 1) * PAGE_SIZE);

  async function send() {
    if (!number || !text) {
      toast("number and message are required", "error");
      return;
    }
    if (await api.cmd("send-sms", { number, text })) {
      toast(`sending to ${number}`, "ok");
      text = "";
      setTimeout(load, 1500);
    }
  }

  async function del(id: number) {
    await api.cmd("delete-sms", { row_id: id });
    setTimeout(load, 800);
  }

  async function clearAll() {
    const ok = await confirmDialog({
      title: "Clear all messages",
      message: "Delete ALL messages stored on the modem? This can't be undone.",
      confirmLabel: "Clear all", danger: true,
    });
    if (!ok) return;
    await api.cmd("clear-sms");
    setTimeout(load, 1000);
  }

  async function refresh() {
    await api.cmd("refresh-sms");
    setTimeout(load, 1200);
  }

  onMount(() => {
    load();
    loadRules();
    api.cmd("refresh-sms");    // pull straight from the modem when the page opens
    api.cmd("mark-sms-read");  // viewing the inbox clears the unread badge
    // Read the (DB-backed) inbox every 5s; force a fresh modem pull every ~15s
    // while the page is open. Only auto-reload page 0 so paging back doesn't jump.
    let n = 0;
    const t = setInterval(() => {
      n += 1;
      if (n % 3 === 0) api.cmd("refresh-sms");
      if (page === 0) load();
    }, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>SMS</h1>
  <button class="ui-btn ui-btn-sm" on:click={refresh}>Refresh</button>
  <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={clearAll}>Clear all</button>
</div>

<section class="ui-card">
  <h2>Send a message</h2>
  <div class="row">
    <input class="ui-input" style="max-width:200px" placeholder="+12025550123" bind:value={number} />
    <input class="ui-input" style="flex:1;min-width:240px" placeholder="Message text" bind:value={text} />
    <button class="ui-btn ui-btn-primary" on:click={send}>Send</button>
  </div>
</section>

<section class="ui-card">
  <div class="row">
    <h2 style="flex:1">Auto-reply</h2>
    <label class="toggle">
      <input type="checkbox" bind:checked={autoEnabled} /> <span>Enabled</span>
    </label>
    <button class="ui-btn ui-btn-sm" class:on={showRules} on:click={() => (showRules = !showRules)}>
      {showRules ? "Hide" : "Show"} rules ({rules.length})
    </button>
  </div>
  <p class="muted hint">
    When an inbound SMS matches a rule, the device automatically texts the sender back.
    Rules are tried top-to-bottom; the first match wins.
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
            <input class="ui-input pat" placeholder="pattern to match in the message" bind:value={r.pattern} />
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
      <span style="flex:1"></span>
      <button class="ui-btn ui-btn-primary ui-btn-sm" on:click={saveRules} disabled={savingRules}>
        {savingRules ? "Saving…" : "Save auto-reply"}
      </button>
    </div>
  {/if}
</section>

<div class="row">
  <h2 style="flex:1">Inbox</h2>
  {#if total > 0}
    <span class="muted">{rangeStart}–{rangeEnd} of {total}</span>
    <button class="ui-btn ui-btn-sm" disabled={page === 0} on:click={() => goPage(page - 1)}>‹ newer</button>
    <button class="ui-btn ui-btn-sm" disabled={page >= pages - 1} on:click={() => goPage(page + 1)}>older ›</button>
  {/if}
</div>

<table>
  <thead><tr><th>Time</th><th>Dir</th><th>Peer</th><th>Message</th><th></th></tr></thead>
  <tbody>
    {#each messages as m (m.id)}
      <tr>
        <td class="nowrap">{ts(m.ts)}</td>
        <td>
          {#if m.direction === "in"}
            <span class="badge green">in{m.status === "unread" ? " •" : ""}</span>
          {:else}<span class="badge">sent</span>{/if}
        </td>
        <td class="nowrap mono">{m.peer ?? "—"}</td>
        <td class="break">
          {m.body}
          {#if m.ota}<span class="badge blue" title={m.ota}>eUICC/OTA</span>{/if}
          {#if m.encoding === "8bit"}<span class="badge amber">binary</span>{/if}
          {#if m.parts > 1}<span class="badge">{m.parts} parts</span>{/if}
        </td>
        <td><button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => del(m.id)}>Delete</button></td>
      </tr>
    {:else}
      <tr><td colspan="5" class="muted">No messages. They sync from the modem automatically.</td></tr>
    {/each}
  </tbody>
</table>
<p class="muted">
  Binary/OTA (class-2) messages are shown as hex; multi-part messages are
  reassembled. Messages tagged <span class="badge blue">eUICC/OTA</span> are
  SIM-directed carrier traffic (data-download PID, class 2, or a secured
  packet header) — hover the badge for the exact reason; each also logs an
  "ota" event in the Timeline. Note: OTA messages the modem hands straight to
  the SIM (and the SIM's own replies) never reach modem storage — those
  surface only as "ota" Timeline events and URC-console lines when the modem
  reports them.
</p>

<style>
  .hint { font-size: var(--fs-xs, 11px); margin: 2px 0 0; }
  .toggle {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: var(--fs-sm, 13px); color: var(--color-text); white-space: nowrap;
  }
  .toggle.sm { gap: 4px; font-size: var(--fs-xs, 11px); color: var(--color-text-muted); }
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
