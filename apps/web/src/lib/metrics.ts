/** Parses the API's Prometheus text exposition (`name value` lines) into numbers. */
export function parseMetrics(text: string): Record<string, number> {
  const values: Record<string, number> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(-?[0-9.eE+-]+|NaN|[+-]Inf)$/.exec(trimmed);
    if (!match) continue;
    const value = Number(match[2]);
    if (Number.isFinite(value)) values[match[1] as string] = value;
  }
  return values;
}
