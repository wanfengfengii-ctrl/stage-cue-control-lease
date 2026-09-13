import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * Rehearsal session (场次) against the real FastAPI + PostgreSQL stack.
 *
 * One console page plays the rehearsal lead: start a named round, run a
 * single action and a linked cross-device cue inside it, end the round, and
 * watch the summary freeze — later executions no longer count.  The error
 * boundaries (duplicate start, blank name, end with no active round) are
 * exercised at the real API and must not change any event.
 */

const LIFT = "lift_up";
const HOIST = "hoist_fly_in";
const EXTRA = "emergency_stop";

async function openSeat(browser: Browser, seatName: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  return { context, page };
}

const card = (page: Page, action: string) =>
  page.getByTestId(`action-${action}`);

const summary = (page: Page) => page.getByTestId("session-summary");

async function transition(
  page: Page,
  body: { op: string; name?: string },
): Promise<{ status: number; json: any }> {
  const res = await page.request.post("/api/sessions/transition", {
    data: body,
  });
  return { status: res.status(), json: await res.json() };
}

test.describe.configure({ mode: "serial" });

test("session round: start → single + linked executes → end → frozen summary", async ({ browser }) => {
  test.setTimeout(120_000);
  const a = await openSeat(browser, "联排负责人");
  const { page } = a;

  // Clean slate: end any round a previous run left active.
  const current = await (
    await page.request.get("/api/sessions/current")
  ).json();
  if (current.session?.status === "active") {
    await transition(page, { op: "end" });
  }
  // The database is shared and persistent: a previous run's ENDED round may
  // legitimately still be summarised. What the boundary rejections must not
  // do is create a NEW session — tracked by the current summary's id.
  const settled = await (
    await page.request.get("/api/sessions/current")
  ).json();
  const settledId: number | null = settled.session?.id ?? null;

  // Boundary: ending with no active round is a recognisable 409.
  const noActive = await transition(page, { op: "end" });
  expect(noActive.status).toBe(409);
  expect(noActive.json.detail.code).toBe("no_active_session");

  // Boundary: a blank session name is a recognisable 400.
  const blank = await transition(page, { op: "start", name: "   " });
  expect(blank.status).toBe(400);
  expect(blank.json.detail.code).toBe("invalid_name");

  // Neither rejection started a new round.
  const afterRejections = await (
    await page.request.get("/api/sessions/current")
  ).json();
  expect(afterRejections.session?.id ?? null).toBe(settledId);

  // Start the round from the console.
  await page.getByTestId("session-name").fill("第一轮联排");
  await page.getByTestId("btn-session-start").click();
  await expect(summary(page).getByTestId("session-summary-status")).toHaveText(
    "进行中",
  );
  await expect(summary(page).getByTestId("session-summary-name")).toContainText(
    "第一轮联排",
  );
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 0 次",
  );
  await expect(summary(page).getByTestId("session-summary-actions")).toHaveText(
    "涉及动作 0 个",
  );

  // Boundary: a duplicate start while active is rejected and changes nothing.
  const dup = await transition(page, { op: "start", name: "插队场次" });
  expect(dup.status).toBe(409);
  expect(dup.json.detail.code).toBe("session_active");
  await expect(summary(page).getByTestId("session-summary-name")).toContainText(
    "第一轮联排",
  );
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 0 次",
  );

  // Single action execution counts once, involving one action.
  const lift = card(page, LIFT);
  await expect(lift.getByTestId("btn-acquire")).toBeEnabled({
    timeout: 45_000,
  });
  await lift.getByTestId("btn-acquire").click();
  await lift.getByTestId("btn-execute").click();
  await expect(lift.getByTestId("notice")).toContainText("执行成功");
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 1 次",
  );
  await expect(summary(page).getByTestId("session-summary-actions")).toHaveText(
    "涉及动作 1 个",
  );

  // Linked execution of the cross-device pair: +2 events, +1 distinct action.
  await lift.getByTestId("btn-acquire").click();
  await expect(lift.getByTestId("btn-execute")).toBeVisible();
  const hoist = card(page, HOIST);
  await hoist.getByTestId("btn-acquire").click();
  await expect(hoist.getByTestId("btn-execute")).toBeVisible();
  await page
    .getByTestId("linked-bar")
    .getByTestId("btn-execute-linked")
    .click();
  await expect(lift.getByTestId("notice")).toContainText("联动执行成功");
  await expect(hoist.getByTestId("notice")).toContainText("联动执行成功");
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 3 次",
  );
  await expect(summary(page).getByTestId("session-summary-actions")).toHaveText(
    "涉及动作 2 个",
  );

  // End the round: the summary stays on screen, frozen at 3 events / 2 actions.
  await page.getByTestId("btn-session-end").click();
  await expect(summary(page).getByTestId("session-summary-status")).toHaveText(
    "已结束",
  );
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 3 次",
  );
  await expect(page.getByTestId("btn-session-end")).toBeDisabled();

  // Actions executed afterwards no longer count towards the ended round.
  const extra = card(page, EXTRA);
  await extra.getByTestId("btn-acquire").click();
  await extra.getByTestId("btn-execute").click();
  await expect(extra.getByTestId("notice")).toContainText("执行成功");
  await expect(summary(page).getByTestId("session-summary-events")).toHaveText(
    "累计事件 3 次",
  );
  await expect(summary(page).getByTestId("session-summary-actions")).toHaveText(
    "涉及动作 2 个",
  );

  // The ended summary remains queryable through the API as well.
  const after = await (
    await page.request.get("/api/sessions/current")
  ).json();
  expect(after.session.name).toBe("第一轮联排");
  expect(after.session.status).toBe("ended");
  expect(after.session.event_count).toBe(3);
  expect(after.session.action_count).toBe(2);

  await a.context.close();
});
