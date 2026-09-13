import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import App from "../src/App";
import { HISTORY_PAGE_SIZE } from "../src/HistoryPanel";
import { MockServer } from "./mock-server";

const LIFT = "lift_up";
const HOIST = "hoist_fly_in";

const CONSOLE_ID_KEY = "handover.console-id.v1";
const CONSOLE_A = "console-test-aaaa";
const CONSOLE_B = "console-test-bbbb";

const card = (id: string) => screen.getByTestId(`action-${id}`);

async function renderConsole(seat = "联排负责人") {
  sessionStorage.setItem(CONSOLE_ID_KEY, CONSOLE_A);
  render(<App />);
  fireEvent.change(screen.getByLabelText("本席名称"), {
    target: { value: seat },
  });
  await waitFor(() =>
    expect(within(card(LIFT)).getByTestId("btn-acquire")).toBeEnabled(),
  );
}

/** Raw fetch helper against the installed mock (mirrors the real envelope). */
async function post(path: string, body: unknown) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: res.status, body: JSON.parse(await res.text()) };
}

/** Execute an action end-to-end through the mock API (no UI). */
async function executeViaApi(actionId: string, holder = "联排控制席") {
  const lease = await post(`/api/actions/${actionId}/lease`, { holder });
  expect(lease.status).toBe(200);
  const out = await post(`/api/actions/${actionId}/execute`, {
    token: lease.body.token,
  });
  expect(out.status).toBe(200);
}

/** Execute the lift+hoist pair as one linked run through the mock API. */
async function linkedViaApi(holder = "联排控制席"): Promise<string> {
  const liftLease = await post(`/api/actions/${LIFT}/lease`, { holder });
  const hoistLease = await post(`/api/actions/${HOIST}/lease`, { holder });
  const out = await post(`/api/actions/execute-linked`, {
    items: [
      { action_id: LIFT, token: liftLease.body.token },
      { action_id: HOIST, token: hoistLease.body.token },
    ],
  });
  expect(out.status).toBe(200);
  return out.body.link_id as string;
}

async function openHistory() {
  fireEvent.click(screen.getByTestId("btn-history-open"));
  await waitFor(() =>
    expect(screen.getByTestId("history-panel")).toBeInTheDocument(),
  );
}

function historyRows(): HTMLElement[] {
  return within(screen.getByTestId("history-list")).getAllByTestId(
    "history-event",
  );
}

function rowEventIds(): string[] {
  return historyRows().map((r) => r.getAttribute("data-event-id")!);
}

function rowByEventId(eventId: string): HTMLElement {
  return screen
    .getByTestId("history-list")
    .querySelector(`[data-event-id="${eventId}"]`) as HTMLElement;
}

describe("App execution history (执行历史)", () => {
  let server: MockServer;

  beforeEach(() => {
    server = new MockServer();
    server.installFetch();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("shows events newest-first with session, linked group and confirmed anomaly", async () => {
    await renderConsole();

    // A named round so the rows carry the session name.
    fireEvent.change(screen.getByTestId("session-name"), {
      target: { value: "复盘场" },
    });
    fireEvent.click(screen.getByTestId("btn-session-start"));
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-status")).toHaveTextContent(
        "进行中",
      ),
    );

    // Single execution -> event #1.
    fireEvent.click(within(card(LIFT)).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card(LIFT)).getByTestId("btn-execute")).toBeInTheDocument(),
    );
    fireEvent.click(within(card(LIFT)).getByTestId("btn-execute"));
    await waitFor(() =>
      expect(within(card(LIFT)).getByTestId("notice")).toHaveTextContent(
        /执行成功/,
      ),
    );

    // Linked execution -> events #2 (lift) and #3 (hoist), one link id.
    fireEvent.click(within(card(LIFT)).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card(LIFT)).getByTestId("btn-execute")).toBeInTheDocument(),
    );
    fireEvent.click(within(card(HOIST)).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(
        within(card(HOIST)).getByTestId("btn-execute"),
      ).toBeInTheDocument(),
    );
    fireEvent.click(
      within(await screen.findByTestId("linked-bar")).getByTestId(
        "btn-execute-linked",
      ),
    );
    await waitFor(() =>
      expect(within(card(LIFT)).getByTestId("notice")).toHaveTextContent(
        /联动执行成功/,
      ),
    );

    // Report the anomaly on the lift's latest (linked) event via the card…
    const panel = within(card(LIFT)).getByTestId("anomaly-panel");
    fireEvent.click(within(panel).getByTestId("btn-anomaly-open"));
    fireEvent.change(within(panel).getByTestId("anomaly-description"), {
      target: { value: "上升末段抖动，已现场复位" },
    });
    fireEvent.click(within(panel).getByTestId("btn-anomaly-submit"));
    await waitFor(() =>
      expect(within(panel).getByTestId("anomaly-status")).toHaveTextContent(
        "待确认",
      ),
    );
    // …and confirm it from ANOTHER console (the next shift).
    const confirmed = await post(`/api/actions/${LIFT}/anomaly/confirm`, {
      confirmer: "下一班-B",
      confirmer_id: CONSOLE_B,
    });
    expect(confirmed.status).toBe(200);

    fireEvent.click(screen.getByTestId("btn-session-end"));
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-status")).toHaveTextContent(
        "已结束",
      ),
    );

    // ---- open the history: newest first, linked pair grouped --------------
    await openHistory();
    await waitFor(() => expect(historyRows()).toHaveLength(3));
    // Descending event ids: hoist(#3), lift(#2), single lift(#1).
    expect(rowEventIds()).toEqual(["3", "2", "1"]);

    // The two linked events render inside one adjacent group.
    const group = screen.getByTestId("history-group");
    expect(
      within(group).getByTestId("history-group-badge"),
    ).toHaveTextContent(/联动执行/);
    const groupedIds = within(group)
      .getAllByTestId("history-event")
      .map((r) => r.getAttribute("data-event-id"));
    expect(groupedIds).toEqual(["3", "2"]);

    // Every row shows execution time, action, seat and its session.
    for (const row of historyRows()) {
      expect(
        within(row).getByTestId("history-event-time").textContent,
      ).not.toBe("—");
      expect(
        within(row).getByTestId("history-event-session"),
      ).toHaveTextContent("场次：复盘场");
    }
    expect(
      within(rowByEventId("1")).getByTestId("history-event-action"),
    ).toHaveTextContent("升降台 上升");
    expect(
      within(rowByEventId("3")).getByTestId("history-event-seat"),
    ).toHaveTextContent("席位：联排负责人");

    // The anomaly — with its confirmation — rides its own event only.
    const anomalyRow = rowByEventId("2");
    const record = within(anomalyRow).getByTestId("history-anomaly");
    expect(record).toHaveAttribute("data-status", "confirmed");
    expect(
      within(anomalyRow).getByTestId("history-anomaly-description"),
    ).toHaveTextContent("上升末段抖动，已现场复位");
    expect(
      within(anomalyRow).getByTestId("history-anomaly-confirmation"),
    ).toHaveTextContent(/已确认：下一班-B/);
    expect(
      within(rowByEventId("1")).queryByTestId("history-anomaly"),
    ).toBeNull();
    expect(
      within(rowByEventId("3")).queryByTestId("history-anomaly"),
    ).toBeNull();

    // No older records: the list announces its end instead of a load-more.
    expect(screen.queryByTestId("btn-history-more")).toBeNull();
    expect(screen.getByTestId("history-end")).toHaveTextContent(
      "已加载全部 3 条执行记录",
    );

    // Closing and reopening starts again from the latest page.
    fireEvent.click(screen.getByTestId("btn-history-close"));
    expect(screen.queryByTestId("history-panel")).toBeNull();
    await openHistory();
    await waitFor(() => expect(historyRows()).toHaveLength(3));
    expect(rowEventIds()).toEqual(["3", "2", "1"]);
  });

  it("pages backwards with the cursor; a failed page keeps rows and retries at the tail", async () => {
    await renderConsole();
    // One page plus a bit of history, created straight through the API.
    const total = HISTORY_PAGE_SIZE + 2;
    for (let i = 0; i < total; i++) {
      await executeViaApi(LIFT);
    }

    await openHistory();
    await waitFor(() =>
      expect(historyRows()).toHaveLength(HISTORY_PAGE_SIZE),
    );
    const firstPageIds = rowEventIds();
    expect(firstPageIds).toEqual(
      Array.from({ length: HISTORY_PAGE_SIZE }, (_, i) => String(total - i)),
    );

    // The follow-up page fails: loaded rows stay, the tail offers a retry.
    server.failNextHistory = 1;
    fireEvent.click(screen.getByTestId("btn-history-more"));
    await waitFor(() =>
      expect(screen.getByTestId("history-tail-error")).toBeInTheDocument(),
    );
    expect(historyRows()).toHaveLength(HISTORY_PAGE_SIZE);
    expect(rowEventIds()).toEqual(firstPageIds);
    expect(screen.queryByTestId("btn-history-more")).toBeNull();

    // Retry succeeds and appends strictly older events, nothing duplicated.
    fireEvent.click(screen.getByTestId("btn-history-retry"));
    await waitFor(() => expect(historyRows()).toHaveLength(total));
    expect(rowEventIds()).toEqual([
      ...firstPageIds,
      ...Array.from({ length: 2 }, (_, i) => String(2 - i)),
    ]);
    expect(new Set(rowEventIds()).size).toBe(total);
    expect(screen.queryByTestId("history-tail-error")).toBeNull();
    expect(screen.getByTestId("history-end")).toHaveTextContent(
      `已加载全部 ${total} 条执行记录`,
    );
  });

  it("keeps a linked run whole when it straddles the page boundary", async () => {
    await renderConsole();
    // A linked run first, then nine singles: in newest-first order the
    // pair's two events sit exactly across the 10-row page boundary
    // (positions 10 and 11). The first screen must show the WHOLE group.
    const linkId = await linkedViaApi();
    for (let i = 0; i < HISTORY_PAGE_SIZE - 1; i++) {
      await executeViaApi(LIFT);
    }

    await openHistory();
    // The page grows by one so the pair is complete: 9 singles + 2 linked.
    await waitFor(() =>
      expect(historyRows()).toHaveLength(HISTORY_PAGE_SIZE + 1),
    );
    const ids = rowEventIds();
    expect(new Set(ids).size).toBe(ids.length);

    // Both events of the linked run render adjacent inside one group — the
    // first screen never shows a broken one-action group.
    const group = screen.getByTestId("history-group");
    expect(group).toHaveAttribute("data-link-id", linkId);
    const groupedIds = within(group)
      .getAllByTestId("history-event")
      .map((r) => r.getAttribute("data-event-id"));
    expect(groupedIds).toEqual(["2", "1"]);
    expect(
      within(group).getByTestId("history-group-badge"),
    ).toHaveTextContent(/2 个动作同属一次联动/);

    // The grown page already reached the oldest record.
    expect(screen.queryByTestId("btn-history-more")).toBeNull();
    expect(screen.getByTestId("history-end")).toHaveTextContent(
      `已加载全部 ${HISTORY_PAGE_SIZE + 1} 条执行记录`,
    );
  });

  it("never appends the same event twice on rapid load-more clicks", async () => {
    await renderConsole();
    const total = HISTORY_PAGE_SIZE + 4;
    for (let i = 0; i < total; i++) {
      await executeViaApi(LIFT);
    }

    await openHistory();
    await waitFor(() =>
      expect(historyRows()).toHaveLength(HISTORY_PAGE_SIZE),
    );

    // Rapid consecutive clicks, dispatched in the SAME synchronous task —
    // before React can commit the "loading" state and disable the button.
    // (fireEvent would flush state between clicks and miss the race that
    // real rapid clicking hits.)
    const more = screen.getByTestId("btn-history-more");
    for (let i = 0; i < 3; i++) {
      more.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    }

    // Every historical event appears exactly once, still strictly
    // descending, and the list announces its end.
    await waitFor(() => expect(historyRows()).toHaveLength(total));
    const ids = rowEventIds();
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual([...ids].sort((a, b) => Number(b) - Number(a)));
    expect(screen.getByTestId("history-end")).toHaveTextContent(
      `已加载全部 ${total} 条执行记录`,
    );
  });

  it("surfaces an initial-load failure with a retry that recovers", async () => {
    await renderConsole();
    await executeViaApi(LIFT);

    server.failNextHistory = 1;
    await openHistory();
    await waitFor(() =>
      expect(screen.getByTestId("history-initial-error")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("history-list")).toBeNull();

    fireEvent.click(screen.getByTestId("btn-history-reload"));
    await waitFor(() => expect(historyRows()).toHaveLength(1));
    expect(rowEventIds()).toEqual(["1"]);
  });
});
