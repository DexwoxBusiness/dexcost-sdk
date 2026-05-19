/**
 * Adapters for automatic cost tracking of HTTP, browser, and compute services.
 *
 * This module re-exports every adapter the SDK ships with.
 */

// HTTP adapter
export {
  registerDomainRate,
  getDomainRates,
  clearDomainRates,
  trackHttp,
  untrackHttp,
  getRecordedEvents,
  clearRecordedEvents,
  getServiceCatalog,
  resetServiceCatalog,
  getSessionManager,
} from "./http.js";

// Browser (Playwright) adapter
export {
  trackBrowser,
  getBrowserEvents,
  clearBrowserEvents,
} from "./browser.js";
export type { TrackBrowserOptions } from "./browser.js";

// AWS Lambda cost calculator
export { lambdaCost, getSupportedRegions } from "./aws-lambda.js";
export type {
  LambdaCostResult,
  LambdaCostDetails,
} from "./aws-lambda.js";
