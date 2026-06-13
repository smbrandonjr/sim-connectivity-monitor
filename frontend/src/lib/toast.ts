import { writable } from "svelte/store";

export interface Toast {
  id: number;
  message: string;
  kind: "ok" | "error" | "info";
}

export const toasts = writable<Toast[]>([]);
let nextId = 1;

export function toast(message: string, kind: Toast["kind"] = "info"): void {
  const id = nextId++;
  toasts.update((t) => [...t, { id, message, kind }]);
  const ttl = kind === "error" ? 7000 : 3500;
  setTimeout(() => toasts.update((t) => t.filter((x) => x.id !== id)), ttl);
}
