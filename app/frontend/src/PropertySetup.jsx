import React, { useState } from "react";
import { saveProperty, deleteProperty, extractBrand } from "./api.js";

const DEFAULT_LAYER_MAP = {
  wall_line: ["A-WALL", "I-WALL"],
  wall_fill: ["A-WALL-PATT"],
  door: ["A-DOOR", "A-DOOR-FRAM"],
  glazing: ["A-GLAZ"],
  dashed: ["A-DETL-HDLN", "A-FLOR-OVHD"],
  room_label: ["G-ANNO-TEXT"],
  drop: ["A-AREA-IDEN", "S-COLS-SYMB", "S-STRS", "S-STRS-MBND"],
  floor_hatch: ["A-FLOR"],
};

const LAYER_ROLES = [
  ["wall_line", "Wall outline"],
  ["wall_fill", "Wall fill (poché)"],
  ["door", "Doors"],
  ["glazing", "Glazing"],
  ["dashed", "Overhead / dashed"],
  ["room_label", "Room-label text"],
  ["drop", "Drop (tags, columns, stairs)"],
  ["floor_hatch", "Floor finish hatch (dropped)"],
];

const PALETTE_ROLES = [
  ["dark", "Dark / primary", "bands, walls, text"],
  ["accent", "Accent", "lockup, watermark, underlines"],
  ["mid", "Mid / secondary", "text on dark bands"],
  ["light", "Light / background", "page bg + label halos"],
];

const slug = (s) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

export default function PropertySetup({ initial, onClose, onSaved, onDeleted }) {
  const isNew = !initial;
  const [p, setP] = useState(() => ({
    id: initial?.id || "",
    name: initial?.name || "",
    location: initial?.location || "",
    lockup: initial?.lockup || "",
    watermark: initial?.watermark || "",
    watermark_image: initial?.watermark_image || null,
    footer_address: initial?.footer_address || "",
    header_right: initial?.header_right || "FLOOR PLAN",
    disclaimer:
      initial?.disclaimer ||
      "FOR ILLUSTRATIVE PURPOSES ONLY. DIMENSIONS ARE APPROXIMATE AND SUBJECT TO CHANGE.",
    palette: {
      dark: initial?.palette?.dark || "#2B1F14",
      accent: initial?.palette?.accent || "#C17F3A",
      mid: initial?.palette?.mid || "#E8D9C0",
      light: initial?.palette?.light || "#F7F3ED",
    },
    fonts: initial?.fonts || null,
    layer_map: initial?.layer_map || DEFAULT_LAYER_MAP,
  }));
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState("");
  const [extracting, setExtracting] = useState(false);
  const [brand, setBrand] = useState(null);     // {swatches, fonts, source}
  const [activeRole, setActiveRole] = useState(null);

  const set = (k, v) => setP((o) => ({ ...o, [k]: v }));
  const setPal = (k, v) => setP((o) => ({ ...o, palette: { ...o.palette, [k]: v } }));
  const setLayers = (role, csv) =>
    setP((o) => ({
      ...o,
      layer_map: {
        ...o.layer_map,
        [role]: csv.split(",").map((s) => s.trim()).filter(Boolean),
      },
    }));

  // Read a watermark image, downscaling to <=600px so the property JSON stays
  // small and the renderer's <image> can't blow up cairo on a huge bitmap.
  function onWatermarkFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const max = 600;
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const w = Math.round(img.width * scale), h = Math.round(img.height * scale);
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      c.getContext("2d").drawImage(img, 0, 0, w, h);
      set("watermark_image", c.toDataURL("image/png"));
      URL.revokeObjectURL(url);
    };
    img.onerror = () => { setErr("Couldn't read that image."); URL.revokeObjectURL(url); };
    img.src = url;
  }

  async function onBrandFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";                  // allow re-picking the same file
    if (!file) return;
    setExtracting(true);
    setErr("");
    try {
      const res = await extractBrand(file);
      setBrand(res);
      setP((o) => ({ ...o, palette: { ...o.palette, ...res.palette } }));
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setExtracting(false);
    }
  }

  async function save() {
    const id = isNew ? slug(p.id || p.name) : p.id;
    if (!id) { setErr("Give the property a name or id."); return; }
    setSaving(true);
    setErr("");
    try {
      const saved = await saveProperty(id, { ...p, id });
      onSaved(saved);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!window.confirm(
      `Delete property "${p.name || p.id}"? Its saved sheets are not removed, ` +
      `but the brand + layer map will be gone. This can't be undone.`)) return;
    setDeleting(true);
    setErr("");
    try {
      await deleteProperty(p.id);
      onDeleted(p);
    } catch (e) {
      setErr(e.message);
      setDeleting(false);
    }
  }

  const pal = p.palette;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>{isNew ? "New property" : `Edit ${p.name || p.id}`}</h2>
          <button className="chip" onClick={onClose}>✕</button>
        </div>

        {err && <div className="error">{err}</div>}

        <div className="modal-body">
          <section>
            <h3>Identity</h3>
            {isNew && (
              <>
                <label>Property id (slug)</label>
                <input type="text" value={p.id}
                  onChange={(e) => set("id", e.target.value)}
                  placeholder={slug(p.name) || "800-princess"} />
              </>
            )}
            <div className="row">
              <div>
                <label>Name</label>
                <input type="text" value={p.name}
                  onChange={(e) => set("name", e.target.value)} placeholder="PRINCESS" />
              </div>
              <div>
                <label>Location</label>
                <input type="text" value={p.location}
                  onChange={(e) => set("location", e.target.value)} placeholder="KINGSTON · ON" />
              </div>
            </div>
            <div className="row">
              <div>
                <label>Header lockup</label>
                <input type="text" value={p.lockup}
                  onChange={(e) => set("lockup", e.target.value)} placeholder="800" />
              </div>
              <div>
                <label>Watermark</label>
                <input type="text" value={p.watermark}
                  onChange={(e) => set("watermark", e.target.value)} placeholder="800"
                  disabled={!!p.watermark_image} />
              </div>
            </div>
            <label>Watermark image (optional — overrides the text watermark)</label>
            <div className="wm-upload">
              <label className="btn ghost file-btn">
                {p.watermark_image ? "Replace image…" : "Upload image…"}
                <input type="file" accept="image/*" hidden onChange={onWatermarkFile} />
              </label>
              {p.watermark_image && (
                <>
                  <img className="wm-preview" src={p.watermark_image} alt="watermark" />
                  <button type="button" className="chip" onClick={() => set("watermark_image", null)}>
                    Remove
                  </button>
                </>
              )}
            </div>
            <label>Footer address</label>
            <input type="text" value={p.footer_address}
              onChange={(e) => set("footer_address", e.target.value)}
              placeholder="800 PRINCESS ST · KINGSTON, ON" />
            <label>Disclaimer</label>
            <input type="text" value={p.disclaimer}
              onChange={(e) => set("disclaimer", e.target.value)} />
          </section>

          <section>
            <h3>Brand palette</h3>
            <div className="brand-extract">
              <label className="btn ghost file-btn">
                {extracting ? "Reading…" : "Auto-fill from brand file…"}
                <input type="file" accept=".pdf,image/*" hidden
                  disabled={extracting} onChange={onBrandFile} />
              </label>
              <span className="subtle">
                Upload a brand PDF/image to auto-read colors. Confirm or re-pick below.
              </span>
            </div>
            {PALETTE_ROLES.map(([k, label, use]) => (
              <div className={"palrow" + (activeRole === k ? " active" : "")} key={k}>
                <input type="color" value={pal[k]}
                  onFocus={() => setActiveRole(k)}
                  onChange={(e) => setPal(k, e.target.value)} />
                <input type="text" value={pal[k]}
                  onFocus={() => setActiveRole(k)}
                  onChange={(e) => setPal(k, e.target.value)} />
                <span className="palmeta"><b>{label}</b><br />{use}</span>
              </div>
            ))}
            {brand && brand.swatches?.length > 0 && (
              <div className="brand-found">
                <p className="subtle">
                  Detected colors — {activeRole
                    ? <>click one to set <b>{activeRole}</b></>
                    : "click a role field above, then a swatch to assign it"}.
                </p>
                <div className="swatch-strip">
                  {brand.swatches.map((s) => (
                    <button type="button" key={s.hex} className="swatch-chip"
                      title={`${s.hex} · ${Math.round(s.frac * 100)}% of image`}
                      disabled={!activeRole}
                      onClick={() => activeRole && setPal(activeRole, s.hex)}>
                      <span className="sw-dot" style={{ background: s.hex }} />
                      {s.hex}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {brand && brand.fonts?.length > 0 && (
              <div className="brand-found">
                <p className="subtle">
                  Fonts embedded in the PDF (for reference — set the CSS font
                  stacks manually; these aren't applied automatically):
                </p>
                <div className="font-hints">
                  {brand.fonts.map((f) => (
                    <code className="font-hint" key={f}>{f}</code>
                  ))}
                </div>
              </div>
            )}
            <div className="swatch">
              <div className="sw-head" style={{ background: pal.dark }}>
                <span style={{ color: pal.accent, fontFamily: "Georgia, serif", fontWeight: "bold" }}>
                  {p.lockup || "—"}
                </span>
                <span style={{ color: "#fff", letterSpacing: 3 }}>{p.name || "NAME"}</span>
                <span style={{ color: pal.mid, fontSize: 9 }}>{p.location}</span>
              </div>
              <div className="sw-body" style={{ background: pal.light }}>
                <span style={{ color: pal.dark, opacity: 0.5 }}>page / halo</span>
              </div>
              <div className="sw-foot" style={{ background: pal.dark }}>
                <span style={{ color: "#fff", fontFamily: "Georgia, serif" }}>UNIT</span>
                <span style={{ background: pal.accent, height: 3, width: 28, display: "inline-block" }} />
              </div>
            </div>
          </section>

          <section>
            <h3>CAD layer map</h3>
            <p className="subtle">
              Which layer names in the DXF map to each role. Comma-separated.
              Defaults match the Revit export scheme.
            </p>
            {LAYER_ROLES.map(([role, label]) => (
              <div key={role}>
                <label>{label}</label>
                <input type="text"
                  value={(p.layer_map[role] || []).join(", ")}
                  onChange={(e) => setLayers(role, e.target.value)} />
              </div>
            ))}
          </section>
        </div>

        <div className="modal-foot">
          {!isNew && (
            <button className="btn danger foot-left" disabled={deleting || saving}
              onClick={remove}>
              {deleting ? "Deleting…" : "Delete property"}
            </button>
          )}
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn ember" disabled={saving || deleting} onClick={save}>
            {saving ? "Saving…" : "Save property"}
          </button>
        </div>
      </div>
    </div>
  );
}
