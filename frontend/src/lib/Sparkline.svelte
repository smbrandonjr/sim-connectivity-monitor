<script lang="ts">
  export let values: (number | null)[] = [];
  const W = 280;
  const H = 44;

  $: nums = values.filter((v): v is number => v != null);
  $: lo = nums.length ? Math.min(...nums) : 0;
  $: hi = nums.length ? Math.max(...nums) : 1;
  $: span = hi - lo || 1;
  $: step = values.length > 1 ? W / (values.length - 1) : W;
  $: points = values
    .map((v, i) => (v == null ? null : `${(i * step).toFixed(1)},${(H - (v - lo) / span * (H - 4) - 2).toFixed(1)}`))
    .filter((p): p is string => p != null)
    .join(" ");
</script>

{#if nums.length >= 2}
  <svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none">
    <polyline {points} fill="none" stroke="var(--color-primary)" stroke-width="1.5" />
  </svg>
{:else}
  <p class="muted">not enough data yet</p>
{/if}
