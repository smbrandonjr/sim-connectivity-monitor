// Thin client for the sim-monitor JSON API.
import { toast } from "./toast";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export const api = {
  status: () => getJSON<any>("/api/status.json"),
  timeline: (params: { source?: string; kind?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.source) q.set("source", params.source);
    if (params.kind) q.set("kind", params.kind);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return getJSON<{ rows: any[]; total: number; kinds: string[]; limit: number; offset: number }>(
      `/api/timeline.json?${q.toString()}`,
    );
  },
  urcs: (after?: number) =>
    getJSON<any[]>(after != null ? `/api/urcs.json?after=${after}` : "/api/urcs.json"),
  identity: () => getJSON<any[]>("/api/identity.json"),
  events: () => getJSON<any[]>("/api/events.json"),
  sms: (limit = 25, offset = 0) =>
    getJSON<{ results: any[]; total: number; limit: number; offset: number }>(
      `/api/sms.json?limit=${limit}&offset=${offset}`,
    ),
  telemetry: () => getJSON<{ latest: any; history: any[] }>("/api/telemetry.json"),
  monitorHistory: (limit = 25, offset = 0) =>
    getJSON<{ results: any[]; total: number; limit: number; offset: number }>(
      `/api/monitor.json?limit=${limit}&offset=${offset}`,
    ),
  profiles: () => getJSON<any>("/api/profiles.json"),
  profile: (name: string) => getJSON<{ name: string; yaml: string; profile: any }>(
    `/api/profiles/${encodeURIComponent(name)}.json`,
  ),
  connectivity: (from?: number, to?: number) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    return getJSON<any>(`/api/connectivity.json?${q.toString()}`);
  },
  latency: (from?: number, to?: number, iface?: string) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    if (iface) q.set("interface", iface);
    return getJSON<any>(`/api/latency.json?${q.toString()}`);
  },
  latencyCsvUrl: (from?: number, to?: number, iface?: string) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    if (iface) q.set("interface", iface);
    return `/api/latency.csv?${q.toString()}`;
  },
  httpChecks: (from?: number, to?: number, iface?: string) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    if (iface) q.set("interface", iface);
    return getJSON<any>(`/api/http-checks.json?${q.toString()}`);
  },
  httpChecksCsvUrl: (from?: number, to?: number, iface?: string) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    if (iface) q.set("interface", iface);
    return `/api/http-checks.csv?${q.toString()}`;
  },
  monitorConfig: () => getJSON<any>("/api/monitor-config.json"),
  latencyConfig: () => getJSON<any>("/api/latency-config.json"),
  httpCheckConfig: () => getJSON<any>("/api/http-checks-config.json"),

  async saveLatencyConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/latency-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },

  async saveHttpCheckConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/http-checks-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },
  smsAutoReply: () => getJSON<any>("/api/sms-autoreply.json"),

  async saveSmsAutoReply(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/sms-autoreply", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },

  udp: (limit = 25, offset = 0) =>
    getJSON<{ results: any[]; total: number; limit: number; offset: number }>(
      `/api/udp.json?limit=${limit}&offset=${offset}`,
    ),
  udpConfig: () => getJSON<any>("/api/udp-config.json"),

  async saveUdpConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/udp-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },
  clearUdp: () => fetch("/api/udp/clear", { method: "POST" }),
  deleteUdp: (id: number) =>
    fetch("/api/udp/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }),
  markUdpRead: () => fetch("/api/udp/mark-read", { method: "POST" }),

  tcp: (limit = 25, offset = 0) =>
    getJSON<{ results: any[]; total: number; limit: number; offset: number }>(
      `/api/tcp.json?limit=${limit}&offset=${offset}`,
    ),
  tcpConfig: () => getJSON<any>("/api/tcp-config.json"),

  async saveTcpConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/tcp-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },
  clearTcp: () => fetch("/api/tcp/clear", { method: "POST" }),
  deleteTcp: (id: number) =>
    fetch("/api/tcp/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }),
  markTcpRead: () => fetch("/api/tcp/mark-read", { method: "POST" }),

  trafficFlows: (params: {
    from?: number; to?: number; ip?: string; port?: number | null;
    proto?: string; direction?: string; interface?: string; active?: boolean;
    limit?: number; offset?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.from != null) q.set("from", String(Math.floor(params.from)));
    if (params.to != null) q.set("to", String(Math.floor(params.to)));
    if (params.ip) q.set("ip", params.ip);
    if (params.port != null) q.set("port", String(params.port));
    if (params.proto) q.set("proto", params.proto);
    if (params.direction) q.set("direction", params.direction);
    if (params.interface) q.set("interface", params.interface);
    if (params.active != null) q.set("active", params.active ? "1" : "0");
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return getJSON<{ flows: any[]; total: number; server_time: number }>(
      `/api/traffic/flows.json?${q.toString()}`,
    );
  },
  trafficSummary: (from?: number, to?: number) => {
    const q = new URLSearchParams();
    if (from != null) q.set("from", String(Math.floor(from)));
    if (to != null) q.set("to", String(Math.floor(to)));
    return getJSON<any>(`/api/traffic/summary.json?${q.toString()}`);
  },
  trafficConfig: () => getJSON<any>("/api/traffic-config.json"),

  async saveTrafficConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/traffic-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },

  placeholders: () => getJSON<Record<string, any>>("/api/placeholders.json"),
  scanStatus: () => getJSON<any>("/api/scan.json"),
  scanInterfaces: () => getJSON<any[]>("/api/scan/interfaces.json"),

  async scanStart(kind: string, body: Record<string, unknown>): Promise<boolean> {
    const res = await fetch(`/api/scan/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "scan failed to start", "error");
      return false;
    }
    return true;
  },
  scanStop: () => fetch("/api/scan/stop", { method: "POST" }),

  async saveMonitorConfig(cfg: Record<string, unknown>): Promise<boolean> {
    const res = await fetch("/api/monitor-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },

  async cmd(name: string, body?: Record<string, unknown>): Promise<boolean> {
    const res = await fetch(`/api/cmd/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || `command ${name} failed`, "error");
      return false;
    }
    if (data.message) toast(data.message, "ok");
    return true;
  },

  async saveProfile(body: { profile?: any; yaml?: string }, name?: string): Promise<boolean> {
    const res = await fetch(name ? `/api/profiles/${encodeURIComponent(name)}` : "/api/profiles", {
      method: name ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "save failed", "error");
      return false;
    }
    return true;
  },

  async deleteProfile(name: string): Promise<boolean> {
    const res = await fetch(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    return res.ok;
  },

  async importProfiles(bundle: unknown): Promise<{ imported: number; errors: any[] } | null> {
    const res = await fetch("/api/profiles/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bundle),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "import failed", "error");
      return null;
    }
    return data;
  },
};
