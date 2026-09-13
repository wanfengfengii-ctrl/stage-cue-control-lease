import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ActionState, type ActionsSnapshot, type ApiError } from "./api";
import { detectTakeover, formatClock, remainingSeconds } from "./lease";

const TOKEN_KEY = "handover.tokens.v1";
const SEAT_KEY = "handover.seat.v1";

// Device each action physically belongs to. A linked cue coordinates the
// lifting platform WITH the flying hoist, so a valid linked pair is exactly
// two held actions on two different devices. Two directions of one device
// (lift_up + lift_down) are mutually exclusive and never linkable; the
// emergency stop never participates.
const DEVICE_OF: Record<string, string> = {
  lift_up: "lift",
  lift_down: "lift",
  hoist_fly_in: "hoist",
  hoist_fly_out: "hoist",
};

type Notice = { kind: "success" | "error" | "info"; text: string };

function loadTokens(): Record<string, string> {
  try {
    return JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "{}");
  } catch {
    return {};
  }
}

export default function App() {
  const [seat, setSeat] = useState(
    () => sessionStorage.getItem(SEAT_KEY) || "",
  );
  const [snapshot, setSnapshot] = useState<ActionsSnapshot | null>(null);
  const [tokens, setTokens] = useState<Record<string, string>>(loadTokens);
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [linkedBusy, setLinkedBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const fetchStartRef = useRef<number>(Date.now());
  const seatRef = useRef(seat);
  seatRef.current = seat;

  const ttl = snapshot?.ttl_seconds ?? 30;
  const pollMs = Number(import.meta.env.VITE_POLL_MS ?? 1000);

  const persistSeat = (name: string) => {
    setSeat(name);
    sessionStorage.setItem(SEAT_KEY, name);
  };

  const setToken = (actionId: string, token: string | null) => {
    setTokens((prev) => {
      const next = { ...prev };
      if (token) next[actionId] = token;
      else delete next[actionId];
      sessionStorage.setItem(TOKEN_KEY, JSON.stringify(next));
      return next;
    });
  };

  const notice = (actionId: string, n: Notice) =>
    setNotices((prev) => ({ ...prev, [actionId]: n }));

  // ---- short polling: every browser session sees the same live snapshot ----
  const refresh = useCallback(async () => {
    fetchStartRef.current = Date.now();
    const data = await api.listActions();
    setSnapshot(data);
  }, []);

  useEffect(() => {
    if (!seat) return;
    let stopped = false;
    const loop = async () => {
      try {
        await refresh();
      } catch {
        /* transient network error: keep polling */
      } finally {
        if (!stopped) timer = window.setTimeout(loop, pollMs);
      }
    };
    let timer = window.setTimeout(loop, 0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [seat, refresh, pollMs]);

  // 1 Hz local re-render for the countdown (no extra HTTP request).
  useEffect(() => {
    if (!seat) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [seat]);

  const guardSeat = (): string | null => {
    const name = seat.trim();
    if (!name) {
      alert("请先填写本席名称（例如：升降台控制席）");
      return null;
    }
    return name;
  };

  const run = async (actionId: string, fn: () => Promise<void>) => {
    setBusy((b) => ({ ...b, [actionId]: true }));
    try {
      await fn();
    } finally {
      setBusy((b) => ({ ...b, [actionId]: false }));
      void refresh();
    }
  };

  const onAcquire = (actionId: string) =>
    run(actionId, async () => {
      const name = guardSeat();
      if (!name) return;
      try {
        const grant = await api.acquire(actionId, name);
        setToken(actionId, grant.token);
        notice(actionId, {
          kind: "success",
          text: `已取得 30 秒控制权，令牌仅本会话可见。`,
        });
      } catch (e) {
        const err = e as ApiError;
        notice(actionId, {
          kind: "error",
          text:
            err.code === "lease_held"
              ? `争抢失败：${err.message}`
              : err.message,
        });
      }
    });

  const onRenew = (actionId: string) =>
    run(actionId, async () => {
      const token = tokens[actionId];
      if (!token) return;
      try {
        await api.renew(actionId, token);
        notice(actionId, { kind: "success", text: "已续期 30 秒。" });
      } catch (e) {
        handleStale(actionId, e as ApiError);
      }
    });

  const onRelease = (actionId: string) =>
    run(actionId, async () => {
      const token = tokens[actionId];
      if (!token) return;
      try {
        await api.release(actionId, token);
        setToken(actionId, null);
        notice(actionId, { kind: "info", text: "已主动释放控制权。" });
      } catch (e) {
        handleStale(actionId, e as ApiError);
      }
    });

  const onForget = (actionId: string) => {
    setToken(actionId, null);
    notice(actionId, { kind: "info", text: "已清除本会话保存的旧令牌，可重新申请。" });
    void refresh();
  };

  const onExecute = (actionId: string) =>
    run(actionId, async () => {
      const token = tokens[actionId];
      if (!token) return;
      try {
        const out = await api.execute(actionId, token);
        notice(actionId, {
          kind: "success",
          text: `执行成功（动作事件 #${out.event_id}，UTC ${formatClock(
            out.occurred_at,
          )}），事件仅写入一次。`,
        });
        setToken(actionId, null);
      } catch (e) {
        handleStale(actionId, e as ApiError);
      }
    });

  // Late renew/release/execute with an old token MUST surface "control lost".
  // The stale token is deliberately retained: the reconnected old page keeps
  // clicking with its old credential and every click is rejected anew.
  const handleStale = (actionId: string, err: ApiError) => {
    if (err.code === "control_lost") {
      notice(actionId, {
        kind: "error",
        text: `控制权已失效：${
          err.state?.holder ? `当前持有者为「${err.state.holder}」，` : ""
        }本旧页面的操作已被服务端拒绝。`,
      });
    } else {
      notice(actionId, { kind: "error", text: err.message });
    }
  };

  const actions = snapshot?.actions ?? [];
  const nowMs = Date.now();
  const fetchStartMs = fetchStartRef.current;
  void tick; // re-render each second

  // The server trims seat names on acquisition, so every holder comparison
  // uses the trimmed local name: " 联排控制席 " must behave exactly like
  // "联排控制席".
  const mySeat = seat.trim();

  // Actions this seat currently controls (valid lease + token in session).
  const myHeld = actions.filter(
    (s) => tokens[s.action_id] && s.holder === mySeat && s.status === "held",
  );
  const labelOf = (actionId: string) =>
    actions.find((a) => a.action_id === actionId)?.label ?? actionId;

  // The linked cue is EXACTLY one lifting-platform action plus one
  // flying-hoist action. Holding three actions, or two directions of the
  // same device, yields no linked entry point.
  const linkedPair =
    myHeld.length === 2 &&
    myHeld.every((s) => DEVICE_OF[s.action_id] !== undefined) &&
    new Set(myHeld.map((s) => DEVICE_OF[s.action_id])).size === 2
      ? myHeld
      : null;

  // Linked execution: submit the cross-device pair as ONE atomic operation.
  const onExecuteLinked = async () => {
    const held = linkedPair;
    if (!held) return;
    const items = held.map((s) => ({
      action_id: s.action_id,
      token: tokens[s.action_id]!,
    }));
    setLinkedBusy(true);
    try {
      const out = await api.executeLinked(items);
      for (const it of items) {
        setToken(it.action_id, null);
        const ev = out.events.find((e) => e.action_id === it.action_id);
        notice(it.action_id, {
          kind: "success",
          text: `联动执行成功（联动标识 ${out.link_id.slice(0, 8)}…，事件 #${
            ev?.event_id ?? "?"
          }），${items.length} 个动作共享同一标识、各写入一条事件。`,
        });
      }
    } catch (e) {
      const err = e as ApiError;
      // Keep every card's real control state: no token is cleared here.
      // The failed card is told to re-acquire; the others learn that the
      // whole run was cancelled and their lease is untouched.
      const failedId = err.action_id;
      const failedLabel = failedId ? labelOf(failedId) : null;
      for (const it of items) {
        if (failedId && it.action_id === failedId) {
          notice(it.action_id, {
            kind: "error",
            text: `联动未执行：${err.message}。请重新取得控制权后再次提交联动。`,
          });
        } else {
          notice(it.action_id, {
            kind: failedId ? "info" : "error",
            text: failedId
              ? `联动未执行：因「${failedLabel}」控制权失效，整次联动已取消，本动作保持原状态（未执行、未终结租约）。`
              : `联动未执行：${err.message}`,
          });
        }
      }
    } finally {
      setLinkedBusy(false);
      void refresh();
    }
  };

  const rows = useMemo(
    () =>
      actions.map((state) => (
        <ActionRow
          key={state.action_id}
          state={state}
          nowMs={nowMs}
          fetchStartMs={fetchStartMs}
          ttl={ttl}
          mySeat={mySeat}
          myToken={tokens[state.action_id] ?? null}
          notice={notices[state.action_id]}
          busy={!!busy[state.action_id]}
          onAcquire={() => onAcquire(state.action_id)}
          onRenew={() => onRenew(state.action_id)}
          onRelease={() => onRelease(state.action_id)}
          onExecute={() => onExecute(state.action_id)}
          onForget={() => onForget(state.action_id)}
        />
      )),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actions, nowMs, seat, tokens, notices, busy, ttl],
  );

  return (
    <main className="page">
      <header>
        <h1>联排控制权交接台</h1>
        <p className="sub">升降台 × 飞行吊点 · 危险动作同一时刻仅一个有效租约（30 秒，服务端 UTC 判定）</p>
      </header>

      <section className="seat-bar" data-testid="seat-bar">
        <label htmlFor="seat">本席名称</label>
        <input
          id="seat"
          value={seat}
          placeholder="如：升降台控制席 / 飞行吊点控制席"
          onChange={(e) => persistSeat(e.target.value)}
          maxLength={64}
        />
        <span className="hint">每个浏览器会话独立，令牌不会跨标签页共享。</span>
      </section>

      {!seat.trim() ? (
        <p className="please">请填写本席名称以查看动作状态。</p>
      ) : (
        <>
          {linkedPair && (
            <section className="linked-bar" data-testid="linked-bar">
              <span className="linked-info">
                本席已持有升降台与飞行吊点各一个动作的控制权（
                {linkedPair.map((s) => s.label).join(" + ")}
                ），可作为一次联动提交。
              </span>
              <button
                type="button"
                className="danger"
                data-testid="btn-execute-linked"
                disabled={linkedBusy}
                onClick={onExecuteLinked}
              >
                联动执行（一次提交 · 全部成功或全部不变）
              </button>
            </section>
          )}
          <section className="grid" data-testid="actions">
            {rows}
          </section>
        </>
      )}

      <footer>
        {snapshot && (
          <span>
            服务端时间 {formatClock(snapshot.server_time)} UTC · 每秒短轮询
          </span>
        )}
      </footer>
    </main>
  );
}

interface RowProps {
  state: ActionState;
  nowMs: number;
  fetchStartMs: number;
  ttl: number;
  mySeat: string;
  myToken: string | null;
  notice?: Notice;
  busy: boolean;
  onAcquire: () => void;
  onRenew: () => void;
  onRelease: () => void;
  onExecute: () => void;
  onForget: () => void;
}

function ActionRow(props: RowProps) {
  const {
    state,
    nowMs,
    fetchStartMs,
    ttl,
    mySeat,
    myToken,
    busy,
  } = props;
  const left =
    state.status === "held"
      ? remainingSeconds(state, nowMs, fetchStartMs, ttl)
      : 0;

  const iHold = !!myToken && state.holder === mySeat && state.status === "held";
  const lostButToken =
    !!myToken && detectTakeover(state, mySeat, true);

  return (
    <article
      className={
        "card " + (iHold ? "mine" : state.status === "held" ? "other" : "free")
      }
      data-testid={`action-${state.action_id}`}
      data-status={state.status}
      data-mine={iHold ? "1" : "0"}
    >
      <div className="card-head">
        <h2>{state.label}</h2>
        <span className="badge" data-testid="badge">
          {state.status === "held" ? (
            <>
              持有席位：<strong>{state.holder}</strong>
            </>
          ) : (
            "空闲可申请"
          )}
        </span>
      </div>

      <div className="meta">
        <span data-testid="remaining">
          剩余：<strong>{state.status === "held" ? left : 0}</strong> 秒
        </span>
        <span data-testid="expires">到期时刻：{formatClock(state.expires_at)}</span>
        <span data-testid="events">
          历史执行：{state.event_count} 次
          {state.last_executed_by ? `（最后：${state.last_executed_by}）` : ""}
        </span>
        {state.last_link_id && (
          <span data-testid="last-link" title={state.last_link_id}>
            最近联动：{state.last_link_id.slice(0, 8)}…
          </span>
        )}
      </div>

      <div className="buttons">
        {!myToken && (
          <button
            type="button"
            className="primary"
            disabled={busy || state.status === "held"}
            onClick={props.onAcquire}
            data-testid="btn-acquire"
          >
            申请控制权
          </button>
        )}
        {iHold && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={props.onRenew}
              data-testid="btn-renew"
            >
              续期 30 秒
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={props.onExecute}
              data-testid="btn-execute"
            >
              执行危险动作
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={props.onRelease}
              data-testid="btn-release"
            >
              释放
            </button>
          </>
        )}
        {/* Reconnected old page: stale token remains, buttons still clickable
            so the server explicitly rejects every late operation. */}
        {lostButToken && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={props.onRenew}
              data-testid="btn-renew-stale"
            >
              旧令牌续期
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={props.onExecute}
              data-testid="btn-execute-stale"
            >
              旧令牌执行
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={props.onRelease}
              data-testid="btn-release-stale"
            >
              旧令牌释放
            </button>
            {state.status === "free" && (
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={props.onForget}
                data-testid="btn-forget"
              >
                丢弃旧令牌并重新申请
              </button>
            )}
          </>
        )}
      </div>

      {lostButToken && (
        <p className="notice error" data-testid="lost-banner">
          控制权已失效：当前动作由「{state.holder ?? "—"}」持有或已空闲，
          本页面保存的是旧令牌，任何操作都会被服务端拒绝。
        </p>
      )}
      {props.notice && (
        <p className={`notice ${props.notice.kind}`} data-testid="notice">
          {props.notice.text}
        </p>
      )}
    </article>
  );
}
