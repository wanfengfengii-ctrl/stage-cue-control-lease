import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../src/App";
import { MockServer } from "./mock-server";

const ACTION = "lift_up";

function card() {
  return screen.getByTestId(`action-${ACTION}`);
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
