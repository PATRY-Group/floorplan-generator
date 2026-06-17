import React, { useEffect, useRef, useState } from "react";
import {
  getCapabilities, listProperties, parseFile, renderSheet,
  listSheets, reopenSheet, deleteSheet,
} from "./api.js";
import LabelOverlay from "./LabelOverlay.jsx";
import PropertySetup from "./PropertySetup.jsx";
import KeyPlanPanel from "./KeyPlanPanel.jsx";
import Library from "./Library.jsx";
import Toasts from "./Toasts.jsx";
import { toast } from "./toast.js";

const LS_PROP = "fpsg.lastProperty";
const LS_SESSION = "fpsg.session.v2";   // multi-document shape
const LS_UI = "fpsg.ui";

const slugify = (s) => (s || "floorplan").replace(/\s+/g, "-").toLowerCase();

// Best-effort unit name when the DXF doesn't carry one: count bedrooms.
function guessTitle(labels) {
  const beds = (labels || []).filter(
    (l) => /BED|MASTER|PRIMARY/.test((l.name || "").toUpperCase())
  ).length;
  return beds === 0 ? "STUDIO" : `${beds} BED`;
}

let _seq = 0;
const uid = () => `d${Date.now().toString(36)}${(_seq++).toString(36)}`;

// One open floor-plan editing session (one tab).
function newDoc(propertyId) {
  return {
    id: uid(),
    propertyId: propertyId || "",
    fileName: "",
    docId: null,           // server geometry handle
    rooms: [],
    deletedRooms: [],      // undo stack: { room, index }
    ignored: [],
    meta: { title: "", suite: "", sf: "" },
    suggestions: {},
    warnings: [],
    parseError: "",
    renderError: "",
    keyplan: null,
    keyplanSvg: null,
    svg: "",
    placement: null,
    savedId: null,
    showHandles: true,
  };
}

export default function App() {
  const [caps, setCaps] = useState(null);
  const [properties, setProperties] = useState([]);
  const [defaultProp, setDefaultProp] = useState("");  // property for new uploads
  const [docs, setDocs] = useState([]);
  const [activeId, setActiveId] = useState(null);      // a doc id, or "library"
  const [sheets, setSheets] = useState([]);

  const [parsing, setParsing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pngBusy, setPngBusy] = useState(false);
  const [dlOpen, setDlOpen] = useState(false);

  const [editing, setEditing] = useState(null);
  const [openSection, setOpenSection] = useState("upload");

  const [panelW, setPanelW] = useState(380);
  const [collapsed, setCollapsed] = useState(false);
  const [resizing, setResizing] = useState(false);

  const debounce = useRef(null);

  const active = docs.find((d) => d.id === activeId) || null;
  const propertyId = active ? active.propertyId : defaultProp;
  const ready = !!(active && active.docId);

  // ---- doc state helpers ---------------------------------------------------
  function patchDoc(id, patch) {
    setDocs((ds) => ds.map((d) => (d.id === id
      ? { ...d, ...(typeof patch === "function" ? patch(d) : patch) } : d)));
  }
  const patchActive = (patch) => { if (activeId) patchDoc(activeId, patch); };

  // ---- mount: capabilities, properties, recents, session restore ----------
  useEffect(() => {
    getCapabilities().then(setCaps).catch(() => {});
    try {
      const ui = JSON.parse(localStorage.getItem(LS_UI) || "{}");
      if (ui.panelW) setPanelW(ui.panelW);
      if (ui.collapsed) setCollapsed(true);
    } catch (e) { /* ignore */ }
    listProperties().then((p) => {
      setProperties(p);
      const last = localStorage.getItem(LS_PROP);
      const initial = (last && p.find((x) => x.id === last)) ? last : (p[0] ? p[0].id : "");
      setDefaultProp(initial);
      restoreSession(initial);
    }).catch(() => restoreSession(""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function restoreSession(initialProp) {
    try {
      const s = JSON.parse(localStorage.getItem(LS_SESSION) || "null");
      if (s && Array.isArray(s.docs) && s.docs.length) {
        const restored = s.docs.map((d) => ({
          ...newDoc(d.propertyId || initialProp), ...d,
          svg: "", placement: null, keyplanSvg: null, savedId: null,
          parseError: "", renderError: "",
        }));
        setDocs(restored);
        const valid = s.activeId === "library" || restored.find((x) => x.id === s.activeId);
        setActiveId(valid ? s.activeId : restored[0].id);
        if (restored.some((d) => d.docId)) setOpenSection("details");
        toast("Restored your in-progress work", "info");
        return;
      }
    } catch (e) { /* ignore */ }
    const d = newDoc(initialProp);
    setDocs([d]);
    setActiveId(d.id);
  }

  useEffect(() => { if (defaultProp) localStorage.setItem(LS_PROP, defaultProp); }, [defaultProp]);
  useEffect(() => {
    localStorage.setItem(LS_UI, JSON.stringify({ panelW, collapsed }));
  }, [panelW, collapsed]);

  // autosave open docs (slim — geometry stays server-side, re-rendered on load)
  useEffect(() => {
    if (!docs.length) return;
    const slim = docs.map((d) => ({
      id: d.id, propertyId: d.propertyId, docId: d.docId, fileName: d.fileName,
      rooms: d.rooms, deletedRooms: d.deletedRooms, ignored: d.ignored,
      meta: d.meta, suggestions: d.suggestions, warnings: d.warnings,
      keyplan: d.keyplan, showHandles: d.showHandles,
    }));
    localStorage.setItem(LS_SESSION, JSON.stringify({ docs: slim, activeId }));
  }, [docs, activeId]);

  // library list follows the active property
  useEffect(() => {
    if (propertyId) listSheets(propertyId).then(setSheets).catch(() => {});
    else setSheets([]);
  }, [propertyId, activeId]);

  // Ctrl/Cmd+Z restores the last deleted room of the active doc, unless a text
  // field is focused (so native input undo keeps working).
  useEffect(() => {
    function onKey(e) {
      if (!((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z") && !e.shiftKey)) return;
      const el = document.activeElement;
      const tag = el && el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (el && el.isContentEditable)) return;
      e.preventDefault();
      undoDeleteRoom();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // auto preview (debounced) whenever the active doc's inputs change
  useEffect(() => {
    if (!active || !active.docId) return;
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => doRender(false), 450);
    return () => clearTimeout(debounce.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, active && active.docId, active && active.rooms,
      active && active.meta, active && active.propertyId, active && active.keyplan]);

  function refreshProperties(selectId) {
    return listProperties().then((p) => {
      setProperties(p);
      if (selectId) setDefaultProp(selectId);
    });
  }

  function chooseProperty(v) {
    setDefaultProp(v);
    if (active) patchActive({ propertyId: v });
  }

  // ---- tabs ----------------------------------------------------------------
  function newTab() {
    const d = newDoc(defaultProp || (properties[0] && properties[0].id) || "");
    setDocs((ds) => [...ds, d]);
    setActiveId(d.id);
    setOpenSection("upload");
  }
  function closeTab(id) {
    setDocs((ds) => {
      const idx = ds.findIndex((d) => d.id === id);
      const next = ds.filter((d) => d.id !== id);
      if (activeId === id) {
        const nb = next[idx] || next[idx - 1] || null;
        setActiveId(nb ? nb.id : "library");
      }
      return next;
    });
  }

  // ---- parse / render ------------------------------------------------------
  async function handleFile(file) {
    if (!file) return;
    let id = activeId;
    let prop = active ? active.propertyId : defaultProp;
    if (!active) {                 // on the Library tab — open a fresh editor tab
      const d = newDoc(defaultProp);
      id = d.id; prop = d.propertyId;
      setDocs((ds) => [...ds, d]);
      setActiveId(d.id);
    }
    setParsing(true);
    patchDoc(id, { fileName: file.name, parseError: "", svg: "", savedId: null });
    try {
      const d = await parseFile(file, prop || undefined);
      patchDoc(id, {
        docId: d.doc_id,
        rooms: d.labels.map((l) => ({ ...l })),
        deletedRooms: [],
        ignored: d.ignored_text || [],
        suggestions: d.suggestions || {},
        warnings: d.warnings || [],
        parseError: "",
        meta: {
          title: (d.suggestions && d.suggestions.title) || guessTitle(d.labels),
          suite: (d.suggestions && d.suggestions.suite) || "",
          sf: (d.suggestions && d.suggestions.sf) || "",
        },
      });
      setOpenSection("details");
    } catch (e) {
      patchDoc(id, { docId: null, rooms: [], parseError: e.message });
      toast(e.message, "error");
    } finally {
      setParsing(false);
    }
  }

  async function doRender(save) {
    const d = docs.find((x) => x.id === activeId);
    if (!d || !d.docId) return;
    if (save) setSaving(true); else setRendering(true);
    try {
      const res = await renderSheet({
        doc_id: d.docId, property_id: d.propertyId || null,
        metadata: d.meta, rooms: d.rooms, keyplan: d.keyplan || null, save,
      });
      patchDoc(d.id, {
        svg: res.svg, placement: res.meta || d.placement,
        keyplanSvg: res.keyplan_svg || null, renderError: "",
        ...(save && res.sheet_id ? { savedId: res.sheet_id } : {}),
      });
      if (save && res.sheet_id) {
        toast("Saved to the library", "success");
        if (d.propertyId) listSheets(d.propertyId).then(setSheets).catch(() => {});
      }
    } catch (e) {
      if (/expired|not found/i.test(e.message)) {
        toast("This unit's upload expired — re-upload the DXF.", "error");
        patchDoc(d.id, { docId: null });
      } else {
        patchDoc(d.id, { renderError: e.message });
      }
    } finally {
      setRendering(false);
      setSaving(false);
    }
  }

  // ---- room edits (active doc) --------------------------------------------
  const updateRoom = (i, p) =>
    patchActive((d) => ({ rooms: d.rooms.map((r, j) => (j === i ? { ...r, ...p } : r)) }));
  const moveLabel = (i, x, y) => updateRoom(i, { x, y });
  const resetLabel = (i) => updateRoom(i, { x: null, y: null });
  const revertLabels = () =>
    patchActive((d) => ({ rooms: d.rooms.map((r) => ({ ...r, x: null, y: null })) }));
  function removeRoom(i) {
    patchActive((d) => {
      const room = d.rooms[i];
      return {
        rooms: d.rooms.filter((_, j) => j !== i),
        deletedRooms: room ? [...d.deletedRooms, { room, index: i }] : d.deletedRooms,
      };
    });
  }
  function undoDeleteRoom() {
    patchActive((d) => {
      if (!d.deletedRooms.length) return {};
      const { room, index } = d.deletedRooms[d.deletedRooms.length - 1];
      const at = Math.min(index, d.rooms.length);
      toast(`Restored "${room.name || "room"}"`, "success");
      return {
        rooms: [...d.rooms.slice(0, at), room, ...d.rooms.slice(at)],
        deletedRooms: d.deletedRooms.slice(0, -1),
      };
    });
  }
  function readdIgnored(item, i) {
    patchActive((d) => ({
      rooms: [...d.rooms, {
        name: item.text.toUpperCase(), dims: null,
        seed_x: item.x, seed_y: item.y, rect: null, font_scale: 1.0, show_dims: true,
      }],
      ignored: d.ignored.filter((_, j) => j !== i),
    }));
  }

  // ---- export --------------------------------------------------------------
  function downloadCurrentSvg() {
    if (!active || !active.svg) return;
    const blob = new Blob([active.svg], { type: "image/svg+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${slugify(active.meta.title)}.svg`;
    a.click();
    URL.revokeObjectURL(url);
  }
  async function downloadCurrentPng() {
    const d = docs.find((x) => x.id === activeId);
    if (!d || !d.docId) return;
    setPngBusy(true);
    try {
      const res = await renderSheet({
        doc_id: d.docId, property_id: d.propertyId || null,
        metadata: d.meta, rooms: d.rooms, keyplan: d.keyplan || null, want_png: true,
      });
      if (!res.png_b64) throw new Error("No PNG returned by the server.");
      const a = document.createElement("a");
      a.href = "data:image/png;base64," + res.png_b64;
      a.download = `${slugify(d.meta.title)}.png`;
      a.click();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setPngBusy(false);
    }
  }

  // ---- library actions -----------------------------------------------------
  async function reopen(s) {
    try {
      const cfg = await reopenSheet(propertyId, s.sheet_id);
      const d = newDoc(cfg.property_id || propertyId);
      d.docId = cfg.doc_id;
      d.rooms = (cfg.rooms || []).map((r) => ({ ...r }));
      d.meta = cfg.metadata || { title: "", suite: "", sf: "" };
      d.keyplan = cfg.keyplan || null;
      d.fileName = `${s.title || "sheet"} (re-opened)`;
      setDocs((ds) => [...ds, d]);
      setActiveId(d.id);
      setOpenSection("details");
      toast("Re-opened in a new tab — edit and re-save", "success");
    } catch (e) {
      toast(e.message, "error");
    }
  }
  async function removeSheet(s) {
    if (!window.confirm(`Delete "${s.title || "Untitled"}"? This can't be undone.`)) return;
    try {
      await deleteSheet(propertyId, s.sheet_id);
      setSheets((xs) => xs.filter((x) => x.sheet_id !== s.sheet_id));
      toast("Sheet deleted", "success");
    } catch (e) {
      toast(e.message, "error");
    }
  }

  // ---- sidebar resize ------------------------------------------------------
  function startResize(e) {
    e.preventDefault();
    e.target.setPointerCapture?.(e.pointerId);
    setResizing(true);
  }
  function onResizeMove(e) {
    if (!resizing) return;
    setPanelW(Math.min(620, Math.max(280, e.clientX)));
  }
  function endResize() { setResizing(false); }

  const tabLabel = (d) => d.meta.title || d.fileName.replace(/\.[^.]+$/, "") || "Untitled";

  return (
    <div className="app">
      <Toasts />

      {collapsed ? (
        <button className="expandbtn" title="Show sidebar" onClick={() => setCollapsed(false)}>›</button>
      ) : (
        <aside className="panel" style={{ width: panelW, minWidth: panelW }}>
          <div className="brandbar">
            <span className="mark">▭</span>
            <span className="title">FLOOR PLAN SHEET GENERATOR</span>
            <button className="collapsebtn" title="Hide sidebar" onClick={() => setCollapsed(true)}>«</button>
          </div>

          {active && active.parseError && <div className="error">{active.parseError}</div>}
          {(active ? active.warnings : []).map((w, i) => <div className="warn" key={i}>{w}</div>)}

          <div className="step">
            <h3><span className="num">1</span> Property</h3>
            <select value={propertyId} onChange={(e) => chooseProperty(e.target.value)}>
              {properties.length === 0 && <option value="">(no properties configured)</option>}
              {properties.map((p) => (
                <option key={p.id} value={p.id}>{p.name} — {p.location}</option>
              ))}
            </select>
            <div className="btnrow">
              <button className="btn ghost" onClick={() => setEditing("new")}>+ New property</button>
              <button className="btn ghost" disabled={!propertyId}
                onClick={() => setEditing(properties.find((x) => x.id === propertyId))}>
                Edit
              </button>
            </div>
          </div>

          <div className="step">
            <h3><span className="num">2</span> Upload floor plan</h3>
            <label className="drop">
              {parsing ? "Parsing…" : (active && active.fileName ? active.fileName : "Click to choose a DXF")}
              <input type="file" accept=".dxf,.dwg"
                onChange={(e) => handleFile(e.target.files[0])} />
            </label>
            {caps && (
              <p className="subtle" style={{ marginTop: 6 }}>
                Accepts {caps.formats_accepted.join(", ").toUpperCase()}.
                {!caps.dwg_conversion && " DWG needs the ODA converter on the server."}
                {" "}.rvt is not supported — export a DXF view from Revit.
              </p>
            )}
          </div>

          {ready && (
            <>
              <div className="step">
                <h3><span className="num">3</span> Unit details</h3>
                <label>Unit name (shown on the sheet footer)</label>
                <input type="text" value={active.meta.title}
                  onChange={(e) => patchActive((d) => ({ meta: { ...d.meta, title: e.target.value } }))}
                  placeholder="ONE BED" />
                {active.suggestions.title && active.suggestions.title !== active.meta.title && (
                  <button className="chip"
                    onClick={() => patchActive((d) => ({ meta: { ...d.meta, title: d.suggestions.title } }))}>
                    use “{active.suggestions.title}”
                  </button>
                )}
                <div className="row">
                  <div>
                    <label>Suite</label>
                    <input type="text" value={active.meta.suite}
                      onChange={(e) => patchActive((d) => ({ meta: { ...d.meta, suite: e.target.value } }))}
                      placeholder="202" />
                  </div>
                  <div>
                    <label>Square footage</label>
                    <input type="text" value={active.meta.sf}
                      onChange={(e) => patchActive((d) => ({ meta: { ...d.meta, sf: e.target.value } }))}
                      placeholder="517 SF" />
                  </div>
                </div>
              </div>

              <div className="step">
                <div className="rooms-head">
                  <h3><span className="num">4</span> Rooms ({active.rooms.length})</h3>
                  <button className="btn ghost" disabled={active.deletedRooms.length === 0}
                    title="Restore the last deleted room (Ctrl+Z)"
                    onClick={undoDeleteRoom}>
                    ↩ Undo delete{active.deletedRooms.length > 1 ? ` (${active.deletedRooms.length})` : ""}
                  </button>
                </div>
                {active.rooms.some((r) => r.dims_estimated && r.dims) && (
                  <p className="subtle" style={{ marginTop: 0 }}>
                    Dimensions are estimated — double-check the ones you intend to keep.
                  </p>
                )}
                {active.rooms.map((r, i) => (
                  <div className="room" key={i}>
                    <div className="top">
                      <input type="text" value={r.name}
                        onChange={(e) => updateRoom(i, { name: e.target.value })} />
                      <button className="chip" onClick={() => removeRoom(i)}>✕</button>
                    </div>
                    <div className="meta">
                      <input type="text" value={r.dims || ""}
                        placeholder={"dimensions e.g. 14'4\" x 9'3\""}
                        onChange={(e) => updateRoom(i, { dims: e.target.value || null })} />
                      <label className="toggle">
                        <input type="checkbox" checked={r.show_dims !== false}
                          onChange={(e) => updateRoom(i, { show_dims: e.target.checked })} />
                        show
                      </label>
                    </div>
                  </div>
                ))}
                {active.ignored.length > 0 && (
                  <div className="ignored">
                    Ignored text (click to add as a room):
                    <div>
                      {active.ignored.map((t, i) => (
                        <button className="chip" key={i} onClick={() => readdIgnored(t, i)}>
                          {t.text}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <KeyPlanPanel key={active.id}
                palette={(properties.find((p) => p.id === propertyId) || {}).palette}
                onChange={(kp) => patchActive({ keyplan: kp })} />
            </>
          )}
        </aside>
      )}

      {!collapsed && (
        <div className={"resizer" + (resizing ? " active" : "")}
          onPointerDown={startResize} onPointerMove={onResizeMove}
          onPointerUp={endResize} onPointerLeave={endResize} />
      )}

      <main className="stage">
        <div className="stagehead">
          <div className="tabbar">
            <div className="tabs">
              {docs.map((d) => (
                <span key={d.id}
                  className={"tab" + (activeId === d.id ? " active" : "")}
                  onClick={() => setActiveId(d.id)}>
                  <span className="tablabel">{tabLabel(d)}</span>
                  <span className="tabx" title="Close" onClick={(e) => { e.stopPropagation(); closeTab(d.id); }}>×</span>
                </span>
              ))}
              <button className="tab newtab" title="New floor plan" onClick={newTab}>+</button>
              <span className={"tab library-tab" + (activeId === "library" ? " active" : "")}
                onClick={() => setActiveId("library")}>
                Library{sheets.length ? ` (${sheets.length})` : ""}
              </span>
            </div>
            {ready && (
              <div className="actions">
                <button className="btn ember" disabled={saving || rendering || !propertyId}
                  onClick={() => doRender(true)}>
                  {saving ? "Saving…" : "Save to library"}
                </button>
                <div className="dropdown">
                  <button className="btn ghost" disabled={!active.svg}
                    onClick={() => setDlOpen((o) => !o)}>
                    {pngBusy ? "Rendering…" : "Download ▾"}
                  </button>
                  {dlOpen && (
                    <>
                      <div className="dropdown-backdrop" onClick={() => setDlOpen(false)} />
                      <div className="menu">
                        <button onClick={() => { setDlOpen(false); downloadCurrentSvg(); }}>SVG</button>
                        <button disabled={pngBusy}
                          onClick={() => { setDlOpen(false); downloadCurrentPng(); }}>PNG</button>
                        <button disabled={pngBusy}
                          onClick={() => { setDlOpen(false); downloadCurrentSvg(); downloadCurrentPng(); }}>
                          SVG + PNG
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {activeId === "library" ? (
          <Library propertyId={propertyId} sheets={sheets}
            onReopen={reopen} onDelete={removeSheet} />
        ) : !ready ? (
          <div className="placeholder">
            <div className="big">▭</div>
            Pick a property and upload a unit DXF to see a live, branded sheet here.
            Room labels are placed automatically from the CAD file.
          </div>
        ) : (
          <>
            <div className="statusline">
              <span className="statustext">
                {rendering ? <span className="spin">rendering…</span>
                  : active.renderError ? <span style={{ color: "#8a3d28" }}>{active.renderError}</span>
                  : active.showHandles
                    ? "Live preview — drag to move a label, double-click to reset. Click “Hide labels” to hide the edit icons."
                    : "Labels hidden. Click “Show labels” to move labels again."}
              </span>
              <div className="actions-right">
                <button className="btn ghost"
                  disabled={!active.rooms.some((r) => r.x != null && r.y != null)}
                  onClick={revertLabels}
                  title="Move every label back to its automatic position">
                  Revert
                </button>
                <button className="btn ghost" onClick={() => patchActive((d) => ({ showHandles: !d.showHandles }))}
                  title="Hide the move handles to see the final sheet">
                  {active.showHandles ? "Hide labels" : "Show labels"}
                </button>
                <button className="btn ghost icon" disabled={rendering} onClick={() => doRender(false)}
                  title="Reload — re-render the preview (e.g. after editing the property's brand)">
                  {rendering ? "…" : "⟳"}
                </button>
              </div>
            </div>
            {active.svg
              ? <LabelOverlay svg={active.svg} meta={active.placement} showHandles={active.showHandles}
                  onMove={moveLabel} onReset={resetLabel} />
              : <div className="sheet" style={{ minHeight: 200 }} />}
            {active.keyplanSvg && (
              <div style={{ width: "100%", maxWidth: 760, marginTop: 18 }}>
                <div className="statusline">Standalone key plan</div>
                <div className="sheet" dangerouslySetInnerHTML={{ __html: active.keyplanSvg }} />
              </div>
            )}
          </>
        )}
      </main>

      {editing && (
        <PropertySetup
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            setEditing(null);
            refreshProperties(saved.id);
            toast(`Property "${saved.name || saved.id}" saved`, "success");
            if (active && active.docId) doRender(false);   // refresh preview with new brand
          }}
          onDeleted={(p) => {
            setEditing(null);
            refreshProperties();
            toast(`Property "${p.name || p.id}" deleted`, "success");
          }}
        />
      )}
    </div>
  );
}
