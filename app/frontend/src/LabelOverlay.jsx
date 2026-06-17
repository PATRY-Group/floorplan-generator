import React, { useLayoutEffect, useRef, useState, useCallback } from "react";

/**
 * Rendered sheet SVG + draggable label handles. Drag to move; click to select
 * and nudge with arrow keys (1 viewBox px, Shift = 10). Double-click resets to
 * auto-placement. Pixel/viewBox -> DXF conversion uses the server transform:
 *   svgX = tx + dxfX*s ;  dxfX = (svgX - tx)/s ;  dxfY = (ty - svgY)/s
 */
export default function LabelOverlay({ svg, meta, onMove, onReset, showHandles = true }) {
  const wrapRef = useRef(null);
  const [scale, setScale] = useState(1);
  const [drag, setDrag] = useState(null);     // {i, x, y} viewBox coords
  const [selected, setSelected] = useState(null);

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
    // Remember where on the handle we grabbed so the label tracks the cursor
    // from that point instead of snapping its anchor under the pointer.
    setDrag({ i: p.i, x: p.px, y: p.py, ox: vx - p.px, oy: vy - p.py });
  }
  function onPointerMove(e) {
    if (!drag || !wrapRef.current) return;
    const [vx, vy] = pointerVB(e);
    setDrag((d) => ({ ...d, x: vx - d.ox, y: vy - d.oy }));
  }
  function endDrag() {
    if (!drag || !meta) return;
    const [dx, dy] = toDxf(drag.x, drag.y);
    onMove(drag.i, dx, dy);
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
    const [dx, dy] = toDxf(p.px + d[0], p.py + d[1]);
    onMove(selected, dx, dy);
  }

  return (
    <div
      className="sheet overlayhost"
      ref={wrapRef}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerLeave={endDrag}
      onClick={(e) => { if (e.target === e.currentTarget) setSelected(null); }}
    >
      <div dangerouslySetInnerHTML={{ __html: svg }} />
      {showHandles && <div className="handles">
        {placements.map((p) => {
          const live = drag && drag.i === p.i ? drag : null;
          const left = (live ? live.x : p.px) * scale;
          const top = (live ? live.y : p.py) * scale;
          const cls = "handle" + (p.overridden ? " moved" : "") +
                      (live ? " dragging" : "") + (selected === p.i ? " selected" : "");
          return (
            <div
              key={p.i}
              className={cls}
              style={{ left, top }}
              onPointerDown={(e) => startDrag(e, p)}
              onClick={(e) => { e.stopPropagation(); setSelected(p.i); }}
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
      {showHandles && selected != null && (
        <div className="nudgehint">Arrow keys nudge · Shift = 10px · dbl-click resets</div>
      )}
    </div>
  );
}
