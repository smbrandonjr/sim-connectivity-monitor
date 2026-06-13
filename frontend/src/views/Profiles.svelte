<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { toast } from "../lib/toast";
  import { confirmDialog } from "../lib/confirm";
  import ContextEditor from "../lib/ContextEditor.svelte";

  let data: any = { profiles: [], errors: [], active: null, forced: null };
  let editor: null | {
    original: string | null; // existing profile name, or null for new
    name: string; description: string;
    patterns: string; priority: number;
    contexts: any[]; variants: { name: string; contexts: any[] }[];
    at_init: string; make_default: boolean; metric: number;
    raw: boolean; yaml: string;
  } = null;

  function blankContext() {
    return [{ cid: 1, apn: "hologram", pdp_type: "IPv4", auth: "none", username: "", password: "", bearer: true }];
  }

  function openNew() {
    editor = {
      original: null, name: "", description: "", patterns: "8944500*", priority: 100,
      contexts: blankContext(), variants: [], at_init: "",
      make_default: true, metric: 50, raw: false, yaml: "",
    };
  }

  async function openEdit(name: string) {
    const res = await api.profile(name);
    const p = res.profile;
    editor = {
      original: name,
      name: p?.name ?? name,
      description: p?.description ?? "",
      patterns: (p?.match?.iccid_patterns ?? []).join("\n"),
      priority: p?.match?.priority ?? 100,
      contexts: p?.pdp_contexts ?? blankContext(),
      variants: (p?.pdp_variants ?? []).map((v: any) => ({ name: v.name, contexts: v.pdp_contexts })),
      at_init: (p?.at_init ?? []).join("\n"),
      make_default: p?.routing?.make_default ?? true,
      metric: p?.routing?.metric ?? 50,
      raw: false, yaml: res.yaml,
    };
  }

  function lines(s: string): string[] {
    return s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  }

  function buildProfile() {
    const e = editor!;
    return {
      name: e.name,
      description: e.description,
      match: { iccid_patterns: lines(e.patterns), priority: e.priority },
      pdp_contexts: e.contexts,
      pdp_variants: e.variants.filter((v) => v.contexts.length).map((v) => ({
        name: v.name, pdp_contexts: v.contexts,
      })),
      at_init: lines(e.at_init),
      routing: { make_default: e.make_default, metric: e.metric },
    };
  }

  async function save() {
    const e = editor!;
    const body = e.raw ? { yaml: e.yaml } : { profile: buildProfile() };
    if (await api.saveProfile(body, e.original ?? undefined)) {
      toast("profile saved", "ok");
      editor = null;
      setTimeout(load, 600);
    }
  }

  function addVariant() {
    editor!.variants = [...editor!.variants, { name: "", contexts: blankContext() }];
  }
  function removeVariant(i: number) {
    editor!.variants = editor!.variants.filter((_, idx) => idx !== i);
  }

  async function load() { data = await api.profiles(); }
  async function del(name: string) {
    const ok = await confirmDialog({
      title: "Delete profile",
      message: `Delete profile "${name}"? This can't be undone.`,
      confirmLabel: "Delete", danger: true,
    });
    if (!ok) return;
    await api.deleteProfile(name);
    setTimeout(load, 400);
  }
  async function force(name: string) {
    await api.cmd("force-profile", { name });
    toast(`forcing ${name}`, "ok");
    setTimeout(load, 600);
  }
  async function release() { await api.cmd("release-force"); setTimeout(load, 600); }

  let fileInput: HTMLInputElement;
  async function onImportFile(e: Event) {
    const file = (e.target as HTMLInputElement).files?.[0];
    if (!file) return;
    let bundle: unknown;
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      toast("not a valid JSON export file", "error");
      return;
    }
    const result = await api.importProfiles(bundle);
    if (result) {
      toast(`imported ${result.imported} profile(s)` +
        (result.errors.length ? `, ${result.errors.length} skipped` : ""),
        result.errors.length ? "info" : "ok");
      for (const er of result.errors) toast(`skipped ${er.name}: ${er.error}`, "error");
      setTimeout(load, 600);
    }
    fileInput.value = "";  // allow re-importing the same file
  }

  onMount(load);
</script>

<div class="row">
  <h1>Profiles</h1>
  {#if !editor}
    <button class="ui-btn ui-btn-primary" on:click={openNew}>New profile</button>
    <span class="nav-spacer"></span>
    <a class="ui-btn ui-btn-sm" href="/api/profiles/export.json" title="Download all profiles as a JSON bundle">Export all</a>
    <button class="ui-btn ui-btn-sm" on:click={() => fileInput.click()} title="Import profiles from an exported bundle">Import</button>
    <input type="file" accept="application/json,.json" bind:this={fileInput} on:change={onImportFile} style="display:none" />
  {/if}
</div>
{#if !editor}
  <p class="muted">Export carries every profile (including any APN credentials) — keep the file
    private; it's meant for copying your set onto other monitors, not for git. Import adds new
    profiles and overwrites same-named ones.</p>
{/if}

{#if data.forced}
  <div class="ui-card alert">
    <strong>{data.forced}</strong> is forced — automatic SIM matching is paused.
    <button class="ui-btn ui-btn-sm" on:click={release}>Release</button>
  </div>
{/if}
{#each data.errors as e}<div class="ui-card alert">{e.file}: {e.error}</div>{/each}

{#if editor}
  <section class="ui-card">
    <div class="row">
      <h2 style="flex:1">{editor.original ? `Edit ${editor.original}` : "New profile"}</h2>
      <label class="muted"><input type="checkbox" bind:checked={editor.raw} /> edit raw YAML</label>
    </div>

    {#if editor.raw}
      <textarea class="ui-textarea" rows="22" bind:value={editor.yaml}></textarea>
    {:else}
      <div class="row">
        <label class="muted">Name <input class="ui-input" style="display:inline-block;width:180px" bind:value={editor.name} /></label>
        <label class="muted" style="flex:1">Description <input class="ui-input" bind:value={editor.description} /></label>
      </div>

      <h2 style="margin-top:14px">Applies to (ICCID patterns)</h2>
      <textarea class="ui-textarea" rows="3" bind:value={editor.patterns}
        placeholder={"8944500*\nor a full ICCID, one per line; * = any SIM"}></textarea>
      <label class="muted">Priority (lower wins ties) <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={editor.priority} /></label>

      <h2 style="margin-top:14px">PDP contexts</h2>
      <ContextEditor bind:contexts={editor.contexts} group="main-bearer" />

      <h2 style="margin-top:14px">Routing</h2>
      <div class="row">
        <label><input type="checkbox" bind:checked={editor.make_default} /> make cellular the default route</label>
        <label class="muted">metric <input class="ui-input" style="width:80px;display:inline-block" type="number" bind:value={editor.metric} /></label>
      </div>

      <h2 style="margin-top:14px">AT init commands</h2>
      <textarea class="ui-textarea" rows="3" bind:value={editor.at_init}
        placeholder={'one AT command per line, e.g. AT+QCFG="nwscanmode",0'}></textarea>

      <details style="margin-top:12px">
        <summary class="muted">Advanced: alternative PDP variants (tried in order until one connects)</summary>
        {#each editor.variants as v, i}
          <div class="ui-card" style="margin-top:8px">
            <div class="row">
              <label class="muted">Variant name <input class="ui-input" style="display:inline-block;width:160px" bind:value={v.name} /></label>
              <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => removeVariant(i)}>remove variant</button>
            </div>
            <ContextEditor bind:contexts={v.contexts} group={"variant-" + i} />
          </div>
        {/each}
        <button class="ui-btn ui-btn-sm" on:click={addVariant} style="margin-top:8px">+ variant</button>
      </details>
    {/if}

    <div class="row" style="margin-top:12px">
      <button class="ui-btn ui-btn-primary" on:click={save}>Save</button>
      <button class="ui-btn" on:click={() => (editor = null)}>Cancel</button>
    </div>
  </section>
{/if}

{#if !editor}
<table>
  <thead><tr><th>Name</th><th>Applies to</th><th>Priority</th><th>Contexts</th><th></th></tr></thead>
  <tbody>
    {#each data.profiles as p}
      <tr>
        <td>
          <strong>{p.name}</strong>
          {#if p.name === data.active}<span class="badge green">in use</span>{/if}
          {#if p.name === data.forced}<span class="badge amber">forced</span>{/if}
          {#if p.description}<div class="muted">{p.description}</div>{/if}
        </td>
        <td class="mono">{p.iccid_patterns.length === 1 && p.iccid_patterns[0] === "*" ? "any SIM" : p.iccid_patterns.join(", ")}</td>
        <td class="mono">{p.priority}</td>
        <td>
          {#each p.contexts as c}<div class="mono">cid {c.cid} · {c.apn} · {c.pdp_type}{#if c.bearer} <span class="badge green">data</span>{/if}</div>{/each}
          {#if p.variants > 0}<span class="badge">{p.variants} variant(s)</span>{/if}
        </td>
        <td class="nowrap">
          {#if p.name !== data.forced}<button class="ui-btn ui-btn-sm" on:click={() => force(p.name)}>Force</button>{/if}
          <button class="ui-btn ui-btn-sm" on:click={() => openEdit(p.name)}>Edit</button>
          <button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => del(p.name)}>Delete</button>
        </td>
      </tr>
    {:else}
      <tr><td colspan="5" class="muted">No profiles.</td></tr>
    {/each}
  </tbody>
</table>
{/if}
