import React, { useState } from "react";
import { saveProperty, deleteProperty, extractBrand, fontInfo } from "./api.js";

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

// Curated, broadly-installed font stacks so the PNG export (cairo) matches the
// browser preview rather than silently falling back.
const FONT_PRESETS = {
  serif: [
    ["Georgia", "Georgia, 'Times New Roman', serif"],
    ["Times New Roman", "'Times New Roman', Times, serif"],
    ["Garamond", "Garamond, 'Times New Roman', serif"],
    ["Palatino", "'Palatino Linotype', Palatino, serif"],
  ],
  sans: [
    ["Helvetica Neue", "'Helvetica Neue', Helvetica, Arial, sans-serif"],
    ["Arial", "Arial, Helvetica, sans-serif"],
    ["Verdana", "Verdana, Geneva, sans-serif"],
    ["Trebuchet MS", "'Trebuchet MS', Helvetica, sans-serif"],
    ["Tahoma", "Tahoma, Geneva, sans-serif"],
  ],
};

const slug = (s) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

const numFrom = (s) => (String(s || "").match(/\d+/) || [""])[0];

// When opening the editor, seed the brand panel so editing never requires
// re-uploading the brand sheet. Colors: prefer the swatches detected from a
// brand sheet (saved as brand_swatches), else fall back to the property's
// current palette so an existing property always offers its colors to re-pick.
// Fonts: restore the font-name hints detected in the brand PDF (saved as
// brand_fonts) the same way.
function seedBrandStrip(initial) {
  const fonts = initial?.brand_fonts || [];
  if (initial?.brand_swatches?.length)
    return { swatches: initial.brand_swatches, fonts, source: "saved" };
  const seen = new Set();
  const swatches = [];
  for (const hex of Object.values(initial?.palette || {})) {
    const h = String(hex || "").toUpperCase();
    if (h && !seen.has(h)) { seen.add(h); swatches.push({ hex: h }); }
  }
  if (swatches.length) return { swatches, fonts, source: "palette" };
  return fonts.length ? { swatches: [], fonts, source: "saved" } : null;
}

export default function PropertySetup({ initial, seedLayerMap, onClose, onSaved, onDeleted }) {
  const isNew = !initial;
  const [p, setP] = useState(() => ({
    id: initial?.id || "",
    name: initial?.name || "",
    location: initial?.location || (initial ? "" : "KINGSTON, ON"),
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
    fonts: {
      serif: initial?.fonts?.serif || "Georgia, 'Times New Roman', serif",
      sans: initial?.fonts?.sans || "'Helvetica Neue', Helvetica, Arial, sans-serif",
    },
    brand_swatches: initial?.brand_swatches || null,
    brand_fonts: initial?.brand_fonts || null,
    font_faces: initial?.font_faces || null,
    // For a new property seeded from upload auto-detection, pre-fill the layer
    // map with the detected roles so the user just confirms + names it.
    layer_map: initial?.layer_map || seedLayerMap || DEFAULT_LAYER_MAP,
  }));
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState("");
  const [extracting, setExtracting] = useState(false);
  // Seed the swatch strip so editing a property never requires re-uploading the
  // brand file just to re-pick a color — from saved brand colors when present,
  // otherwise from the property's current palette (see seedBrandStrip).
  const [brand, setBrand] = useState(() => seedBrandStrip(initial));
  const [activeRole, setActiveRole] = useState(null);
  // For a new property, auto-derive lockup + watermark from the building number
  // in the name (e.g. "800 PRINCESS" -> "800") until the user edits them.
  const [lockTouched, setLockTouched] = useState(!!initial?.lockup);
  const [wmTouched, setWmTouched] = useState(!!initial?.watermark);
  const [typeMode, setTypeMode] = useState({});   // role -> show the installed-name text box

  const set = (k, v) => setP((o) => ({ ...o, [k]: v }));
  const setPal = (k, v) => setP((o) => ({ ...o, palette: { ...o.palette, [k]: v } }));
  const setFont = (k, v) => setP((o) => ({ ...o, fonts: { ...o.fonts, [k]: v } }));
  const faceFor = (role) => (p.font_faces || []).find((f) => f.role === role) || null;
  // a role uses a typed installed-name font (not a preset, not an embedded file)
  const usesTypedFont = (role, presets) => {
    const cur = p.fonts[role] || "";
    return !faceFor(role) && !!cur && !presets.some(([, s]) => s === cur);
  };
  const showFontWarn = usesTypedFont("serif", FONT_PRESETS.serif)
    || usesTypedFont("sans", FONT_PRESETS.sans);

  // Upload a brand font file: the backend reads its family name and returns it
  // embedded, so the sheet can render it (preview + PNG) without installing it.
  async function onFontFile(role, e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setErr("");
    try {
      const info = await fontInfo(file);   // { family, data, format }
      setP((o) => {
        const faces = (o.font_faces || []).filter((f) => f.role !== role);
        faces.push({ role, family: info.family, data: info.data, format: info.format });
        return { ...o, fonts: { ...o.fonts, [role]: info.family }, font_faces: faces };
      });
    } catch (e2) {
      setErr(e2.message);
    }
  }
  function removeFace(role, presets) {
    setTypeMode((m) => ({ ...m, [role]: false }));
    setP((o) => {
      const faces = (o.font_faces || []).filter((f) => f.role !== role);
      return { ...o, fonts: { ...o.fonts, [role]: presets[0][1] },
               font_faces: faces.length ? faces : null };
    });
  }

  // Upload is the primary path (embeds the font so it renders anywhere). The
  // type-an-installed-name box is tucked behind a small link. Rendered as a
  // plain function so the text input keeps focus across keystrokes.
  const fontField = (role, label, presets) => {
    const cur = p.fonts[role] || "";
    const face = faceFor(role);
    const known = presets.some(([, s]) => s === cur);
    const typing = !face && (typeMode[role] || (!known && !!cur));
    return (
      <div>
        <label>{label}</label>
        {face ? (
          <div className="font-current">
            <span>{face.family} <span className="subtle">· embedded</span></span>
            <button type="button" className="chip" onClick={() => removeFace(role, presets)}>✕</button>
          </div>
        ) : typing ? (
          <input type="text" value={cur} placeholder="Installed font name, e.g. Oswald"
            onChange={(e) => setFont(role, e.target.value)} />
        ) : (
          <select value={known ? cur : presets[0][1]}
            onChange={(e) => setFont(role, e.target.value)}>
            {presets.map(([l, s]) => <option key={s} value={s}>{l}</option>)}
          </select>
        )}
        <div className="wm-upload" style={{ marginTop: 6 }}>
          <label className="btn ghost file-btn">
            {face ? "Replace font…" : "Upload font (.ttf/.otf)…"}
            <input type="file" accept=".ttf,.otf,.ttc" hidden
              onChange={(e) => onFontFile(role, e)} />
          </label>
        </div>
        {!face && (
          <button type="button" className="linkish"
            onClick={() => {
              if (typing) { setTypeMode((m) => ({ ...m, [role]: false })); setFont(role, presets[0][1]); }
              else { setTypeMode((m) => ({ ...m, [role]: true })); setFont(role, ""); }
            }}>
            {typing ? "use a preset instead" : "use a font already installed on this computer"}
          </button>
        )}
      </div>
    );
  };
  function handleName(v) {
    const num = numFrom(v);
    setP((o) => ({
      ...o, name: v,
      ...(lockTouched ? {} : { lockup: num }),
      ...(wmTouched ? {} : { watermark: num }),
    }));
  }
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
      setP((o) => ({ ...o, palette: { ...o.palette, ...res.palette },
                     brand_swatches: res.swatches || o.brand_swatches,
                     brand_fonts: res.fonts?.length ? res.fonts : o.brand_fonts }));
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
                  onChange={(e) => handleName(e.target.value)} placeholder="800 PRINCESS" />
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
                  onChange={(e) => { setLockTouched(true); set("lockup", e.target.value); }}
                  placeholder="800" />
              </div>
              <div>
                <label>Watermark</label>
                <input type="text" value={p.watermark}
                  onChange={(e) => { setWmTouched(true); set("watermark", e.target.value); }}
                  placeholder="800" disabled={!!p.watermark_image} />
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
                  {brand.source === "palette" ? "Saved palette colors" : "Detected colors"} — {activeRole
                    ? <>click one to set <b>{activeRole}</b></>
                    : "click a role field above, then a swatch to assign it"}.
                </p>
                <div className="swatch-strip">
                  {brand.swatches.map((s) => (
                    <button type="button" key={s.hex} className="swatch-chip"
                      title={s.frac != null ? `${s.hex} · ${Math.round(s.frac * 100)}% of image` : s.hex}
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
            <h3>Floor plan fonts</h3>
            <p className="subtle">
              Display face styles the lockup, unit title and watermark; the body
              face is everything else. Upload a .ttf/.otf to embed a brand font
              (e.g. Oswald) so it renders in the preview and the PNG without being
              installed anywhere. Or pick a preset / type an installed font name.
            </p>
            {showFontWarn && (
              <p className="font-warn">
                Be careful with typing font names — machines without the font installed won’t
                render correctly and may fall back to unwanted fonts. Upload the .ttf/.otf to embed it.
              </p>
            )}
            <div className="row">
              {fontField("serif", "Display font", FONT_PRESETS.serif)}
              {fontField("sans", "Body font", FONT_PRESETS.sans)}
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
