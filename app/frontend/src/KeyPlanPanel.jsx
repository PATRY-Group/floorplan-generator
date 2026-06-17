import React, { useEffect, useRef, useState } from "react";
import { uploadPlate } from "./api.js";

const clamp01 = (v) => Math.max(0, Math.min(1, v));

/**
 * Optional key-plan controls: upload a floor-plate screenshot, drag a box over
 * the unit's location, pick a floor label and footer/standalone placement.
 * Calls onChange(keyplanConfig | null) whenever the config is complete.
 */
export default function KeyPlanPanel({ onChange }) {
  const [on, setOn] = useState(false);
  const [plate, setPlate] = useState(null);   // {plate_id, url}
  const [box, setBox] = useState(null);        // [fx, fy, fw, fh]
  const [floor, setFloor] = useState("");
  const [placement, setPlacement] = useState("footer");
  const [drag, setDrag] = useState(null);
  const [busy, setBusy] = useState(false);
  const imgRef = useRef(null);

  function emit(next) {
    const s = { on, plate, box, floor, placement, ...next };
    if (s.on && s.plate && s.box) {
      onChange({
        plate_id: s.plate.plate_id,
        box: s.box,
        floor_label: s.floor,
        placement: s.placement,
        north_deg: 0,
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
