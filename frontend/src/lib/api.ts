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
  monitorHistory: () => getJSON<any[]>("/api/monitor.json"),
  profiles: () => getJSON<any>("/api/profiles.json"),
  profileYaml: (name: string) => getJSON<{ name: string; yaml: string }>(
    `/api/profiles/${encodeURIComponent(name)}.json`,
  ),

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

  async saveProfile(yamlText: string, name?: string): Promise<boolean> {
    const res = await fetch(name ? `/api/profiles/${encodeURIComponent(name)}` : "/api/profiles", {
      method: name ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: yamlText }),
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
};
