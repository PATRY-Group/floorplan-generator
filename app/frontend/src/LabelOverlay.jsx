import React, { useLayoutEffect, useEffect, useRef, useState, useCallback } from "react";
import PaintCanvas from "./PaintCanvas";

/**
 * Rendered sheet SVG + draggable label handles. Drag to move; click to select
 * and nudge with arrow keys (1 viewBox px, Shift = 10). Double-click resets to
 * auto-placement. Pixel/viewBox -> DXF conversion uses the server transform:
 *   svgX = tx + dxfX*s ;  dxfX = (svgX - tx)/s ;  dxfY = (ty - svgY)/s
 *
 * Zoom is width-based: the inner host is sized to `zoom * 100%` inside a
 * scrolling viewport. Everything keys off the host's measured clientWidth
 * (label `scale`) / getBoundingClientRect (paint coords), so zooming "just
 * works" for both labels and paint without touching stored coordinates. A
 * CSS transform would break this (it doesn't change clientWidth).
 */
export default function LabelOverlay({
  svg, meta, onMove, onReset, showHandles = true,
  paintMode = false, paintTool, paintColor, paintSize,
  paintImage = null, onPaintChange, registerPaint,
  zoom = 1, onZoom,
}) {
  const wrapRef = useRef(null);
  const viewportRef = useRef(null);
  const [scale, setScale] = useState(1);
  const [drag, setDrag] = useState(null);     // {i, x, y, sx, sy} viewBox coords
  const [selected, setSelected] = useState(null);
  const [pending, setPending] = useState(null); // {i, x, y} viewBox coords, local nudge accumulator

  const page = (meta && meta.page) || { w: 1000, h: 1080 };
  const placements = (meta && meta.placements) || [];

  const measure = useCallback(() => {
    if (wrapRef.current) setScale(wrapRef.current.clientWidth / page.w);
  }, [page.w]);

  useLayoutEffect(() => {
    measure();
    const ro = new ResizeObserver(measure);
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [measure, svg]);

  // Invalidate the local nudge accumulator when fresh placements arrive (a new
  // render) or a different label is selected, so it never positions from stale base.
  useLayoutEffect(() => { setPending(null); }, [meta, selected]);

  // Ctrl/⌘ + wheel zooms the preview (plain wheel scrolls/pans the viewport).
  // Bound natively as non-passive so we can preventDefault the browser's own
  // page-zoom; the functional updater means we never re-bind on zoom change.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el || !onZoom) return undefined;
    function onWheel(e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      onZoom((z) => z * factor);
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [onZoom]);

  function toDxf(vbx, vby) {
    const { tx, ty, s } = meta.transform;
    return [(vbx - tx) / s, (ty - vby) / s];
  }

  // Pointer client coords -> viewBox coords.
  function pointerVB(e) {
    const rect = wrapRef.current.getBoundingClientRect();
    return [(e.clientX - rect.left) / scale, (e.clientY - rect.top) / scale];
  }

  function startDrag(e, p) {
    e.preventDefault();
    e.target.setPointerCapture?.(e.pointerId);
    const [vx, vy] = pointerVB(e);
    setSelected(p.i);
    wrapRef.current?.focus();
    // Remember where on the handle we grabbed so the label tracks the cursor
    // from that point instead of snapping its anchor under the pointer. sx/sy
    // record the start position so endDrag can tell a click from a real drag.
    setDrag({ i: p.i, x: p.px, y: p.py, sx: p.px, sy: p.py, ox: vx - p.px, oy: vy - p.py });
  }
  function onPointerMove(e) {
    if (!drag || !wrapRef.current) return;
    const [vx, vy] = pointerVB(e);
    setDrag((d) => ({ ...d, x: vx - d.ox, y: vy - d.oy }));
  }
  function endDrag() {
    if (!drag || !meta) return;
    // Only commit an override if the pointer actually moved beyond a small
    // threshold (viewBox px); a sub-threshold drag is just a selecting click.
    if (Math.hypot(drag.x - drag.sx, drag.y - drag.sy) >= 3) {
      const [dx, dy] = toDxf(drag.x, drag.y);
      onMove(drag.i, dx, dy);
    }
    setDrag(null);
  }

  function onKeyDown(e) {
    if (selected == null || !meta) return;
    const step = e.shiftKey ? 10 : 1;
    const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0],
                ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!d) return;
    e.preventDefault();
    const p = placements.find((q) => q.i === selected);
    if (!p) return;
    // Accumulate from the last intended position (local pending), not the last
    // rendered one, so rapid presses within the render-debounce window add up.
    const base = pending && pending.i === selected ? pending : { i: selected, x: p.px, y: p.py };
    const nx = base.x + d[0], ny = base.y + d[1];
    setPending({ i: selected, x: nx, y: ny });
    const [dx, dy] = toDxf(nx, ny);
    onMove(selected, dx, dy);
  }

  const wm = meta && meta.watermark_svg;

  return (
    <div className="sheet-viewport" ref={viewportRef}
      // When zoomed, cap the viewport so the oversized sheet scrolls *within* it
      // (both axes) — that's what makes the pan tool able to move vertically too.
      // At 100% it's uncapped, so the page scrolls a tall sheet exactly as before.
      style={{ maxHeight: zoom > 1 ? "78vh" : undefined }}>
      <div
        className="overlayhost"
        ref={wrapRef}
        style={{ width: `${zoom * 100}%` }}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
        onClick={(e) => { if (e.target === e.currentTarget) setSelected(null); }}
      >
      <div dangerouslySetInnerHTML={{ __html: svg }} />
      <PaintCanvas
        active={paintMode}
        tool={paintTool}
        color={paintColor}
        size={paintSize}
        initialImage={paintImage}
        page={page}
        onPaintChange={onPaintChange}
        registerUndo={registerPaint}
      />
      {/* Ghost watermark, laid above the paint canvas so the brand stays visible
          over painted-over quirks. The live preview SVG omits it (the backend's
          live_preview flag) precisely so it isn't drawn twice; this overlay is
          the exact markup the export bakes inline, so preview == export. We inject
          a FULL <svg> string into a <div> (same mechanism as the sheet itself) —
          setting innerHTML on a bare <svg> element drops SVG-namespaced children. */}
      {wm && (
        <div className="wm-overlay" aria-hidden="true" dangerouslySetInnerHTML={{
          __html: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${page.w} ${page.h}" preserveAspectRatio="xMidYMid meet">${wm}</svg>`,
        }} />
      )}
      {showHandles && <div className="handles">
        {placements.map((p) => {
          const live = drag && drag.i === p.i ? drag : null;
          const pend = !live && pending && pending.i === p.i ? pending : null;
          const left = (live ? live.x : pend ? pend.x : p.px) * scale;
          const top = (live ? live.y : pend ? pend.y : p.py) * scale;
          const cls = "handle" + (p.overridden ? " moved" : "") +
                      (live ? " dragging" : "") + (selected === p.i ? " selected" : "");
          return (
            <div
              key={p.i}
              className={cls}
              style={{ left, top }}
              onPointerDown={(e) => startDrag(e, p)}
              onClick={(e) => { e.stopPropagation(); setSelected(p.i); wrapRef.current?.focus(); }}
              onDoubleClick={() => onReset(p.i)}
            >
              {live
                ? <span className="dragghost">{p.name}</span>
                : (
                  <span className="movearrow" aria-label="move label">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
                      stroke="currentColor" strokeWidth="2"
                      strokeLinecap="round" strokeLinejoin="round">
                      <line x1="12" y1="4" x2="12" y2="20" />
                      <line x1="4" y1="12" x2="20" y2="12" />
                      <polyline points="9,7 12,4 15,7" />
                      <polyline points="9,17 12,20 15,17" />
                      <polyline points="7,9 4,12 7,15" />
                      <polyline points="17,9 20,12 17,15" />
                    </svg>
                  </span>
                )}
            </div>
          );
        })}
      </div>}
      </div>
    </div>
  );
}
