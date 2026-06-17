import React, { useEffect, useState } from "react";
import { subscribe } from "./toast.js";

export default function Toasts() {
  const [items, setItems] = useState([]);
  useEffect(() => subscribe((t) => {
    setItems((xs) => [...xs, t]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== t.id)), 3600);
  }), []);
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={"toast " + t.type}>{t.message}</div>
      ))}
    </div>
  );
}
