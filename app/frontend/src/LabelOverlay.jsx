import React, { useLayoutEffect, useRef, useState, useCallback } from "react";

/**
 * Rendered sheet SVG + draggable label handles. Drag to move; click to select
 * and nudge with arrow keys (1 viewBox px, Shift = 10). Double-click resets to
 * auto-placement. Pixel/viewBox -> DXF conversion uses the server transform:
 *   svgX = tx + dxfX*s ;  dxfX = (svgX - tx)/s ;  dxfY = (ty - svgY)/s
 */
export default function LabelOverlay({ svg, meta, onMove, onReset }) {
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

  function startDrag(e, p) {
    e.preventDefault();
    e.target.setPointerCapture?.(e.pointerId);
    setSelected(p.i);
    setDrag({ i: p.i, x: p.px, y: p.py });
  }
  function onPointerMove(e) {
    if (!drag || !wrapRef.current) return;
    const rect = wrapRef.current.getBoundingClientRect();
    setDrag((d) => ({ ...d,
      x: (e.clientX - rect.left) / scale,
      y: (e.clientY - rect.top) / scale }));
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
      <div className="handles">
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
              title={`${p.name} — drag or arrow-key to move, double-click to reset`}
              onPointerDown={(e) => startDrag(e, p)}
              onClick={(e) => { e.stopPropagation(); setSelected(p.i); }}
              onDoubleClick={() => onReset(p.i)}
            >
              <span className="dot" />
              <span className="tag">{p.name}</span>
            </div>
          );
        })}
      </div>
      {selected != null && (
        <div className="nudgehint">Arrow keys nudge · Shift = 10px · dbl-click resets</div>
      )}
    </div>
  );
}
