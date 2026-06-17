// Thin wrapper over the backend HTTP API. In dev, Vite proxies /api -> :8000.
const BASE = import.meta.env.VITE_API_BASE || "/api";

async function jget(path) {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
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
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Parse failed");
  return data;
}

export async function renderSheet(payload) {
  const r = await fetch(BASE + "/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Render failed");
  return data;
}

export async function uploadPlate(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(BASE + "/plate", { method: "POST", body: fd });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Plate upload failed");
  return data;
}

export async function extractBrand(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(BASE + "/extract-brand", { method: "POST", body: fd });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Brand extraction failed");
  return data;
}

export async function saveProperty(id, data) {
  const r = await fetch(BASE + `/properties/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const out = await r.json();
  if (!r.ok) throw new Error(out.detail || "Save failed");
  return out;
}

export async function deleteProperty(id) {
  const r = await fetch(BASE + `/properties/${id}`, { method: "DELETE" });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Delete failed");
  return data;
}

export async function listSheets(propertyId) {
  return jget(`/sheets/${propertyId}`);
}

export function sheetUrl(propertyId, sheetId, ext) {
  return `${BASE}/sheets/${propertyId}/${sheetId}.${ext}`;
}

export async function reopenSheet(propertyId, sheetId) {
  const r = await fetch(BASE + `/sheets/${propertyId}/${sheetId}/reopen`, { method: "POST" });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Re-open failed");
  return data;
}

export async function deleteSheet(propertyId, sheetId) {
  const r = await fetch(BASE + `/sheets/${propertyId}/${sheetId}`, { method: "DELETE" });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Delete failed");
  return data;
}
