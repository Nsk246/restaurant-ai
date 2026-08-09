import type { MenuItem, Restaurant, Ticket } from "./types";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const getRestaurant = () => json<Restaurant>("/api/restaurant");

export const getRail = () => json<{ tickets: Ticket[] }>("/api/rail");

export const getMenu = () =>
  json<{ categories: { name: string; items: MenuItem[] }[] }>("/api/menu");

export const setAvailability = (code: string, available: boolean) =>
  json<{ code: string; available: boolean }>(
    `/api/menu/${code}/availability?available=${available}`,
    { method: "POST" },
  );

export const advance = (id: string, to: string) =>
  json<{ status: string }>(`/api/rail/${id}/advance?to=${to}`, { method: "POST" });

export const resetDemo = () =>
  json<{ reset: boolean }>("/api/demo/reset", { method: "POST" });

/** Websocket that reconnects. A dropped socket mid-demo should heal itself
 *  rather than needing a page refresh in front of a prospect. */
export function persistentSocket(path: string, onMessage: (data: unknown) => void) {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 500;

  const open = () => {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}${path}`);
    ws.onopen = () => (retry = 500);
    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data));
      } catch {
        /* a malformed frame must not take down the view */
      }
    };
    ws.onclose = () => {
      if (closed) return;
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 8000);
    };
    ws.onerror = () => ws?.close();
  };

  open();
  return () => {
    closed = true;
    ws?.close();
  };
}
