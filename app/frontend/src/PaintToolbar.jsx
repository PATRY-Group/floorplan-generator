import React from "react";

/**
 * Paint tools. The bucket toggle (PaintBucketButton) lives in the stagehead,
 * directly left of the hamburger (☰); when open it just darkens (selected). The
 * feature row (default export) rolls out in its own right-aligned row underneath
 * the stagehead — see App.jsx and the .paint-tools-rowwrap styles.
 */

const NEUTRALS = ["#FFFFFF", "#000000"];
const BRAND_ROLES = ["dark", "accent", "mid", "light"];
const HAS_DROPPER = typeof window !== "undefined" && "EyeDropper" in window;

function Icon({ children, size = 16 }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}
const BucketIcon = () => (
  <Icon>
    <path d="M3.5 11.3 12 2.8l8.5 8.5-6.8 6.8a2 2 0 0 1-2.8 0l-5.4-5.4a1.4 1.4 0 0 1 0-1.4z" />
    <path d="M8.6 7.4 12 10.8" />
    <path d="M20.8 14.6c1 1.4 1.4 2.3 1.4 2.9a1.5 1.5 0 1 1-3 0c0-.6.5-1.5 1.6-2.9z" />
  </Icon>
);
const BrushIcon = () => (
  <Icon>
    <path d="M9.06 11.9 18.6 2.4a2.1 2.1 0 0 1 3 3l-9.5 9.5" />
    <path d="M7.4 12.6c-1.7 0-3 1.4-3 3 0 1.3-1 2-2 2 1 1.3 2.6 2 4 2a3.5 3.5 0 0 0 3.5-3.5c0-1.7-1.3-3.5-2.5-3.5z" />
  </Icon>
);
const EraserIcon = () => (
  <Icon>
    <path d="m7 21-4.3-4.3a1 1 0 0 1 0-1.4l9.6-9.6a1 1 0 0 1 1.4 0l4.3 4.3a1 1 0 0 1 0 1.4L13 21" />
    <path d="M22 21H7" />
    <path d="m5 11 9 9" />
  </Icon>
);
const RectIcon = () => (
  <Icon>
    <rect x="3.5" y="5.5" width="17" height="13" rx="1.5" />
  </Icon>
);
const HandIcon = () => (
  <Icon>
    <path d="M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2" />
    <path d="M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2" />
    <path d="M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8" />
    <path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15" />
  </Icon>
);
const UndoIcon = () => (
  <Icon>
    <path d="M9 14 4 9l5-5" />
    <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
  </Icon>
);
const TrashIcon = () => (
  <Icon>
    <path d="M3 6h18" />
    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M10 11v6" /><path d="M14 11v6" />
  </Icon>
);

function Swatch({ value, current, onPick, title }) {
  const active = current && current.toLowerCase() === value.toLowerCase();
  return (
    <button type="button" className={"swatch" + (active ? " active" : "")}
      style={{ background: value }} title={title || value}
      onClick={() => onPick(value)} aria-label={`color ${value}`} />
  );
}

export function PaintBucketButton({ open, onToggle }) {
  return (
    <button type="button" className={"btn ghost icon paint-bucket" + (open ? " active" : "")}
      onClick={onToggle} title={open ? "Close paint tools" : "Paint over quirks"}
      aria-label="Paint tool" aria-pressed={open}>
      <BucketIcon />
    </button>
  );
}

export default function PaintToolbar({
  tool, onTool, color, onColor, size, onSize, palette = {}, onUndo, onClear,
}) {
  const brand = BRAND_ROLES
    .map((role) => ({ role, hex: palette[role] }))
    .filter((c) => c.hex);

  async function pickDropper() {
    try {
      const r = await new window.EyeDropper().open();
      if (r && r.sRGBHex) onColor(r.sRGBHex);
    } catch { /* user cancelled — ignore */ }
  }

  return (
    <div className="paint-toolbar" role="toolbar" aria-label="Paint tools">
      <span className="current-color" style={{ background: color }}
        title={`Current color ${color}`} />
      <div className="pt-group">
        <button type="button" className={"tool-btn icon" + (tool === "brush" ? " active" : "")}
          onClick={() => onTool("brush")} title="Brush" aria-label="Brush"><BrushIcon /></button>
        <button type="button" className={"tool-btn icon" + (tool === "eraser" ? " active" : "")}
          onClick={() => onTool("eraser")} title="Eraser — rub out paint" aria-label="Eraser"><EraserIcon /></button>
        <button type="button" className={"tool-btn icon" + (tool === "rect" ? " active" : "")}
          onClick={() => onTool("rect")} title="Rectangle — click-drag to fill an area in bulk"
          aria-label="Rectangle"><RectIcon /></button>
        <button type="button" className={"tool-btn icon" + (tool === "pan" ? " active" : "")}
          onClick={() => onTool("pan")} title="Pan — drag to move around when zoomed in"
          aria-label="Pan"><HandIcon /></button>
      </div>

      {(tool === "brush" || tool === "rect") && (
        <div className="pt-group swatches">
          {NEUTRALS.map((c) => (
            <Swatch key={c} value={c} current={color} onPick={onColor} />
          ))}
          {brand.length > 0 && <span className="pt-sep" />}
          {brand.map((c) => (
            <Swatch key={c.role} value={c.hex} current={color} onPick={onColor}
              title={`Brand ${c.role}`} />
          ))}
          {HAS_DROPPER && (
            <button type="button" className="eyedropper-btn" onClick={pickDropper}
              title="Eyedropper — pick a color from the screen" aria-label="Eyedropper">
              <Icon size={15}>
                <path d="m2 22 1-4 9-9" />
                <path d="m11 7 6 6" />
                <path d="M18 2.5a2.1 2.1 0 0 1 3 3L14.5 12 12 9.5 18.5 3z" />
              </Icon>
            </button>
          )}
        </div>
      )}

      {(tool === "brush" || tool === "eraser") && (
        <div className="pt-group size">
          <input type="range" min="1" max="40" value={size}
            onChange={(e) => onSize(Number(e.target.value))}
            title={`${tool} size`} aria-label={`${tool} size`} />
          <span className="size-readout">{size}px</span>
        </div>
      )}

      <div className="pt-group">
        <button type="button" className="tool-btn icon" onClick={onUndo}
          title="Undo last stroke (Ctrl+Z)" aria-label="Undo"><UndoIcon /></button>
        <button type="button" className="tool-btn icon" onClick={onClear}
          title="Clear all paint" aria-label="Clear"><TrashIcon /></button>
      </div>
    </div>
  );
}
