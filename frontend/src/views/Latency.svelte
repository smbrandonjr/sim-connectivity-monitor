<script lang="ts">
  import { api } from "../lib/api";
  import LatencyPanel from "../lib/LatencyPanel.svelte";
</script>

<!-- Two independent monitors share this tab: ICMP ping and HTTP web checks.
     Each has its own config/storage/charts via the reusable LatencyPanel. -->
<LatencyPanel
  kind="ping"
  title="Latency & packet loss (ping)"
  loadData={(f, t, i) => api.latency(f, t, i)}
  csvUrl={(f, t, i) => api.latencyCsvUrl(f, t, i)}
  loadCfg={() => api.latencyConfig()}
  saveCfg={(c) => api.saveLatencyConfig(c)}
/>

<LatencyPanel
  kind="http"
  title="Web checks (HTTP)"
  loadData={(f, t, i) => api.httpChecks(f, t, i)}
  csvUrl={(f, t, i) => api.httpChecksCsvUrl(f, t, i)}
  loadCfg={() => api.httpCheckConfig()}
  saveCfg={(c) => api.saveHttpCheckConfig(c)}
/>
