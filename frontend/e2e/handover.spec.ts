import { expect, test, type Browser } from "@playwright/test";

/**
 * Real dual-browser handover against FastAPI + PostgreSQL (no mocks).
 *
 * Seat A and Seat B are two independent browser contexts, like the lifting
 * platform console and the flying-hoist console opened side by side.
 */

const ACTION = "lift_up";

async function openSeat(browser: Browser, seatName: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  const card = page.getByTestId(`action-${ACTION}`);
  await expect(card.getByTestId("badge")).toBeVisible();
  return { context, page, card };
}

test.describe.configure({ mode: "serial" });

test("simultaneous clicks: exactly one seat wins the lease", async ({ browser }) => {
  const a = await openSeat(browser, "升降台席-A");
  const b = await openSeat(browser, "飞行吊点席-B");

  await expect(a.card.getByTestId("badge")).toHaveText("空闲可申请");
  await expect(b.card.getByTestId("badge")).toHaveText("空闲可申请");

  // Both seats click in the same instant; the DB transaction picks one.
  await Promise.all([
    a.card.getByTestId("btn-acquire").click(),
    b.card.getByTestId("btn-acquire").click(),
  ]);

  // Exactly one page shows the holder-only controls...
  await expect
    .poll(async () => {
      const [ca, cb] = await Promise.all([
        a.card.getByTestId("btn-execute").count(),
        b.card.getByTestId("btn-execute").count(),
      ]);
      return ca + cb;
    })
    .toBe(1);
  const holders = await Promise.all([
    a.card.getByTestId("btn-execute").count(),
    b.card.getByTestId("btn-execute").count(),
  ]);

  // ...the loser is told the contention outcome and renders the winner.
  const loser = holders[0] === 0 ? a : b;
  const winner = holders[0] === 1 ? a : b;
  await expect(loser.card.getByTestId("notice")).toContainText(/争抢失败|已由/);

  // Both sessions converge on the same single holder within one poll.
  for (const seat of [a, b]) {
    await expect(seat.card.getByTestId("badge")).toContainText(
      holders[0] === 1 ? "升降台席-A" : "飞行吊点席-B",
    );
  }

  // Winner releases so the following test starts from a free action.
  await winner.card.getByTestId("btn-release").click();
  await expect(winner.card.getByTestId("badge")).toHaveText("空闲可申请");

  await a.context.close();
  await b.context.close();
});

test("offline expiry: the other seat takes over; the stale page is rejected", async ({ browser }) => {
  // Seat A acquires...
  const a = await openSeat(browser, "失联升降台席");
  await a.card.getByTestId("btn-acquire").click();
  await expect(a.card.getByTestId("btn-execute")).toBeVisible();
  await expect(a.card.getByTestId("remaining")).toContainText("30");

  // ...Seat B watches the holder and cannot acquire while the lease is live.
  const b = await openSeat(browser, "接管飞行吊点席");
  await expect(b.card.getByTestId("badge")).toContainText("失联升降台席");
  await expect(b.card.getByTestId("btn-acquire")).toBeDisabled();

  // Seat A "loses network" for the full fixed 30 s lease. Server-side UTC
  // decides expiry; at 30 s exactly the lease is already invalid.
  test.setTimeout(75_000);
  await expect(b.card.getByTestId("btn-acquire")).toBeEnabled({
    timeout: 45_000,
  });

  // B takes over immediately.
  await b.card.getByTestId("btn-acquire").click();
  await expect(b.card.getByTestId("btn-execute")).toBeVisible();
  await expect(b.card.getByTestId("badge")).toContainText("接管飞行吊点席");

  // A's page polls, discovers the takeover, and flags its old token.
  await expect(a.card.getByTestId("lost-banner")).toContainText("控制权已失效");

  // Clicking with the stale token is explicitly rejected by the server and
  // must not affect B's new lease (renew / release / execute alike).
  await a.card.getByTestId("btn-execute-stale").click();
  await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");
  await a.card.getByTestId("btn-renew-stale").click();
  await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");
  await a.card.getByTestId("btn-release-stale").click();
  await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");

  // B's new lease is intact and can execute exactly once.
  await expect(b.card.getByTestId("badge")).toContainText("接管飞行吊点席");
  const beforeCount = parseInt(
    (await b.card.getByTestId("events").textContent())?.match(/\d+/)?.[0] ?? "0",
    10,
  );
  await b.card.getByTestId("btn-execute").click();
  await expect(b.card.getByTestId("notice")).toContainText("执行成功");
  await expect
    .poll(async () =>
      parseInt(
        (await b.card.getByTestId("events").textContent())?.match(/\d+/)?.[0] ??
          "-1",
        10,
      ),
    )
    .toBe(beforeCount + 1);

  await a.context.close();
  await b.context.close();
});
