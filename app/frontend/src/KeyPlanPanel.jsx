import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { uploadPlate, plateUrl } from "./api.js";
import { toast } from "./toast.js";

const clamp01 = (v) => Math.max(0, Math.min(1, v));

// Turn a #rrggbb into an rgba() with the given alpha, for the translucent box
// fill. Falls back to the ember accent if the palette has no usable hex.
function rgba(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
  const n = m ? parseInt(m[1], 16) : 0xc17f3a;
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

/**
 * Optional key-plan controls. Two intake modes (spec §6):
 *
 *   - "highlight" -> upload/paste a plain floor-plate image, then drag a
 *                    rectangle over this unit. The backend shades that cell in
 *                    the brand accent. Schematic only — approximate is fine.
 *   - "upload"    -> upload/paste a *finished* key-plan image (the unit already
 *                    marked on it). We just crop and embed it as-is; no box.
 *
 * Both modes trim surrounding whitespace on intake, so the preview shown here is
 * the same cropped image that lands on the sheet — which is why a highlight box
 * (normalized to that cropped preview) maps 1:1 onto the embedded image. The
 * magnifier opens a large sharp-corner modal that draws the SAME box at a bigger
 * scale, for placing it precisely on a busy plan.
 *
 * Calls onChange(keyplanConfig | null) whenever the config is complete: an image
 * is enough in upload mode; highlight mode also needs a box.
 *
 * `initial` is a previously-saved keyplan config (from re-open / session
 * restore). The panel seeds its state from it so the UI matches what will
 * actually render — otherwise the first interaction would emit a blank config
 * and wipe the restored key plan. The mode is inferred from a saved box (which
 * also absorbs the retired mode:"traced"/"raw" configs). The panel is keyed by
 * doc id upstream, so it remounts (and re-seeds) per tab.
 */
export default function KeyPlanPanel({ onChange, initial, palette }) {
  // Match the rendered unit box to the property's brand accent (same color the
  // backend fills the cell with), so the picker preview is WYSIWYG.
  const accent = (palette && palette.accent) || "#C17F3A";
  const [on, setOn] = useState(!!initial);
  const [mode, setMode] = useState(              // "highlight" | "upload"
    initial?.box ? "highlight" : (initial?.mode === "highlight" ? "highlight" : (initial ? "upload" : "highlight")));
  const [plate, setPlate] = useState(            // {plate_id, url}
    initial?.plate_id ? { plate_id: initial.plate_id, url: plateUrl(initial.plate_id) } : null);
  const [box, setBox] = useState(initial?.box || null);   // [fx, fy, fw, fh]
  const [floor, setFloor] = useState(initial?.floor_label || "");
  const [placement, setPlacement] = useState(initial?.placement || "footer");
  const [rotate, setRotate] = useState(initial?.rotate || 0);   // 0/90/180/270 CW
  const [kpScale, setKpScale] = useState(initial?.scale || 1);  // footer plate size, 0.5–1.6
  const [drag, setDrag] = useState(null);
  const [busy, setBusy] = useState(false);
  const [zoom, setZoom] = useState(false);       // sharp-corner "draw big" modal

  function emit(next) {
    const s = { on, mode, plate, box, floor, placement, rotate, scale: kpScale, ...next };
    // A plate alone is a valid, persistable key plan. The highlight box is
    // OPTIONAL (added once the user draws it) — requiring it here meant uploading
    // a plate in highlight mode (which clears the box) emitted null and wiped the
    // saved key plan, losing the plate association on the next tab remount.
    const complete = s.on && s.plate;
    if (complete) {
      onChange({
        plate_id: s.plate.plate_id,
        floor_label: s.floor,
        placement: s.placement,
        mode: s.mode,
        rotate: s.rotate || 0,   // key-plan image rotation, degrees clockwise
        scale: s.scale || 1,     // footer plate size multiplier
        // Only the highlight mode carries a unit box; upload embeds as-is.
        ...(s.mode === "highlight" && s.box ? { box: s.box } : {}),
      });
    } else {
      onChange(null);
    }
  }

  function toggle(v) {
    setOn(v);
    emit({ on: v });
  }

  function pickMode(m) {
    setMode(m);
    setZoom(false);          // the modal is highlight-only; don't leave it armed
    emit({ mode: m });
  }

  // Paste an image from the clipboard (Ctrl/Cmd+V) while the panel is open. The
  // listener is armed only on `on`, so it would otherwise close over a stale
  // `choose` (and the state its emit reads — mode/floor/placement) from when the
  // panel last opened. Call the latest via a ref so a paste after changing mode
  // or floor emits the CURRENT config, not the config as it was at open time.
  const chooseRef = useRef(choose);
  chooseRef.current = choose;
  useEffect(() => {
    if (!on) return;
    function onPaste(e) {
      const item = [...(e.clipboardData?.items || [])]
        .find((it) => it.type.startsWith("image/"));
      if (!item) return;
      const blob = item.getAsFile();
      if (!blob) return;
      e.preventDefault();
      const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
      chooseRef.current(new File([blob], `pasted.${ext}`, { type: blob.type }));
    }
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [on]);

  // Escape closes the zoom modal.
  useEffect(() => {
    if (!zoom) return;
    const onKey = (e) => { if (e.key === "Escape") setZoom(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  async function choose(file) {
    if (!file) return;
    setBusy(true);
    try {
      const r = await uploadPlate(file);
      // The backend cropped the image; repaint from the served (cropped) copy so
      // the preview — and any box drawn over it — matches the embedded image.
      const p = { plate_id: r.plate_id, url: plateUrl(r.plate_id) };
      setPlate(p);
      setBox(null);                     // a new image invalidates the old box
      emit({ plate: p, box: null });
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  // --- box-drag picker (highlight mode) ------------------------------------ //
  // frac() is relative to the surface the pointer is on (e.currentTarget), so
  // the same handlers drive both the inline preview and the big zoom modal —
  // the normalized box comes out identical at either scale.
  function frac(e) {
    const r = e.currentTarget.getBoundingClientRect();
    return [clamp01((e.clientX - r.left) / r.width),
            clamp01((e.clientY - r.top) / r.height)];
  }
  function down(e) {
    e.preventDefault();
    const [x, y] = frac(e);
    setDrag({ x0: x, y0: y, x1: x, y1: y });
  }
  function move(e) {
    if (!drag) return;
    const [x, y] = frac(e);
    setDrag((d) => ({ ...d, x1: x, y1: y }));
  }
  function up(opts) {
    if (!drag) return;
    const fx = Math.min(drag.x0, drag.x1), fy = Math.min(drag.y0, drag.y1);
    const fw = Math.abs(drag.x1 - drag.x0), fh = Math.abs(drag.y1 - drag.y0);
    setDrag(null);
    if (fw > 0.01 && fh > 0.01) {
      const b = [fx, fy, fw, fh];
      setBox(b);
      emit({ box: b });
    } else if (opts && opts.tapOpensZoom) {
      setZoom(true);          // a tap (no real drag) on the mini preview -> zoom in
    }
  }

  const live = drag
    ? [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1),
       Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)]
    : box;

  const highlight = mode === "highlight";

  // The draggable image surface + live unit box. Reused inline and in the modal.
  // `inline` adds the tap-to-zoom behavior and the hover affordance (dim + 🔍)
  // that signals the mini preview can be clicked to open the big picker.
  const surface = (extraClass, inline) => (
    <div
      className={"platepick draw" + (extraClass ? " " + extraClass : "")}
      title={inline ? "Drag to mark the unit, or click to zoom in" : undefined}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={() => up({ tapOpensZoom: inline })}
      onPointerLeave={() => up({})}
    >
      <img src={plate.url} alt="floor plate" draggable={false}
        style={{ background: "#F7F3ED" }} />
      {live && (
        <div className="platebox" style={{
          left: `${live[0] * 100}%`, top: `${live[1] * 100}%`,
          width: `${live[2] * 100}%`, height: `${live[3] * 100}%`,
          background: rgba(accent, 0.55), borderColor: accent,   // matches the rendered fill-opacity
        }} />
      )}
      {inline && !drag && (
        <div className="kp-hover" aria-hidden="true">
          <span className="zoomglyph">🔍</span>
        </div>
      )}
    </div>
  );

  return (
    <div className="step">
      <label className="toggle" style={{ marginBottom: on ? 10 : 0 }}>
        <input type="checkbox" checked={on} onChange={(e) => toggle(e.target.checked)} />
        Add a key plan to this sheet
      </label>

      {on && (
        <>
          <label>Key-plan source</label>
          <div className="btnrow">
            {[["highlight", "Manual"], ["upload", "Upload"]].map(([m, lbl]) => (
              <button key={m}
                className={"btn " + (mode === m ? "ember" : "ghost")}
                onClick={() => pickMode(m)}>
                {lbl}
              </button>
            ))}
          </div>

          <p className="subtle">
            {highlight
              ? "Upload or paste a floor-plate image, then drag a box over this unit. Schematic only — approximate is fine."
              : "Upload or paste a finished key-plan image (with this unit already marked). We'll trim the whitespace and drop it in as reference."}
          </p>

          <label className="drop small">
            {busy ? "Uploading…" : (plate ? "Replace image" : "Choose or paste an image (Ctrl+V)")}
            <input type="file" accept="image/*" onChange={(e) => { choose(e.target.files[0]); e.target.value = ""; }} />
          </label>

          {plate && highlight && (
            <>
              <label>Unit location</label>
              {surface(null, true)}
              {!box && (
                <p className="subtle">Drag a rectangle over the unit, or click the image to zoom in for detail.</p>
              )}
            </>
          )}

          {plate && !highlight && (
            <>
              <label>Preview</label>
              <div className="platepick">
                <img src={plate.url} alt="key plan" draggable={false}
                  style={{ background: "#F7F3ED", transform: `rotate(${rotate}deg)` }} />
              </div>
            </>
          )}

          {plate && (
            <div className="kp-controls">
              <button type="button" className="btn ghost"
                onClick={() => { const r = (rotate + 90) % 360; setRotate(r); emit({ rotate: r }); }}
                title="Rotate the key-plan image 90° clockwise">
                ⟳ Rotate 90°
              </button>
              <div className="kp-size">
                <button type="button" className="btn ghost" title="Smaller"
                  onClick={() => { const v = Math.max(0.5, Math.round((kpScale - 0.1) * 10) / 10); setKpScale(v); emit({ scale: v }); }}>−</button>
                <span className="kp-pct">{Math.round(kpScale * 100)}%</span>
                <button type="button" className="btn ghost" title="Bigger"
                  onClick={() => { const v = Math.min(1.6, Math.round((kpScale + 0.1) * 10) / 10); setKpScale(v); emit({ scale: v }); }}>+</button>
              </div>
            </div>
          )}

          <label>Floor label</label>
          <input type="text" value={floor}
            onChange={(e) => { setFloor(e.target.value); emit({ floor: e.target.value }); }}
            placeholder="SECOND FLOOR" />

          <label>Placement</label>
          <div className="btnrow">
            {["footer", "standalone"].map((p) => (
              <button key={p}
                className={"btn " + (placement === p ? "ember" : "ghost")}
                onClick={() => { setPlacement(p); emit({ placement: p }); }}>
                {p === "footer" ? "Footer mini-plate" : "Standalone sheet"}
              </button>
            ))}
          </div>
        </>
      )}

      {/* Portal to <body> so the modal escapes the sidebar's stacking context —
          otherwise the editor tab rail / toolbar paint over it. */}
      {zoom && plate && highlight && createPortal(
        <div className="kp-zoom-backdrop" onClick={() => setZoom(false)}>
          <div className="kp-zoom-modal" onClick={(e) => e.stopPropagation()}>
            <div className="kp-zoom-head">
              <span>Drag over the unit — approximate is fine. Esc or Done to close.</span>
              <button type="button" className="btn ember" onClick={() => setZoom(false)}>Done</button>
            </div>
            <div className="kp-zoom-body">
              {surface("kp-zoom-surface", false)}
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
