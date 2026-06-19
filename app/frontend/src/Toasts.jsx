import React, { useEffect, useState } from "react";
import { subscribe } from "./toast.js";

export default function Toasts() {
  const [items, setItems] = useState([]);
  const dismiss = (id) => setItems((xs) => xs.filter((x) => x.id !== id));
  useEffect(() => subscribe((t) => {
    setItems((xs) => [...xs, t]);
    // toasts with an action linger a little longer so it's clickable
    setTimeout(() => dismiss(t.id), t.action ? 6000 : 3600);
  }), []);
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={"toast " + t.type}>
          <span>{t.message}</span>
          {t.action && (
            <button className="toast-action"
              onClick={() => { dismiss(t.id); t.action.run(); }}>
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
