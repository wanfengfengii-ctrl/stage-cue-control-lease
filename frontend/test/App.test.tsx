import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../src/App";
import { MockServer } from "./mock-server";

const ACTION = "lift_up";
const LIFT_DOWN = "lift_down";
const LINKED_SECOND = "hoist_fly_in";

function card() {
  return screen.getByTestId(`action-${ACTION}`);
}

function downCard() {
  return screen.getByTestId(`action-${LIFT_DOWN}`);
}

function secondCard() {
  return screen.getByTestId(`action-${LINKED_SECOND}`);
}

describe("App control-handover interactions", () => {
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

  it("renders all actions as free before login", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "升降台席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("badge")).toHaveTextContent("空闲可申请"),
    );
    expect(within(card()).getByTestId("btn-acquire")).toBeEnabled();
  });

  it("acquires a lease and shows holder + countdown", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "升降台席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    fireEvent.click(within(card()).getByTestId("btn-acquire"));

    await waitFor(() =>
      expect(within(card()).getByTestId("remaining")).toHaveTextContent(/3[0]/),
    );
    expect(within(card()).getByTestId("badge")).toHaveTextContent("升降台席");
    expect(within(card()).getByTestId("btn-execute")).toBeEnabled();
    expect(within(card()).queryByTestId("btn-acquire")).toBeNull();
  });

  it("concurrent contention: a second seat cannot acquire while held", async () => {
    server.leases[ACTION] = {
      token: "tok-other",
      holder: "飞行吊点席",
      expiresAt: server.serverNow + 30_000,
      released: false,
      executed: false,
    };
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "升降台席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("badge")).toHaveTextContent("飞行吊点席"),
    );
    // Held by someone else: acquire button is present but disabled,
    // so a simultaneous dangerous click cannot be sent.
    expect(within(card()).getByTestId("btn-acquire")).toBeDisabled();
  });

  it("execute succeeds once and records the event count", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "升降台席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    fireEvent.click(within(card()).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );
    fireEvent.click(within(card()).getByTestId("btn-execute"));

    await waitFor(() =>
      expect(within(card()).getByTestId("notice")).toHaveTextContent(/执行成功/),
    );
    expect(within(card()).getByTestId("events")).toHaveTextContent("历史执行：1 次");
    expect(server.events[ACTION]).toBe(1);
  });

  it("takeover after disconnect: old page clicks are explicitly rejected", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<App />);
      fireEvent.change(screen.getByLabelText("本席名称"), {
        target: { value: "失联席" },
      });
      await waitFor(() =>
        expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
      );
      fireEvent.click(within(card()).getByTestId("btn-acquire"));
      await waitFor(() =>
        expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
      );

      // Network outage + 30 s pass, then the other seat takes over.
      act(() => server.elapse30s());
      server.leases[ACTION] = {
        token: "tok-new",
        holder: "接管席",
        expiresAt: server.serverNow + 30_000,
        released: false,
        executed: false,
      };

      // Poll picks up the new holder; the old page flags its stale token.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_100);
      });
      await waitFor(() =>
        expect(within(card()).getByTestId("badge")).toHaveTextContent("接管席"),
      );
      expect(within(card()).getByTestId("lost-banner")).toHaveTextContent(
        /控制权已失效/,
      );

      // The old token's buttons are still clickable; server rejects execute.
      fireEvent.click(within(card()).getByTestId("btn-execute-stale"));
      await waitFor(() =>
        expect(within(card()).getByTestId("notice")).toHaveTextContent(
          /本旧页面的操作已被服务端拒绝|控制权已失效/,
        ),
      );
      // New lease is untouched: still held by 接管席, zero events.
      expect(server.leases[ACTION].token).toBe("tok-new");
      expect(server.leases[ACTION].executed).toBe(false);
      expect(server.events[ACTION] ?? 0).toBe(0);

      // Renew and release with the stale token are rejected too.
      fireEvent.click(within(card()).getByTestId("btn-renew-stale"));
      await waitFor(() =>
        expect(within(card()).getByTestId("notice")).toHaveTextContent(
          /控制权已失效/,
        ),
      );
      fireEvent.click(within(card()).getByTestId("btn-release-stale"));
      await waitFor(() =>
        expect(within(card()).getByTestId("notice")).toHaveTextContent(
          /控制权已失效/,
        ),
      );
      expect(server.leases[ACTION].released).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("App linked execution (two actions as one atomic submit)", () => {
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

  async function acquireBoth() {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "联排控制席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    fireEvent.click(within(card()).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );
    fireEvent.click(within(secondCard()).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(
        within(secondCard()).getByTestId("btn-execute"),
      ).toBeInTheDocument(),
    );
  }

  it("shows 联动执行 once both actions are held; success links both cards", async () => {
    await acquireBoth();

    // The linked bar appears only while this seat holds both actions.
    const bar = await screen.findByTestId("linked-bar");
    expect(bar).toHaveTextContent("升降台 上升");
    expect(bar).toHaveTextContent("飞行吊点 进场");

    fireEvent.click(within(bar).getByTestId("btn-execute-linked"));

    // Both cards report the linked success and each counts exactly one event.
    await waitFor(() => {
      expect(within(card()).getByTestId("notice")).toHaveTextContent(
        /联动执行成功/,
      );
      expect(within(secondCard()).getByTestId("notice")).toHaveTextContent(
        /联动执行成功/,
      );
    });
    await waitFor(() => {
      expect(within(card()).getByTestId("events")).toHaveTextContent(
        "历史执行：1 次",
      );
      expect(within(secondCard()).getByTestId("events")).toHaveTextContent(
        "历史执行：1 次",
      );
    });
    expect(server.events[ACTION]).toBe(1);
    expect(server.events[LINKED_SECOND]).toBe(1);

    // Both cards display the SAME server-generated link id.
    const linkA = within(card()).getByTestId("last-link").textContent;
    const linkB = within(secondCard()).getByTestId("last-link").textContent;
    expect(linkA).toBeTruthy();
    expect(linkA).toBe(linkB);

    // Both leases are finished: the bar disappears, acquire is offered again.
    await waitFor(() =>
      expect(screen.queryByTestId("linked-bar")).toBeNull(),
    );
    expect(within(card()).getByTestId("btn-acquire")).toBeEnabled();
  });

  it("failure names the failed action and keeps both cards' real state", async () => {
    await acquireBoth();
    const bar = await screen.findByTestId("linked-bar");

    // The hoist is taken over behind our back; no poll has refreshed yet,
    // so the linked button is still offered and the click goes out.
    server.leases[LINKED_SECOND] = {
      token: "tok-new",
      holder: "接管席",
      expiresAt: server.serverNow + 30_000,
      released: false,
      executed: false,
    };
    fireEvent.click(within(bar).getByTestId("btn-execute-linked"));

    // The failed card is told to re-acquire; the other card learns the
    // whole run was cancelled and its lease is untouched.
    await waitFor(() => {
      expect(within(secondCard()).getByTestId("notice")).toHaveTextContent(
        /联动未执行.*请重新取得控制权/,
      );
    });
    expect(within(card()).getByTestId("notice")).toHaveTextContent(
      /整次联动已取消，本动作保持原状态/,
    );

    // No side effects on the server: zero events, lift lease still live.
    expect(server.events[ACTION] ?? 0).toBe(0);
    expect(server.events[LINKED_SECOND] ?? 0).toBe(0);
    expect(server.leases[ACTION].executed).toBe(false);
    expect(server.leases[LINKED_SECOND].executed).toBe(false);

    // After the refresh both cards show their REAL control state:
    // the lift is still mine; the hoist shows the takeover banner.
    await waitFor(() =>
      expect(
        within(secondCard()).getByTestId("lost-banner"),
      ).toHaveTextContent(/控制权已失效/),
    );
    expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument();
    expect(screen.queryByTestId("linked-bar")).toBeNull();
  });

  async function acquireCards(ids: string[]) {
    for (const id of ids) {
      fireEvent.click(
        within(screen.getByTestId(`action-${id}`)).getByTestId("btn-acquire"),
      );
      await waitFor(() =>
        expect(
          within(screen.getByTestId(`action-${id}`)).getByTestId("btn-execute"),
        ).toBeInTheDocument(),
      );
    }
  }

  it("does not offer linkage for the two lift directions (same device)", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "升降台席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    await acquireCards([ACTION, LIFT_DOWN]);

    // Two directions of the SAME device are mutually exclusive: no link bar.
    expect(screen.queryByTestId("linked-bar")).toBeNull();
    // Each card is still recognised as held by this seat.
    expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument();
    expect(within(downCard()).getByTestId("btn-execute")).toBeInTheDocument();
  });

  it("does not offer linkage while holding three actions", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "联排控制席" },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    await acquireCards([ACTION, LIFT_DOWN, LINKED_SECOND]);

    // Three held actions (even though two form a cross-device pair) yield no
    // linked entry point: a linked cue is exactly two actions.
    expect(screen.queryByTestId("linked-bar")).toBeNull();
  });

  it("trims surrounding spaces in the seat name and still recognises holdings", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("本席名称"), {
      target: { value: "  联排控制席  " },
    });
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    await acquireCards([ACTION, LINKED_SECOND]);

    // Behaves exactly like the trimmed seat: both cards are "mine" and the
    // cross-device linked bar is offered.
    expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument();
    expect(within(secondCard()).getByTestId("btn-execute")).toBeInTheDocument();
    const bar = await screen.findByTestId("linked-bar");
    expect(bar).toBeVisible();

    // The server stores the trimmed name.
    expect(server.leases[ACTION]?.holder).toBe("联排控制席");
  });

  it("keeps the last linked executing seat visible after re-acquisition", async () => {
    await acquireBoth();
    fireEvent.click(
      within(await screen.findByTestId("linked-bar")).getByTestId(
        "btn-execute-linked",
      ),
    );
    await waitFor(() =>
      expect(within(card()).getByTestId("events")).toHaveTextContent(
        "历史执行：1 次",
      ),
    );
    expect(within(card()).getByTestId("events")).toHaveTextContent(
      "最后：联排控制席",
    );

    // Re-acquire one card: history count and last executing seat remain.
    fireEvent.click(within(card()).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );
    expect(within(card()).getByTestId("events")).toHaveTextContent(
      "历史执行：1 次",
    );
    expect(within(card()).getByTestId("events")).toHaveTextContent(
      "最后：联排控制席",
    );
    expect(within(card()).getByTestId("last-link")).toBeInTheDocument();
  });
});
