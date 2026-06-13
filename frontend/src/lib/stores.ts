import { writable } from "svelte/store";
import { api } from "./api";

// Live status, polled. Components subscribe; the dashboard stays current.
export const status = writable<any>(null);

let timer: number | undefined;

export function startStatusPolling(intervalMs = 3000): void {
  const tick = async () => {
    try {
      status.set(await api.status());
    } catch {
      /* transient; keep last */
    }
  };
  tick();
  timer = window.setInterval(tick, intervalMs);
}

export function stopStatusPolling(): void {
  if (timer) window.clearInterval(timer);
}

export const THEME_KEY = "theme";

export function currentTheme(): "dark" | "light" {
  const t = document.documentElement.dataset.theme;
  return t === "light" ? "light" : "dark";
}

export function toggleTheme(): void {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* ignore */
  }
}
