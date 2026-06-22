import React, { useEffect, useRef, useState } from "react";
import {
  getCapabilities, listProperties, parseFile, renderSheet,
  listAllSheets, reopenSheet, deleteSheet, renameSheet,
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

// Batch upload (spec §11): select up to 10 files; each parsed unit opens as its
// own editable tab and nothing auto-saves to the library. The queue throttles
// parsing to BATCH_CONCURRENCY at a time and isolates per-file failures.
const BATCH_MAX = 10;
const BATCH_CONCURRENCY = 2;

const slugify = (s) => (s || "floorplan").replace(/\s+/g, "-").toLowerCase();

// Download/save filename: the property slug prefixes the unit title so files
// group by property on disk. This is intentionally NOT the footer name on the
// sheet (which stays just the unit title) — the file on the computer carries the
// property prefix, the diagram's footer does not.
const exportName = (propId, title, suffix = "") => {
  const base = slugify(title) + suffix;
  return propId ? `${slugify(propId)}-${base}` : base;
};

// Best-effort unit name when the DXF doesn't carry one: count bedrooms.
function guessTitle(labels) {
  const beds = (labels || []).filter(
    (l) => /BED|MASTER|PRIMARY/.test((l.name || "").toUpperCase())
  ).length;
  return beds === 0 ? "STUDIO" : `${beds} BED`;
}

let _seq = 0;
const uid = () => `d${Date.now().toString(36)}${(_seq++).toString(36)}`;

// CAD layer roles the renderer understands (mirrors PropertySetup's LAYER_ROLES);
// "" = unmapped/ignored. Used by the detected-layers review table.
const LAYER_ROLE_OPTS = [
  ["", "(ignore)"],
  ["wall_line", "Wall outline"],
  ["wall_fill", "Wall fill"],
  ["door", "Doors"],
  ["glazing", "Glazing"],
  ["dashed", "Overhead / dashed"],
  ["room_label", "Room-label text"],
  ["drop", "Drop (tags/cols/stairs)"],
  ["floor_hatch", "Floor hatch"],
];

// {layer: role} (from a layer_report) -> a layer_map {role: [layer,…]} the
// backend accepts as a manual override. Unassigned ("") layers are omitted.
function rolesToLayerMap(roles) {
  const m = {};
  for (const [layer, role] of Object.entries(roles || {})) {
    if (!role) continue;
    (m[role] = m[role] || []).push(layer);
  }
  return m;
}

// Map a /parse response onto the doc fields it populates. Shared by the single
// upload (handleFile) and batch (runBatch) paths so they can't drift.
function parsedDocFields(d, fileName) {
  return {
    fileName,
    docId: d.doc_id,
    rooms: d.labels.map((l) => ({ ...l })),
    deletedRooms: [],
    ignored: d.ignored_text || [],
    suggestions: d.suggestions || {},
    warnings: d.warnings || [],
    // layer auto-detection: set when the file's layers didn't match the active
    // map and roles were inferred (so the UI can offer review + save-as-property)
    layerInferred: !!d.layer_inferred,
    layerReport: d.layer_report || null,
    layerMapUsed: d.layer_map_used || null,
    // editable {layer: role} seeded from the report, for the review table
    layerRoles: d.layer_report
      ? Object.fromEntries(d.layer_report.map((r) => [r.layer, r.role || ""]))
      : null,
    parseError: "",
    svg: "",
    savedId: null,
    meta: {
      title: (d.suggestions && d.suggestions.title) || guessTitle(d.labels),
      suite: (d.suggestions && d.suggestions.suite) || "",
      sf: (d.suggestions && d.suggestions.sf) || "",
    },
  };
}

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
    layerInferred: false,
    layerReport: null,
    layerMapUsed: null,
    layerRoles: null,
    fileObj: null,         // the uploaded File, kept in-memory to re-parse on a layer-map correction
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
  const [queue, setQueue] = useState([]);              // batch upload progress rows

  const [parsing, setParsing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pngBusy, setPngBusy] = useState(false);
  const [dlOpen, setDlOpen] = useState(false);
  const [saveMenuOpen, setSaveMenuOpen] = useState(false);

  const [editing, setEditing] = useState(null);
  const [seedLayer, setSeedLayer] = useState(null);  // detected layer_map to pre-fill a NEW property
  const [showLayerReview, setShowLayerReview] = useState(false);  // detected-layers review table expanded
  const [openSection, setOpenSection] = useState("upload");

  const [panelW, setPanelW] = useState(380);
  const [collapsed, setCollapsed] = useState(false);
  const [resizing, setResizing] = useState(false);
  const [tabOrient, setTabOrient] = useState("horizontal");  // "horizontal" | "vertical"
  const [railW, setRailW] = useState(170);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [railResizing, setRailResizing] = useState(false);
  const railResize = useRef(null);   // { startX, startW } while dragging the rail edge
  const [winW, setWinW] = useState(typeof window !== "undefined" ? window.innerWidth : 1400);

  const debounce = useRef(null);
  const renderSeq = useRef({});  // per-doc latest-wins guard (keyed by doc id) so a slow /render can't clobber a newer one for the SAME doc

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
      if (ui.tabOrient) setTabOrient(ui.tabOrient);
      if (ui.railW) setRailW(ui.railW);
      if (ui.railCollapsed) setRailCollapsed(true);
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
          svg: "", placement: null, keyplanSvg: null,
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
    localStorage.setItem(LS_UI, JSON.stringify({ panelW, collapsed, tabOrient, railW, railCollapsed }));
  }, [panelW, collapsed, tabOrient, railW, railCollapsed]);

  // Track viewport width so vertical tabs can auto-fall back to horizontal when
  // the sidebar + rail would leave the stage too narrow (see effectiveOrient).
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // autosave open docs (slim — geometry stays server-side, re-rendered on load).
  // Debounced: the serialize-all-docs + stringify is deferred to a quiet moment
  // so it doesn't run on every keystroke. A change in the last ~600ms before an
  // abrupt tab close may not persist — acceptable for a convenience autosave.
  useEffect(() => {
    if (!docs.length) return;
    const t = setTimeout(() => {
      const slim = docs.map((d) => ({
        id: d.id, propertyId: d.propertyId, docId: d.docId, fileName: d.fileName,
        savedId: d.savedId, rooms: d.rooms, deletedRooms: d.deletedRooms, ignored: d.ignored,
        meta: d.meta, suggestions: d.suggestions, warnings: d.warnings,
        keyplan: d.keyplan, showHandles: d.showHandles,
        // layer review (small): survives reload so the banner/table persist; the
        // File can't be persisted, so Apply&re-parse disables until a re-upload.
        layerInferred: d.layerInferred, layerReport: d.layerReport,
        layerRoles: d.layerRoles, layerMapUsed: d.layerMapUsed,
      }));
      localStorage.setItem(LS_SESSION, JSON.stringify({ docs: slim, activeId }));
    }, 600);
    return () => clearTimeout(t);
  }, [docs, activeId]);

  // Unified library: every saved sheet across all properties, refreshed on
  // mount and whenever the library tab is opened (and after save/delete/rename).
  const refreshSheets = () => listAllSheets().then(setSheets).catch(() => {});
  useEffect(() => { refreshSheets(); }, []);
  useEffect(() => { if (activeId === "library") refreshSheets(); }, [activeId]);

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
    delete renderSeq.current[id];   // drop the closed doc's per-doc render counter
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
  // File picker entry point. One file keeps the original in-place flow; two or
  // more switches to the batch queue (§11).
  function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (files.length === 1) { handleFile(files[0]); return; }
    let chosen = files;
    if (files.length > BATCH_MAX) {
      chosen = files.slice(0, BATCH_MAX);
      toast(`Batch is limited to ${BATCH_MAX} files — processing the first ${BATCH_MAX}.`, "info");
    }
    runBatch(chosen, defaultProp || (active && active.propertyId) || "");
  }

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
      patchDoc(id, { ...parsedDocFields(d, file.name), fileObj: file });
      setOpenSection("details");
    } catch (e) {
      patchDoc(id, { docId: null, rooms: [], parseError: e.message });
      toast(e.message, "error");
    } finally {
      setParsing(false);
    }
  }

  function setLayerRole(layer, role) {
    if (!active) return;
    patchDoc(active.id, { layerRoles: { ...active.layerRoles, [layer]: role } });
  }

  // Re-parse the CURRENT file with the manually-corrected layer roles (the inline
  // review table). Needs the uploaded File in memory, so it's unavailable after a
  // page reload (the File can't be persisted) — the button disables in that case.
  async function applyLayerRoles() {
    if (!active || !active.fileObj) return;
    const corrected = rolesToLayerMap(active.layerRoles);
    setParsing(true);
    try {
      const d = await parseFile(active.fileObj, active.propertyId || undefined, corrected);
      // Refresh geometry/labels and the applied map; keep the user's edited
      // metadata and the review table. Clear the stale preview so it repaints.
      patchDoc(active.id, {
        docId: d.doc_id,
        rooms: d.labels.map((l) => ({ ...l })),
        deletedRooms: [],
        ignored: d.ignored_text || [],
        suggestions: d.suggestions || {},
        warnings: d.warnings || [],
        layerMapUsed: corrected,
        svg: "", savedId: null, parseError: "",
      });
      toast("Re-parsed with your layer roles.", "success");
    } catch (e) {
      patchDoc(active.id, { parseError: e.message });
      toast(e.message, "error");
    } finally {
      setParsing(false);
    }
  }

  // Parse several files through a fixed-size worker pool. Each success appends a
  // ready editor tab; failures stay in the queue with their reason so one bad
  // file never sinks the rest. Nothing is saved to the library (spec §11).
  function runBatch(files, prop) {
    const items = files.map((f) => ({
      qid: uid(), file: f, fileName: f.name, status: "queued", error: "", docId: null,
    }));
    setQueue(items);
    const setStatus = (qid, patch) =>
      setQueue((q) => q.map((it) => (it.qid === qid ? { ...it, ...patch } : it)));

    let next = 0;
    let firstReady = null;
    const rejected = [];   // file names that failed to parse
    let accepted = 0;
    async function worker() {
      while (next < items.length) {
        const item = items[next++];
        setStatus(item.qid, { status: "parsing" });
        try {
          const d = await parseFile(item.file, prop || undefined);
          const doc = { ...newDoc(prop), ...parsedDocFields(d, item.fileName), fileObj: item.file };
          setDocs((ds) => [...ds, doc]);
          setStatus(item.qid, { status: "ready", docId: doc.id });
          accepted++;
          if (!firstReady) {            // jump to the first finished unit so the user can start reviewing
            firstReady = doc.id;
            setActiveId(doc.id);
            setOpenSection("details");
          }
        } catch (e) {
          setStatus(item.qid, { status: "failed", error: e.message });
          rejected.push(item.fileName);
        }
      }
    }
    // Run BATCH_CONCURRENCY workers sharing the `next` cursor, so the queue never
    // has more than that many parses in flight. Summarize when all have settled:
    // open every valid file, and name the rejected ones so nothing fails silently.
    return Promise.all(
      Array.from({ length: Math.min(BATCH_CONCURRENCY, items.length) }, worker)
    ).then(() => {
      const names = rejected.join(", ");
      if (rejected.length === 0) {
        toast(`Opened all ${accepted} files in tabs.`, "success");
      } else if (accepted === 0) {
        toast(`No files could be opened. Rejected: ${names}`, "error");
      } else {
        toast(`Opened ${accepted} file${accepted === 1 ? "" : "s"}; rejected ${rejected.length}: ${names}`, "info");
      }
    });
  }

  // Shared expired-upload recovery for every /render caller (preview + the two
  // download paths). The uploads cache sweep makes a doc_id go stale after the
  // TTL; the backend then returns 404 "Upload expired"/"not found". When that's
  // the failure, clear docId so the editor stops believing the doc is live and
  // tells the user to re-upload — instead of surfacing a cryptic raw message.
  // Returns true if it handled the error (caller should not surface it further).
  function handleExpiredUpload(d, e) {
    if (!/expired|not found/i.test(e.message)) return false;
    toast("This unit's upload expired — re-upload the DXF.", "error");
    patchDoc(d.id, { docId: null });
    return true;
  }

  async function doRender(save, asNew = false) {
    const d = docs.find((x) => x.id === activeId);
    if (!d || !d.docId) return;
    const mySeq = (renderSeq.current[d.id] = (renderSeq.current[d.id] || 0) + 1);   // claim the latest slot for THIS doc's preview output
    if (save) setSaving(true); else setRendering(true);
    try {
      const res = await renderSheet({
        doc_id: d.docId, property_id: d.propertyId || null,
        metadata: d.meta, rooms: d.rooms, keyplan: d.keyplan || null,
        sheet_id: asNew ? null : (d.savedId || null), save,
      });
      const latest = mySeq === renderSeq.current[d.id];
      patchDoc(d.id, {
        // only the newest render may repaint the preview — an earlier response
        // resolving after a newer one must not overwrite fresher geometry
        ...(latest ? {
          svg: res.svg, placement: res.meta || d.placement,
          keyplanSvg: res.keyplan_svg || null, renderError: "",
        } : {}),
        // a completed save is a committed server action: always record it, even
        // if a newer preview has since superseded the on-screen output
        ...(save && res.sheet_id ? { savedId: res.sheet_id } : {}),
      });
      if (save && res.sheet_id) {
        toast(asNew ? "Saved as a new sheet" :
          d.savedId ? "Changes saved to the library" : "Saved to the library", "success");
        refreshSheets();
      }
    } catch (e) {
      // surface save failures always; suppress errors from a stale preview
      if (!handleExpiredUpload(d, e) && (save || mySeq === renderSeq.current[d.id])) {
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
  function revertLabels() {
    if (!active) return;
    const id = active.id;
    const snapshot = active.rooms.map((r) => ({ x: r.x ?? null, y: r.y ?? null }));
    const hadOverride = snapshot.some((p) => p.x != null || p.y != null);
    patchActive((d) => ({ rooms: d.rooms.map((r) => ({ ...r, x: null, y: null })) }));
    if (!hadOverride) return;   // nothing was moved — nothing to undo
    toast("Reverted label positions", "success", {
      label: "Undo",
      run: () => patchDoc(id, (d) => ({
        rooms: d.rooms.map((r, i) => ({
          ...r, x: snapshot[i]?.x ?? null, y: snapshot[i]?.y ?? null,
        })),
      })),
    });
  }
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
    a.download = `${exportName(active.propertyId, active.meta.title)}.svg`;
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
      a.download = `${exportName(d.propertyId, d.meta.title)}.png`;
      a.click();
    } catch (e) {
      if (!handleExpiredUpload(d, e)) toast(e.message, "error");
    } finally {
      setPngBusy(false);
    }
  }
  // Bare line drawing — no header/footer/watermark. kind: "svg" | "png".
  async function downloadPlanOnly(kind) {
    const d = docs.find((x) => x.id === activeId);
    if (!d || !d.docId) return;
    setPngBusy(true);
    try {
      const res = await renderSheet({
        doc_id: d.docId, property_id: d.propertyId || null,
        metadata: d.meta, rooms: d.rooms, plan_only: true, want_png: kind === "png",
      });
      const a = document.createElement("a");
      if (kind === "png") {
        if (!res.png_b64) throw new Error("No PNG returned by the server.");
        a.href = "data:image/png;base64," + res.png_b64;
      } else {
        const blob = new Blob([res.svg], { type: "image/svg+xml" });
        a.href = URL.createObjectURL(blob);
      }
      a.download = `${exportName(d.propertyId, d.meta.title, "-plan")}.${kind}`;
      a.click();
      if (kind !== "png") URL.revokeObjectURL(a.href);
    } catch (e) {
      if (!handleExpiredUpload(d, e)) toast(e.message, "error");
    } finally {
      setPngBusy(false);
    }
  }

  // ---- library actions -----------------------------------------------------
  async function reopen(s) {
    const prop = s.property_id || propertyId;
    try {
      const cfg = await reopenSheet(prop, s.sheet_id);
      const d = newDoc(cfg.property_id || prop);
      d.docId = cfg.doc_id;
      d.rooms = (cfg.rooms || []).map((r) => ({ ...r }));
      d.meta = cfg.metadata || { title: "", suite: "", sf: "" };
      d.keyplan = cfg.keyplan || null;
      d.savedId = s.sheet_id;       // re-saving overwrites this library entry
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
      await deleteSheet(s.property_id || propertyId, s.sheet_id);
      setSheets((xs) => xs.filter((x) => x.sheet_id !== s.sheet_id));
      toast("Sheet deleted", "success");
    } catch (e) {
      toast(e.message, "error");
    }
  }
  async function renameSheetAction(s, title) {
    const next = (title || "").trim();
    if (!next || next === s.title) return;
    const prev = s.title;
    const prop = s.property_id || propertyId;
    // optimistic; offer an undo back to the previous title
    setSheets((xs) => xs.map((x) => (x.sheet_id === s.sheet_id ? { ...x, title: next } : x)));
    try {
      await renameSheet(prop, s.sheet_id, next);
      toast(`Renamed to "${next}"`, "success", {
        label: "Undo",
        run: async () => {
          setSheets((xs) => xs.map((x) => (x.sheet_id === s.sheet_id ? { ...x, title: prev } : x)));
          try { await renameSheet(prop, s.sheet_id, prev); } catch (e) { toast(e.message, "error"); }
        },
      });
    } catch (e) {
      setSheets((xs) => xs.map((x) => (x.sheet_id === s.sheet_id ? { ...x, title: prev } : x)));
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

  // ---- vertical tab rail resize -------------------------------------------
  // The rail isn't anchored at x=0 (the sidebar sits to its left), so width is
  // tracked as a delta from the drag's start rather than absolute clientX.
  function startRailResize(e) {
    e.preventDefault();
    e.target.setPointerCapture?.(e.pointerId);
    railResize.current = { startX: e.clientX, startW: railW };
    setRailResizing(true);
  }
  function onRailResizeMove(e) {
    if (!railResize.current) return;
    const { startX, startW } = railResize.current;
    setRailW(Math.min(360, Math.max(120, startW + (e.clientX - startX))));
  }
  function endRailResize() { railResize.current = null; setRailResizing(false); }

  // Tabs use the same name the library shows for a saved sheet — the
  // property-prefixed export name (exportName), not the raw upload filename — so
  // a tab and its library card read identically. "Untitled" until a unit title.
  const tabLabel = (d) => (d.meta.title ? exportName(d.propertyId, d.meta.title) : "Untitled");

  // When two open tabs resolve to the same label (e.g. two units both titled
  // "1 BED"), number every member of the colliding group — "name (1)", "name (2)"
  // — so they're tellable apart. A unique label is left untouched.
  function tabLabelUnique(d) {
    const base = tabLabel(d);
    const group = docs.filter((x) => tabLabel(x) === base);
    if (group.length < 2) return base;
    return `${base} (${group.findIndex((x) => x.id === d.id) + 1})`;
  }

  // Export names can still be long; middle-truncate to keep both the property
  // prefix and the unit suffix visible rather than clipping one end.
  function midTruncate(s, max = 36) {
    if (s.length <= max) return s;
    const keep = max - 1;                 // leave room for the ellipsis
    const head = Math.ceil(keep * 0.55);
    return s.slice(0, head) + "…" + s.slice(s.length - (keep - head));
  }

  // Vertical tabs need the sidebar + rail + a readable stage to all fit; when the
  // window is too narrow we render horizontal regardless of the saved preference.
  const railFootprint = railCollapsed ? 28 : railW;
  const verticalFits = winW >= (collapsed ? 28 : panelW) + railFootprint + 520;
  const effectiveOrient = tabOrient === "vertical" && verticalFits ? "vertical" : "horizontal";

  // The tab list (open docs + new-tab + Library). Shared by the horizontal tab
  // strip and the vertical tab rail — only the wrapping element's class differs,
  // so the two layouts diverge via CSS, not markup.
  function renderTabList() {
    return (
      <>
        {docs.map((d) => (
          <span key={d.id}
            className={"tab" + (activeId === d.id ? " active" : "")}
            title={tabLabelUnique(d)}
            onClick={() => setActiveId(d.id)}>
            <span className="tablabel">{midTruncate(tabLabelUnique(d))}</span>
            <span className="tabx" title="Close" onClick={(e) => { e.stopPropagation(); closeTab(d.id); }}>×</span>
          </span>
        ))}
        <button className="tab newtab" title="New floor plan" onClick={newTab}>+</button>
        <span className={"tab library-tab" + (activeId === "library" ? " active" : "")}
          onClick={() => setActiveId("library")}>
          Library{sheets.length ? ` (${sheets.length})` : ""}
        </span>
      </>
    );
  }

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
          {active && active.layerReport && (
            <div className="warn">
              <b>Auto-detected layers.</b> This drawing doesn't use the standard
              Revit layer names, so roles were guessed from the layer names and
              text. Walls → <code>{(active.layerMapUsed?.wall_line || []).join(", ") || "—"}</code>;
              labels → <code>{(active.layerMapUsed?.room_label || []).join(", ") || "—"}</code>.
              <div className="btnrow" style={{ marginTop: 6 }}>
                <button className="linkish" onClick={() => setShowLayerReview((v) => !v)}>
                  {showLayerReview ? "Hide" : `Review ${active.layerReport.length} layers`}
                </button>
                <button className="linkish"
                  onClick={() => { setSeedLayer(rolesToLayerMap(active.layerRoles)); setEditing("new"); }}>
                  Save as a property
                </button>
              </div>
              {showLayerReview && (
                <div style={{ marginTop: 8, maxHeight: 260, overflowY: "auto", fontSize: 12 }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <tbody>
                      {active.layerReport.map((r) => (
                        <tr key={r.layer}>
                          <td title={(r.samples || []).join(" · ")}
                            style={{ padding: "2px 4px", wordBreak: "break-all" }}>{r.layer}</td>
                          <td className="subtle" style={{ padding: "2px 4px", whiteSpace: "nowrap" }}>
                            {r.line_count}L{r.text_count ? ` · ${r.text_count}T` : ""}
                          </td>
                          <td style={{ padding: "2px 4px" }}>
                            <select value={active.layerRoles?.[r.layer] ?? ""}
                              onChange={(e) => setLayerRole(r.layer, e.target.value)}>
                              {LAYER_ROLE_OPTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="btnrow" style={{ marginTop: 6 }}>
                    <button className="btn ghost" disabled={parsing || !active.fileObj}
                      onClick={applyLayerRoles}>
                      {parsing ? "Re-parsing…" : "Apply & re-parse"}
                    </button>
                    {!active.fileObj &&
                      <span className="subtle">Re-upload to change the mapping.</span>}
                  </div>
                </div>
              )}
            </div>
          )}

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
            <h3>
              <span className="num">2</span> Upload floor plan
              <span className="infodot" tabIndex={0}>
                ⓘ
                <span className="infotip" role="tooltip">
                  Each file opens in its own tab for review and exporting when ready.
                </span>
              </span>
            </h3>
            <label className="drop">
              {parsing ? "Parsing…" : (active && active.fileName ? active.fileName : "Select up to 10 DXF files")}
              <input type="file" multiple accept=".dxf,.dwg"
                onChange={(e) => { handleFiles(e.target.files); e.target.value = ""; }} />
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
                initial={active.keyplan}
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

      {effectiveOrient === "vertical" && (
        railCollapsed ? (
          <button className="expandbtn" title="Show tabs" onClick={() => setRailCollapsed(false)}>›</button>
        ) : (
          <>
            <nav className="tabrail" style={{ width: railW, minWidth: railW }}>
              <div className="tabrail-head">
                <button className="collapsebtn" title="Hide tabs" onClick={() => setRailCollapsed(true)}>«</button>
              </div>
              {renderTabList()}
            </nav>
            <div className={"resizer" + (railResizing ? " active" : "")}
              onPointerDown={startRailResize} onPointerMove={onRailResizeMove}
              onPointerUp={endRailResize} onPointerLeave={endRailResize} />
          </>
        )
      )}

      <main className="stage">
        {queue.length > 0 && (() => {
          const ready = queue.filter((q) => q.status === "ready").length;
          const failed = queue.filter((q) => q.status === "failed").length;
          const pending = queue.length - ready - failed;
          return (
            <div className="batchqueue">
              <div className="batchqueue-head">
                <strong>Batch upload — {queue.length} files</strong>
                <span className="subtle" style={{ flex: 1 }}>
                  {ready} ready{failed ? ` · ${failed} failed` : ""}{pending ? ` · ${pending} processing…` : ""}
                </span>
                <button className="chip" onClick={() => setQueue([])}>Dismiss</button>
              </div>
              {queue.map((it) => (
                <div className={"batchqueue-row " + it.status} key={it.qid}>
                  <span className={"qdot " + it.status} />
                  <span className="qname" title={it.fileName}>{it.fileName}</span>
                  {it.status === "queued" && <span className="qstatus subtle">Queued</span>}
                  {it.status === "parsing" && <span className="qstatus subtle">Parsing…</span>}
                  {it.status === "ready" && (
                    <button className="qstatus linkish" onClick={() => setActiveId(it.docId)}>Open tab ↗</button>
                  )}
                  {it.status === "failed" && <span className="qstatus qerr" title={it.error}>{it.error}</span>}
                </div>
              ))}
            </div>
          );
        })()}
        <div className="stagehead">
          <div className="tabbar">
            {effectiveOrient === "horizontal" && (
              <div className="tabs">{renderTabList()}</div>
            )}
            <div className="tabbar-right">
              <button className="btn ghost icon orient-toggle"
                disabled={!verticalFits && tabOrient === "horizontal"}
                onClick={() => setTabOrient(effectiveOrient === "horizontal" ? "vertical" : "horizontal")}
                title={!verticalFits && tabOrient === "horizontal"
                  ? "Window too narrow for vertical tabs"
                  : "Toggle Tab View — " + (effectiveOrient === "horizontal" ? "switch to vertical tabs" : "switch to horizontal tabs")}
                aria-label="Toggle Tab View">
                {effectiveOrient === "horizontal" ? "☰" : "▤"}
              </button>
            {ready && (
              <div className="actions">
                <div className="dropdown split">
                  <button className="btn ember" disabled={saving || rendering || !propertyId}
                    onClick={() => doRender(true)}
                    title={active.savedId ? "Overwrite the saved sheet in the library" : "Save a new sheet to the library"}>
                    {saving ? "Saving…" : active.savedId ? "Save changes" : "Save to library"}
                  </button>
                  {active.savedId && (
                    <button className="btn ember caret" disabled={saving || rendering || !propertyId}
                      title="More save options" onClick={() => setSaveMenuOpen((o) => !o)}>▾</button>
                  )}
                  {saveMenuOpen && (
                    <>
                      <div className="dropdown-backdrop" onClick={() => setSaveMenuOpen(false)} />
                      <div className="menu">
                        <button onClick={() => { setSaveMenuOpen(false); doRender(true); }}>
                          Save changes
                        </button>
                        <button onClick={() => { setSaveMenuOpen(false); doRender(true, true); }}>
                          Save as new copy
                        </button>
                      </div>
                    </>
                  )}
                </div>
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
                        <div className="menu-sep" />
                        <div className="menu-label">Plan only — no branding</div>
                        <button disabled={pngBusy}
                          onClick={() => { setDlOpen(false); downloadPlanOnly("svg"); }}>
                          Plan SVG
                        </button>
                        <button disabled={pngBusy}
                          onClick={() => { setDlOpen(false); downloadPlanOnly("png"); }}>
                          Plan PNG
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
            </div>
          </div>
        </div>

        {activeId === "library" ? (
          <Library sheets={sheets}
            onReopen={reopen} onDelete={removeSheet} onRename={renameSheetAction} />
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
                <button className={"btn ghost" + (active.meta.sold_out ? " active" : "")}
                  onClick={() => patchActive((d) => ({ meta: { ...d.meta, sold_out: !d.meta.sold_out } }))}
                  title="Overlay a centered SOLD OUT stamp on the sheet">
                  {active.meta.sold_out ? "✓ Sold out" : "Sold out"}
                </button>
                <button className={"btn ghost" + (active.meta.wall_style !== "solid" ? " active" : "")}
                  onClick={() => patchActive((d) => ({ meta: { ...d.meta, wall_style: d.meta.wall_style === "solid" ? "skinny" : "solid" } }))}
                  title="Skinny outline walls (default) vs solid poché fill">
                  {active.meta.wall_style !== "solid" ? "✓ Skinny walls" : "Skinny walls"}
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
          seedLayerMap={editing === "new" ? seedLayer : null}
          onClose={() => { setEditing(null); setSeedLayer(null); }}
          onSaved={(saved) => {
            setEditing(null);
            setSeedLayer(null);
            refreshProperties(saved.id);
            toast(`Property "${saved.name || saved.id}" saved`, "success");
            if (active && active.docId) doRender(false);   // refresh preview with new brand
          }}
          onDeleted={(p) => {
            setEditing(null);
            setSeedLayer(null);
            refreshProperties();
            toast(`Property "${p.name || p.id}" deleted`, "success");
          }}
        />
      )}
    </div>
  );
}
