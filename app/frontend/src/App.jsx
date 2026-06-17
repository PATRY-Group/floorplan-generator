import React, { useEffect, useRef, useState } from "react";
import {
  getCapabilities, listProperties, parseFile, renderSheet,
  listSheets, sheetUrl, reopenSheet, deleteSheet,
} from "./api.js";
import LabelOverlay from "./LabelOverlay.jsx";
import PropertySetup from "./PropertySetup.jsx";
import KeyPlanPanel from "./KeyPlanPanel.jsx";
import Library from "./Library.jsx";
import Toasts from "./Toasts.jsx";
import { toast } from "./toast.js";

const LS_PROP = "fpsg.lastProperty";
const LS_SESSION = "fpsg.session";

export default function App() {
  const [caps, setCaps] = useState(null);
  const [properties, setProperties] = useState([]);
  const [propertyId, setPropertyId] = useState("");

  const [fileName, setFileName] = useState("");
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState("");
  const [warnings, setWarnings] = useState([]);

  const [doc, setDoc] = useState(null);
  const [rooms, setRooms] = useState([]);
  const [ignored, setIgnored] = useState([]);
  const [meta, setMeta] = useState({ title: "", suite: "", sf: "" });
  const [suggestions, setSuggestions] = useState({});

  const [svg, setSvg] = useState("");
  const [placement, setPlacement] = useState(null);
  const [keyplan, setKeyplan] = useState(null);
  const [keyplanSvg, setKeyplanSvg] = useState(null);
  const [rendering, setRendering] = useState(false);
  const [renderError, setRenderError] = useState("");

  const [savedId, setSavedId] = useState(null);
  const [sheets, setSheets] = useState([]);

  const [editing, setEditing] = useState(null);

  const debounce = useRef(null);

  function refreshProperties(selectId) {
    return listProperties().then((p) => {
      setProperties(p);
      if (selectId) setPropertyId(selectId);
      else if (p.length && !p.find((x) => x.id === propertyId)) setPropertyId(p[0].id);
    });
  }

  // ---- mount: capabilities, properties, recents, session restore ----------
  useEffect(() => {
    getCapabilities().then(setCaps).catch(() => {});
    listProperties().then((p) => {
      setProperties(p);
      const last = localStorage.getItem(LS_PROP);
      const initial = (last && p.find((x) => x.id === last)) ? last : (p[0] ? p[0].id : "");
      setPropertyId(initial);
      restoreSession();
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function restoreSession() {
    try {
      const raw = localStorage.getItem(LS_SESSION);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (s.doc_id && Array.isArray(s.rooms)) {
        if (s.propertyId) setPropertyId(s.propertyId);
        setDoc({ doc_id: s.doc_id });
        setRooms(s.rooms);
        setMeta(s.meta || { title: "", suite: "", sf: "" });
        setIgnored(s.ignored || []);
        setFileName(s.fileName || "(restored)");
        toast("Restored your in-progress unit", "info");
      }
    } catch (e) { /* ignore */ }
  }

  useEffect(() => { if (propertyId) localStorage.setItem(LS_PROP, propertyId); }, [propertyId]);

  // autosave the in-progress unit
  useEffect(() => {
    if (!doc) return;
    localStorage.setItem(LS_SESSION, JSON.stringify({
      propertyId, doc_id: doc.doc_id, rooms, meta, ignored, fileName,
    }));
  }, [doc, rooms, meta, ignored, fileName, propertyId]);

  useEffect(() => {
    if (propertyId) listSheets(propertyId).then(setSheets).catch(() => {});
  }, [propertyId, savedId]);

  // auto preview (debounced) whenever inputs change
  useEffect(() => {
    if (!doc) return;
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => doRender(false), 450);
    return () => clearTimeout(debounce.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc, rooms, meta, propertyId, keyplan]);

  async function handleFile(file) {
    if (!file) return;
    setFileName(file.name);
    setParsing(true);
    setParseError("");
    setWarnings([]);
    setSvg("");
    setSavedId(null);
    try {
      const d = await parseFile(file, propertyId);
      setDoc({ doc_id: d.doc_id });
      setRooms(d.labels.map((l) => ({ ...l })));
      setIgnored(d.ignored_text || []);
      setSuggestions(d.suggestions || {});
      setWarnings(d.warnings || []);
      setMeta({
        title: (d.suggestions && d.suggestions.title) || "",
        suite: (d.suggestions && d.suggestions.suite) || "",
        sf: (d.suggestions && d.suggestions.sf) || "",
      });
    } catch (e) {
      setDoc(null);
      setRooms([]);
      setParseError(e.message);
      toast(e.message, "error");
    } finally {
      setParsing(false);
    }
  }

  async function doRender(save) {
    if (!doc) return;
    setRendering(true);
    setRenderError("");
    try {
      const res = await renderSheet({
        doc_id: doc.doc_id,
        property_id: propertyId || null,
        metadata: meta,
        rooms,
        keyplan: keyplan || null,
        save,
      });
      setSvg(res.svg);
      if (res.meta) setPlacement(res.meta);
      setKeyplanSvg(res.keyplan_svg || null);
      if (save && res.sheet_id) {
        setSavedId(res.sheet_id);
        toast("Saved to the library", "success");
      }
    } catch (e) {
      setRenderError(e.message);
      if (/expired|not found/i.test(e.message)) {
        toast("Your earlier upload expired — re-upload the DXF.", "error");
        setDoc(null);
        localStorage.removeItem(LS_SESSION);
      }
    } finally {
      setRendering(false);
    }
  }

  function updateRoom(i, patch) {
    setRooms((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }
  function moveLabel(i, x, y) { updateRoom(i, { x, y }); }
  function resetLabel(i) { updateRoom(i, { x: null, y: null }); }
  function removeRoom(i) { setRooms((rs) => rs.filter((_, j) => j !== i)); }
  function readdIgnored(item, i) {
    setRooms((rs) => [...rs, {
      name: item.text.toUpperCase(), dims: null,
      seed_x: item.x, seed_y: item.y, rect: null, font_scale: 1.0, show_dims: true,
    }]);
    setIgnored((ig) => ig.filter((_, j) => j !== i));
  }

  function downloadCurrentSvg() {
    const blob = new Blob([svg], { type: "image/svg+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(meta.title || "floorplan").replace(/\s+/g, "-").toLowerCase()}.svg`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function reopen(s) {
    try {
      const cfg = await reopenSheet(propertyId, s.sheet_id);
      setDoc({ doc_id: cfg.doc_id });
      setRooms((cfg.rooms || []).map((r) => ({ ...r })));
      setMeta(cfg.metadata || { title: "", suite: "", sf: "" });
      setIgnored([]);
      setKeyplan(null);
      setSavedId(null);
      setFileName(`${s.title || "sheet"} (re-opened)`);
      toast("Re-opened — edit and re-save", "success");
      window.scrollTo({ top: 0, behavior: "smooth" });
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

  const ready = !!doc;

  return (
    <div className="app">
      <Toasts />
      <aside className="panel">
        <div className="brandbar">
          <span className="mark">▭</span>
          <span className="title">FLOOR PLAN SHEET GENERATOR</span>
        </div>
        <p className="subtle">CAD in, branded marketing sheet out.</p>

        {parseError && <div className="error">{parseError}</div>}
        {warnings.map((w, i) => <div className="warn" key={i}>{w}</div>)}

        <div className="step">
          <h3><span className="num">1</span> Property</h3>
          <select value={propertyId} onChange={(e) => setPropertyId(e.target.value)}>
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
            {parsing ? "Parsing…" : (fileName || "Click to choose a DXF")}
            <input type="file" accept=".dxf,.dwg"
              onChange={(e) => handleFile(e.target.files[0])} />
            {fileName && !parsing && <div className="filename">{fileName}</div>}
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
              <label>Unit title</label>
              <input type="text" value={meta.title}
                onChange={(e) => setMeta({ ...meta, title: e.target.value })}
                placeholder="ONE BED" />
              {suggestions.title && suggestions.title !== meta.title && (
                <button className="chip" onClick={() => setMeta({ ...meta, title: suggestions.title })}>
                  use “{suggestions.title}”
                </button>
              )}
              <div className="row">
                <div>
                  <label>Suite</label>
                  <input type="text" value={meta.suite}
                    onChange={(e) => setMeta({ ...meta, suite: e.target.value })}
                    placeholder="202" />
                </div>
                <div>
                  <label>Square footage</label>
                  <input type="text" value={meta.sf}
                    onChange={(e) => setMeta({ ...meta, sf: e.target.value })}
                    placeholder="517 SF" />
                </div>
              </div>
            </div>

            <div className="step">
              <h3><span className="num">4</span> Rooms ({rooms.length})</h3>
              {rooms.map((r, i) => (
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
              {ignored.length > 0 && (
                <div className="ignored">
                  Ignored text (click to add as a room):
                  <div>
                    {ignored.map((t, i) => (
                      <button className="chip" key={i} onClick={() => readdIgnored(t, i)}>
                        {t.text}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <KeyPlanPanel onChange={setKeyplan} />

            <div className="step">
              <button className="btn ember" disabled={rendering || !propertyId}
                onClick={() => doRender(true)}>
                {rendering ? "Working…" : "Save to library & export"}
              </button>
              <div className="btnrow">
                <button className="btn ghost" disabled={!svg} onClick={downloadCurrentSvg}>
                  Download SVG
                </button>
              </div>
            </div>
          </>
        )}
      </aside>

      <main className="stage">
        {!ready && (
          <div className="placeholder">
            <div className="big">▭</div>
            Pick a property and upload a unit DXF to see a live, branded sheet here.
            Room labels are placed automatically from the CAD file.
          </div>
        )}

        {ready && (
          <>
            <div className="stagebar">
              <span className="status">
                {rendering ? <span className="spin">rendering…</span>
                  : renderError ? <span style={{ color: "#8a3d28" }}>{renderError}</span>
                  : "Live preview — drag or click+arrow to move a label, double-click to reset"}
              </span>
              <div className="actions">
                {savedId && (
                  <>
                    <a className="btn ghost" href={sheetUrl(propertyId, savedId, "svg")}
                       target="_blank" rel="noreferrer" download>SVG</a>
                    <a className="btn" href={sheetUrl(propertyId, savedId, "png")}
                       target="_blank" rel="noreferrer" download>PNG</a>
                  </>
                )}
              </div>
            </div>
            {svg
              ? <LabelOverlay svg={svg} meta={placement}
                              onMove={moveLabel} onReset={resetLabel} />
              : <div className="sheet" style={{ minHeight: 200 }} />}
            {keyplanSvg && (
              <div style={{ width: "100%", maxWidth: 760, marginTop: 18 }}>
                <div className="stagebar"><span className="status">Standalone key plan</span></div>
                <div className="sheet" dangerouslySetInnerHTML={{ __html: keyplanSvg }} />
              </div>
            )}
          </>
        )}

        <Library propertyId={propertyId} sheets={sheets}
                 onReopen={reopen} onDelete={removeSheet} />
      </main>

      {editing && (
        <PropertySetup
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            setEditing(null);
            refreshProperties(saved.id);
            toast(`Property "${saved.name || saved.id}" saved`, "success");
          }}
        />
      )}
    </div>
  );
}
