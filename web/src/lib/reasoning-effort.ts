/**
 * Pure reasoning-effort helpers shared by the dashboard ReasoningPicker.
 *
 * Server-provided capabilities are authoritative when present. The defaults
 * below are only a fallback for older backends that do not return
 * capabilities.reasoning_efforts.
 */

export interface EffortOption {
  value: string;
  label: string;
}

export const DEFAULT_REASONING_EFFORTS: ReadonlyArray<string> = [
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
];

const KNOWN_EFFORTS: ReadonlySet<string> = new Set([
  "none",
  ...DEFAULT_REASONING_EFFORTS,
  "max",
]);

const EFFORT_LABELS: Record<string, string> = {
  none: "Off (no thinking)",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "XHigh",
  max: "Max",
};

export const EFFORT_OPTIONS: ReadonlyArray<EffortOption> = effortOptionsFor();

export const VALID_EFFORTS: ReadonlySet<string> = new Set(
  EFFORT_OPTIONS.map((o) => o.value),
);

export function effortLabel(value: string): string {
  return EFFORT_LABELS[value] ?? value;
}

export function normalizeSupportedEfforts(
  raw?: readonly unknown[],
): string[] {
  const source = Array.isArray(raw) && raw.length > 0 ? raw : DEFAULT_REASONING_EFFORTS;
  const seen = new Set<string>();
  const values: string[] = [];

  for (const item of source) {
    const value = String(item ?? "").trim().toLowerCase();
    if (!value || value === "none" || !KNOWN_EFFORTS.has(value) || seen.has(value)) {
      continue;
    }
    seen.add(value);
    values.push(value);
  }

  return values.length > 0 ? values : [...DEFAULT_REASONING_EFFORTS];
}

export function effortOptionsFor(
  supportedEfforts?: readonly unknown[],
  includeNone = true,
): EffortOption[] {
  const values = normalizeSupportedEfforts(supportedEfforts);
  const withNone = includeNone ? ["none", ...values] : values;

  return withNone.map((value) => ({ value, label: effortLabel(value) }));
}

/** Normalize a raw `agent.reasoning_effort` config value to a selectable
 * option. Empty/unknown -> `medium` when supported, otherwise the first
 * supported effort. */
export function normalizeEffort(
  raw: unknown,
  supportedEfforts?: readonly unknown[],
): string {
  const value = String(raw ?? "").trim().toLowerCase();
  const efforts = normalizeSupportedEfforts(supportedEfforts);
  const allowed = new Set(["none", ...efforts]);

  if (value && allowed.has(value)) {
    return value;
  }

  if (allowed.has("medium")) {
    return "medium";
  }

  return efforts[0] ?? "none";
}
