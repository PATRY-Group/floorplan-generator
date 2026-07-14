// Thin wrapper over the backend HTTP API. In dev, Vite proxies /api -> :8000.
const BASE = import.meta.env.VITE_API_BASE || "/api";

// Single response handler: check status BEFORE parsing, and guard the parse so
// a non-JSON error body (a 500 HTML page, a proxy/gateway error, an empty body)
// surfaces a real message instead of throwing a cryptic JSON SyntaxError. The
// server's `detail` is preserved (doRender's expired-upload detection keys off it).
async function handle(r, fallback) {
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.detail) || fallback || r.statusText || "Request failed");
  return data;
}

// Every request gets a timeout so a hung/unreachable backend doesn't leave a
// "Parsing…/Saving…" spinner up forever. A timeout aborts with a TimeoutError so
// it surfaces as a real error, distinct from the caller's own AbortError (the
// live-preview supersede-cancel, which the UI intentionally swallows). A
// caller-supplied signal is chained onto ours so both cancellation paths work.
//
// The default must stay ABOVE the backend's own ceilings for the same request,
// or we'd abort a request the server is still working on: the ODA DWG converter
// runs up to 120s (engine/convert.py), so /parse and the bulk ZIP download get
// LONG_TIMEOUT_MS; plain reads/render/save use the shorter default.
const REQUEST_TIMEOUT_MS = 120000;
const LONG_TIMEOUT_MS = 300000;

async function fetchT(url, opts = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(
    () => ctrl.abort(new DOMException("Request timed out", "TimeoutError")), timeoutMs);
  const outer = opts.signal;
  if (outer) {
    if (outer.aborted) ctrl.abort(outer.reason);
    else outer.addEventListener("abort", () => ctrl.abort(outer.reason), { once: true });
  }
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function jget(path) {
  return handle(await fetchT(BASE + path));
}

export async function getCapabilities() {
  return jget("/capabilities");
}

export async function listProperties() {
  return jget("/properties");
}

export async function parseFile(file, propertyId, layerMap) {
  const fd = new FormData();
  fd.append("file", file);
  if (propertyId) fd.append("property_id", propertyId);
  // layerMap is an optional manual override (the corrected detected mapping):
  // re-parse the same file with the roles the user confirmed.
  if (layerMap) fd.append("layer_map", JSON.stringify(layerMap));
  // DWG uploads run the ODA converter (up to 120s backend), so allow extra time.
  const r = await fetchT(BASE + "/parse", { method: "POST", body: fd }, LONG_TIMEOUT_MS);
  return handle(r, "Parse failed");
}

// `signal` (optional) lets the caller cancel a superseded request — the debounced
// live preview aborts its previous in-flight render when a newer edit fires one.
export async function renderSheet(payload, signal) {
  const r = await fetchT(BASE + "/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // asset_base lets an image-plan live preview reference /planimg/{doc_id} by
    // URL (through this same base) instead of inlining a multi-MB raster.
    body: JSON.stringify({ asset_base: BASE, ...payload }),
    signal,
  });
  return handle(r, "Render failed");
}

// A PDF of an already-finished floor plan (no vector geometry) — rasterized,
// autocropped, and cached server-side the same way parseFile() caches DXF
// geometry, so the returned doc_id works with renderSheet() unchanged.
export async function parsePlanPdf(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetchT(BASE + "/plan-pdf", { method: "POST", body: fd });
  return handle(r, "PDF parse failed");
}

export async function uploadPlate(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetchT(BASE + "/plate", { method: "POST", body: fd });
  return handle(r, "Plate upload failed");
}

export function plateUrl(plateId) {
  return `${BASE}/plate/${plateId}`;
}

export async function extractBrand(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetchT(BASE + "/extract-brand", { method: "POST", body: fd });
  return handle(r, "Brand extraction failed");
}

export async function fontInfo(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetchT(BASE + "/font-info", { method: "POST", body: fd });
  return handle(r, "Font read failed");
}

export async function saveProperty(id, data) {
  const r = await fetchT(BASE + `/properties/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return handle(r, "Save failed");
}

export async function deleteProperty(id) {
  const r = await fetchT(BASE + `/properties/${id}`, { method: "DELETE" });
  return handle(r, "Delete failed");
}

export async function listAllSheets() {
  return jget("/sheets");
}

export async function renameSheet(propertyId, sheetId, title) {
  const r = await fetchT(BASE + `/sheets/${propertyId}/${sheetId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return handle(r, "Rename failed");
}

export function sheetUrl(propertyId, sheetId, ext) {
  return `${BASE}/sheets/${propertyId}/${sheetId}.${ext}`;
}

// Small (~600px) library-grid thumbnail — a fraction of the full sheet PNG's
// bytes. Backend builds it on save and lazily for older sheets (GET .thumb.png).
export function sheetThumbUrl(propertyId, sheetId) {
  return `${BASE}/sheets/${propertyId}/${sheetId}.thumb.png`;
}

// Batch download: POST selected sheets + formats, get back a ZIP blob. Has its
// own error path because handle() assumes a JSON body; this response is binary.
export async function downloadSheets(items, formats, planOnly = false) {
  const r = await fetchT(BASE + "/sheets/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, formats, plan_only: planOnly }),
  }, LONG_TIMEOUT_MS);   // re-renders every selected sheet into one ZIP — can be slow
  if (!r.ok) {
    const data = await r.json().catch(() => null);
    throw new Error((data && data.detail) || "Download failed");
  }
  return r.blob();
}

export async function reopenSheet(propertyId, sheetId) {
  const r = await fetchT(BASE + `/sheets/${propertyId}/${sheetId}/reopen`, { method: "POST" });
  return handle(r, "Re-open failed");
}

export async function deleteSheet(propertyId, sheetId) {
  const r = await fetchT(BASE + `/sheets/${propertyId}/${sheetId}`, { method: "DELETE" });
  return handle(r, "Delete failed");
}
