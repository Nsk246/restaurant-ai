import type { MenuItem } from "./types";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not every error body is json */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const post = <T>(url: string, body?: unknown) =>
  json<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export type NewItem = {
  name: string;
  category: string;
  price: number;
  description?: string | null;
};

export type ParsedItem = {
  name: string;
  category: string;
  price: number | null;
  description: string | null;
  tags: string[];
  aliases: string[];
};

export const createItem = (item: NewItem) =>
  post<MenuItem>("/api/menu/items", item);

export const patchItem = (code: string, patch: Record<string, unknown>) =>
  json<MenuItem>(`/api/menu/items/${code}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });

export const removeItem = (code: string) =>
  json<{ deleted?: boolean; deactivated?: boolean; orders?: number }>(
    `/api/menu/items/${code}`,
    { method: "DELETE" },
  );

export const previewImport = (text: string) =>
  post<{
    source: string;
    count: number;
    items: ParsedItem[];
    missing_price: string[];
  }>("/api/menu/import/preview", { text });

export const commitImport = (items: ParsedItem[], replace: boolean) =>
  post<{
    created: number;
    codes: string[];
    skipped_no_price: string[];
    failed: string[];
  }>("/api/menu/import/commit", { items, replace });
