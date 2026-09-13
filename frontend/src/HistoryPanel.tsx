import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ANOMALY_CATEGORY_LABELS,
  api,
  type AnomalyCategory,
  type ApiError,
  type HistoryEvent,
} from "./api";
import { formatDateTime } from "./lease";

// Fixed page size for the console's history view; the server clamps to its
// own maximum regardless of what the client asks for.
export const HISTORY_PAGE_SIZE = 10;

interface HistoryPanelProps {
  onClose: () => void;
}

/**
 * Execution history (执行历史): the review trail of every executed action,
 * newest first, paged backwards with the immutable event id as cursor.
 *
 * The panel opens on the LATEST page; "加载更早记录" follows the server's
 * next_cursor to strictly older events. A failed follow-up page (including a
 * rejected history_cursor_invalid) never clears what is already on screen —
 * the error and a retry button appear at the tail of the list instead.
 */
export default function HistoryPanel({ onClose }: HistoryPanelProps) {
  const [events, setEvents] = useState<HistoryEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  // Failure of the FIRST page: the list is still empty, so the error takes
  // the panel body. Failure of a LATER page: the loaded rows stay and the
  // error + retry move to the list tail.
  const [initialError, setInitialError] = useState<string | null>(null);
  const [tailError, setTailError] = useState<string | null>(null);

  const loadFirstPage = useCallback(async () => {
    setInitialLoading(true);
    setInitialError(null);
    try {
      const page = await api.listHistory(null, HISTORY_PAGE_SIZE);
      setEvents(page.events);
      setNextCursor(page.next_cursor);
    } catch (e) {
      setInitialError((e as ApiError).message);
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFirstPage();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (nextCursor == null || loadingMore) return;
    setLoadingMore(true);
    setTailError(null);
    try {
      const page = await api.listHistory(nextCursor, HISTORY_PAGE_SIZE);
      // Append only: rows already shown stay exactly where they are; the
      // keyset cursor guarantees this older page cannot overlap them.
      setEvents((prev) => [...prev, ...page.events]);
      setNextCursor(page.next_cursor);
    } catch (e) {
      // Keep every loaded row; the retry entry appears at the list tail.
      setTailError((e as ApiError).message);
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore]);

  // Adjacent rows sharing one link id are the two events of a single linked
  // run — render them as one group. Grouping is computed over the whole
  // loaded list, so a pair split across a page boundary merges as soon as
  // the next page arrives.
  const groups = useMemo(() => groupLinkedEvents(events), [events]);

  return (
    <div className="history-overlay" data-testid="history-overlay">
      <section
        className="history-panel"
        data-testid="history-panel"
        role="dialog"
        aria-label="执行历史"
      >
        <header className="history-head">
          <h2>执行历史</h2>
          <span className="hint">
            按发生顺序（事件编号倒序）追查每一次执行；翻页期间新产生的执行不影响已加载记录。
          </span>
          <button type="button" data-testid="btn-history-close" onClick={onClose}>
            关闭
          </button>
        </header>

        {initialLoading && (
          <p className="history-status" data-testid="history-loading">
            正在加载最新一页…
          </p>
        )}
        {initialError && (
          <div className="history-status" data-testid="history-initial-error">
            <p className="notice error">{initialError}</p>
            <button
              type="button"
              className="primary"
              data-testid="btn-history-reload"
              onClick={() => void loadFirstPage()}
            >
              重试
            </button>
          </div>
        )}
        {!initialLoading && !initialError && events.length === 0 && (
          <p className="history-status" data-testid="history-empty">
            暂无执行记录。
          </p>
        )}

        {events.length > 0 && (
          <ol className="history-list" data-testid="history-list">
            {groups.map((group) =>
              group[0].link_id ? (
                <li
                  key={group[0].link_id}
                  className="history-group"
                  data-testid="history-group"
                  data-link-id={group[0].link_id}
                >
                  <div
                    className="history-group-badge"
                    data-testid="history-group-badge"
                  >
                    联动执行 · 标识 {group[0].link_id!.slice(0, 8)}…（
                    {group.length} 个动作同属一次联动）
                  </div>
                  <ol className="history-group-items">
                    {group.map((ev) => (
                      <HistoryRow key={ev.event_id} event={ev} />
                    ))}
                  </ol>
                </li>
              ) : (
                <HistoryRow key={group[0].event_id} event={group[0]} />
              ),
            )}
          </ol>
        )}

        {tailError && (
          <div className="history-tail" data-testid="history-tail-error">
            <p className="notice error">
              更早记录加载失败：{tailError}（已加载内容保留，可重试）
            </p>
            <button
              type="button"
              className="primary"
              data-testid="btn-history-retry"
              disabled={loadingMore}
              onClick={() => void loadMore()}
            >
              重试加载更早记录
            </button>
          </div>
        )}
        {!tailError && nextCursor != null && (
          <div className="history-tail">
            <button
              type="button"
              data-testid="btn-history-more"
              disabled={loadingMore}
              onClick={() => void loadMore()}
            >
              {loadingMore ? "正在加载…" : "加载更早记录"}
            </button>
          </div>
        )}
        {!tailError && nextCursor == null && events.length > 0 && (
          <p className="history-status" data-testid="history-end">
            已加载全部 {events.length} 条执行记录。
          </p>
        )}
      </section>
    </div>
  );
}

/** Group consecutive events that share a non-null link id. */
export function groupLinkedEvents(events: HistoryEvent[]): HistoryEvent[][] {
  const groups: HistoryEvent[][] = [];
  for (const ev of events) {
    const prev = groups[groups.length - 1];
    if (ev.link_id && prev && prev[0].link_id === ev.link_id) {
      prev.push(ev);
    } else {
      groups.push([ev]);
    }
  }
  return groups;
}

function HistoryRow({ event }: { event: HistoryEvent }) {
  const anomaly = event.anomaly;
  return (
    <li
      className="history-event"
      data-testid="history-event"
      data-event-id={event.event_id}
      data-action-id={event.action_id}
    >
      <div className="history-event-head">
        <span className="history-event-id" data-testid="history-event-id">
          #{event.event_id}
        </span>
        <span data-testid="history-event-time">
          {formatDateTime(event.occurred_at)}
        </span>
        <strong data-testid="history-event-action">{event.label}</strong>
        <span data-testid="history-event-seat">席位：{event.holder}</span>
        <span data-testid="history-event-session">
          场次：{event.session ? event.session.name : "—"}
        </span>
      </div>
      {anomaly && (
        <div
          className={`history-anomaly ${anomaly.status}`}
          data-testid="history-anomaly"
          data-status={anomaly.status}
        >
          <span className="anomaly-tag" data-testid="history-anomaly-category">
            {ANOMALY_CATEGORY_LABELS[anomaly.category as AnomalyCategory] ??
              anomaly.category}
          </span>
          <span data-testid="history-anomaly-description">
            {anomaly.description}
          </span>
          <span data-testid="history-anomaly-reporter">
            报告人：{anomaly.reported_by}（{formatDateTime(anomaly.reported_at)}）
          </span>
          {anomaly.status === "confirmed" ? (
            <span data-testid="history-anomaly-confirmation">
              已确认：{anomaly.confirmed_by}（
              {formatDateTime(anomaly.confirmed_at)}）
            </span>
          ) : (
            <span data-testid="history-anomaly-pending">待确认</span>
          )}
        </div>
      )}
    </li>
  );
}
