import React, { useState } from "react";
import { sheetUrl } from "./api.js";

// Download filename: property slug prefixes the unit title, matching the editor's
// export naming. The internal sheet id (a uuid) is never the saved-file name.
const slugify = (s) => (s || "floorplan").replace(/\s+/g, "-").toLowerCase();
const exportName = (propId, title, suffix = "") => {
  const base = slugify(title) + suffix;
  return propId ? `${slugify(propId)}-${base}` : base;
};

// Unified saved-sheet library across all properties: filter by property,
// search, thumbnails, downloads, rename, re-open, delete. Each sheet carries
// its own property_id / property_name (from GET /sheets).
export default function Library({ sheets, onReopen, onDelete, onRename }) {
  const [q, setQ] = useState("");
  const [prop, setProp] = useState("");        // "" = all properties
  const [editing, setEditing] = useState(null); // sheet_id being renamed
  const [draft, setDraft] = useState("");

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

  function startRename(s) {
    setEditing(s.sheet_id);
    setDraft(s.title || "");
  }
  function commitRename(s) {
    const next = draft.trim();
    setEditing(null);
    if (next && next !== s.title) onRename(s, next);
  }

  return (
    <div className="library">
      <div className="libhead">
        <h4>Library ({sheets.length})</h4>
        {sheets.length > 0 && (
          <input className="libsearch" type="text" placeholder="Search title / suite / property…"
            value={q} onChange={(e) => setQ(e.target.value)} />
        )}
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
        {visible.map((s) => {
          // cache-bust artifacts after an overwrite (same URL, new content)
          const bust = s.updated ? `?v=${encodeURIComponent(s.updated)}` : "";
          return (
          <div className="libcard" key={`${s.property_id}/${s.sheet_id}`}>
            <a href={sheetUrl(s.property_id, s.sheet_id, "png") + bust} target="_blank" rel="noreferrer">
              <img src={sheetUrl(s.property_id, s.sheet_id, "png") + bust} alt={s.title} />
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
