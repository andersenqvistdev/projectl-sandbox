import { handleRequest } from './router.js';

export default {
  fetch: handleRequest,
};

export { EntitlementType, checkEntitlement } from './entitlements.js';
export { signPath, verifySignedPath } from './signing.js';
