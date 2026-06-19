// Minimal pub/sub toast bus. Call toast(msg, type, action?) from anywhere;
// <Toasts/> renders them. type: "success" | "error" | "info".
// action (optional): { label, run } renders a button (e.g. an Undo) in the toast.
let listeners = [];
let counter = 0;

export function subscribe(fn) {
  listeners.push(fn);
  return () => { listeners = listeners.filter((l) => l !== fn); };
}

export function toast(message, type = "info", action = null) {
  const t = { id: ++counter, message, type, action };
  listeners.forEach((l) => l(t));
}
