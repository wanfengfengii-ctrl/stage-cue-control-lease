import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * Linked execution against the real FastAPI + PostgreSQL stack (no mocks).
 *
 *  1. One seat holds both dangerous actions and submits them as ONE linked
 *     run: both cards count exactly one event and show the same link id.
 *  2. A stale token in the pair aborts the whole run: the API names the
 *     failed action, writes nothing, and does NOT terminate the other
 *     (still valid) lease.
 */

const LIFT = "lift_up";
const HOIST = "hoist_fly_in";

async function openSeat(browser: Browser, seatName: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  return { context, page };
}

const card = (page: Page, action: string) =>
  page.getByTestId(`action-${action}`);

async function eventCount(page: Page, action: string): Promise<number> {
  const text =
    (await card(page, action).getByTestId("events").textContent()) ?? "";
  return parseInt(text.match(/\d+/)?.[0] ?? "-1", 10);
}

test.describe.configure({ mode: "serial" });

test("linked execute: two held actions commit as one with a shared link id", async ({ browser }) => {
  const a = await openSeat(browser, "联排控制席");
  const lift = card(a.page, LIFT);
  const hoist = card(a.page, HOIST);

  // The seat acquires both dangerous actions, one after the other.
  await lift.getByTestId("btn-acquire").click();
  await expect(lift.getByTestId("btn-execute")).toBeVisible();
  await hoist.getByTestId("btn-acquire").click();
  await expect(hoist.getByTestId("btn-execute")).toBeVisible();

  // Only then does the console offer the single linked submit.
  const bar = a.page.getByTestId("linked-bar");
  await expect(bar).toBeVisible();

  const liftBefore = await eventCount(a.page, LIFT);
  const hoistBefore = await eventCount(a.page, HOIST);

  await bar.getByTestId("btn-execute-linked").click();

  // Both cards report the linked success; each counter advances exactly once.
  await expect(lift.getByTestId("notice")).toContainText("联动执行成功");
  await expect(hoist.getByTestId("notice")).toContainText("联动执行成功");
  await expect.poll(() => eventCount(a.page, LIFT)).toBe(liftBefore + 1);
  await expect.poll(() => eventCount(a.page, HOIST)).toBe(hoistBefore + 1);

  // Both cards display the SAME server-generated link id.
  const linkLift = await lift.getByTestId("last-link").textContent();
  const linkHoist = await hoist.getByTestId("last-link").textContent();
  expect(linkLift).toBeTruthy();
  expect(linkLift).toBe(linkHoist);

  // Both leases are finished: the bar disappears, both actions are free.
  await expect(a.page.getByTestId("linked-bar")).toHaveCount(0);
  await expect(lift.getByTestId("badge")).toHaveText("空闲可申请");
  await expect(hoist.getByTestId("badge")).toHaveText("空闲可申请");

  await a.context.close();
});

test("stale token aborts the whole linked run with no side effects", async ({ browser }) => {
  test.setTimeout(90_000);
  const a = await openSeat(browser, "联排控制席-A");
  const liftA = card(a.page, LIFT);
  const hoistA = card(a.page, HOIST);
  await liftA.getByTestId("btn-acquire").click();
  await expect(liftA.getByTestId("btn-execute")).toBeVisible();
  await hoistA.getByTestId("btn-acquire").click();
  await expect(hoistA.getByTestId("btn-execute")).toBeVisible();

  // Seat B waits out the 30 s server-side lease, then takes the hoist over.
  const b = await openSeat(browser, "接管席-B");
  const hoistB = card(b.page, HOIST);
  await expect(hoistB.getByTestId("btn-acquire")).toBeEnabled({
    timeout: 45_000,
  });
  await hoistB.getByTestId("btn-acquire").click();
  await expect(hoistB.getByTestId("btn-execute")).toBeVisible();

  // A's hoist token is now stale (lost-banner); A's lift lease has expired
  // as well, so A forgets that stale token and re-acquires the lift — a
  // fresh, VALID lease that the failed linked run must not terminate.
  await expect(hoistA.getByTestId("lost-banner")).toContainText(
    "控制权已失效",
  );
  await expect(liftA.getByTestId("btn-forget")).toBeVisible();
  await liftA.getByTestId("btn-forget").click();
  await liftA.getByTestId("btn-acquire").click();
  await expect(liftA.getByTestId("btn-execute")).toBeVisible();
  // Holding only one action, the linked bar is correctly NOT offered.
  await expect(a.page.getByTestId("linked-bar")).toHaveCount(0);

  // A's session still keeps the stale hoist token; submit the pair directly
  // at the real API, as the console would have before the takeover.
  const tokens = await a.page.evaluate(
    () =>
      JSON.parse(sessionStorage.getItem("handover.tokens.v1") || "{}") as Record<
        string,
        string
      >,
  );
  expect(tokens[LIFT]).toBeTruthy();
  expect(tokens[HOIST]).toBeTruthy();

  const hoistEventsBefore = await eventCount(b.page, HOIST);
  const res = await a.page.request.post("/api/actions/execute-linked", {
    data: {
      items: [
        { action_id: LIFT, token: tokens[LIFT] },
        { action_id: HOIST, token: tokens[HOIST] },
      ],
    },
  });

  // The API rejects the run and names the concrete failed action.
  expect(res.status()).toBe(409);
  const detail = (await res.json()).detail;
  expect(detail.code).toBe("control_lost");
  expect(detail.action_id).toBe(HOIST);
  expect(detail.states[LIFT].status).toBe("held");
  expect(detail.states[HOIST].holder).toBe("接管席-B");

  // No side effects: B's hoist lease is intact and no event was written.
  await expect(hoistB.getByTestId("badge")).toContainText("接管席-B");
  expect(await eventCount(b.page, HOIST)).toBe(hoistEventsBefore);

  // A's valid lift lease was NOT terminated by the failed linked run:
  // A can still execute it normally, exactly once.
  const liftEventsBefore = await eventCount(a.page, LIFT);
  await liftA.getByTestId("btn-execute").click();
  await expect(liftA.getByTestId("notice")).toContainText("执行成功");
  await expect.poll(() => eventCount(a.page, LIFT)).toBe(liftEventsBefore + 1);

  // Leave every action free for other suites.
  await hoistB.getByTestId("btn-release").click();
  await expect(hoistB.getByTestId("badge")).toHaveText("空闲可申请");

  await a.context.close();
  await b.context.close();
});
