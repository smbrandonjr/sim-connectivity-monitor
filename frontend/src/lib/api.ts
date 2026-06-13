// Thin client for the sim-monitor JSON API.
import { toast } from "./toast";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export const api = {
  status: () => getJSON<any>("/api/status.json"),
  timeline: () => getJSON<any[]>("/api/timeline.json"),
  urcs: () => getJSON<any[]>("/api/urcs.json"),
  identity: () => getJSON<any[]>("/api/identity.json"),
  events: () => getJSON<any[]>("/api/events.json"),
  sms: () => getJSON<any[]>("/api/sms.json"),
  telemetry: () => getJSON<{ latest: any; history: any[] }>("/api/telemetry.json"),
  monitorHistory: (limit = 25, offset = 0) =>
    getJSON<{ results: any[]; total: number; limit: number; offset: number }>(
      `/api/monitor.json?limit=${limit}&offset=${offset}`,
    ),
  profiles: () => getJSON<any>("/api/profiles.json"),
  profile: (name: string) => getJSON<{ name: string; yaml: string; profile: any }>(
    `/api/profiles/${encodeURIComponent(name)}.json`,
  ),
  monitorConfig: () => getJSON<any>("/api/monitor-config.json"),
  placeholders: () => getJSON<Record<string, any>>("/api/placeholders.json"),

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
