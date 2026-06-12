// Dashboard live refresh: poll /api/status.json and update fields in place.
(function () {
  const POLL_MS = 3000;
  const fields = document.querySelectorAll("[data-f]");
  if (!fields.length) return;

  function set(name, value) {
    document.querySelectorAll(`[data-f="${name}"]`).forEach((el) => {
      el.textContent = value;
    });
  }

  function fmt(v, suffix) {
    return v === null || v === undefined || v === "" ? "—" : String(v) + (suffix || "");
  }

  let fallbackUntil = null;

  function renderFallbackCountdown() {
    const banner = document.getElementById("fallback-banner");
    if (!banner) return;
    if (!fallbackUntil) { banner.hidden = true; return; }
    const remaining = Math.max(0, Math.round(fallbackUntil - Date.now() / 1000));
    banner.hidden = false;
    const m = Math.floor(remaining / 60), s = remaining % 60;
    document.getElementById("fallback-remaining").textContent =
      `${m}:${String(s).padStart(2, "0")}`;
  }

  async function refresh() {
    let data;
    try {
      const res = await fetch("/api/status.json", { cache: "no-store" });
      data = await res.json();
    } catch (e) {
      return; // transient; keep last values
    }
    const badge = document.getElementById("state-badge");
    if (badge && badge.textContent !== data.state) {
      badge.textContent = data.state;
      badge.className = "badge state-" + data.state;
    }
    set("vendor", fmt(data.vendor));
    set("model", data.model || "");
    set("imei", fmt(data.imei));
    set("operator", fmt(data.operator));
    set("signal", data.signal_rssi !== null
      ? `${data.signal_rssi} dBm (${data.signal_percent}%)` : "—");
    set("sim_present", data.sim_present ? "yes" : "no");
    set("iccid", fmt(data.iccid));
    set("imsi", fmt(data.imsi));
    set("active_profile", fmt(data.active_profile));
    set("interface", fmt(data.interface));
    set("ip_address", fmt(data.ip_address));
    set("routing_ok", data.routing_ok === null ? "—" : data.routing_ok ? "yes" : "no");
    set("last_error", fmt(data.last_error));
    if (data.last_monitor) {
      set("monitor_ok", data.last_monitor.ok ? "ok" : "FAILED");
      set("monitor_detail",
        `${data.last_monitor.status_code ?? "—"} / ${Math.round(data.last_monitor.latency_ms ?? 0)} ms`);
      set("monitor_error", fmt(data.last_monitor.error));
    }
    fallbackUntil = data.fallback && data.fallback.active ? data.fallback.until : null;
    renderFallbackCountdown();
  }

  setInterval(refresh, POLL_MS);
  setInterval(renderFallbackCountdown, 1000);
  refresh();
})();
