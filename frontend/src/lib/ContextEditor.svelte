<script lang="ts">
  export let contexts: any[] = [];
  export let group = "bearer"; // radio-group name (unique per editor instance)

  const PDP_TYPES = ["IPv4", "IPv6", "IPv4v6"];
  const AUTHS = ["none", "pap", "chap"];

  function add() {
    if (contexts.length >= 3) return;
    const usedCids = new Set(contexts.map((c) => c.cid));
    let cid = 1;
    while (usedCids.has(cid)) cid++;
    contexts = [...contexts, {
      cid, apn: "", pdp_type: "IPv4", auth: "none",
      username: "", password: "", bearer: contexts.length === 0,
    }];
  }
  function remove(i: number) {
    const wasBearer = contexts[i].bearer;
    contexts = contexts.filter((_, idx) => idx !== i);
    if (wasBearer && contexts.length) contexts[0].bearer = true;
  }
  function setBearer(i: number) {
    contexts = contexts.map((c, idx) => ({ ...c, bearer: idx === i }));
  }
</script>

<table>
  <thead>
    <tr><th>Bearer</th><th>CID</th><th>APN</th><th>Type</th><th>Auth</th><th>User</th><th>Password</th><th></th></tr>
  </thead>
  <tbody>
    {#each contexts as c, i}
      <tr>
        <td><input type="radio" name={group} checked={c.bearer} on:change={() => setBearer(i)} /></td>
        <td><input class="ui-input" style="width:48px" type="number" min="1" max="16" bind:value={c.cid} /></td>
        <td><input class="ui-input" style="min-width:130px" bind:value={c.apn} placeholder="hologram" /></td>
        <td>
          <select class="ui-select" bind:value={c.pdp_type}>
            {#each PDP_TYPES as t}<option>{t}</option>{/each}
          </select>
        </td>
        <td>
          <select class="ui-select" bind:value={c.auth}>
            {#each AUTHS as a}<option>{a}</option>{/each}
          </select>
        </td>
        <td><input class="ui-input" style="width:90px" bind:value={c.username} disabled={c.auth === "none"} /></td>
        <td><input class="ui-input" style="width:90px" type="password" bind:value={c.password} disabled={c.auth === "none"} /></td>
        <td><button class="ui-btn ui-btn-sm ui-btn-danger" on:click={() => remove(i)} disabled={contexts.length <= 1}>×</button></td>
      </tr>
    {/each}
  </tbody>
</table>
<button class="ui-btn ui-btn-sm" on:click={add} disabled={contexts.length >= 3}>+ context</button>
<span class="muted"> · the bearer context's APN drives the data connection (max 3)</span>
