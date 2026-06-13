export function ts(epoch: number | null | undefined): string {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function stateClass(state: string): string {
  if (state === "CONNECTED") return "green";
  if (state === "DEGRADED" || state === "NO_MODEM") return "red";
  if (state === "FALLBACK_TEST") return "amber";
  return "blue";
}
