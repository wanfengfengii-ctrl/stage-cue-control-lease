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
import { MockServer } from "./mock-server";

const ACTION = "lift_up";

// Stable per-browser-console identities, independent of the editable seat
// name exactly as in the real app.
const CONSOLE_ID_KEY = "handover.console-id.v1";
const CONSOLE_A = "console-test-aaaa";
const CONSOLE_B = "console-test-bbbb";

const card = () => screen.getByTestId(`action-${ACTION}`);
const panel = () => within(card()).getByTestId("anomaly-panel");

async function renderConsole(
  seat = "升降台一席",
  consoleId: string = CONSOLE_A,
) {
  cleanup();
  sessionStorage.setItem(CONSOLE_ID_KEY, consoleId);
  render(<App />);
  fireEvent.change(screen.getByLabelText("本席名称"), {
    target: { value: seat },
  });
  await waitFor(() =>
    expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
  );
}

/** Simulate opening the page in ANOTHER browser console (fresh identity). */
async function reopenConsole(consoleId: string, seat: string) {
  await renderConsole(seat, consoleId);
  await waitFor(() =>
    expect(within(panel()).getByTestId("anomaly-record")).toBeInTheDocument(),
  );
}

async function executeOnce() {
  fireEvent.click(within(card()).getByTestId("btn-acquire"));
  await waitFor(() =>
    expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
  );
  fireEvent.click(within(card()).getByTestId("btn-execute"));
  await waitFor(() =>
    expect(within(card()).getByTestId("notice")).toHaveTextContent(/执行成功/),
  );
}

describe("App anomaly report (现场异常) on the latest execution event", () => {
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

  it("offers the report entry only after an execution event exists", async () => {
    await renderConsole();
    // No execution yet: no anomaly entry point.
    expect(within(card()).queryByTestId("anomaly-panel")).toBeNull();

    await executeOnce();
    expect(
      within(panel()).getByTestId("btn-anomaly-open"),
    ).toBeInTheDocument();
  });

  it("submits a report: polling shows category, content, reporter and time", async () => {
    await renderConsole();
    await executeOnce();

    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    const form = within(panel()).getByTestId("anomaly-form");

    // Pick a non-default category and describe the on-site anomaly.
    fireEvent.change(within(form).getByTestId("anomaly-category"), {
      target: { value: "environment" },
    });
    fireEvent.change(within(form).getByTestId("anomaly-description"), {
      target: { value: "侧台追光直射操作员视线" },
    });
    fireEvent.click(within(form).getByTestId("btn-anomaly-submit"));

    // The pending record is created and, via the next poll, rendered with
    // reporter, report time, category and content.
    const record = await within(panel()).findByTestId("anomaly-record");
    await waitFor(() =>
      expect(within(record).getByTestId("anomaly-status")).toHaveTextContent(
        "待确认",
      ),
    );
    expect(within(record).getByTestId("anomaly-category-text")).toHaveTextContent(
      "环境异常",
    );
    expect(
      within(record).getByTestId("anomaly-description-text"),
    ).toHaveTextContent("侧台追光直射操作员视线");
    expect(within(record).getByTestId("anomaly-reporter")).toHaveTextContent(
      "升降台一席",
    );
    expect(within(record).getByTestId("anomaly-reported-at")).toHaveTextContent(
      /报告时间：\d{2}:\d{2}:\d{2}/,
    );
    // The report entry is gone while a record exists, and the reporting
    // console gets only the "awaiting the next shift" hint.
    expect(within(panel()).queryByTestId("btn-anomaly-open")).toBeNull();
    expect(within(record).queryByTestId("btn-anomaly-confirm")).toBeNull();
    expect(
      within(record).getByTestId("anomaly-await-confirm"),
    ).toBeInTheDocument();

    // Exactly one record, on the latest event, stamped with this console id.
    const eventId = server.lastEventIds[ACTION]!;
    expect(Object.keys(server.anomalies)).toHaveLength(1);
    expect(server.anomalies[eventId].status).toBe("pending");
    expect(server.anomalies[eventId].reported_by).toBe("升降台一席");
    expect(server.anomalies[eventId].reporter_id).toBe(CONSOLE_A);
  });

  it("gives inline feedback for a blank description without sending a request", async () => {
    await renderConsole();
    await executeOnce();

    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    const form = within(panel()).getByTestId("anomaly-form");

    // Empty submit: feedback appears in place, no record is created.
    fireEvent.click(within(form).getByTestId("btn-anomaly-submit"));
    expect(within(form).getByTestId("anomaly-error")).toHaveTextContent(
      /异常说明不能为空/,
    );
    expect(Object.keys(server.anomalies)).toHaveLength(0);

    // Whitespace-only is blank as well.
    fireEvent.change(within(form).getByTestId("anomaly-description"), {
      target: { value: "   " },
    });
    fireEvent.click(within(form).getByTestId("btn-anomaly-submit"));
    expect(within(form).getByTestId("anomaly-error")).toBeInTheDocument();
    expect(Object.keys(server.anomalies)).toHaveLength(0);

    // Typing clears the inline error; submitting then succeeds.
    fireEvent.change(within(form).getByTestId("anomaly-description"), {
      target: { value: "上升 3 米处异响" },
    });
    expect(within(form).queryByTestId("anomaly-error")).toBeNull();
    fireEvent.click(within(form).getByTestId("btn-anomaly-submit"));
    await within(panel()).findByTestId("anomaly-record");
    expect(Object.keys(server.anomalies)).toHaveLength(1);
  });

  it("renaming the reporting page to the next shift does not reveal confirm", async () => {
    await renderConsole("升降台一席", CONSOLE_A);
    await executeOnce();

    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    fireEvent.change(
      within(panel()).getByTestId("anomaly-description"),
      { target: { value: "限位开关偶发误触发" } },
    );
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-submit"));
    await within(panel()).findByTestId("anomaly-record");

    // The operator edits the SEAT NAME on the SAME page to impersonate the
    // next shift. The console id is unchanged, so no confirm entry appears —
    // the bypass is closed in the UI (and rejected by the server too).
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "下一班-B" },
    });
    await waitFor(() =>
      expect(
        within(panel()).queryByTestId("btn-anomaly-confirm"),
      ).toBeNull(),
    );
    expect(
      within(panel()).getByTestId("anomaly-await-confirm"),
    ).toBeInTheDocument();
    expect(server.anomalies[server.lastEventIds[ACTION]!].status).toBe(
      "pending",
    );
  });

  it("another browser console confirms: record becomes confirmed with seat and time", async () => {
    // Console A executes and reports.
    await renderConsole("升降台一席", CONSOLE_A);
    await executeOnce();
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    fireEvent.change(
      within(panel()).getByTestId("anomaly-description"),
      { target: { value: "限位开关偶发误触发" } },
    );
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-submit"));
    await within(panel()).findByTestId("anomaly-record");

    // A genuinely different browser console (the next shift) opens the page;
    // even typing the SAME seat name the reporter used, it is another seat.
    await reopenConsole(CONSOLE_B, "下一班-B");
    const confirmButton = within(panel()).getByTestId("btn-anomaly-confirm");
    expect(within(panel()).queryByTestId("anomaly-await-confirm")).toBeNull();
    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(within(panel()).getByTestId("anomaly-status")).toHaveTextContent(
        "已确认",
      ),
    );
    const confirmation = within(panel()).getByTestId("anomaly-confirmation");
    expect(within(confirmation).getByTestId("anomaly-confirmer")).toHaveTextContent(
      "下一班-B",
    );
    expect(
      within(confirmation).getByTestId("anomaly-confirmed-at"),
    ).toHaveTextContent(/确认时间：\d{2}:\d{2}:\d{2}/);
    // First report's content is preserved.
    expect(
      within(panel()).getByTestId("anomaly-description-text"),
    ).toHaveTextContent("限位开关偶发误触发");
    expect(within(panel()).getByTestId("anomaly-reporter")).toHaveTextContent(
      "升降台一席",
    );
    expect(
      within(panel()).queryByTestId("btn-anomaly-confirm"),
    ).toBeNull();

    const record = server.anomalies[server.lastEventIds[ACTION]!];
    expect(record.status).toBe("confirmed");
    expect(record.confirmed_by).toBe("下一班-B");
    expect(record.confirmer_id).toBe(CONSOLE_B);
    expect(record.reporter_id).toBe(CONSOLE_A);
  });

  it("switches to the new execution event: old anomaly is hidden but kept", async () => {
    await renderConsole("升降台一席", CONSOLE_A);
    await executeOnce();
    const firstEventId = server.lastEventIds[ACTION]!;

    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    fireEvent.change(
      within(panel()).getByTestId("anomaly-description"),
      { target: { value: "第一次执行后的异常" } },
    );
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-submit"));
    await within(panel()).findByTestId("anomaly-record");

    // The reporting console cannot self-confirm; the next shift's console
    // acknowledges it.
    await reopenConsole(CONSOLE_B, "下一班-B");
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-confirm"));
    await waitFor(() =>
      expect(within(panel()).getByTestId("anomaly-status")).toHaveTextContent(
        "已确认",
      ),
    );

    // Back on the first console, a new execution arrives: the card switches
    // naturally to the new event and must not show the previous anomaly.
    await renderConsole("升降台一席", CONSOLE_A);
    await executeOnce();
    await waitFor(() =>
      expect(within(panel()).queryByTestId("anomaly-record")).toBeNull(),
    );
    expect(
      within(panel()).getByTestId("btn-anomaly-open"),
    ).toBeInTheDocument();
    expect(server.lastEventIds[ACTION]).not.toBe(firstEventId);

    // The old record survives untouched in storage.
    expect(server.anomalies[firstEventId].status).toBe("confirmed");
    expect(server.anomalies[firstEventId].description).toBe("第一次执行后的异常");

    // A new report attaches to the NEW event.
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-open"));
    fireEvent.change(
      within(panel()).getByTestId("anomaly-description"),
      { target: { value: "第二次执行后的新异常" } },
    );
    fireEvent.click(within(panel()).getByTestId("btn-anomaly-submit"));
    const record = await within(panel()).findByTestId("anomaly-record");
    expect(
      within(record).getByTestId("anomaly-description-text"),
    ).toHaveTextContent("第二次执行后的新异常");
    expect(Object.keys(server.anomalies)).toHaveLength(2);
  });
});
