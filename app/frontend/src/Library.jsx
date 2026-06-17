import React, { useState } from "react";
import { sheetUrl } from "./api.js";

// Per-property saved-sheet library: search, thumbnails, downloads, re-open, delete.
export default function Library({ propertyId, sheets, onReopen, onDelete }) {
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const visible = query
    ? sheets.filter((s) => `${s.title} ${s.suite} ${s.sf}`.toLowerCase().includes(query))
    : sheets;

  return (
    <div className="library">
      <div className="libhead">
        <h4>{propertyId || "—"} library ({sheets.length})</h4>
        {sheets.length > 0 && (
          <input className="libsearch" type="text" placeholder="Search title / suite…"
            value={q} onChange={(e) => setQ(e.target.value)} />
        )}
      </div>
      {sheets.length === 0 && (
        <p className="subtle">No saved sheets yet. Save a unit to start the library.</p>
      )}
      <div className="libgrid">
        {visible.map((s) => (
          <div className="libcard" key={s.sheet_id}>
            <a href={sheetUrl(propertyId, s.sheet_id, "png")} target="_blank" rel="noreferrer">
              <img src={sheetUrl(propertyId, s.sheet_id, "png")} alt={s.title} />
            </a>
            <div className="cap">
              <div className="capttl">
                {s.title || "Untitled"}{s.keyplan && <span className="kpbadge">KEY PLAN</span>}
              </div>
              <div className="capsub">{s.suite} · {s.sf} · {s.created}</div>
              <div className="libactions">
                <a href={sheetUrl(propertyId, s.sheet_id, "svg")} target="_blank" rel="noreferrer" download>SVG</a>
                <a href={sheetUrl(propertyId, s.sheet_id, "png")} target="_blank" rel="noreferrer" download>PNG</a>
                {s.keyplan && (
                  <a href={sheetUrl(propertyId, `${s.sheet_id}-keyplan`, "svg")}
                     target="_blank" rel="noreferrer" download>Key plan</a>
                )}
                <button onClick={() => onReopen(s)}>Re-open</button>
                <button className="del" onClick={() => onDelete(s)}>Delete</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
