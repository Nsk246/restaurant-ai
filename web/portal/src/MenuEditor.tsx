import { useCallback, useEffect, useMemo, useState } from "react";
import { getMenu, setAvailability } from "./api";
import {
  commitImport,
  createItem,
  patchItem,
  previewImport,
  removeItem,
  type ParsedItem,
} from "./menuApi";
import type { MenuItem } from "./types";

type Category = { name: string; items: MenuItem[] };

/** Money in, money out. The API speaks dollars; the database keeps cents. */
function money(v: string): number | null {
  const n = Number(v.replace(/[^0-9.]/g, ""));
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export default function MenuEditor({ onClose }: { onClose: () => void }) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"edit" | "import">("edit");

  const refresh = useCallback(async () => {
    const { categories } = await getMenu();
    setCategories(categories);
  }, []);

  useEffect(() => {
    void refresh().catch((e) => setError(String(e)));
  }, [refresh]);

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const count = useMemo(
    () => categories.reduce((n, c) => n + c.items.length, 0),
    [categories],
  );

  return (
    <div className="sheet" role="dialog" aria-label="Menu">
      <header className="sheet__head">
        <span className="sheet__title">Menu</span>
        <span className="sheet__count">{count} items</span>
        <nav className="sheet__tabs">
          <button
            className={tab === "edit" ? "tab tab--on" : "tab"}
            onClick={() => setTab("edit")}
          >
            Edit
          </button>
          <button
            className={tab === "import" ? "tab tab--on" : "tab"}
            onClick={() => setTab("import")}
          >
            Import
          </button>
        </nav>
        <button className="btn" onClick={onClose}>
          Close
        </button>
      </header>

      {error && <p className="sheet__error">{error}</p>}

      {tab === "edit" ? (
        <EditTab
          categories={categories}
          busy={busy}
          run={run}
        />
      ) : (
        <ImportTab run={run} busy={busy} />
      )}
    </div>
  );
}

function EditTab({
  categories,
  busy,
  run,
}: {
  categories: Category[];
  busy: string | null;
  run: (key: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [draft, setDraft] = useState({ name: "", category: "", price: "" });

  return (
    <div className="sheet__body">
      {categories.map((cat) => (
        <section key={cat.name} className="mgroup">
          <h3 className="mgroup__name">{cat.name}</h3>
          {cat.items.map((item) => (
            <Row key={item.code} item={item} busy={busy} run={run} />
          ))}
        </section>
      ))}

      <section className="mgroup">
        <h3 className="mgroup__name">Add an item</h3>
        <div className="mrow mrow--new">
          <input
            className="minput minput--name"
            placeholder="Dish name"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
          <input
            className="minput"
            placeholder="Section"
            list="menu-categories"
            value={draft.category}
            onChange={(e) => setDraft({ ...draft, category: e.target.value })}
          />
          <datalist id="menu-categories">
            {categories.map((c) => (
              <option key={c.name} value={c.name} />
            ))}
          </datalist>
          <input
            className="minput minput--price"
            placeholder="0.00"
            inputMode="decimal"
            value={draft.price}
            onChange={(e) => setDraft({ ...draft, price: e.target.value })}
          />
          <button
            className="btn"
            disabled={
              busy === "new" || !draft.name.trim() || money(draft.price) === null
            }
            onClick={() =>
              void run("new", async () => {
                await createItem({
                  name: draft.name.trim(),
                  category: draft.category.trim() || "Menu",
                  price: money(draft.price) ?? 0,
                });
                setDraft({ name: "", category: draft.category, price: "" });
              })
            }
          >
            Add
          </button>
        </div>
      </section>
    </div>
  );
}

function Row({
  item,
  busy,
  run,
}: {
  item: MenuItem;
  busy: string | null;
  run: (key: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [name, setName] = useState(item.name);
  const [price, setPrice] = useState(item.price.toFixed(2));

  useEffect(() => {
    setName(item.name);
    setPrice(item.price.toFixed(2));
  }, [item.name, item.price]);

  const nameChanged = name.trim() !== item.name;
  const priceChanged = money(price) !== null && money(price) !== item.price;
  const dirty = nameChanged || priceChanged;

  const save = () =>
    void run(item.code, async () => {
      const patch: Record<string, unknown> = {};
      if (nameChanged) patch.name = name.trim();
      if (priceChanged) patch.price = money(price);
      await patchItem(item.code, patch);
    });

  return (
    <div className={item.available ? "mrow" : "mrow mrow--off"}>
      <input
        className="minput minput--name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && dirty && save()}
      />
      {/* The code is what the agent emits in a tool call, so it is worth
          seeing while editing: renaming never changes it. */}
      <code className="mcode">{item.code}</code>
      <input
        className="minput minput--price"
        inputMode="decimal"
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && dirty && save()}
      />
      <button className="btn btn--tiny" disabled={!dirty || busy === item.code} onClick={save}>
        {dirty ? "Save" : "Saved"}
      </button>
      <button
        className="btn btn--tiny"
        title={item.available ? "Take off the menu tonight" : "Put it back on"}
        onClick={() =>
          void run(item.code, () => setAvailability(item.code, !item.available))
        }
      >
        {item.available ? "86" : "On"}
      </button>
      <button
        className="btn btn--tiny btn--danger"
        title="Remove from the menu"
        onClick={() => void run(item.code, () => removeItem(item.code))}
      >
        Remove
      </button>
    </div>
  );
}

function ImportTab({
  run,
  busy,
}: {
  run: (key: string, fn: () => Promise<unknown>) => Promise<void>;
  busy: string | null;
}) {
  const [text, setText] = useState("");
  const [parsed, setParsed] = useState<ParsedItem[] | null>(null);
  const [source, setSource] = useState("");
  const [replace, setReplace] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const priced = (parsed ?? []).filter((i) => i.price !== null);
  const unpriced = (parsed ?? []).filter((i) => i.price === null);

  return (
    <div className="sheet__body">
      <p className="hint">
        Paste a menu. Nothing is saved until you review it and press import.
      </p>
      <textarea
        className="mpaste"
        rows={8}
        placeholder={"STARTERS\nWings 14\n\nMAINS\nBurger 16"}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="mrow mrow--new">
        <button
          className="btn"
          disabled={!text.trim() || busy === "parse"}
          onClick={() =>
            void run("parse", async () => {
              const r = await previewImport(text);
              setParsed(r.items);
              setSource(r.source);
              setResult(null);
            })
          }
        >
          {busy === "parse" ? "Reading..." : "Read menu"}
        </button>
      </div>

      {parsed && (
        <section className="mgroup">
          <h3 className="mgroup__name">
            Found {priced.length} items{" "}
            <span className="hint">
              via {source === "model" ? "model" : "text rules"}
            </span>
          </h3>

          {priced.map((item, i) => (
            <div className="mrow" key={`${item.name}-${i}`}>
              <input
                className="minput minput--name"
                value={item.name}
                onChange={(e) => {
                  const next = [...parsed];
                  next[i] = { ...item, name: e.target.value };
                  setParsed(next);
                }}
              />
              <input
                className="minput"
                value={item.category}
                onChange={(e) => {
                  const next = [...parsed];
                  next[i] = { ...item, category: e.target.value };
                  setParsed(next);
                }}
              />
              <input
                className="minput minput--price"
                inputMode="decimal"
                value={item.price ?? ""}
                onChange={(e) => {
                  const next = [...parsed];
                  next[i] = { ...item, price: money(e.target.value) };
                  setParsed(next);
                }}
              />
              <button
                className="btn btn--tiny btn--danger"
                onClick={() => setParsed(parsed.filter((_, j) => j !== i))}
              >
                Drop
              </button>
            </div>
          ))}

          {unpriced.length > 0 && (
            <p className="hint hint--warn">
              {unpriced.length} had no price and will be skipped:{" "}
              {unpriced.map((i) => i.name).join(", ")}
            </p>
          )}

          <div className="mrow mrow--new">
            <label className="hint">
              <input
                type="checkbox"
                checked={replace}
                onChange={(e) => setReplace(e.target.checked)}
              />{" "}
              Replace the current menu (takes existing items off, keeps past
              orders intact)
            </label>
            <button
              className="btn"
              disabled={priced.length === 0 || busy === "commit"}
              onClick={() =>
                void run("commit", async () => {
                  const r = await commitImport(parsed, replace);
                  setResult(
                    `Imported ${r.created}` +
                      (r.failed.length ? `, ${r.failed.length} failed` : ""),
                  );
                  setParsed(null);
                  setText("");
                })
              }
            >
              {busy === "commit" ? "Importing..." : `Import ${priced.length}`}
            </button>
          </div>
        </section>
      )}

      {result && <p className="hint">{result}</p>}
    </div>
  );
}
