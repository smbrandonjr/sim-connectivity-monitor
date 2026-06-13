<script lang="ts">
  import { confirmStore } from "./confirm";

  function done(ok: boolean) {
    const req = $confirmStore;
    confirmStore.set(null);
    req?.resolve(ok);
  }

  function onKey(e: KeyboardEvent) {
    if (!$confirmStore) return;
    if (e.key === "Escape") done(false);
    if (e.key === "Enter") done(true);
  }
</script>

<svelte:window on:keydown={onKey} />

{#if $confirmStore}
  <div class="modal-overlay" on:click|self={() => done(false)} role="presentation">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="modal-header">{$confirmStore.title}</div>
      <div class="modal-body">{$confirmStore.message}</div>
      <div class="modal-footer">
        <button class="ui-btn" on:click={() => done(false)}>{$confirmStore.cancelLabel}</button>
        <button class="ui-btn {$confirmStore.danger ? 'ui-btn-danger' : 'ui-btn-primary'}"
                on:click={() => done(true)}>{$confirmStore.confirmLabel}</button>
      </div>
    </div>
  </div>
{/if}
