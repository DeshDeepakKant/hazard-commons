// Shared "filter by operator" state (dashboards operator cards -> decision feed + order list).
type L = (op: string | null) => void;
const ls: L[] = [];
export const opFilter = {
  op: null as string | null,
  set(op: string | null): void { this.op = op; ls.forEach((f) => f(op)); },
  toggle(op: string): void { this.set(this.op === op ? null : op); },
  on(f: L): void { ls.push(f); },
};
