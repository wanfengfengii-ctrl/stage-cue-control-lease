import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type ActionState,
  type ActionsSnapshot,
  type AnomalyCategory,
  type ApiError,
} from "./api";
import { detectTakeover, formatClock, remainingSeconds } from "./lease";

const TOKEN_KEY = "handover.tokens.v1";
const SEAT_KEY = "handover.seat.v1";
// Stable identity of THIS browser console. It is deliberately separate from
// the freely editable seat name: the server decides "another seat" by this
// id when acknowledging an anomaly, so renaming the reporting page to the
// next shift's name cannot let it confirm its own report.
const CONSOLE_ID_KEY = "handover.console-id.v1";

function loadOrCreateConsoleId(): string {
  try {
    const existing = sessionStorage.getItem(CONSOLE_ID_KEY);
    if (existing) return existing;
  } catch {
    /* sessionStorage unavailable: fall through to a fresh in-memory id */
  }
  const id =
    globalThis.crypto?.randomUUID?.() ??
    `console-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  try {
    sessionStorage.setItem(CONSOLE_ID_KEY, id);
  } catch {
    /* private mode etc.: the id still lasts for this page session */
  }
  return id;
}

// Anomaly categories offered on the card; the stored value is the code, the
// snapshot/poll renders this Chinese label.
export const ANOMALY_CATEGORY_LABELS: Record<AnomalyCategory, string> = {
  equipment: "设备异常",
  operation: "操作异常",
  environment: "环境异常",
  other: "其他异常",
};
const ANOMALY_CATEGORIES = Object.keys(
  ANOMALY_CATEGORY_LABELS,
) as AnomalyCategory[];

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
  // Fixed for this browser console; survives seat-name edits in the same
  // session, so the "another seat only" rule cannot be bypassed by renaming.
  const [consoleId] = useState(loadOrCreateConsoleId);
  const [snapshot, setSnapshot] = useState<ActionsSnapshot | null>(null);
  const [tokens, setTokens] = useState<Record<string, string>>(loadTokens);
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [linkedBusy, setLinkedBusy] = useState(false);
  const [sessionName, setSessionName] = useState("");
  const [sessionBusy, setSessionBusy] = useState(false);
  const [sessionNotice, setSessionNotice] = useState<Notice | null>(null);
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
  const session = snapshot?.session ?? null;
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

  // Session control: the rehearsal lead starts/ends the named round. The
  // server alone decides validity — a duplicate start, a blank name, or
  // ending with no active round comes back as a recognisable business error
  // and changes nothing; the notice surfaces it verbatim.
  const onSessionTransition = async (op: "start" | "end") => {
    setSessionBusy(true);
    try {
      const out = await api.sessionTransition(op, sessionName);
      setSessionNotice({
        kind: "success",
        text:
          op === "start"
            ? `场次「${out.name}」已开始，此后的动作事件将计入本轮。`
            : `场次「${out.name}」已结束：本轮共 ${out.event_count} 个事件、涉及 ${out.action_count} 个动作，摘要保持可查。`,
      });
      if (op === "start") setSessionName("");
    } catch (e) {
      const err = e as ApiError;
      setSessionNotice({ kind: "error", text: err.message });
    } finally {
      setSessionBusy(false);
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
          onAnomalyChanged={() => void refresh()}
          consoleId={consoleId}
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
          <section className="session-bar" data-testid="session-bar">
            <div className="session-controls">
              <label htmlFor="session-name">场次</label>
              <input
                id="session-name"
                data-testid="session-name"
                value={sessionName}
                placeholder="场次名称，如：第一轮联排"
                onChange={(e) => setSessionName(e.target.value)}
                maxLength={64}
              />
              <button
                type="button"
                className="primary"
                data-testid="btn-session-start"
                disabled={sessionBusy}
                onClick={() => onSessionTransition("start")}
              >
                开始场次
              </button>
              <button
                type="button"
                data-testid="btn-session-end"
                disabled={sessionBusy || session?.status !== "active"}
                onClick={() => onSessionTransition("end")}
              >
                结束场次
              </button>
            </div>
            {session && (
              <div
                className={`session-summary ${session.status}`}
                data-testid="session-summary"
                data-status={session.status}
              >
                <span data-testid="session-summary-name">
                  场次「{session.name}」
                </span>
                <span data-testid="session-summary-status">
                  {session.status === "active" ? "进行中" : "已结束"}
                </span>
                <span data-testid="session-summary-events">
                  累计事件 {session.event_count} 次
                </span>
                <span data-testid="session-summary-actions">
                  涉及动作 {session.action_count} 个
                </span>
              </div>
            )}
            {sessionNotice && (
              <p
                className={`notice ${sessionNotice.kind}`}
                data-testid="session-notice"
              >
                {sessionNotice.text}
              </p>
            )}
          </section>
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
  onAnomalyChanged: () => void;
  consoleId: string;
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
      {/* The anomaly record follows the card's most recent execution event;
          a key on that event id remounts the panel when a new event arrives,
          so an unsubmitted form or old state can never bleed across events. */}
      {state.last_event_id != null && (
        <AnomalyPanel
          key={state.last_event_id}
          state={state}
          seat={mySeat}
          consoleId={props.consoleId}
          onChanged={props.onAnomalyChanged}
        />
      )}
      {props.notice && (
        <p className={`notice ${props.notice.kind}`} data-testid="notice">
          {props.notice.text}
        </p>
      )}
    </article>
  );
}

interface AnomalyPanelProps {
  state: ActionState;
  seat: string;
  /** Stable identity of this browser console (independent of seat name). */
  consoleId: string;
  onChanged: () => void;
}

function AnomalyPanel({ state, seat, consoleId, onChanged }: AnomalyPanelProps) {
  const record = state.anomaly;
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<AnomalyCategory>("equipment");
  const [description, setDescription] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const submitReport = async () => {
    // Inline feedback for a blank description: no request is sent.
    if (!description.trim()) {
      setFormError("异常说明不能为空，请填写现场异常情况。");
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      await api.reportAnomaly(state.action_id, {
        category,
        description: description.trim(),
        reporter: seat,
        reporter_id: consoleId,
      });
      setOpen(false);
      setDescription("");
      onChanged();
    } catch (e) {
      const err = e as ApiError;
      if (err.code === "anomaly_exists") {
        // Another seat reported first: the record rides along; poll shows it.
        setOpen(false);
        onChanged();
      } else {
        setFormError(err.message);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const confirmRecord = async () => {
    setConfirming(true);
    try {
      await api.confirmAnomaly(state.action_id, seat, consoleId);
      onChanged();
    } catch (e) {
      const err = e as ApiError;
      // anomaly_confirmed / anomaly_not_found: the polled snapshot is the
      // authority — refresh to render what the server already holds.
      if (err.code === "anomaly_confirmed" || err.code === "anomaly_not_found") {
        onChanged();
      } else {
        // anomaly_self_confirm (the server never lets a seat confirm its
        // own report) and any other rejection: show it in place, keep the
        // record untouched.
        setFormError(err.message);
      }
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div className="anomaly" data-testid="anomaly-panel">
      {!record && !open && (
        <button
          type="button"
          data-testid="btn-anomaly-open"
          onClick={() => {
            setFormError(null);
            setOpen(true);
          }}
        >
          报告异常（最近一次执行事件）
        </button>
      )}

      {!record && open && (
        <div className="anomaly-form" data-testid="anomaly-form">
          <label>
            异常类别
            <select
              data-testid="anomaly-category"
              value={category}
              onChange={(e) => setCategory(e.target.value as AnomalyCategory)}
            >
              {ANOMALY_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {ANOMALY_CATEGORY_LABELS[c]}
                </option>
              ))}
            </select>
          </label>
          <textarea
            data-testid="anomaly-description"
            value={description}
            placeholder="填写现场异常情况，供下一班确认留痕"
            rows={2}
            maxLength={500}
            onChange={(e) => {
              setDescription(e.target.value);
              if (formError) setFormError(null);
            }}
          />
          {formError && (
            <p className="notice error" data-testid="anomaly-error">
              {formError}
            </p>
          )}
          <div className="anomaly-actions">
            <button
              type="button"
              className="danger"
              data-testid="btn-anomaly-submit"
              disabled={submitting}
              onClick={submitReport}
            >
              提交异常报告
            </button>
            <button
              type="button"
              data-testid="btn-anomaly-cancel"
              disabled={submitting}
              onClick={() => {
                setOpen(false);
                setFormError(null);
              }}
            >
              取消
            </button>
          </div>
        </div>
      )}

      {record && (
        <div
          className={`anomaly-record ${record.status}`}
          data-testid="anomaly-record"
          data-status={record.status}
        >
          <div className="anomaly-head">
            <span className="anomaly-tag" data-testid="anomaly-category-text">
              {ANOMALY_CATEGORY_LABELS[record.category as AnomalyCategory] ??
                record.category}
            </span>
            <span className="anomaly-status" data-testid="anomaly-status">
              {record.status === "pending" ? "待确认" : "已确认"}
            </span>
          </div>
          <p className="anomaly-text" data-testid="anomaly-description-text">
            {record.description}
          </p>
          <div className="anomaly-meta">
            <span data-testid="anomaly-reporter">
              报告人：{record.reported_by}
            </span>
            <span data-testid="anomaly-reported-at">
              报告时间：{formatClock(record.reported_at)} UTC
            </span>
          </div>
          {record.status === "pending" ? (
            record.reporter_id === consoleId ? (
              // This console is the reporter: it can never acknowledge its
              // own report — even after the operator edits the seat name,
              // this console's stable id is unchanged. Only another console
              // (the next shift) gets the confirm button.
              <p className="anomaly-await" data-testid="anomaly-await-confirm">
                已留痕，等待下一班（另一席）确认已看到
              </p>
            ) : (
              <button
                type="button"
                className="primary"
                data-testid="btn-anomaly-confirm"
                disabled={confirming}
                onClick={confirmRecord}
              >
                下一班确认已看到
              </button>
            )
          ) : (
            <div className="anomaly-meta" data-testid="anomaly-confirmation">
              <span data-testid="anomaly-confirmer">
                确认席位：{record.confirmed_by}
              </span>
              <span data-testid="anomaly-confirmed-at">
                确认时间：{formatClock(record.confirmed_at)} UTC
              </span>
            </div>
          )}
          {formError && (
            <p className="notice error" data-testid="anomaly-error">
              {formError}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
