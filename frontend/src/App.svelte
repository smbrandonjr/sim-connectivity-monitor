<script lang="ts">
  import { onMount } from "svelte";
  import { startStatusPolling, status, toggleTheme, currentTheme } from "./lib/stores";
  import { stateClass } from "./lib/format";
  import { api } from "./lib/api";
  import { toast } from "./lib/toast";
  import Toasts from "./lib/Toasts.svelte";
  import ConfirmModal from "./lib/ConfirmModal.svelte";
  import Dashboard from "./views/Dashboard.svelte";
  import Profiles from "./views/Profiles.svelte";
  import Messaging from "./views/Messaging.svelte";
  import Timeline from "./views/Timeline.svelte";
  import Diagnostics from "./views/Diagnostics.svelte";
  import Monitoring from "./views/Monitoring.svelte";
  import Scan from "./views/Scan.svelte";
  import Latency from "./views/Latency.svelte";

  const TABS = [
    { id: "dashboard", label: "Dashboard", icon: "dashboard-line", view: Dashboard },
    { id: "profiles", label: "Profiles", icon: "settings-3-line", view: Profiles },
    { id: "messaging", label: "Messaging", icon: "message-2-line", view: Messaging },
    { id: "monitoring", label: "Monitoring", icon: "heart-pulse-line", view: Monitoring },
    { id: "latency", label: "Latency", icon: "pulse-line", view: Latency },
    { id: "scan", label: "Scan", icon: "radar-line", view: Scan },
    { id: "timeline", label: "Timeline", icon: "time-line", view: Timeline },
    { id: "diagnostics", label: "Diagnostics", icon: "terminal-box-line", view: Diagnostics },
  ];

  let route = "dashboard";
  let theme = currentTheme();
  let editingName = false;
  let nameInput = "";
  let savingName = false;
  let cancelName = false;

  // autofocus is unreliable on dynamically-inserted inputs; focus + select on mount.
  function focusOnMount(node: HTMLInputElement) {
    node.focus();
    node.select();
  }

  function startEditName() {
    nameInput = $status?.sim_name ?? "";
    editingName = true;
  }
  function cancelEditName() {
    cancelName = true; // suppress the blur-save that unmounting the input triggers
    editingName = false;
  }
  async function saveName() {
    if (cancelName) { cancelName = false; return; }
    if (savingName) return; // Enter unmounts the input -> induced blur; save once
    savingName = true;
    editingName = false;
    const name = nameInput.trim();
    const ok = await api.cmd("set-sim-name", { name });
    // The daemon applies the name out-of-band (queued command); reflect it now
    // instead of waiting up to a poll interval for the next /status refresh.
    if (ok) status.update((s) => (s ? { ...s, sim_name: name || null } : s));
    savingName = false;
  }

  function applyHash() {
    // Route on the first hash segment only, so sub-routes like
    // #/messaging/udp still resolve to the "messaging" top-level tab.
    const id = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/")[0];
    route = TABS.some((t) => t.id === id) ? id : "dashboard";
  }

  function go(id: string) {
    location.hash = `#/${id}`;
  }

  function onToggleTheme() {
    toggleTheme();
    theme = currentTheme();
  }

  onMount(() => {
    applyHash();
    window.addEventListener("hashchange", applyHash);
    startStatusPolling();
    return () => window.removeEventListener("hashchange", applyHash);
  });

  $: current = TABS.find((t) => t.id === route) ?? TABS[0];
  // Combined unread across all messaging channels for the nav badge.
  $: msgCount =
    ($status?.sms_unread ?? 0) + ($status?.udp_unread ?? 0) + ($status?.tcp_unread ?? 0);

  // Surface new messages on any channel with a toast (nav badge shows the count),
  // so the user is alerted even when they're not on that channel's view.
  let prevSms = -1;
  let prevUdp = -1;
  let prevTcp = -1;
  function alertNew(label: string, prev: number, cur: number): number {
    if (prev >= 0 && cur > prev) {
      toast(`${cur - prev} new ${label} message${cur - prev > 1 ? "s" : ""} — see Messaging`, "info");
    }
    return cur;
  }
  $: prevSms = alertNew("SMS", prevSms, $status?.sms_unread ?? 0);
  $: prevUdp = alertNew("UDP", prevUdp, $status?.udp_unread ?? 0);
  $: prevTcp = alertNew("TCP", prevTcp, $status?.tcp_unread ?? 0);
</script>

<svelte:head>
  <title>{$status?.sim_name ? `${$status.sim_name} · sim-monitor` : "sim-monitor"}</title>
</svelte:head>

<div class="app-bg"></div>
<Toasts />
<ConfirmModal />

<nav class="topbar">
  {#if editingName}
    <input
      class="ui-input brand-edit"
      bind:value={nameInput}
      placeholder="name this SIM"
      use:focusOnMount
      on:blur={saveName}
      on:keydown={(e) => { if (e.key === "Enter") saveName(); if (e.key === "Escape") cancelEditName(); }}
    />
  {:else}
    <span class="brand" title={$status?.sim_present ? "click to name this SIM" : "insert a SIM to name it"}
          class:editable={$status?.sim_present}
          on:click={() => $status?.sim_present && startEditName()}
          role="button" tabindex="0">
      {$status?.sim_name || "sim-monitor"}
      {#if $status?.sim_present}<i class="ri-pencil-line edit-hint"></i>{/if}
    </span>
  {/if}
  {#each TABS as t}
    <button class="nav-tab" class:active={route === t.id} on:click={() => go(t.id)}>
      <i class="ri-{t.icon}"></i>{t.label}
      {#if t.id === "messaging" && msgCount > 0}<span class="badge lime">{msgCount}</span>{/if}
    </button>
  {/each}
  <span class="nav-spacer"></span>
  {#if $status}
    <span class="state-badge badge {stateClass($status.state)}">
      <span class="dot {stateClass($status.state)}"></span> {$status.state}
    </span>
  {/if}
  <button class="ui-btn ui-btn-sm" title="Toggle theme" on:click={onToggleTheme}>
    <i class="ri-{theme === 'dark' ? 'sun' : 'moon'}-line"></i>
  </button>
</nav>

<main class="shell">
  <svelte:component this={current.view} />
</main>
