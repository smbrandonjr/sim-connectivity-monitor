<script lang="ts">
  import { onMount } from "svelte";
  import { startStatusPolling, status, toggleTheme, currentTheme } from "./lib/stores";
  import { stateClass } from "./lib/format";
  import { api } from "./lib/api";
  import { toast } from "./lib/toast";
  import { confirmDialog } from "./lib/confirm";
  import Toasts from "./lib/Toasts.svelte";
  import ConfirmModal from "./lib/ConfirmModal.svelte";
  import Dashboard from "./views/Dashboard.svelte";
  import Profiles from "./views/Profiles.svelte";
  import Messages from "./views/Messages.svelte";
  import Timeline from "./views/Timeline.svelte";
  import Diagnostics from "./views/Diagnostics.svelte";
  import Monitoring from "./views/Monitoring.svelte";

  const TABS = [
    { id: "dashboard", label: "Dashboard", icon: "dashboard-line", view: Dashboard },
    { id: "profiles", label: "Profiles", icon: "settings-3-line", view: Profiles },
    { id: "messages", label: "Messages", icon: "message-2-line", view: Messages },
    { id: "monitoring", label: "Monitoring", icon: "heart-pulse-line", view: Monitoring },
    { id: "timeline", label: "Timeline", icon: "time-line", view: Timeline },
    { id: "diagnostics", label: "Diagnostics", icon: "terminal-box-line", view: Diagnostics },
  ];

  let route = "dashboard";
  let theme = currentTheme();
  let editingName = false;
  let nameInput = "";

  function startEditName() {
    nameInput = $status?.sim_name ?? "";
    editingName = true;
  }
  async function saveName() {
    editingName = false;
    await api.cmd("set-sim-name", { name: nameInput.trim() });
  }

  function applyHash() {
    const id = location.hash.replace(/^#\/?/, "") || "dashboard";
    route = TABS.some((t) => t.id === id) ? id : "dashboard";
  }

  function go(id: string) {
    location.hash = `#/${id}`;
  }

  function onToggleTheme() {
    toggleTheme();
    theme = currentTheme();
  }

  async function update() {
    const ok = await confirmDialog({
      title: "Update this device",
      message: "Pull the latest code, reinstall, and restart the service on this device? "
        + "It will be briefly offline while it restarts.",
      confirmLabel: "Update & restart",
      danger: true,
    });
    if (ok) await api.cmd("update-app");
  }

  onMount(() => {
    applyHash();
    window.addEventListener("hashchange", applyHash);
    startStatusPolling();
    return () => window.removeEventListener("hashchange", applyHash);
  });

  $: current = TABS.find((t) => t.id === route) ?? TABS[0];
  $: smsCount = $status?.sms_unread ?? 0;

  // Surface new SMS from anywhere with a toast (the nav badge shows the count).
  let prevUnread = -1;
  $: {
    const u = $status?.sms_unread ?? 0;
    if (prevUnread >= 0 && u > prevUnread) {
      toast(`${u - prevUnread} new SMS — see Messages`, "info");
    }
    prevUnread = u;
  }
</script>

<div class="app-bg"></div>
<Toasts />
<ConfirmModal />

<nav class="topbar">
  {#if editingName}
    <input
      class="ui-input brand-edit"
      bind:value={nameInput}
      placeholder="name this SIM"
      autofocus
      on:blur={saveName}
      on:keydown={(e) => { if (e.key === "Enter") saveName(); if (e.key === "Escape") (editingName = false); }}
    />
  {:else}
    <span class="brand" title={$status?.sim_present ? "click to name this SIM" : "sim-monitor"}
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
      {#if t.id === "messages" && smsCount > 0}<span class="badge lime">{smsCount}</span>{/if}
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
  <button class="ui-btn ui-btn-sm" title="Update this device" on:click={update}>
    <i class="ri-download-cloud-2-line"></i>
  </button>
</nav>

<main class="shell">
  <svelte:component this={current.view} />
</main>
