import React, { useEffect, useRef, useState } from "react";
import { uploadPlate, tracePlate } from "./api.js";

const clamp01 = (v) => Math.max(0, Math.min(1, v));

/**
 * Optional key-plan controls: upload a floor-plate screenshot, drag a box over
 * the unit's location, pick a floor label and footer/standalone placement.
 *
 * Two looks for the plate:
 *   - "traced"  -> the app auto-traces the screenshot into a clean filled
 *                  footprint silhouette (the "basic key plan" look). A seal-
 *                  strength slider tunes how aggressively wall gaps are closed.
 *   - "raw"     -> the original screenshot embedded lightened (fallback when a
 *                  busy/odd plate won't trace cleanly).
 * The box-placement picker always shows the RAW screenshot — you need the
 * interior walls to find the unit — and the box fraction maps onto either look.
 *
 * Calls onChange(keyplanConfig | null) whenever the config is complete.
 */
export default function KeyPlanPanel({ onChange, palette }) {
  const [on, setOn] = useState(false);
  const [plate, setPlate] = useState(null);   // {plate_id, url}
  const [box, setBox] = useState(null);        // [fx, fy, fw, fh]
  const [floor, setFloor] = useState("");
  const [placement, setPlacement] = useState("footer");
  const [mode, setMode] = useState("traced");  // "traced" | "raw"
  const [seal, setSeal] = useState(35);
  const [trace, setTrace] = useState(null);    // {preview, coverage}
  const [tracing, setTracing] = useState(false);
  const [drag, setDrag] = useState(null);
  const [busy, setBusy] = useState(false);
  const imgRef = useRef(null);

  function emit(next) {
    const s = { on, plate, box, floor, placement, mode, seal, ...next };
    if (s.on && s.plate && s.box) {
      onChange({
        plate_id: s.plate.plate_id,
        box: s.box,
        floor_label: s.floor,
        placement: s.placement,
        north_deg: 0,
        mode: s.mode,
        seal: s.seal,
      });
    } else {
      onChange(null);
    }
  }

  function toggle(v) {
    setOn(v);
    emit({ on: v });
  }

  // Paste an image from the clipboard (Ctrl/Cmd+V) while the panel is open.
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
      choose(new File([blob], `pasted.${ext}`, { type: blob.type }));
    }
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [on]);

  // Auto-trace the plate into a footprint silhouette (debounced for the slider).
  useEffect(() => {
    if (!on || !plate || mode !== "traced") { setTrace(null); return; }
    let cancelled = false;
    setTracing(true);
    const t = setTimeout(async () => {
      try {
        const r = await tracePlate(plate.plate_id, seal, palette);
        if (!cancelled) setTrace(r);
      } catch {
        if (!cancelled) setTrace(null);
      } finally {
        if (!cancelled) setTracing(false);
      }
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [on, plate, mode, seal, palette]);

  async function choose(file) {
    if (!file) return;
    setBusy(true);
    const url = URL.createObjectURL(file);
    try {
      const r = await uploadPlate(file);
      const p = { plate_id: r.plate_id, url };
      setPlate(p);
      emit({ plate: p });
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  function frac(e) {
    const r = imgRef.current.getBoundingClientRect();
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
  function up() {
    if (!drag) return;
    const fx = Math.min(drag.x0, drag.x1), fy = Math.min(drag.y0, drag.y1);
    const fw = Math.abs(drag.x1 - drag.x0), fh = Math.abs(drag.y1 - drag.y0);
    setDrag(null);
    if (fw > 0.01 && fh > 0.01) {
      const b = [fx, fy, fw, fh];
      setBox(b);
      emit({ box: b });
    }
  }

  const live = drag
    ? [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1),
       Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)]
    : box;

  // A coverage well outside this band means the trace missed (caught only walls)
  // or over-sealed (bridged into neighbours) — nudge the user to adjust/fallback.
  const cov = trace && typeof trace.coverage === "number" ? trace.coverage : null;
  const covWarn = cov !== null && (cov < 0.08 || cov > 0.92);

  return (
    <div className="step">
      <label className="toggle" style={{ marginBottom: on ? 10 : 0 }}>
        <input type="checkbox" checked={on} onChange={(e) => toggle(e.target.checked)} />
        Add a key plan to this sheet
      </label>

      {on && (
        <>
          <p className="subtle" style={{ marginTop: 0 }}>
            Upload or paste a floor-plate screenshot, then drag a box over this
            unit. Schematic only — approximate is fine.
          </p>
          <label className="drop small">
            {busy ? "Uploading…" : (plate ? "Replace plate image" : "Choose or paste an image (Ctrl+V)")}
            <input type="file" accept="image/*" onChange={(e) => choose(e.target.files[0])} />
          </label>

          {plate && (
            <>
              <label>Look</label>
              <div className="btnrow">
                {[["traced", "Auto-traced plan"], ["raw", "Screenshot"]].map(([m, lbl]) => (
                  <button key={m}
                    className={"btn " + (mode === m ? "ember" : "ghost")}
                    onClick={() => { setMode(m); emit({ mode: m }); }}>
                    {lbl}
                  </button>
                ))}
              </div>

              {mode === "traced" && (
                <>
                  <label>
                    Seal strength {tracing ? "· tracing…" : ""}
                  </label>
                  <input type="range" min="7" max="61" step="2" value={seal}
                    style={{ width: "100%" }}
                    onChange={(e) => { const v = +e.target.value; setSeal(v); emit({ seal: v }); }} />
                  <p className="subtle" style={{ marginTop: 2 }}>
                    Lower keeps detail; higher closes wall gaps so the floor fills
                    in. {trace && trace.preview && (
                      <span>If the shape looks wrong, adjust this or switch to
                      Screenshot.</span>
                    )}
                  </p>
                  {trace && trace.preview && (
                    <div className="platepick" style={{ marginTop: 6 }}>
                      <img src={trace.preview} alt="traced footprint"
                        draggable={false}
                        style={{ background: "#F7F3ED" }} />
                    </div>
                  )}
                  {covWarn && (
                    <p className="subtle" style={{ color: "#b4571f" }}>
                      The auto-trace looks off on this plate — try the slider, or
                      switch to Screenshot.
                    </p>
                  )}
                </>
              )}
            </>
          )}

          {plate && (
            <>
              <label>Unit location</label>
              <div
                className="platepick"
                ref={imgRef}
                onPointerDown={down}
                onPointerMove={move}
                onPointerUp={up}
                onPointerLeave={up}
              >
                <img src={plate.url} alt="floor plate" draggable={false} />
                {live && (
                  <div className="platebox" style={{
                    left: `${live[0] * 100}%`, top: `${live[1] * 100}%`,
                    width: `${live[2] * 100}%`, height: `${live[3] * 100}%`,
                  }} />
                )}
              </div>
            </>
          )}

          {plate && !box && (
            <p className="subtle">Drag a rectangle over the unit's location.</p>
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
    </div>
  );
}
