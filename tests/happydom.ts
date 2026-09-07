import { GlobalRegistrator } from "@happy-dom/global-registrator";

// Register DOM globals before anything that depends on them gets imported.
GlobalRegistrator.register();
