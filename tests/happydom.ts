import { GlobalRegistrator } from "@happy-dom/global-registrator";

// Register DOM globals (window, document, etc.) before anything
// that depends on them gets imported.
GlobalRegistrator.register();
