import { GlobalRegistrator } from "@happy-dom/global-registrator";

// Register DOM globals (window, document, etc.) before anything
// that depends on them gets imported. The URL matters since ticket #1223:
// the app derives its base from document.baseURI, so the tests need a real
// hierarchical URL (`about:blank` cannot resolve relative paths).
GlobalRegistrator.register({ url: "http://localhost/" });
