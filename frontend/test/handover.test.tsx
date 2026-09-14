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
const TOKEN_KEY = "handover.tokens.v1";
const CONSOLE_A = "console-test-aaaa";
const CONSOLE_B = "console-test-bbbb";

const card = () => screen.getByTestId(`action-${ACTION}`);
const panel = () => within(card()).getByTestId("handover-panel");

async function renderConsole(seat: string, consoleId: string) {
  cleanup();
  sessionStorage.setItem(CONSOLE_ID_KEY, consoleId);
  render(<App />);
  fireEvent.change(screen.getByLabelText("本席名称"), {
    target: { value: seat },
  });
  // The card's badge renders on the first polled snapshot regardless of
  // whether this console already holds a token.
  await waitFor(() =>
    expect(within(card()).getByTestId("badge")).toBeInTheDocument(),
  );
}

/**
 * Switch to ANOTHER browser console: a different browser has its own
 * sessionStorage, so tokens never leak across consoles. Pass `keepTokens`
 * to reopen the SAME console (its tokens ride along).
 */
async function switchConsole(
  consoleId: string,
  seat: string,
  keepTokens: string | null = null,
) {
  if (keepTokens !== null) sessionStorage.setItem(TOKEN_KEY, keepTokens);
  else sessionStorage.removeItem(TOKEN_KEY);
  await renderConsole(seat, consoleId);
}

async function acquire() {
  fireEvent.click(within(card()).getByTestId("btn-acquire"));
  await waitFor(() =>
    expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
  );
}

/** Console A acquires and initiates; returns the one-time code shown. */
async function acquireAndInitiate(): Promise<string> {
  await acquire();
  fireEvent.click(within(panel()).getByTestId("btn-handover-initiate"));
  await waitFor(() =>
    expect(within(panel()).getByTestId("handover-code")).toBeInTheDocument(),
  );
  const text = within(panel()).getByTestId("handover-code").textContent ?? "";
  const code = text.match(/[A-Z2-9]{8}/)?.[0];
  expect(code).toBeTruthy();
  return code!;
}

describe("App shift handover (换班交接) on the action card", () => {
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

  it("holder generates a one-time code and keeps every right until acceptance", async () => {
    await renderConsole("交班席", CONSOLE_A);
    const code = await acquireAndInitiate();
    expect(code).toMatch(/^[A-Z2-9]{8}$/);

    // The code is shown exactly once with the pending hint.
    expect(
      within(panel()).getByTestId("handover-rights"),
    ).toHaveTextContent("对方接收前，您仍可续期、释放或执行");

    // The lease is untouched: renew and execute still work right away.
    fireEvent.click(within(card()).getByTestId("btn-renew"));
    await waitFor(() =>
      expect(within(card()).getByTestId("notice")).toHaveTextContent("已续期"),
    );
    fireEvent.click(within(card()).getByTestId("btn-execute"));
    await waitFor(() =>
      expect(within(card()).getByTestId("notice")).toHaveTextContent("执行成功"),
    );

    // Execution ended the lease: the pending record reads as invalidated.
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-invalid"),
      ).toHaveTextContent("上一次换班交接已失效"),
    );
  });

  it("transfers control to the receiving console; the old token dies", async () => {
    await renderConsole("交班席", CONSOLE_A);
    const code = await acquireAndInitiate();
    const tokensOfA = sessionStorage.getItem(TOKEN_KEY);

    // Console B (another browser) sees the accept form on the same card.
    await switchConsole(CONSOLE_B, "接班席");
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-accept-form"),
      ).toBeInTheDocument(),
    );
    expect(within(panel()).getByTestId("handover-from")).toHaveTextContent(
      "「交班席」发起了换班交接",
    );

    // A wrong code is an identifiable, in-place error; nothing changes.
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: "WRONGCODE" },
    });
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(panel()).getByTestId("handover-error")).toHaveTextContent(
        "交接码错误",
      ),
    );
    expect(within(card()).queryByTestId("btn-execute")).toBeNull();

    // The right code transfers control: B gets the holder controls.
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: code.toLowerCase() }, // case-insensitive
    });
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(card()).getByTestId("notice")).toHaveTextContent(
        /已接收「.*」控制权/,
      ),
    );
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );

    // B really holds it: execution succeeds and writes exactly one event.
    fireEvent.click(within(card()).getByTestId("btn-execute"));
    await waitFor(() =>
      expect(within(card()).getByTestId("notice")).toHaveTextContent("执行成功"),
    );

    // Console A reopens with its OLD token: the poll flags the takeover.
    await switchConsole(CONSOLE_A, "交班席", tokensOfA);
    await waitFor(() =>
      expect(within(card()).getByTestId("lost-banner")).toHaveTextContent(
        "控制权已",
      ),
    );
  });

  it("same seat name on both consoles: console identity still flags the transfer", async () => {
    await renderConsole("联排控制席", CONSOLE_A);
    const code = await acquireAndInitiate();
    const tokensOfA = sessionStorage.getItem(TOKEN_KEY);

    // The relieving console uses the SAME seat name (a different browser).
    await switchConsole(CONSOLE_B, "联排控制席");
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-accept-form"),
      ).toBeInTheDocument(),
    );
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: code },
    });
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );

    // Back on console A the holder NAME is identical, yet the accepted
    // record's console ids prove the transfer: stale-token banner, not
    // holder controls.
    await switchConsole(CONSOLE_A, "联排控制席", tokensOfA);
    await waitFor(() =>
      expect(within(card()).getByTestId("lost-banner")).toHaveTextContent(
        "控制权已交接",
      ),
    );
    expect(within(card()).queryByTestId("btn-execute")).toBeNull();
    expect(within(card()).getByTestId("btn-execute-stale")).toBeInTheDocument();
    expect(
      within(panel()).getByTestId("handover-done"),
    ).toHaveTextContent("已交接给「联排控制席」");
  });

  it("initiating console never gets the accept form for its own code", async () => {
    await renderConsole("交班席", CONSOLE_A);
    await acquireAndInitiate();
    // The initiator sees its pending code, never an accept form.
    expect(
      within(panel()).queryByTestId("handover-accept-form"),
    ).toBeNull();
    expect(
      within(panel()).getByTestId("handover-code"),
    ).toBeInTheDocument();
  });

  it("expired handover is rejected with an identifiable reason; the action can be re-applied", async () => {
    await renderConsole("交班席", CONSOLE_A);
    const code = await acquireAndInitiate();

    // Console B opens the accept form while the lease is still live...
    await switchConsole(CONSOLE_B, "接班席");
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-accept-form"),
      ).toBeInTheDocument(),
    );
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: code },
    });

    // ...but the lease expires exactly before the confirm click lands.
    server.elapse30s();
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(panel()).getByTestId("handover-error")).toHaveTextContent(
        "接收时租约刚好到期",
      ),
    );

    // No new lease was created: the action is free and B can apply normally.
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-invalid"),
      ).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-acquire")).toBeEnabled(),
    );
    fireEvent.click(within(card()).getByTestId("btn-acquire"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );
  });

  it("re-generating replaces the code: the old one dies, the new one transfers", async () => {
    await renderConsole("交班席", CONSOLE_A);
    const first = await acquireAndInitiate();

    fireEvent.click(within(panel()).getByTestId("btn-handover-initiate"));
    await waitFor(() => {
      const text =
        within(panel()).getByTestId("handover-code").textContent ?? "";
      const code = text.match(/[A-Z2-9]{8}/)?.[0];
      expect(code).toBeTruthy();
      expect(code).not.toBe(first);
    });
    const second =
      within(panel())
        .getByTestId("handover-code")
        .textContent?.match(/[A-Z2-9]{8}/)?.[0] ?? "";

    await switchConsole(CONSOLE_B, "接班席");
    await waitFor(() =>
      expect(
        within(panel()).getByTestId("handover-accept-form"),
      ).toBeInTheDocument(),
    );
    // The replaced code is dead...
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: first },
    });
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(panel()).getByTestId("handover-error")).toHaveTextContent(
        "交接码错误",
      ),
    );
    // ...and the fresh code transfers control.
    fireEvent.change(within(panel()).getByTestId("handover-code-input"), {
      target: { value: second },
    });
    fireEvent.click(within(panel()).getByTestId("btn-handover-accept"));
    await waitFor(() =>
      expect(within(card()).getByTestId("btn-execute")).toBeInTheDocument(),
    );
  });
});
