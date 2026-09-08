// Load-time smoke test for index.html's script, run under node with a DOM stub.
// It cannot tell you the viewer looks right; it catches the class of mistake a
// browser reports instantly and a Python test suite cannot see at all -- a
// handler bound to an element that is not in the markup, a const used before
// its declaration, a typo in a name. Two of those shipped before this existed.
//
//   node exrstream/static/smoke.js
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const body = src.slice(0, src.indexOf("<script>"));
const script = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));

const ids = new Set([...body.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const missing = new Set();

const el = id => new Proxy({ id, style: {}, classList: {
    toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false } }, {
  get(t, k) {
    if (k in t) return t[k];
    if (k === "append" || k === "addEventListener" || k === "focus" ||
        k === "setPointerCapture" || k === "releasePointerCapture" ||
        k === "getBoundingClientRect" || k === "contains" || k === "getContext")
      return () => ({ left: 0, top: 0, width: 100, height: 100,
                      drawImage(){}, fillRect(){}, beginPath(){}, moveTo(){},
                      lineTo(){}, stroke(){}, save(){}, restore(){} });
    return undefined;
  },
  set(t, k, v) { t[k] = v; return true; }
});

globalThis.document = {
  querySelector(sel) {
    const id = sel.startsWith("#") ? sel.slice(1) : null;
    if (id && !ids.has(id)) missing.add(id);
    return el(id);
  },
  createElement: () => el("created"),
  activeElement: null,
  body: el("body"),
};
globalThis.window = globalThis;
globalThis.location = { protocol: "https:", host: "x", origin: "https://x" };
globalThis.isSecureContext = true;
globalThis.requestAnimationFrame = () => 0;
globalThis.addEventListener = () => {};
globalThis.getSelection = () => null;
globalThis.performance = { now: () => 0 };
globalThis.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
globalThis.VideoDecoder = class { constructor() {} configure() {} close() {} decode() {} };
globalThis.EncodedVideoChunk = class {};
globalThis.WebSocket = class { constructor() { this.readyState = 0; } send() {} };

try {
  new Function(script)();
} catch (e) {
  console.error("FAIL  script threw at load:", e.message);
  process.exit(1);
}
if (missing.size) {
  console.error("FAIL  selectors with no matching id:", [...missing].join(", "));
  process.exit(1);
}
console.log(`  ok  script loads, ${ids.size} ids, every selector resolves`);
console.log("OK");
