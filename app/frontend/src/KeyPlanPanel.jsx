import React, { useEffect, useState } from "react";
import { uploadPlate, plateUrl } from "./api.js";
import { toast } from "./toast.js";

/**
 * Optional key-plan controls: upload (or paste) a finished key-plan image — the
 * unit already marked on it — pick a floor label and footer/standalone
 * placement. The backend trims the surrounding whitespace on intake, so the
 * preview here shows the same cropped image that lands on the sheet.
 *
 * Calls onChange(keyplanConfig | null) whenever the config is complete (an
 * image is the only requirement — there's no box to place anymore).
 *
 * `initial` is a previously-saved keyplan config (from re-open / session
 * restore). The panel seeds its state from it so the UI matches what will
 * actually render — otherwise the first interaction would emit a blank config
 * and wipe the restored key plan. The panel is keyed by doc id upstream, so it
 * remounts (and re-seeds) per tab.
 */
export default function KeyPlanPanel({ onChange, initial }) {
  const [on, setOn] = useState(!!initial);
  const [plate, setPlate] = useState(          // {plate_id, url}
    initial?.plate_id ? { plate_id: initial.plate_id, url: plateUrl(initial.plate_id) } : null);
  const [floor, setFloor] = useState(initial?.floor_label || "");
  const [placement, setPlacement] = useState(initial?.placement || "footer");
  const [busy, setBusy] = useState(false);

  function emit(next) {
    const s = { on, plate, floor, placement, ...next };
    if (s.on && s.plate) {
      onChange({
        plate_id: s.plate.plate_id,
        floor_label: s.floor,
        placement: s.placement,
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
    try {
      const r = await uploadPlate(file);
      // The backend cropped the image; repaint from the served (cropped) copy
      // so the preview matches the sheet, not the un-cropped local file.
      const p = { plate_id: r.plate_id, url: plateUrl(r.plate_id) };
      setPlate(p);
      emit({ plate: p });
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="step">
      <label className="toggle" style={{ marginBottom: on ? 10 : 0 }}>
        <input type="checkbox" checked={on} onChange={(e) => toggle(e.target.checked)} />
        Add a key plan to this sheet
      </label>

      {on && (
        <>
          <p className="subtle" style={{ marginTop: 0 }}>
            Upload or paste a finished key-plan image (with this unit marked).
            We'll trim the surrounding whitespace and drop it in as reference.
          </p>
          <label className="drop small">
            {busy ? "Uploading…" : (plate ? "Replace key-plan image" : "Choose or paste an image (Ctrl+V)")}
            <input type="file" accept="image/*" onChange={(e) => choose(e.target.files[0])} />
          </label>

          {plate && (
            <>
              <label>Preview</label>
              <div className="platepick">
                <img src={plate.url} alt="key plan" draggable={false}
                  style={{ background: "#F7F3ED" }} />
              </div>
            </>
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
