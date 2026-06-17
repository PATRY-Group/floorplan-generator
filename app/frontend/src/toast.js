// Minimal pub/sub toast bus. Call toast(msg, type) from anywhere; <Toasts/>
// renders them. type: "success" | "error" | "info".
let listeners = [];
let counter = 0;

export function subscribe(fn) {
  listeners.push(fn);
  return () => { listeners = listeners.filter((l) => l !== fn); };
}

export function toast(message, type = "info") {
  const t = { id: ++counter, message, type };
  listeners.forEach((l) => l(t));
}
