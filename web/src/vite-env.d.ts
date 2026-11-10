/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USE_MOCK?: string;
  readonly VITE_API_BASE?: string;
  readonly VITE_SESSION_TOKEN?: string;
  readonly VITE_ORG_ID?: string;
  readonly VITE_PERIOD?: string;
  readonly VITE_SYSTEM_IDS?: string;
  readonly VITE_SESSION_ROLES?: string;
  readonly VITE_VIEWER_NAME?: string;
  readonly VITE_VIEWER_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
