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

async function jget(path) {
  return handle(await fetch(BASE + path));
}

export async function getCapabilities() {
  return jget("/capabilities");
}

export async function listProperties() {
  return jget("/properties");
}

export async function parseFile(file, propertyId) {
  const fd = new FormData();
  fd.append("file", file);
  if (propertyId) fd.append("property_id", propertyId);
  const r = await fetch(BASE + "/parse", { method: "POST", body: fd });
  return handle(r, "Parse failed");
}

export async function renderSheet(payload) {
  const r = await fetch(BASE + "/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return handle(r, "Render failed");
}

export async function uploadPlate(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(BASE + "/plate", { method: "POST", body: fd });
  return handle(r, "Plate upload failed");
}

export async function tracePlate(plateId, seal, palette) {
  const r = await fetch(BASE + "/plate/trace", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plate_id: plateId, seal, palette }),
  });
  return handle(r, "Trace failed");
}

export async function extractBrand(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(BASE + "/extract-brand", { method: "POST", body: fd });
  return handle(r, "Brand extraction failed");
}

export async function fontInfo(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(BASE + "/font-info", { method: "POST", body: fd });
  return handle(r, "Font read failed");
}

export async function saveProperty(id, data) {
  const r = await fetch(BASE + `/properties/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return handle(r, "Save failed");
}

export async function deleteProperty(id) {
  const r = await fetch(BASE + `/properties/${id}`, { method: "DELETE" });
  return handle(r, "Delete failed");
}

export async function listSheets(propertyId) {
  return jget(`/sheets/${propertyId}`);
}

export function sheetUrl(propertyId, sheetId, ext) {
  return `${BASE}/sheets/${propertyId}/${sheetId}.${ext}`;
}

export async function reopenSheet(propertyId, sheetId) {
  const r = await fetch(BASE + `/sheets/${propertyId}/${sheetId}/reopen`, { method: "POST" });
  return handle(r, "Re-open failed");
}

export async function deleteSheet(propertyId, sheetId) {
  const r = await fetch(BASE + `/sheets/${propertyId}/${sheetId}`, { method: "DELETE" });
  return handle(r, "Delete failed");
}
