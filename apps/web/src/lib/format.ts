export function money(value: string | null | undefined) {
  return value == null ? "—" : `RM ${value}`;
}
