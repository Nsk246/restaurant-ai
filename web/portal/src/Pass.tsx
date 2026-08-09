import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  advance,
  getCalls,
  getMenu,
  getRail,
  getRestaurant,
  persistentSocket,
  resetDemo,
  setAvailability,
} from "./api";
import MenuEditor from "./MenuEditor";
import type {
  CallEvent,
  CallRow,
  FeedEntry,
  MenuItem,
  Restaurant,
  Ticket,
} from "./types";
import "./pass.css";

/** A ticket older than this is late. Tuned to feel urgent without crying wolf. */
const LATE_SECONDS = 600;

type DraftLine = {
  name: string;
  quantity: number;
  modifiers: string[];
  note: string | null;
};

type Draft = {
  lines: DraftLine[];
  total: number;
  confirmed: boolean;
  number?: number;
};

/** Wall-clock time of a call, for the idle history list. */
function clockOf(iso: string | null) {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function mmss(total: number) {
  const m = Math.floor(total / 60);
  const s = Math.floor(total % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Ticks once a second so every timer on the rail ages in step. */
function useNow() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export default function Pass() {
  const [restaurant, setRestaurant] = useState<Restaurant | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [menu, setMenu] = useState<{ name: string; items: MenuItem[] }[]>([]);
  const [feed, setFeed] = useState<FeedEntry[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [live, setLive] = useState(false);
  const [latency, setLatency] = useState<number | null>(null);
  const [callSeconds, setCallSeconds] = useState(0);
  const [history, setHistory] = useState<CallRow[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const now = useNow();
  const feedRef = useRef<HTMLDivElement>(null);
  const railLoadedAt = useRef(Date.now());

  const refreshRail = useCallback(async () => {
    const { tickets } = await getRail();
    railLoadedAt.current = Date.now();
    setTickets(tickets);
  }, []);

  const refreshMenu = useCallback(async () => {
    const { categories } = await getMenu();
    setMenu(categories);
  }, []);

  useEffect(() => {
    getRestaurant().then(setRestaurant).catch(() => undefined);
    refreshRail().catch(() => undefined);
    refreshMenu().catch(() => undefined);
  }, [refreshRail, refreshMenu]);

  // The rail pushes on every fire, so polling is only a safety net for a
  // socket that died between reconnects.
  useEffect(() => {
    const id = setInterval(() => void refreshRail().catch(() => undefined), 15000);
    return () => clearInterval(id);
  }, [refreshRail]);

  useEffect(
    () =>
      persistentSocket("/api/ws/rail", (raw) => {
        const e = raw as CallEvent;
        if (e.type === "reset") {
          setTickets([]);
          setFeed([]);
          setDraft(null);
          void refreshMenu();
          return;
        }
        if (e.type === "availability") void refreshMenu();
        if (e.type === "call_started" && e.call_id) {
          attachMonitor(e.call_id);
          return;
        }
        if (e.type === "call_finished") {
          detachMonitor();
          return;
        }
        void refreshRail();
      }),
    [refreshRail, refreshMenu],
  );

  // The rail socket announces call_started, so there is nothing to poll for.
  // A single slow check on mount covers the case where the portal is opened
  // while a call is already in progress.
  const monitorStop = useRef<(() => void) | null>(null);

  const detachMonitor = useCallback(() => {
    monitorStop.current?.();
    monitorStop.current = null;
    setLive(false);
  }, []);

  const attachMonitor = useCallback((callId: string) => {
    monitorStop.current?.();
    setLive(true);
    setCallSeconds(0);
    setFeed([]);
    setDraft(null);
    monitorStop.current = persistentSocket(`/ws/monitor/${callId}`, (raw) =>
      handleCallEvent(raw as CallEvent),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void getCalls(1)
      .then(({ calls }) => {
        if (calls[0]?.live && calls[0].call_id) attachMonitor(calls[0].call_id);
      })
      .catch(() => undefined);
    return () => monitorStop.current?.();
  }, [attachMonitor]);

  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => setCallSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [live]);

  // Recent calls fill the front-of-house band when nothing is on the line.
  useEffect(() => {
    const load = () =>
      getCalls(8)
        .then((r) => setHistory(r.calls))
        .catch(() => undefined);
    void load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [live]);

  function handleCallEvent(e: CallEvent) {
    switch (e.type) {
      case "status":
        setLive(true);
        setFeed([]);
        setDraft(null);
        break;
      case "transcript":
        if (e.text && e.role) {
          setFeed((f) => [...f, { kind: "turn", role: e.role!, text: e.text! }]);
        }
        break;
      case "tool_call":
        if (e.name) setFeed((f) => [...f, { kind: "tool", name: e.name! }]);
        // The chit fills the instant add_item fires, using the menu we
        // already hold. It is corrected to exact lines when review_order
        // comes back. That immediacy is the whole demo.
        if (e.name === "add_item") {
          const a = (e.args ?? {}) as {
            item_code?: string;
            quantity?: number;
            note?: string;
          };
          if (a.item_code) {
            setDraft((d) => ({
              confirmed: false,
              total: d?.total ?? 0,
              lines: [
                ...(d?.lines ?? []),
                {
                  name: nameFor(a.item_code!),
                  quantity: a.quantity ?? 1,
                  modifiers: [],
                  note: a.note ?? null,
                },
              ],
            }));
          }
        }
        break;
      case "tool_result":
        // Attach timing to the mark already in the feed rather than adding a
        // second one, so the feed reads as speech with annotations.
        setFeed((f) => {
          const next = [...f];
          for (let i = next.length - 1; i >= 0; i--) {
            const entry = next[i];
            if (entry.kind === "tool" && entry.name === e.name && entry.ms == null) {
              next[i] = { ...entry, ms: e.ms, error: Boolean(e.result?.error) };
              break;
            }
          }
          return next;
        });
        if (e.name === "review_order" && e.result && !e.result.error) {
          const r = e.result as {
            lines?: { name: string; quantity: number; modifiers: string[]; note: string | null }[];
            total?: number;
          };
          if (r.lines) {
            setDraft({ lines: r.lines, total: r.total ?? 0, confirmed: false });
          }
        }
        if (e.name === "confirm_order" && e.result && !e.result.error) {
          const r = e.result as { order_number?: number; total?: number };
          setDraft((d) =>
            d
              ? { ...d, confirmed: true, number: r.order_number, total: r.total ?? d.total }
              : d,
          );
        }
        break;
      case "review_order_result":
        break;
      case "latency":
        if (typeof e.ms === "number") setLatency(e.ms);
        break;
      case "ticket":
        void refreshRail();
        break;
      case "call_ended":
        setLive(false);
        break;
    }
  }

  // Menu is already loaded for the 86 board, so a code resolves to a name
  // without another round trip.
  const nameByCode = useMemo(() => {
    const m = new Map<string, string>();
    for (const cat of menu) for (const it of cat.items) m.set(it.code, it.name);
    return m;
  }, [menu]);

  const nameFor = useCallback(
    (code: string) => nameByCode.get(code) ?? code.replace(/-/g, " "),
    [nameByCode],
  );

  const sorted = useMemo(
    () => [...tickets].sort((a, b) => b.age_seconds - a.age_seconds),
    [tickets],
  );

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight });
  }, [feed]);

  const ageOf = (t: Ticket) =>
    t.age_seconds + Math.floor((now - railLoadedAt.current) / 1000);

  const nextStep = (t: Ticket) =>
    t.status === "fired"
      ? { to: "preparing", label: "Start" }
      : t.status === "preparing"
        ? { to: "ready", label: "Ready" }
        : { to: "completed", label: "Served" };

  return (
    <div className="pass">
      {menuOpen && (
        <MenuEditor
          onClose={() => {
            setMenuOpen(false);
            void refreshMenu();
          }}
        />
      )}
      <header className="status">
        <span className="status__name">{restaurant?.name ?? "\u2014"}</span>
        <span className="status__phone">{restaurant?.phone}</span>
        <span className={live ? "live" : "live live--idle"}>
          <span className="live__dot" />
          {live ? `On a call ${mmss(callSeconds)}` : "No call"}
        </span>
        <span className="status__spacer" />
        <span
          className={
            latency && latency > 900
              ? "status__metric status__metric--latency is-slow"
              : "status__metric status__metric--latency"
          }
        >
          reply <b>{latency ? `${latency}ms` : "\u2014"}</b>
        </span>
        <span className="status__metric">
          on the rail <b>{tickets.length}</b>
        </span>
        <button className="btn" onClick={() => setMenuOpen(true)}>
          Menu
        </button>
        <button
          className="btn"
          onClick={() => {
            void resetDemo().then(() => {
              setFeed([]);
              setDraft(null);
              setLatency(null);
              void refreshRail();
              void refreshMenu();
            });
          }}
        >
          Reset
        </button>
      </header>

      <section className="foh">
        <div className="feed">
          <p className="section-label">
            {feed.length > 0 || live ? "Front of house" : "Front of house · recent calls"}
          </p>
          <div className="feed__scroll" ref={feedRef}>
            {feed.length === 0 &&
              (history.length > 0 ? (
                <div className="history">
                  {history.map((c) => (
                    <div className="history__row" key={c.id}>
                      <span className="history__when">{clockOf(c.started_at)}</span>
                      <span className="history__from">{c.from ?? "unknown"}</span>
                      <span
                        className={`history__outcome history__outcome--${
                          c.outcome ?? "none"
                        }`}
                      >
                        {(c.outcome ?? "no outcome").replace(/_/g, " ")}
                      </span>
                      <span className="history__latency">
                        {c.p50_ms ? `${c.p50_ms}ms` : "\u2014"}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="empty">
                  Nothing on the line. The feed fills when someone calls.
                </p>
              ))}
            {feed.map((entry, i) =>
              entry.kind === "turn" ? (
                <div key={i} className={`turn turn--${entry.role}`}>
                  <div className="turn__who">
                    {entry.role === "caller" ? "Caller" : restaurant?.name ?? "Agent"}
                  </div>
                  <div className="turn__text">{entry.text}</div>
                </div>
              ) : (
                <span
                  key={i}
                  className={entry.error ? "tool tool--error" : "tool"}
                  title={entry.error ? "tool returned an error" : undefined}
                >
                  {entry.name}
                  {entry.ms != null && <span className="tool__ms">{entry.ms}ms</span>}
                </span>
              ),
            )}
          </div>
        </div>

        <div>
          <p className="section-label">Ticket</p>
          {draft ? (
            <div className="chit">
              <div className="chit__head">
                <span>{draft.confirmed ? `#${draft.number}` : "IN PROGRESS"}</span>
                <span>{mmss(callSeconds)}</span>
              </div>
              {draft.lines.map((ln, i) => (
                <div className="chit__line" key={`${ln.name}-${i}`}>
                  <span className="chit__qty">{ln.quantity}</span>
                  <span>
                    {ln.name}
                    {ln.modifiers.length > 0 && (
                      <span className="chit__mod">{ln.modifiers.join(", ")}</span>
                    )}
                    {ln.note && <span className="chit__mod">{ln.note}</span>}
                  </span>
                </div>
              ))}
              <div className="chit__total">
                <span>{draft.confirmed ? "FIRED" : "NOT YET FIRED"}</span>
                {draft.total > 0 && <span>${draft.total.toFixed(2)}</span>}
              </div>
            </div>
          ) : (
            <div className="chit chit--empty">No ticket started</div>
          )}
        </div>
      </section>

      <div className="pass__line" role="separator" aria-label="the pass" />

      <section className="boh">
        <p className="section-label">
          Back of house &middot; oldest first
        </p>
        {sorted.length === 0 ? (
          <p className="empty">No tickets on the rail.</p>
        ) : (
          <div className="rail">
            {sorted.map((t) => {
              const age = ageOf(t);
              const late = age > LATE_SECONDS;
              const step = nextStep(t);
              return (
                <article
                  key={t.id}
                  className={`ticket ticket--${t.status}${late ? " ticket--late" : ""}`}
                >
                  <div className="ticket__head">
                    <span className="ticket__number">#{t.number}</span>
                    <span className="ticket__timer">{mmss(age)}</span>
                  </div>
                  <div className="ticket__meta">
                    {t.type.replace("_", " ")}
                    {t.table ? ` · table ${t.table}` : ""}
                    {t.customer ? ` · ${t.customer}` : ""}
                  </div>
                  <div className="ticket__lines">
                    {t.lines.map((ln, i) => (
                      <div className="ticket__line" key={i}>
                        <span className="ticket__qty">{ln.quantity}</span>
                        <span className="ticket__body">
                          <span>{ln.name}</span>
                          {ln.modifiers.length > 0 && (
                            <span className="ticket__mods">
                              {ln.modifiers.join(", ")}
                            </span>
                          )}
                          {ln.note && <span className="ticket__note">{ln.note}</span>}
                        </span>
                      </div>
                    ))}
                  </div>
                  <button
                    className={
                      t.status === "preparing"
                        ? "ticket__action ticket__action--ready"
                        : "ticket__action"
                    }
                    onClick={() => {
                      void advance(t.id, step.to).then(refreshRail);
                    }}
                  >
                    {step.label}
                  </button>
                </article>
              );
            })}
          </div>
        )}

        <p className="section-label" style={{ margin: "16px 0 8px" }}>
          86 board &middot; tap to take an item off
        </p>
        <div className="eightysix">
          {menu.flatMap((cat) =>
            cat.items.map((item) => (
              <button
                key={item.code}
                className={item.available ? "chip" : "chip chip--off"}
                onClick={() => {
                  void setAvailability(item.code, !item.available).then(refreshMenu);
                }}
                aria-pressed={!item.available}
              >
                {item.name}
              </button>
            )),
          )}
        </div>
      </section>
    </div>
  );
}
