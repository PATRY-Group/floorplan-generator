import React, { useRef, useEffect, useCallback } from "react";

/**
 * Manual paint layer: a raster <canvas> stacked over the sheet SVG. Brush paints
 * (source-over); eraser rubs out paint (destination-out) so the floorplan shows
 * through — MS-Paint-style, and re-painting an erased spot works because it all
 * composites in order on one bitmap.
 *
 * The canvas is the LIVE display in the editor; it is never baked into the live
 * preview SVG. On export (Save / PNG) App reads the latest dataURL (kept current
 * via onPaintChange) and the backend embeds it as a single <image>.
 *
 * Backing store is page * SS so the flattened PNG is crisp at the export width
 * (SHEET_PNG_W = 2000 = PAGE_W * 2), while CSS scales it down to the preview.
 */
const SS = 2;              // supersample: backing px per viewBox unit
const UNDO_CAP = 20;

export default function PaintCanvas({
  active, tool = "brush", color = "#ffffff", size = 6,
  initialImage = null, page = { w: 1000, h: 1080 },
  onPaintChange, registerUndo,
}) {
  const canvasRef = useRef(null);
  const drawing = useRef(false);
  const last = useRef(null);          // last point in backing coords
  const undoStack = useRef([]);       // dataURL snapshots, pre-stroke
  const loaded = useRef(null);        // last dataURL we drew or emitted (round-trip guard)

  // latest tool params + emit callback, read by handlers without re-binding
  const live = useRef({ tool, color, size, onPaintChange });
  live.current = { tool, color, size, onPaintChange };

  const w = Math.round(page.w * SS);
  const h = Math.round(page.h * SS);

  const ctx = useCallback(() => canvasRef.current?.getContext("2d"), []);

  const emit = useCallback(() => {
    const url = canvasRef.current?.toDataURL("image/png") || null;
    loaded.current = url;
    live.current.onPaintChange?.(url);
  }, []);

  // Seed (or reset) the bitmap from initialImage — fires on mount, reopen, and
  // tab switch. Skips the round-trip where our own emitted dataURL comes back as
  // a prop, so a stroke never triggers a reload/flicker.
  useEffect(() => {
    if (initialImage === loaded.current) return;
    loaded.current = initialImage;
    // External bitmap swap (mount / tab switch / reopen) — drop the prior doc's
    // undo history so Ctrl+Z can't paint one doc's strokes onto another.
    undoStack.current = [];
    const c = ctx();
    if (!c) return;
    c.clearRect(0, 0, w, h);
    if (initialImage) {
      const img = new Image();
      img.onload = () => c.drawImage(img, 0, 0, w, h);
      img.src = initialImage;
    }
  }, [initialImage, ctx, w, h]);

  // Expose undo()/clear() to the parent (toolbar buttons + Ctrl+Z).
  useEffect(() => {
    function restore(url) {
      const c = ctx();
      if (!c) return;
      c.clearRect(0, 0, w, h);
      const finish = () => { loaded.current = url; live.current.onPaintChange?.(url); };
      if (url) {
        const img = new Image();
        img.onload = () => { c.drawImage(img, 0, 0, w, h); finish(); };
        img.src = url;
      } else { finish(); }
    }
    const api = {
      // Returns true if it undid a stroke; false (no paint history) lets the
      // caller fall through to the normal label/room undo so Ctrl+Z isn't lost.
      undo() {
        if (!undoStack.current.length) return false;
        restore(undoStack.current.pop());
        return true;
      },
      clear() {
        const c = ctx();
        if (!c) return;
        undoStack.current.push(canvasRef.current.toDataURL("image/png"));
        if (undoStack.current.length > UNDO_CAP) undoStack.current.shift();
        c.clearRect(0, 0, w, h);
        emit();
      },
      hasPaint: () => !!loaded.current,
    };
    registerUndo?.(api);
  }, [registerUndo, ctx, emit, w, h]);

  // Pointer client coords -> canvas backing coords (independent of CSS scaling).
  function toCanvas(e) {
    const r = canvasRef.current.getBoundingClientRect();
    return [(e.clientX - r.left) / r.width * w, (e.clientY - r.top) / r.height * h];
  }

  function stroke(c, ax, ay, bx, by) {
    const { tool: t, color: col, size: sz } = live.current;
    c.globalCompositeOperation = t === "eraser" ? "destination-out" : "source-over";
    c.strokeStyle = col;
    c.lineWidth = Math.max(1, sz) * SS;
    c.lineCap = "round";
    c.lineJoin = "round";
    c.beginPath();
    c.moveTo(ax, ay);
    c.lineTo(bx, by);
    c.stroke();
  }

  function onPointerDown(e) {
    if (!active) return;
    const c = ctx();
    if (!c) return;
    e.preventDefault();
    canvasRef.current.setPointerCapture?.(e.pointerId);
    undoStack.current.push(canvasRef.current.toDataURL("image/png"));
    if (undoStack.current.length > UNDO_CAP) undoStack.current.shift();
    drawing.current = true;
    const [x, y] = toCanvas(e);
    last.current = [x, y];
    stroke(c, x, y, x, y);   // a tap leaves a dot
  }

  function onPointerMove(e) {
    if (!drawing.current) return;
    const c = ctx();
    if (!c) return;
    const [x, y] = toCanvas(e);
    const [px, py] = last.current;
    stroke(c, px, py, x, y);
    last.current = [x, y];
  }

  function onPointerUp(e) {
    if (!drawing.current) return;
    drawing.current = false;
    canvasRef.current?.releasePointerCapture?.(e.pointerId);
    emit();
  }

  return (
    <canvas
      ref={canvasRef}
      className="paint-canvas"
      width={w}
      height={h}
      style={{ pointerEvents: active ? "auto" : "none", cursor: active ? "crosshair" : "default" }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      onPointerCancel={onPointerUp}
    />
  );
}
