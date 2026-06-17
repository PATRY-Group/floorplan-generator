import React, { useState } from "react";
import { saveProperty } from "./api.js";

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

export default function PropertySetup({ initial, onClose, onSaved }) {
  const isNew = !initial;
  const [p, setP] = useState(() => ({
    id: initial?.id || "",
    name: initial?.name || "",
    location: initial?.location || "",
    lockup: initial?.lockup || "",
    watermark: initial?.watermark || "",
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
  const [err, setErr] = useState("");

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
                  onChange={(e) => set("watermark", e.target.value)} placeholder="800" />
              </div>
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
            {PALETTE_ROLES.map(([k, label, use]) => (
              <div className="palrow" key={k}>
                <input type="color" value={pal[k]}
                  onChange={(e) => setPal(k, e.target.value)} />
                <input type="text" value={pal[k]}
                  onChange={(e) => setPal(k, e.target.value)} />
                <span className="palmeta"><b>{label}</b><br />{use}</span>
              </div>
            ))}
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
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn ember" disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save property"}
          </button>
        </div>
      </div>
    </div>
  );
}
