// Signal-quality metadata for the LTE telemetry metrics shown on the dashboard:
// what each metric means, the value bands that count as good/bad, and a
// classifier that maps a reading to a quality tier (and colour). Thresholds
// follow common LTE field-service conventions; all four are "higher is better"
// (less negative for the dBm/dB power metrics).

export type Tier = "excellent" | "good" | "fair" | "poor";

const TIER_COLORS: Record<Tier, string> = {
  excellent: "var(--status-cyan)",
  good: "var(--status-green)",
  fair: "var(--status-amber)",
  poor: "var(--status-red)",
};

export function tierColor(tier: Tier): string {
  return TIER_COLORS[tier];
}

export interface Band {
  tier: Tier;
  label: string; // "Excellent", "Good", …
  range: string; // human range, e.g. "≥ -80 dBm"
}

export interface MetricInfo {
  key: string;
  label: string;
  unit: string;
  what: string; // one-line plain-English description
  // Descending thresholds: a value >= excellent is excellent, >= good is good,
  // >= fair is fair, otherwise poor.
  thresholds: { excellent: number; good: number; fair: number };
  bands: Band[];
}

export function classify(m: MetricInfo, value: number): Tier {
  const t = m.thresholds;
  if (value >= t.excellent) return "excellent";
  if (value >= t.good) return "good";
  if (value >= t.fair) return "fair";
  return "poor";
}

export const METRICS: MetricInfo[] = [
  {
    key: "rsrp",
    label: "RSRP",
    unit: "dBm",
    what: "Reference Signal Received Power — the raw strength of the tower's LTE reference signal. The primary “how strong is the signal” number.",
    thresholds: { excellent: -80, good: -90, fair: -100 },
    bands: [
      { tier: "excellent", label: "Excellent", range: "≥ -80 dBm" },
      { tier: "good", label: "Good", range: "-80 to -90 dBm" },
      { tier: "fair", label: "Fair", range: "-90 to -100 dBm" },
      { tier: "poor", label: "Poor", range: "< -100 dBm" },
    ],
  },
  {
    key: "rsrq",
    label: "RSRQ",
    unit: "dB",
    what: "Reference Signal Received Quality — signal quality accounting for interference and load. Tells you how clean the signal is, not just how strong.",
    thresholds: { excellent: -10, good: -15, fair: -20 },
    bands: [
      { tier: "excellent", label: "Excellent", range: "≥ -10 dB" },
      { tier: "good", label: "Good", range: "-10 to -15 dB" },
      { tier: "fair", label: "Fair", range: "-15 to -20 dB" },
      { tier: "poor", label: "Poor", range: "< -20 dB" },
    ],
  },
  {
    key: "sinr",
    label: "SINR",
    unit: "dB",
    what: "Signal-to-Interference-plus-Noise Ratio — how far the signal rises above noise/interference. The best predictor of usable throughput.",
    thresholds: { excellent: 20, good: 13, fair: 0 },
    bands: [
      { tier: "excellent", label: "Excellent", range: "≥ 20 dB" },
      { tier: "good", label: "Good", range: "13 to 20 dB" },
      { tier: "fair", label: "Fair", range: "0 to 13 dB" },
      { tier: "poor", label: "Poor", range: "< 0 dB" },
    ],
  },
  {
    key: "rssi",
    label: "RSSI",
    unit: "dBm",
    what: "Received Signal Strength Indicator — total received power across the band (signal + noise + interference). A rough overall strength gauge.",
    thresholds: { excellent: -65, good: -75, fair: -85 },
    bands: [
      { tier: "excellent", label: "Excellent", range: "≥ -65 dBm" },
      { tier: "good", label: "Good", range: "-65 to -75 dBm" },
      { tier: "fair", label: "Fair", range: "-75 to -85 dBm" },
      { tier: "poor", label: "Poor", range: "< -85 dBm" },
    ],
  },
];
