<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";

  let data: any = { profiles: [], errors: [], active: null, forced: null };
  let editing: { name: string | null; yaml: string } | null = null;

  const NEW_TEMPLATE = `name: my-profile
description: ""
match:
  iccid_patterns: ["8944500*"]
  priority: 100
pdp_contexts:
  - cid: 1
    apn: hologram
    pdp_type: IPv4
    bearer: true
routing:
  make_default: true
  metric: 50
monitor:
  enabled: false
`;

  async function load() {
    data = await api.profiles();
  }

  async function openNew() {
    editing = { name: null, yaml: NEW_TEMPLATE };
  }
  async function openEdit(name: string) {
    const p = await api.profileYaml(name);
    editing = { name, yaml: p.yaml };
  }
  async function save() {
    if (!editing) return;
    if (await api.saveProfile(editing.yaml, editing.name ?? undefined)) {
      toast("profile saved", "ok");
      editing = null;
      setTimeout(load, 600);
    }
  }
  async function del(name: string) {
    if (!confirm(`Delete profile ${name}?`)) return;
    await api.deleteProfile(name);
    setTimeout(load, 400);
  }
  async function force(name: string) {
    await api.cmd("force-profile", { name });
    toast(`forcing ${name}`, "ok");
    setTimeout(load, 600);
  }
  async function release() {
    await api.cmd("release-force");
    setTimeout(load, 600);
  }

  onMount(load);
</script>

<div class="row">
  <h1>Profiles</h1>
  <button class="ui-btn ui-btn-primary" on:click={openNew}>New profile</button>
</div>

{#if data.forced}
  <div class="ui-card alert">
    <strong>{data.forced}</strong> is forced — automatic SIM matching is paused.
    <button class="ui-btn ui-btn-sm" on:click={release}>Release</button>
  </div>
{/if}

{#each data.errors as e}
  <div class="ui-card alert">{e.file}: {e.error}</div>
{/each}

{#if editing}
  <section class="ui-card">
    <h2>{editing.name ? `Edit ${editing.name}` : "New profile"}</h2>
    <textarea class="ui-textarea" rows="20" bind:value={editing.yaml}></textarea>
    <div class="row" style="margin-top:8px">
      <button class="ui-btn ui-btn-primary" on:click={save}>Save</button>
      <button class="ui-btn" on:click={() => (editing = null)}>Cancel</button>
    </div>
    <p class="muted">1–3 PDP contexts, unique CIDs, exactly one <code>bearer: true</code>. Optional
      <code>pdp_variants</code> are tried in order until one connects.</p>
  </section>
{/if}

<table>
  <thead><tr><th>Name</th><th>Applies to</th><th>Priority</th><th>Contexts</th><th>Heartbeat</th><th></th></tr></thead>
  <tbody>
    {#each data.profiles as p}
      <tr>
        <td>
          <strong>{p.name}</strong>
          {#if p.name === data.active}<span class="badge green">in use</span>{/if}
          {#if p.name === data.forced}<span class="badge amber">forced</span>{/if}
          {#if p.description}<div class="muted">{p.description}</div>{/if}
        </td>
        <td class="mono">{p.iccid_patterns.includes("*") && p.iccid_patterns.length === 1 ? "any SIM" : p.iccid_patterns.join(", ")}</td>
        <td class="mono">{p.priority}</td>
        <td>
          {#each p.contexts as c}<div class="mono">cid {c.cid} · {c.apn} · {c.pdp_type}{#if c.bearer} <span class="badge green">data</span>{/if}</div>{/each}
          {#if p.variants > 0}<span class="badge">{p.variants} variant(s)</span>{/if}
        </td>
        <td>{p.monitor_enabled ? "on" : "—"}</td>
        <td class="nowrap">
          {#if p.name !== data.forced}
            <button class="ui-btn ui-btn-sm" on:click={() => force(p.name)}>Force</button>
          {/if}
          <button class="ui-btn ui-btn-sm" on:click={() => openEdit(p.name)}>Edit</button>
          <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => del(p.name)}>Delete</button>
        </td>
      </tr>
    {:else}
      <tr><td colspan="6" class="muted">No profiles.</td></tr>
    {/each}
  </tbody>
</table>
