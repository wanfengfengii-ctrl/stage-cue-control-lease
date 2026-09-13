import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * Execution history (执行历史) against the real FastAPI + PostgreSQL stack.
 *
 * One console plays the rehearsal lead reviewing a round: filler executions
 * are seeded through the API, then the console runs a single action and a
 * linked cross-device cue inside a named session, reports an anomaly on the
 * linked event and has it confirmed by another console. The review then
 * opens 执行历史, checks the newest page (order, linked grouping, session
 * and anomaly riding their events), pages to older records, and finally
 * reloads the whole page — reopening the history must show the same
 * records again.
 */

const LIFT = "lift_up";
const HOIST = "hoist_fly_in";
const FILLER_ACTIONS = ["emergency_stop", "lift_down", "hoist_fly_out"];
const SESSION_NAME = "历史复盘场";
const SEAT_A = "历史复盘席-A";
const SEED_HOLDER = "联排种子席";
const CONFIRMER = "接班确认席-B";
const ANOMALY_TEXT = "联动末段吊点微晃，已复核锁扣";

async function openSeat(browser: Browser, seatName: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  return { context, page };
}

const card = (page: Page, action: string) =>
  page.getByTestId(`action-${action}`);

/** Execute an action straight through the real API; returns the event id. */
async function executeViaApi(
  page: Page,
  actionId: string,
  holder: string,
): Promise<number> {
  const lease = await page.request.post(`/api/actions/${actionId}/lease`, {
    data: { holder },
  });
  expect(lease.status()).toBe(200);
  const { token } = await lease.json();
  const out = await page.request.post(`/api/actions/${actionId}/execute`, {
    data: { token },
  });
  expect(out.status()).toBe(200);
  return (await out.json()).event_id as number;
}

async function lastEventId(page: Page, actionId: string): Promise<number> {
  const state = await (await page.request.get(`/api/actions/${actionId}`)).json();
  return state.last_event_id as number;
}

const rows = (page: Page) => page.getByTestId("history-event");

async function rowEventIds(page: Page): Promise<number[]> {
  const attrs = await rows(page).evaluateAll((els) =>
    els.map((el) => el.getAttribute("data-event-id")),
  );
  return attrs.map((v) => Number(v));
}

test.describe.configure({ mode: "serial" });

test("history review: single + linked + anomaly, paged and consistent after reload", async ({
  browser,
}) => {
  test.setTimeout(180_000);
  const a = await openSeat(browser, SEAT_A);
  const { page } = a;

  // Clean slate: end any round a previous run left active.
  const current = await (
    await page.request.get("/api/sessions/current")
  ).json();
  if (current.session?.status === "active") {
    await page.request.post("/api/sessions/transition", {
      data: { op: "end" },
    });
  }

  // ---- seed filler executions (no active session) so the review spans pages
  const seededIds: number[] = [];
  for (let i = 0; i < 9; i++) {
    seededIds.push(
      await executeViaApi(page, FILLER_ACTIONS[i % FILLER_ACTIONS.length], SEED_HOLDER),
    );
  }

  // ---- a named round: single execution + one linked cue, from the console
  await page.getByTestId("session-name").fill(SESSION_NAME);
  await page.getByTestId("btn-session-start").click();
  await expect(
    page.getByTestId("session-summary").getByTestId("session-summary-status"),
  ).toHaveText("进行中");

  const lift = card(page, LIFT);
  await lift.getByTestId("btn-acquire").click();
  await lift.getByTestId("btn-execute").click();
  await expect(lift.getByTestId("notice")).toContainText("执行成功");
  const singleEventId = await lastEventId(page, LIFT);

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
  const liftLinkedEventId = await lastEventId(page, LIFT);
  const hoistLinkedEventId = await lastEventId(page, HOIST);
  const linkId: string = (
    await (await page.request.get(`/api/actions/${LIFT}`)).json()
  ).last_link_id;
  expect(linkId).toBeTruthy();
  expect(liftLinkedEventId).toBeGreaterThan(singleEventId);
  expect(hoistLinkedEventId).toBeGreaterThan(singleEventId);
  expect(liftLinkedEventId).not.toBe(hoistLinkedEventId);

  // ---- anomaly on the linked event, confirmed by ANOTHER console ----------
  const panel = lift.getByTestId("anomaly-panel");
  await panel.getByTestId("btn-anomaly-open").click();
  await panel.getByTestId("anomaly-category").selectOption("equipment");
  await panel.getByTestId("anomaly-description").fill(ANOMALY_TEXT);
  await panel.getByTestId("btn-anomaly-submit").click();
  await expect(panel.getByTestId("anomaly-status")).toHaveText("待确认");

  const confirm = await page.request.post(
    `/api/actions/${LIFT}/anomaly/confirm`,
    { data: { confirmer: CONFIRMER, confirmer_id: "history-confirmer-console" } },
  );
  expect(confirm.status()).toBe(200);
  await expect(panel.getByTestId("anomaly-status")).toHaveText("已确认");

  await page.getByTestId("btn-session-end").click();
  await expect(
    page.getByTestId("session-summary").getByTestId("session-summary-status"),
  ).toHaveText("已结束");

  // The twelve events of this run, newest first.
  const runIds = [
    liftLinkedEventId,
    hoistLinkedEventId,
    singleEventId,
    ...[...seededIds].sort((x, y) => y - x),
  ];

  // ---- open the history: the latest page first ----------------------------
  await page.getByTestId("btn-history-open").click();
  const historyPanel = page.getByTestId("history-panel");
  await expect(historyPanel).toBeVisible();
  await expect(rows(page)).toHaveCount(10);

  // Newest-first by immutable event id; the linked pair heads the list.
  const page1Ids = await rowEventIds(page);
  expect(page1Ids).toEqual(runIds.slice(0, 10));
  expect(page1Ids[0]).toBe(liftLinkedEventId);
  expect(page1Ids[1]).toBe(hoistLinkedEventId);

  // The two events of the linked run are adjacent inside one group. Older
  // runs may have left their own linked groups in the shared database, so
  // every group assertion is scoped to THIS run's link id.
  const group = page.locator(
    `[data-testid="history-group"][data-link-id="${linkId}"]`,
  );
  await expect(group).toHaveCount(1);
  await expect(group.getByTestId("history-group-badge")).toContainText(
    "联动执行",
  );
  const groupedIds = await group
    .getByTestId("history-event")
    .evaluateAll((els) => els.map((el) => Number(el.getAttribute("data-event-id"))));
  expect(groupedIds).toEqual([liftLinkedEventId, hoistLinkedEventId]);

  // Rows show execution time, action, seat and session; the in-round events
  // carry the session name, the seeded ones none.
  const linkedRow = page.locator(
    `[data-testid="history-event"][data-event-id="${liftLinkedEventId}"]`,
  );
  await expect(linkedRow.getByTestId("history-event-action")).toHaveText(
    "升降台 上升",
  );
  await expect(linkedRow.getByTestId("history-event-seat")).toContainText(
    SEAT_A,
  );
  await expect(linkedRow.getByTestId("history-event-session")).toContainText(
    SESSION_NAME,
  );
  await expect(linkedRow.getByTestId("history-event-time")).not.toHaveText("—");
  const singleRow = page.locator(
    `[data-testid="history-event"][data-event-id="${singleEventId}"]`,
  );
  await expect(singleRow.getByTestId("history-event-session")).toContainText(
    SESSION_NAME,
  );
  const fillerRow = page.locator(
    `[data-testid="history-event"][data-event-id="${seededIds[8]}"]`,
  );
  await expect(fillerRow.getByTestId("history-event-seat")).toContainText(
    SEED_HOLDER,
  );
  await expect(fillerRow.getByTestId("history-event-session")).toContainText(
    "场次：—",
  );

  // The anomaly and its confirmation ride the linked event's row only.
  const anomaly = linkedRow.getByTestId("history-anomaly");
  await expect(anomaly).toHaveAttribute("data-status", "confirmed");
  await expect(
    anomaly.getByTestId("history-anomaly-description"),
  ).toContainText(ANOMALY_TEXT);
  await expect(
    anomaly.getByTestId("history-anomaly-confirmation"),
  ).toContainText(`已确认：${CONFIRMER}`);
  await expect(singleRow.getByTestId("history-anomaly")).toHaveCount(0);

  // ---- page to older records: appended, never duplicated or reordered -----
  await page.getByTestId("btn-history-more").click();
  // Older runs share the database, so the second page may hold more than
  // this run's two remaining events; wait for this run's oldest one.
  await expect(
    page.locator(
      `[data-testid="history-event"][data-event-id="${seededIds[0]}"]`,
    ),
  ).toBeAttached({ timeout: 15_000 });
  const allIds = await rowEventIds(page);
  // This run's events all present, still strictly descending, each once.
  expect(allIds.slice(0, 12)).toEqual(runIds);
  expect(new Set(allIds).size).toBe(allIds.length);
  const sorted = [...allIds].sort((x, y) => y - x);
  expect(allIds).toEqual(sorted);
  // The linked group survived the page turn.
  await expect(
    page.locator(`[data-testid="history-group"][data-link-id="${linkId}"]`),
  ).toHaveCount(1);

  // ---- reload the whole page: the same records come back ------------------
  await page.reload();
  await page.getByLabel("本席名称").fill(SEAT_A);
  await page.getByTestId("btn-history-open").click();
  await expect(rows(page)).toHaveCount(10);
  const reloadedIds = await rowEventIds(page);
  expect(reloadedIds).toEqual(page1Ids);
  const linkedRowAgain = page.locator(
    `[data-testid="history-event"][data-event-id="${liftLinkedEventId}"]`,
  );
  await expect(
    linkedRowAgain.getByTestId("history-anomaly"),
  ).toHaveAttribute("data-status", "confirmed");
  await expect(
    linkedRowAgain.getByTestId("history-anomaly-confirmation"),
  ).toContainText(`已确认：${CONFIRMER}`);
  await expect(
    page.locator(`[data-testid="history-group"][data-link-id="${linkId}"]`),
  ).toHaveCount(1);

  await a.context.close();
});

test("history API: keyset pages are stable and an invalid cursor is recognisable", async ({
  browser,
}) => {
  const a = await openSeat(browser, "历史接口核对席");
  const { page } = a;

  // Two fresh events so paging has something to walk.
  const first = await executeViaApi(page, LIFT, "接口核对席");
  const second = await executeViaApi(page, LIFT, "接口核对席");
  expect(second).toBeGreaterThan(first);

  const page1 = await (
    await page.request.get("/api/history", { params: { limit: "1" } })
  ).json();
  expect(page1.events).toHaveLength(1);
  expect(page1.events[0].event_id).toBe(second);
  expect(page1.next_cursor).toBe(second);

  const page2 = await (
    await page.request.get("/api/history", {
      params: { limit: "1", cursor: String(page1.next_cursor) },
    })
  ).json();
  // The cursor selects strictly older events: no overlap with page 1.
  expect(page2.events[0].event_id).toBeLessThan(second);
  expect(page2.events[0].event_id).toBe(first);

  // An unparsable cursor is a recognisable business error.
  const bad = await page.request.get("/api/history", {
    params: { cursor: "不是事件编号" },
  });
  expect(bad.status()).toBe(400);
  expect((await bad.json()).detail.code).toBe("history_cursor_invalid");

  await a.context.close();
});
