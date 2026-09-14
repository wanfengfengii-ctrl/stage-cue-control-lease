import { expect, test, type Browser, type Page } from "@playwright/test";

/**
 * Shift handover (换班交接) against the real FastAPI + PostgreSQL stack:
 * the current holder generates a one-time code on its card; the receiving
 * console redeems it on the same card of a SECOND browser context. No mocks.
 */

async function openSeat(browser: Browser, seatName: string, action: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("本席名称").fill(seatName);
  const card = page.getByTestId(`action-${action}`);
  await expect(card.getByTestId("badge")).toBeVisible();
  return { context, page, card };
}

async function acquireAndInitiate(page: Page, card: ReturnType<typeof page.getByTestId>) {
  await card.getByTestId("btn-acquire").click();
  await expect(card.getByTestId("btn-execute")).toBeVisible();
  await card.getByTestId("btn-handover-initiate").click();
  await expect(card.getByTestId("handover-code")).toBeVisible();
  const text = await card.getByTestId("handover-code").textContent();
  const code = text?.match(/[A-Z2-9]{8}/)?.[0];
  expect(code).toBeTruthy();
  return code!;
}

test.describe.configure({ mode: "serial" });

test("dual-browser handover: the old token dies after the transfer", async ({ browser }) => {
  const ACTION = "lift_up";
  const a = await openSeat(browser, "交班席-A", ACTION);
  const code = await acquireAndInitiate(a.page, a.card);

  // Until the code is redeemed the original seat keeps every right.
  await a.card.getByTestId("btn-renew").click();
  await expect(a.card.getByTestId("notice")).toContainText("已续期");

  // The receiving console sees the accept form on the same card.
  const b = await openSeat(browser, "接班席-B", ACTION);
  await expect(b.card.getByTestId("handover-accept-form")).toBeVisible();
  await expect(b.card.getByTestId("handover-from")).toContainText("交班席-A");

  // A wrong code is rejected in place; the handover stays pending.
  await b.card.getByTestId("handover-code-input").fill("WRONGCODE");
  await b.card.getByTestId("btn-handover-accept").click();
  await expect(b.card.getByTestId("handover-error")).toContainText("交接码错误");
  await expect(b.card.getByTestId("btn-execute")).toHaveCount(0);

  // The right code transfers control to console B.
  await b.card.getByTestId("handover-code-input").fill(code);
  await b.card.getByTestId("btn-handover-accept").click();
  await expect(b.card.getByTestId("notice")).toContainText("已接收");
  await expect(b.card.getByTestId("btn-execute")).toBeVisible();
  await expect(b.card.getByTestId("badge")).toContainText("接班席-B");

  // Console A polls, sees the accepted record and flags its old token.
  await expect(a.card.getByTestId("lost-banner")).toContainText("控制权已交接");
  await expect(a.card.getByTestId("handover-done")).toContainText("接班席-B");

  // Every late operation of the old token is explicitly rejected...
  await a.card.getByTestId("btn-execute-stale").click();
  await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");
  await a.card.getByTestId("btn-renew-stale").click();
  await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");

  // ...while the receiver executes exactly once (event count +1).
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

test("accept racing the original execute: exactly one side commits", async ({ browser }) => {
  const ACTION = "lift_down";
  const a = await openSeat(browser, "原席-A", ACTION);
  const code = await acquireAndInitiate(a.page, a.card);

  const b = await openSeat(browser, "接班席-B", ACTION);
  await expect(b.card.getByTestId("handover-accept-form")).toBeVisible();
  await b.card.getByTestId("handover-code-input").fill(code);

  // The original seat executes at the same instant the receiver confirms.
  await Promise.all([
    a.card.getByTestId("btn-execute").click(),
    b.card.getByTestId("btn-handover-accept").click(),
  ]);

  // Wait for the decisive outcome on either side.
  const safeText = async (locator: ReturnType<Page["getByTestId"]>) =>
    (await locator.count()) > 0 ? (await locator.textContent()) ?? "" : "";
  let winner: "A" | "B" | null = null;
  await expect
    .poll(
      async () => {
        const aText = await safeText(a.card.getByTestId("notice"));
        const bText = await safeText(b.card.getByTestId("notice"));
        if (/执行成功/.test(aText)) winner = "A";
        else if (/已接收/.test(bText)) winner = "B";
        return winner;
      },
      { timeout: 15_000 },
    )
    .not.toBeNull();

  if (winner === "A") {
    // The execute committed first: the accept is rejected as invalidated
    // and no new lease was created — the action is simply free again.
    await expect(b.card.getByTestId("handover-error")).toContainText(
      "交接已失效",
    );
    await expect(b.card.getByTestId("btn-execute")).toHaveCount(0);
    await expect(a.card.getByTestId("badge")).toContainText("空闲可申请");
  } else {
    // The accept committed first: the old token's execute is rejected.
    await expect(a.card.getByTestId("notice")).toContainText("控制权已失效");
    await expect(b.card.getByTestId("btn-execute")).toBeVisible();
    // The receiver's fresh token works; leave the action free afterwards.
    await b.card.getByTestId("btn-execute").click();
    await expect(b.card.getByTestId("notice")).toContainText("执行成功");
  }

  await a.context.close();
  await b.context.close();
});

test("expired handover is rejected; the action can be re-applied", async ({ browser }) => {
  test.setTimeout(75_000);
  const ACTION = "hoist_fly_in";
  const a = await openSeat(browser, "交班席-A", ACTION);
  const code = await acquireAndInitiate(a.page, a.card);

  // Console B opens the accept form and types the code in time...
  const b = await openSeat(browser, "接班席-B", ACTION);
  await expect(b.card.getByTestId("handover-accept-form")).toBeVisible();
  await b.card.getByTestId("handover-code-input").fill(code);

  // ...but the 30 s lease expires before the confirmation is sent.
  await expect(a.card.getByTestId("badge")).toContainText("空闲可申请", {
    timeout: 45_000,
  });

  // The pending record is judged invalidated at query time: the accept
  // form disappears and the card prompts a fresh initiation instead.
  await expect(b.card.getByTestId("handover-accept-form")).toHaveCount(0);
  await expect(b.card.getByTestId("handover-invalid")).toContainText(
    "上一次换班交接已失效",
  );

  // A late accept with the code is rejected by the server with the
  // recognisable expiry reason — and creates no lease.
  const resp = await b.page.request.post(
    `/api/actions/${ACTION}/handover/accept`,
    {
      data: {
        code,
        recipient: "接班席-B",
        recipient_id: "e2e-console-b",
      },
    },
  );
  expect(resp.status()).toBe(409);
  const detail = (await resp.json()).detail;
  expect(detail.code).toBe("handover_expired");
  expect(detail.state.status).toBe("free");

  // The action can be applied for normally right away.
  await expect(b.card.getByTestId("btn-acquire")).toBeEnabled();
  await b.card.getByTestId("btn-acquire").click();
  await expect(b.card.getByTestId("btn-execute")).toBeVisible();
  await expect(b.card.getByTestId("badge")).toContainText("接班席-B");

  // Leave the action free for the next test.
  await b.card.getByTestId("btn-release").click();
  await expect(b.card.getByTestId("badge")).toContainText("空闲可申请");

  await a.context.close();
  await b.context.close();
});
