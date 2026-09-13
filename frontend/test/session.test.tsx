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

const LIFT = "lift_up";
const HOIST = "hoist_fly_in";
const EXTRA = "emergency_stop";

const card = (id: string) => screen.getByTestId(`action-${id}`);
const summary = () => screen.getByTestId("session-summary");

async function renderConsole(seat = "联排负责人") {
  render(<App />);
  fireEvent.change(screen.getByLabelText("本席名称"), {
    target: { value: seat },
  });
  await waitFor(() =>
    expect(within(card(LIFT)).getByTestId("btn-acquire")).toBeEnabled(),
  );
}

async function acquireAndExecute(id: string) {
  fireEvent.click(within(card(id)).getByTestId("btn-acquire"));
  await waitFor(() =>
    expect(within(card(id)).getByTestId("btn-execute")).toBeInTheDocument(),
  );
  fireEvent.click(within(card(id)).getByTestId("btn-execute"));
  await waitFor(() =>
    expect(within(card(id)).getByTestId("notice")).toHaveTextContent(
      /执行成功/,
    ),
  );
}

async function startSession(name: string) {
  fireEvent.change(screen.getByTestId("session-name"), {
    target: { value: name },
  });
  fireEvent.click(screen.getByTestId("btn-session-start"));
  await waitFor(() =>
    expect(screen.getByTestId("session-summary-status")).toHaveTextContent(
      "进行中",
    ),
  );
}

describe("App rehearsal session (场次) flow", () => {
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

  it("start → single + linked executes → end: summary freezes and stays visible", async () => {
    await renderConsole();

    // No session yet: no summary, and the end button is disabled.
    expect(screen.queryByTestId("session-summary")).toBeNull();
    expect(screen.getByTestId("btn-session-end")).toBeDisabled();

    await startSession("第一轮联排");
    expect(summary()).toHaveTextContent("场次「第一轮联排」");
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 0 次",
    );
    expect(screen.getByTestId("session-summary-actions")).toHaveTextContent(
      "涉及动作 0 个",
    );
    expect(screen.getByTestId("btn-session-end")).toBeEnabled();

    // Single execution: one event, one involved action.
    await acquireAndExecute(LIFT);
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
        "累计事件 1 次",
      ),
    );
    expect(screen.getByTestId("session-summary-actions")).toHaveTextContent(
      "涉及动作 1 个",
    );

    // Linked execution of the cross-device pair: +2 events, +1 action.
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
      expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
        "累计事件 3 次",
      ),
    );
    expect(screen.getByTestId("session-summary-actions")).toHaveTextContent(
      "涉及动作 2 个",
    );

    // End the round: the summary stays on screen, marked ended.
    fireEvent.click(screen.getByTestId("btn-session-end"));
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-status")).toHaveTextContent(
        "已结束",
      ),
    );
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 3 次",
    );
    expect(screen.getByTestId("btn-session-end")).toBeDisabled();

    // Actions executed afterwards no longer count towards the ended round.
    await acquireAndExecute(EXTRA);
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 3 次",
    );
    expect(screen.getByTestId("session-summary-actions")).toHaveTextContent(
      "涉及动作 2 个",
    );
    expect(server.eventLog.filter((e) => e.session_id === 1)).toHaveLength(3);
    expect(server.eventLog).toHaveLength(4);
  });

  it("blank session name is rejected and starts nothing", async () => {
    await renderConsole();
    fireEvent.click(screen.getByTestId("btn-session-start"));
    await waitFor(() =>
      expect(screen.getByTestId("session-notice")).toHaveTextContent(
        /场次名称不能为空/,
      ),
    );
    expect(screen.queryByTestId("session-summary")).toBeNull();
    expect(server.sessions).toHaveLength(0);
  });

  it("duplicate start is rejected and the running round is untouched", async () => {
    await renderConsole();
    await startSession("第一轮联排");
    await acquireAndExecute(LIFT);
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
        "累计事件 1 次",
      ),
    );

    // A second start while one is active: identifiable error, no change.
    fireEvent.change(screen.getByTestId("session-name"), {
      target: { value: "插队场次" },
    });
    fireEvent.click(screen.getByTestId("btn-session-start"));
    await waitFor(() =>
      expect(screen.getByTestId("session-notice")).toHaveTextContent(
        /已有进行中的场次/,
      ),
    );
    expect(server.sessions).toHaveLength(1);
    expect(summary()).toHaveTextContent("场次「第一轮联排」");
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 1 次",
    );
  });

  it("a new round after ending starts from zero while the old summary was frozen", async () => {
    await renderConsole();
    await startSession("第一轮联排");
    await acquireAndExecute(LIFT);
    fireEvent.click(screen.getByTestId("btn-session-end"));
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-status")).toHaveTextContent(
        "已结束",
      ),
    );
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 1 次",
    );

    await startSession("第二轮联排");
    expect(summary()).toHaveTextContent("场次「第二轮联排」");
    expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
      "累计事件 0 次",
    );
    await acquireAndExecute(HOIST);
    await waitFor(() =>
      expect(screen.getByTestId("session-summary-events")).toHaveTextContent(
        "累计事件 1 次",
      ),
    );
    // The first round kept its single event; the new one counts only its own.
    expect(server.eventLog.filter((e) => e.session_id === 1)).toHaveLength(1);
    expect(server.eventLog.filter((e) => e.session_id === 2)).toHaveLength(1);
  });
});
