<script lang="ts">
  import { onMount } from "svelte";
  import { startStatusPolling, status, toggleTheme, currentTheme } from "./lib/stores";
  import { stateClass } from "./lib/format";
  import { api } from "./lib/api";
  import { toast } from "./lib/toast";
  import Toasts from "./lib/Toasts.svelte";
  import Dashboard from "./views/Dashboard.svelte";
  import Profiles from "./views/Profiles.svelte";
  import Messages from "./views/Messages.svelte";
  import Telemetry from "./views/Telemetry.svelte";
  import Timeline from "./views/Timeline.svelte";
  import Diagnostics from "./views/Diagnostics.svelte";
  import Events from "./views/Events.svelte";

  const TABS = [
    { id: "dashboard", label: "Dashboard", icon: "dashboard-line", view: Dashboard },
    { id: "profiles", label: "Profiles", icon: "settings-3-line", view: Profiles },
    { id: "messages", label: "Messages", icon: "message-2-line", view: Messages },
    { id: "telemetry", label: "Telemetry", icon: "line-chart-line", view: Telemetry },
    { id: "timeline", label: "Timeline", icon: "time-line", view: Timeline },
    { id: "diagnostics", label: "Diagnostics", icon: "terminal-box-line", view: Diagnostics },
    { id: "events", label: "Events", icon: "file-list-line", view: Events },
  ];

  let route = "dashboard";
  let theme = currentTheme();

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
    if (!confirm("Pull the latest code and restart the service on this device?")) return;
    await api.cmd("update-app");
  }

  onMount(() => {
    applyHash();
    window.addEventListener("hashchange", applyHash);
    startStatusPolling();
    return () => window.removeEventListener("hashchange", applyHash);
  });

  $: current = TABS.find((t) => t.id === route) ?? TABS[0];
  $: smsCount = $status?.sms_unread ?? 0;
</script>

<div class="app-bg"></div>
<Toasts />

<nav class="topbar">
  <span class="brand">sim-monitor</span>
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
