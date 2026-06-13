<script lang="ts">
  export let value: string | number | null | undefined;
  export let label: string | null = null;
  let copied = false;

  async function copy() {
    if (value == null) return;
    try {
      await navigator.clipboard.writeText(String(value));
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }
</script>

<span class="copyable mono" class:copied on:click={copy} title="click to copy" role="button" tabindex="0">
  {label ?? (value ?? "—")}
  {#if value != null}<i class="ri-{copied ? 'check' : 'file-copy'}-line"></i>{/if}
</span>
