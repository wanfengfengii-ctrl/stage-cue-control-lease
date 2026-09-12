import { describe, expect, it } from "vitest";
import { detectTakeover, remainingSeconds } from "../src/lease";

const T = 1_700_000_000_000; // arbitrary epoch ms

describe("remainingSeconds — server clock, inclusive expiry boundary", () => {
  const state = (expiresOffsetMs: number, serverOffsetMs = 0) => ({
    expires_at: new Date(T + expiresOffsetMs).toISOString(),
    server_time: new Date(T + serverOffsetMs).toISOString(),
  });

  it("counts down anchored to the server clock, not the local clock", () => {
    // fetch happened 10 s ago locally; server said expiry in 30 s.
    expect(remainingSeconds(state(30_000), T + 10_000, T, 30)).toBe(20);
  });

  it("30 s left right after grant", () => {
    expect(remainingSeconds(state(30_000), T, T, 30)).toBe(30);
  });

  it("one millisecond before expiry still shows 1 second", () => {
    expect(remainingSeconds(state(1), T, T, 30)).toBe(1);
  });

  it("exactly at expiry -> 0 (now == expires_at is invalid)", () => {
    expect(remainingSeconds(state(0), T, T, 30)).toBe(0);
  });

  it("after expiry -> 0", () => {
    expect(remainingSeconds(state(-5_000), T, T, 30)).toBe(0);
  });

  it("no lease -> 0", () => {
    expect(
      remainingSeconds({ expires_at: null, server_time: new Date(T).toISOString() }, T, T),
    ).toBe(0);
  });
});

describe("detectTakeover", () => {
  const heldBy = (holder: string | null, status: "held" | "free" = "held") =>
    ({
      status,
      holder,
    } as any);

  it("false while I still hold it", () => {
    expect(detectTakeover(heldBy("A"), "A", true)).toBe(false);
  });

  it("true when another holder appears while my token still exists", () => {
    expect(detectTakeover(heldBy("B"), "A", true)).toBe(true);
  });

  it("true when the lease vanished (released/expired) with my token present", () => {
    expect(detectTakeover(heldBy(null, "free"), "A", true)).toBe(true);
  });

  it("false when I never had a token", () => {
    expect(detectTakeover(heldBy("B"), "A", false)).toBe(false);
  });
});
