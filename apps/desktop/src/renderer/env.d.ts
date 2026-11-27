/// <reference types="vite/client" />
import type { DesktopAPI } from '../shared/protocol';
declare global { interface Window { rocky: DesktopAPI } }
