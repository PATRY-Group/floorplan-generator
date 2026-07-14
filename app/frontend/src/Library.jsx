import React, { useState, useEffect, useRef } from "react";
import { sheetUrl, sheetThumbUrl, downloadSheets } from "./api.js";
import { toast } from "./toast.js";

// Download filename: property slug prefixes the unit title, matching the editor's
// export naming. The internal sheet id (a uuid) is never the saved-file name.
const slugify = (s) => (s || "floorplan").replace(/\s+/g, "-").toLowerCase();
const exportName = (propId, title, suffix = "") => {
  const base = slugify(title) + suffix;
  return propId ? `${slugify(propId)}-${base}` : base;
};

// Library sort options. `created`/`updated` are zero-padded "YYYY-MM-DD HH:MM[:SS]"
// strings (from GET /sheets), so a plain string compare sorts them chronologically.
const SORTS = [
  { key: "edited-desc", label: "Recent first" },
  { key: "edited-asc", label: "Recent last" },
  { key: "title-asc", label: "Name A–Z" },
  { key: "title-desc", label: "Name Z–A" },
];

// Sort order and the property-pill filter are sticky: they persist across tab
// switches (the Library unmounts when you leave it) and reloads.
const SORT_KEY = "fpsg.lib.sort";
const FILTER_KEY = "fpsg.lib.filter";

// Unified saved-sheet library across all properties: filter by property,
// search, thumbnails, downloads, rename, re-open, delete. Each sheet carries
// its own property_id / property_name (from GET /sheets).
export default function Library({ sheets, onReopen, onDelete, onRename, onBatchDelete, onReopenAll }) {
  const [q, setQ] = useState("");
  const [prop, setProp] = useState(() => localStorage.getItem(FILTER_KEY) || "");  // "" = all properties
  const [editing, setEditing] = useState(null); // sheet_id being renamed
  const [draft, setDraft] = useState("");
  // batch selection: a mode you opt into — checkboxes are hidden until then.
  // keys are `${property_id}/${sheet_id}`
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [downloading, setDownloading] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);   // batch-action menu
  const [dlOpen, setDlOpen] = useState(false);        // download format submenu
  const menuRef = useRef(null);
  const closeMenu = () => { setMenuOpen(false); setDlOpen(false); };
  const [sort, setSort] = useState(() => {           // library sort order
    const saved = localStorage.getItem(SORT_KEY);
    return SORTS.some((o) => o.key === saved) ? saved : "edited-desc";
  });
  const [sortOpen, setSortOpen] = useState(false);   // sort dropdown
  const sortRef = useRef(null);

  const skey = (s) => `${s.property_id}/${s.sheet_id}`;

  // distinct properties present, for the filter chips
  const props = [];
  const seen = new Set();
  for (const s of sheets) {
    if (!seen.has(s.property_id)) {
      seen.add(s.property_id);
      props.push({ id: s.property_id, name: s.property_name || s.property_id });
    }
  }
  const countFor = (id) => sheets.filter((s) => s.property_id === id).length;

  const query = q.trim().toLowerCase();
  const visible = sheets.filter((s) => {
    if (prop && s.property_id !== prop) return false;
    if (!query) return true;
    return `${s.title} ${s.suite} ${s.sf} ${s.property_name}`.toLowerCase().includes(query);
  });

  // Sort what's shown. "Last edited" prefers `updated`, falling back to `created`
  // for entries saved before that field existed. Selection logic stays keyed off
  // `visible` (membership, not order), so only the grid below renders `sorted`.
  const editedAt = (s) => s.updated || s.created || "";
  // sort by the *displayed* name so "Name A–Z" matches what's on the card; in the
  // "All" view that's the property-prefixed export name, so it groups by property
  // (within one property the prefix is constant, so it's a plain title sort).
  const titleKey = (s) => (s.title ? exportName(s.property_id, s.title) : "untitled").toLowerCase();
  const sorted = [...visible].sort((a, b) => {
    switch (sort) {
      case "edited-asc": return editedAt(a).localeCompare(editedAt(b));
      case "title-asc": return titleKey(a).localeCompare(titleKey(b));
      case "title-desc": return titleKey(b).localeCompare(titleKey(a));
      case "edited-desc":
      default: return editedAt(b).localeCompare(editedAt(a));
    }
  });

  function startRename(s) {
    setEditing(s.sheet_id);
    setDraft(s.title || "");
  }
  function commitRename(s) {
    const next = draft.trim();
    setEditing(null);
    if (next && next !== s.title) onRename(s, next);
  }

  // Selection is keyed globally but "select all" is scoped to what's visible.
  // Switching the property chip clears it (you're working within one property).
  useEffect(() => setSelected(new Set()), [prop]);

  // Persist the sticky preferences so they survive unmount/reload. Guarded:
  // setItem throws at quota / in private-mode, and an uncaught throw in an effect
  // would unmount the app (no error boundary).
  useEffect(() => { try { localStorage.setItem(SORT_KEY, sort); } catch { /* ignore */ } }, [sort]);
  useEffect(() => { try { localStorage.setItem(FILTER_KEY, prop); } catch { /* ignore */ } }, [prop]);

  // A persisted filter can point at a property with no sheets anymore (all
  // deleted, or a different dataset). Fall back to "All" so the grid isn't
  // silently empty with no chip to recover — chips only show with 2+ properties.
  useEffect(() => {
    // Guard on sheets being loaded (non-empty): running while `sheets` is still
    // [] (before /sheets resolves — a restored session can land here first)
    // would reset — and re-persist as "" — a perfectly valid saved filter.
    if (sheets.length && prop && !sheets.some((s) => s.property_id === prop)) setProp("");
  }, [sheets, prop]);

  function exitSelecting() {
    setSelecting(false);
    setSelected(new Set());
    closeMenu();
  }

  // close the batch-action menu on an outside click
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) closeMenu();
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen]);

  // close the sort dropdown on an outside click
  useEffect(() => {
    if (!sortOpen) return;
    const onDoc = (e) => {
      if (sortRef.current && !sortRef.current.contains(e.target)) setSortOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [sortOpen]);

  function toggleSel(key) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }
  const allVisibleSelected = visible.length > 0 && visible.every((s) => selected.has(skey(s)));
  function toggleSelectAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) visible.forEach((s) => next.delete(skey(s)));
      else visible.forEach((s) => next.add(skey(s)));
      return next;
    });
  }
  const selectedSheets = () => sheets.filter((s) => selected.has(skey(s)));

  async function deleteSelected() {
    closeMenu();
    const items = selectedSheets().map((s) => ({
      property_id: s.property_id, sheet_id: s.sheet_id, title: s.title,
    }));
    if (!items.length) return;
    const proceeded = await onBatchDelete(items);   // parent confirms + deletes
    if (proceeded) exitSelecting();
  }

  // Re-open the selected sheets, each as its own editor tab — the portfolio-
  // update path: "Select all" then this opens the whole library for editing.
  // Parent does the work (and confirms large batches); we just exit on success.
  async function reopenSelected() {
    closeMenu();
    const items = selectedSheets().map((s) => ({
      property_id: s.property_id, sheet_id: s.sheet_id, title: s.title,
    }));
    if (!items.length) return;
    const proceeded = await onReopenAll(items);
    if (proceeded) exitSelecting();
  }

  async function downloadSelected(format, planOnly = false) {
    closeMenu();
    const items = selectedSheets()
      .map((s) => ({ property_id: s.property_id, sheet_id: s.sheet_id }));
    if (!items.length) return;
    const formats = format === "both" ? ["png", "svg"] : [format];
    setDownloading(true);
    try {
      const blob = await downloadSheets(items, formats, planOnly);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "floorplans.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast(`Downloaded ${items.length} sheet${items.length > 1 ? "s" : ""}.`, "success");
    } catch (e) {
      toast(e.message || "Download failed", "error");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="library">
      <div className="libhead">
        <h4>Library ({sheets.length})</h4>
        <div className="libtools">
          {sheets.length > 0 && (
            <div className="libsearch-wrap">
              <input className="libsearch" type="text" placeholder="Search title / suite / property…"
                value={q} onChange={(e) => setQ(e.target.value)} />
              <div className="libsort-wrap" ref={sortRef}>
                <button className="libsort" title="Sort"
                  onClick={() => setSortOpen((o) => !o)}>
                  <span className="sorticon">⇅</span><span className="caret">▾</span>
                </button>
                {sortOpen && (
                  <div className="libmenu">
                    {SORTS.map((o) => (
                      <button key={o.key} className={sort === o.key ? "on" : ""}
                        onClick={() => { setSort(o.key); setSortOpen(false); }}>
                        {o.label}{sort === o.key && <span className="sortcheck">✓</span>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
          {sheets.length > 0 && !selecting && (
            <button className="libdownload" onClick={() => setSelecting(true)}>Batch actions</button>
          )}
          {selecting && (
            <div className="libbatch">
              {visible.length > 0 && (
                <button className="libselectall" onClick={toggleSelectAll}>
                  {allVisibleSelected ? "Clear" : "Select all"}
                </button>
              )}
              <div className="libdownload-wrap" ref={menuRef}>
                <button className="libdownload" disabled={downloading || selected.size === 0}
                  onClick={() => (menuOpen ? closeMenu() : setMenuOpen(true))}>
                  {downloading ? "Zipping…" : `Batch actions (${selected.size})`}
                  <span className="caret">▾</span>
                </button>
                {menuOpen && (
                  <div className="libmenu">
                    <button className="libmenu-item" onClick={reopenSelected}>Open in editor tabs</button>
                    <div className="libmenu-sep" />
                    <button className="libmenu-item" onClick={() => setDlOpen((o) => !o)}>
                      Download as<span className="caret">{dlOpen ? "▾" : "▸"}</span>
                    </button>
                    {dlOpen && (
                      <div className="libsubmenu">
                        <button onClick={() => downloadSelected("png")}>PNG</button>
                        <button onClick={() => downloadSelected("svg")}>SVG</button>
                        <button onClick={() => downloadSelected("both")}>PNG + SVG</button>
                        <div className="libsub-label">Plan only — no branding</div>
                        <button onClick={() => downloadSelected("svg", true)}>Plan SVG</button>
                        <button onClick={() => downloadSelected("png", true)}>Plan PNG</button>
                      </div>
                    )}
                    <div className="libmenu-sep" />
                    <button className="libmenu-item danger" onClick={deleteSelected}>Delete</button>
                  </div>
                )}
              </div>
              <button className="libcancel" onClick={exitSelecting}>Cancel</button>
            </div>
          )}
        </div>
      </div>

      {props.length > 1 && (
        <div className="libfilters">
          <button className={"libchip" + (prop === "" ? " on" : "")}
            onClick={() => setProp("")}>All ({sheets.length})</button>
          {props.map((p) => (
            <button key={p.id} className={"libchip" + (prop === p.id ? " on" : "")}
              onClick={() => setProp(p.id)}>{p.name} ({countFor(p.id)})</button>
          ))}
        </div>
      )}

      {sheets.length === 0 && (
        <p className="subtle">No saved sheets yet. Save a unit to start the library.</p>
      )}
      {sheets.length > 0 && visible.length === 0 && (
        <p className="subtle">No sheets match this filter.</p>
      )}

      <div className="libgrid">
        {sorted.map((s) => {
          // cache-bust artifacts after an overwrite (same URL, new content)
          const bust = s.updated ? `?v=${encodeURIComponent(s.updated)}` : "";
          const sel = selected.has(skey(s));
          return (
          <div className={"libcard" + (selecting && sel ? " sel" : "")} key={`${s.property_id}/${s.sheet_id}`}>
            {selecting && (
              <input type="checkbox" className="libcheck" checked={sel}
                aria-label="Select sheet for download" onChange={() => toggleSel(skey(s))} />
            )}
            <a className="libthumb" href={sheetUrl(s.property_id, s.sheet_id, "png") + bust}
               target="_blank" rel="noreferrer"
               onClick={selecting ? (e) => { e.preventDefault(); toggleSel(skey(s)); } : undefined}>
              <img src={sheetThumbUrl(s.property_id, s.sheet_id) + bust} alt={s.title}
                   loading="lazy" decoding="async" />
              {!selecting && <span className="thumbhint">Preview in new tab</span>}
            </a>
            <div className="cap">
              <div className="libprop">{s.property_name || s.property_id}</div>
              {editing === s.sheet_id ? (
                <input className="librename" autoFocus value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => commitRename(s)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename(s);
                    if (e.key === "Escape") setEditing(null);
                  }} />
              ) : (
                <div className="capttl" title="Double-click to rename"
                  onDoubleClick={() => startRename(s)}>
                  {s.title ? exportName(s.property_id, s.title) : "Untitled"}
                  <button className="renamepen" title="Rename"
                    onClick={() => startRename(s)}>✎</button>
                  {s.keyplan && <span className="kpbadge">KEY PLAN</span>}
                </div>
              )}
              <div className="capsub">{[s.suite, s.sf, s.created].filter(Boolean).join(" · ")}</div>
              <div className="libactions">
                <a href={sheetUrl(s.property_id, s.sheet_id, "svg") + bust} target="_blank" rel="noreferrer"
                   download={`${exportName(s.property_id, s.title)}.svg`}>SVG</a>
                <a href={sheetUrl(s.property_id, s.sheet_id, "png") + bust} target="_blank" rel="noreferrer"
                   download={`${exportName(s.property_id, s.title)}.png`}>PNG</a>
                {s.keyplan && (
                  <a href={sheetUrl(s.property_id, `${s.sheet_id}-keyplan`, "svg") + bust}
                     target="_blank" rel="noreferrer"
                     download={`${exportName(s.property_id, s.title, "-keyplan")}.svg`}>Key plan</a>
                )}
                <button onClick={() => onReopen(s)}>Re-open</button>
                <button className="del" onClick={() => onDelete(s)}>Delete</button>
              </div>
            </div>
          </div>
          );
        })}
      </div>
    </div>
  );
}
