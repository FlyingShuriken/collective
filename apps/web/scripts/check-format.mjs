import assert from "node:assert/strict";
import { date, decimal, money } from "../src/lib/format.ts";

assert.equal(money("1234567890123456.78"), "RM 1234567890123456.78");
assert.equal(money("0"), "RM 0.00");
assert.equal(money("0.275"), "RM 0.275");
assert.equal(money("-5.5"), "RM -5.50");
assert.equal(money(null), "—");
assert.equal(money("NaN"), "—");
assert.equal(decimal("1000"), "1000");
assert.equal(decimal("0.270000"), "0.27");
assert.equal(date("2026-10-23T18:30:00Z", "en"), "24 Oct 2026");
assert.match(date("2026-10-23T18:30:00Z", "en", true), /2:30/);
console.log("Decimal display and Malaysian date checks passed.");
