<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { confirmDialog } from "../lib/confirm";
  import { ts } from "../lib/format";

  let messages: any[] = [];
  let number = "";
  let text = "";

  async function load() {
    messages = await api.sms();
  }

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
    api.cmd("mark-sms-read");  // viewing the inbox clears the unread badge
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="row">
  <h1>Messages</h1>
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
<p class="muted">Binary/OTA (class-2) messages are shown as hex; multi-part messages are reassembled.</p>
