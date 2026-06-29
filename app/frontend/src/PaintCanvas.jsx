import React, { useRef, useEffect, useState, useCallback } from "react";

/**
 * Manual paint layer: a raster <canvas> stacked over the sheet SVG. Tools:
 *   brush  — freehand paint (source-over)
 *   eraser — rub out paint (destination-out) so the floorplan shows through
 *   rect   — click-drag a filled rectangle to cover spots in bulk
 *   pan    — drag to scroll the zoomed viewport (no paint); use to move around
 * It all composites in order on one bitmap, so re-painting an erased spot works.
 *
 * The canvas is the LIVE display in the editor; it is never baked into the live
 * preview SVG. On export (Save / PNG) App reads the latest dataURL (kept current
 * via onPaintChange) and the backend embeds it as a single <image>.
 *
 * Backing store is page * SS so the flattened PNG is crisp at the export width
 * (SHEET_PNG_W = 2000 = PAGE_W * 2), while CSS scales it down to the preview.
 * Pointer->backing math goes through getBoundingClientRect, so it stays correct
 * at any zoom (the host is CSS-scaled, the backing store is not).
 */
const SS = 2;              // supersample: backing px per viewBox unit
const UNDO_CAP = 20;

export default function PaintCanvas({
  active, tool = "brush", color = "#ffffff", size = 6,
  initialImage = null, page = { w: 1000, h: 1080 },
  onPaintChange, registerUndo,
}) {
  const canvasRef = useRef(null);
  const gesture = useRef(null);       // active pointer gesture: {kind:"draw"|"rect"|"pan", ...}
  const last = useRef(null);          // last point in backing coords (brush/eraser)
  const undoStack = useRef([]);       // dataURL snapshots, pre-stroke
  const loaded = useRef(null);        // last dataURL we drew or emitted (round-trip guard)
  const [rubber, setRubber] = useState(null);  // live rect drag box (css px, overlayhost space)

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
  // Pointer client coords -> css px within the host (for the rubber-band div,
  // which is positioned in the same overlayhost space the canvas fills).
  function toCss(e) {
    const r = canvasRef.current.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  // The scrolling viewport the host lives in — panned by the pan tool.
  function viewport() {
    return canvasRef.current?.closest(".sheet-viewport") || null;
  }

  function snapshot() {
    undoStack.current.push(canvasRef.current.toDataURL("image/png"));
    if (undoStack.current.length > UNDO_CAP) undoStack.current.shift();
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
    const t = live.current.tool;
    e.preventDefault();
    canvasRef.current.setPointerCapture?.(e.pointerId);

    if (t === "pan") {
      // Grab-scroll the viewport; no painting, no undo entry.
      const vp = viewport();
      gesture.current = vp
        ? { kind: "pan", x: e.clientX, y: e.clientY, sl: vp.scrollLeft, st: vp.scrollTop }
        : { kind: "pan" };
      return;
    }

    const c = ctx();
    if (!c) return;
    snapshot();   // pre-gesture state, for undo (rect pops it again if it's a no-op)

    if (t === "rect") {
      const [bx, by] = toCanvas(e);
      const [cx, cy] = toCss(e);
      gesture.current = { kind: "rect", bx, by, cx, cy };
      setRubber({ left: cx, top: cy, width: 0, height: 0 });
      return;
    }

    gesture.current = { kind: "draw" };
    const [x, y] = toCanvas(e);
    last.current = [x, y];
    stroke(c, x, y, x, y);   // a tap leaves a dot
  }

  function onPointerMove(e) {
    const g = gesture.current;
    if (!g) return;

    if (g.kind === "pan") {
      const vp = viewport();
      if (vp) {
        vp.scrollLeft = g.sl - (e.clientX - g.x);
        vp.scrollTop = g.st - (e.clientY - g.y);
      }
      return;
    }

    if (g.kind === "rect") {
      const [cx, cy] = toCss(e);
      setRubber({
        left: Math.min(g.cx, cx), top: Math.min(g.cy, cy),
        width: Math.abs(cx - g.cx), height: Math.abs(cy - g.cy),
      });
      return;
    }

    const c = ctx();
    if (!c) return;
    const [x, y] = toCanvas(e);
    const [px, py] = last.current;
    stroke(c, px, py, x, y);
    last.current = [x, y];
  }

  function onPointerUp(e) {
    const g = gesture.current;
    if (!g) return;
    gesture.current = null;
    canvasRef.current?.releasePointerCapture?.(e.pointerId);

    if (g.kind === "pan") return;

    if (g.kind === "rect") {
      setRubber(null);
      const c = ctx();
      const [bx, by] = toCanvas(e);
      const x = Math.min(g.bx, bx), y = Math.min(g.by, by);
      const rw = Math.abs(bx - g.bx), rh = Math.abs(by - g.by);
      if (!c || rw < 2 || rh < 2) {
        undoStack.current.pop();   // negligible drag: drop the no-op undo entry
        return;
      }
      c.globalCompositeOperation = "source-over";
      c.fillStyle = live.current.color;
      c.fillRect(x, y, rw, rh);
      emit();
      return;
    }

    emit();   // draw
  }

  const cursor = active ? (tool === "pan" ? "grab" : "crosshair") : "default";

  return (
    <>
      <canvas
        ref={canvasRef}
        className="paint-canvas"
        width={w}
        height={h}
        style={{ pointerEvents: active ? "auto" : "none", cursor }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        onPointerCancel={onPointerUp}
      />
      {rubber && (
        <div className="paint-rubber" style={{
          left: rubber.left, top: rubber.top, width: rubber.width, height: rubber.height,
        }} />
      )}
    </>
  );
}
