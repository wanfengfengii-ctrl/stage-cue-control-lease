import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * On-site anomaly handover against the real FastAPI + PostgreSQL stack.
 *
 * Two independent browser contexts play two shifts:
 *   Seat A executes a dangerous action, then reports the on-site anomaly
 *   (category + description) on that executed event;
 *   Seat B sees the pending record (reporter, report time, content),
 *   confirms it from the other seat, and BOTH consoles — including after a
 *   full page refresh — show the confirmed record with the confirming seat
 *   and time, so the verbal handover is now traceable in PostgreSQL.
 */

const ACTION = "lift_up";

async function openSeat(browser: Browser, seatName: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  return { context, page };
}

const card = (page: Page) => page.getByTestId(`action-${ACTION}`);

test.describe.configure({ mode: "serial" });

test("one seat reports an anomaly; the other seat confirms; result survives refresh", async ({
  browser,
}) => {
  // ---- Seat A: execute the action so the event exists ---------------------
  const a = await openSeat(browser, "异常报告席-A");
  const cardA = card(a.page);
  await cardA.getByTestId("btn-acquire").click();
  await expect(cardA.getByTestId("btn-execute")).toBeVisible();
  await cardA.getByTestId("btn-execute").click();
  await expect(cardA.getByTestId("notice")).toContainText("执行成功");

  // The report entry is offered for the most recent execution event.
  const panelA = cardA.getByTestId("anomaly-panel");
  await panelA.getByTestId("btn-anomaly-open").click();
  const formA = panelA.getByTestId("anomaly-form");
  await formA.getByTestId("anomaly-category").selectOption("environment");
  await formA.getByTestId("anomaly-description").fill("侧台追光直射操作员视线，需调整灯位");
  await formA.getByTestId("btn-anomaly-submit").click();

  // Seat A sees its own pending record with reporter and report time.
  await expect(panelA.getByTestId("anomaly-status")).toHaveText("待确认");
  await expect(panelA.getByTestId("anomaly-category-text")).toHaveText("环境异常");
  await expect(panelA.getByTestId("anomaly-description-text")).toHaveText(
    "侧台追光直射操作员视线，需调整灯位",
  );
  await expect(panelA.getByTestId("anomaly-reporter")).toContainText("异常报告席-A");
  await expect(panelA.getByTestId("anomaly-reported-at")).toContainText("报告时间：");

  // The reporting console itself cannot acknowledge its own report — the
  // server only accepts confirmation from ANOTHER console.
  await expect(panelA.getByTestId("btn-anomaly-confirm")).toHaveCount(0);
  await expect(panelA.getByTestId("anomaly-await-confirm")).toBeVisible();

  // The reported bypass: rename THIS page to the next shift. The console's
  // stable id is unchanged, so no confirm button appears.
  await a.page.getByLabel("本席名称").fill("接班确认席-B");
  await a.page.waitForTimeout(300);
  await expect(panelA.getByTestId("btn-anomaly-confirm")).toHaveCount(0);
  await expect(panelA.getByTestId("anomaly-await-confirm")).toBeVisible();

  // A direct request with the renamed seat name but the SAME browser-console
  // id is still rejected and leaves the record pending and unstamped.
  const consoleIdA = await a.page.evaluate(() =>
    sessionStorage.getItem("handover.console-id.v1"),
  );
  expect(consoleIdA).toBeTruthy();
  const selfConfirm = await a.page.request.post(
    `/api/actions/${ACTION}/anomaly/confirm`,
    { data: { confirmer: "接班确认席-B", confirmer_id: consoleIdA } },
  );
  expect(selfConfirm.status()).toBe(409);
  const selfDetail = (await selfConfirm.json()).detail;
  expect(selfDetail.code).toBe("anomaly_self_confirm");
  expect(selfDetail.anomaly.status).toBe("pending");
  expect(selfDetail.anomaly.confirmed_by).toBeNull();

  // Restore A's display name so the two seats stay distinguishable.
  await a.page.getByLabel("本席名称").fill("异常报告席-A");

  // ---- Seat B: second browser sees the pending record --------------------
  const b = await openSeat(browser, "接班确认席-B");
  const panelB = card(b.page).getByTestId("anomaly-panel");
  await expect(panelB.getByTestId("anomaly-status")).toHaveText("待确认");
  await expect(panelB.getByTestId("anomaly-category-text")).toHaveText("环境异常");
  await expect(panelB.getByTestId("anomaly-description-text")).toHaveText(
    "侧台追光直射操作员视线，需调整灯位",
  );
  await expect(panelB.getByTestId("anomaly-reporter")).toContainText("异常报告席-A");

  // The other seat confirms having seen it.
  await panelB.getByTestId("btn-anomaly-confirm").click();
  await expect(panelB.getByTestId("anomaly-status")).toHaveText("已确认");
  const confirmationB = panelB.getByTestId("anomaly-confirmation");
  await expect(
    confirmationB.getByTestId("anomaly-confirmer"),
  ).toContainText("接班确认席-B");
  await expect(confirmationB.getByTestId("anomaly-confirmed-at")).toContainText(
    "确认时间：",
  );

  // Seat A's poll converges on the same confirmed record.
  await expect(panelA.getByTestId("anomaly-status")).toHaveText("已确认");
  await expect(
    panelA.getByTestId("anomaly-confirmation").getByTestId("anomaly-confirmer"),
  ).toContainText("接班确认席-B");

  // ---- The confirmation survives a real page refresh in BOTH browsers ----
  await b.page.reload();
  const panelB2 = card(b.page).getByTestId("anomaly-panel");
  await expect(panelB2.getByTestId("anomaly-status")).toHaveText("已确认");
  await expect(panelB2.getByTestId("anomaly-reporter")).toContainText("异常报告席-A");
  await expect(
    panelB2.getByTestId("anomaly-confirmation").getByTestId("anomaly-confirmer"),
  ).toContainText("接班确认席-B");
  await expect(panelB2.getByTestId("anomaly-description-text")).toHaveText(
    "侧台追光直射操作员视线，需调整灯位",
  );
  // No re-confirmation entry remains.
  await expect(panelB2.getByTestId("btn-anomaly-confirm")).toHaveCount(0);

  await a.page.reload();
  const panelA2 = card(a.page).getByTestId("anomaly-panel");
  await expect(panelA2.getByTestId("anomaly-status")).toHaveText("已确认");
  await expect(
    panelA2.getByTestId("anomaly-confirmation").getByTestId("anomaly-confirmer"),
  ).toContainText("接班确认席-B");

  // The polling API itself returns the persisted, confirmed record.
  const snap = await (await b.page.request.get("/api/actions")).json();
  const state = snap.actions.find((x: any) => x.action_id === ACTION);
  expect(state.anomaly.status).toBe("confirmed");
  expect(state.anomaly.confirmed_by).toBe("接班确认席-B");
  expect(state.anomaly.reported_by).toBe("异常报告席-A");
  expect(state.anomaly.event_id).toBe(state.last_event_id);

  await a.context.close();
  await b.context.close();
});
