export type TicketLine = {
  name: string;
  quantity: number;
  modifiers: string[];
  note: string | null;
};

export type Ticket = {
  id: string;
  number: number;
  status: "fired" | "preparing" | "ready";
  type: string;
  table: string | null;
  customer: string | null;
  age_seconds: number;
  quoted_minutes: number | null;
  total: number;
  note: string | null;
  lines: TicketLine[];
};

export type MenuItem = {
  code: string;
  name: string;
  price: number;
  available: boolean;
  active: boolean;
};

export type Restaurant = {
  id: string;
  name: string;
  slug: string;
  phone: string | null;
};

/** One entry in the live call feed. Tool marks sit inline with speech so you
 *  can see the machine react to the caller in real time. */
export type FeedEntry =
  | { kind: "turn"; role: "caller" | "agent"; text: string }
  | { kind: "tool"; name: string; ms?: number; error?: boolean };

export type CallEvent = {
  type: string;
  call_id?: string;
  role?: "caller" | "agent";
  text?: string;
  name?: string;
  args?: Record<string, unknown>;
  ms?: number;
  result?: { error?: string; [k: string]: unknown };
  order_number?: number;
  [k: string]: unknown;
};

export type CallRow = {
  id: string;
  call_id: string;
  from: string | null;
  started_at: string | null;
  live: boolean;
  outcome: string | null;
  turns: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
};
