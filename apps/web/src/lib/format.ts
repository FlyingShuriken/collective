export function money(value: string | null | undefined) {
  return value == null ? "—" : `RM ${value}`;
}

export function decimal(value: string) {
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}
