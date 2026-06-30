<script lang="ts">
  import { onMount } from "svelte";
  import { status } from "../lib/stores";
  import Sms from "./Sms.svelte";
  import Udp from "./Udp.svelte";
  import Tcp from "./Tcp.svelte";

  // Sub-tabs under the single "Messaging" nav entry. The active sub lives in the
  // URL hash (#/messaging/<sub>) so it survives reload/bookmark, with a
  // localStorage fallback so a bare #/messaging restores the last-used sub.
  const SUBS = [
    { id: "sms", label: "SMS", view: Sms, unread: (s: any) => s?.sms_unread ?? 0 },
    { id: "udp", label: "UDP", view: Udp, unread: (s: any) => s?.udp_unread ?? 0 },
    { id: "tcp", label: "TCP", view: Tcp, unread: (s: any) => s?.tcp_unread ?? 0 },
  ];
  const KEY = "messagingTab";

  let sub = "sms";

  function hashSub(): string {
    return location.hash.replace(/^#\/?/, "").split("/")[1] ?? "";
  }
  function resolveSub(): string {
    const seg = hashSub();
    if (SUBS.some((s) => s.id === seg)) return seg;
    let saved = "";
    try { saved = localStorage.getItem(KEY) || ""; } catch { /* ignore */ }
    return SUBS.some((s) => s.id === saved) ? saved : "sms";
  }
  function syncFromHash() {
    sub = resolveSub();
  }

  function go(id: string) {
    try { localStorage.setItem(KEY, id); } catch { /* ignore */ }
    location.hash = `#/messaging/${id}`; // triggers hashchange -> syncFromHash
  }

  onMount(() => {
    sub = resolveSub();
    // Reflect the resolved sub in the URL so reload/bookmark restores it.
    if (hashSub() !== sub) {
      try { localStorage.setItem(KEY, sub); } catch { /* ignore */ }
      location.hash = `#/messaging/${sub}`;
    }
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  });

  $: current = SUBS.find((s) => s.id === sub) ?? SUBS[0];
</script>

<div class="subnav">
  {#each SUBS as s}
    <button class="subtab" class:active={sub === s.id} on:click={() => go(s.id)}>
      {s.label}
      {#if s.unread($status) > 0}<span class="badge lime">{s.unread($status)}</span>{/if}
    </button>
  {/each}
</div>

<svelte:component this={current.view} />

<style>
  .subnav {
    display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap;
    border-bottom: 1px solid var(--color-border, #333);
  }
  .subtab {
    background: none; border: none; cursor: pointer;
    padding: 8px 16px; margin-bottom: -1px;
    color: var(--color-text-muted); font-size: var(--fs-sm, 13px);
    border-bottom: 2px solid transparent;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .subtab:hover { color: var(--color-text); }
  .subtab.active {
    color: var(--color-text);
    border-bottom-color: var(--color-accent, #a3e635);
  }
</style>
