import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  verifyPurchaseSession,
  PurchaseVerificationError,
} from "../src/entitlements/purchase.js";
import { createFakeKv } from "./helpers/fakeKv.js";

const VALID_SESSION_ID = "cs_test_a1B2c3D4e5F6";

function makeEnv(overrides = {}) {
  return {
    STRIPE_SECRET_KEY: "sk_test_should_never_appear_in_source",
    SESSIONS: createFakeKv(),
    ...overrides,
  };
}

function stripeResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
  };
}

test("paid session grants the purchase entitlement", async () => {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    return stripeResponse(200, {
      payment_status: "paid",
      amount_total: 7900,
      customer_details: { email: "buyer@example.com" },
    });
  };

  const env = makeEnv();
  const result = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });

  assert.equal(result.granted, true);
  assert.equal(result.entitlementType, "purchase");
  assert.equal(result.sessionId, VALID_SESSION_ID);
  assert.equal(result.customerEmail, "buyer@example.com");
  assert.equal(result.amountTotal, 7900);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, new RegExp(VALID_SESSION_ID));
});

test("unpaid session is denied", async () => {
  const fetchImpl = async () => stripeResponse(200, { payment_status: "unpaid" });
  const env = makeEnv();

  const result = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });

  assert.equal(result.granted, false);
  assert.equal(result.reason, "unpaid");
});

test("unknown session id is denied", async () => {
  const fetchImpl = async () => stripeResponse(404, { error: { message: "No such session" } });
  const env = makeEnv();

  const result = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });

  assert.equal(result.granted, false);
  assert.equal(result.reason, "unknown_session");
});

test("malformed session id is rejected without calling Stripe or the store", async () => {
  let fetchCalled = false;
  const fetchImpl = async () => {
    fetchCalled = true;
    return stripeResponse(200, { payment_status: "paid" });
  };
  const env = makeEnv();

  const result = await verifyPurchaseSession("not-a-real-session-id", env, { fetchImpl });

  assert.equal(result.granted, false);
  assert.equal(result.reason, "invalid_session_id");
  assert.equal(fetchCalled, false);
  assert.equal(env.SESSIONS._size(), 0);
});

test("replayed session id is rate limited on the second use", async () => {
  let fetchCallCount = 0;
  const fetchImpl = async () => {
    fetchCallCount += 1;
    return stripeResponse(200, { payment_status: "paid" });
  };
  const env = makeEnv();

  const first = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });
  const second = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });

  assert.equal(first.granted, true);
  assert.equal(second.granted, false);
  assert.equal(second.reason, "rate_limited");
  assert.equal(fetchCallCount, 1, "Stripe should only be queried once for a replayed session id");
});

test("a second distinct session id is unaffected by another session's replay guard", async () => {
  const fetchImpl = async () => stripeResponse(200, { payment_status: "paid" });
  const env = makeEnv();

  const first = await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });
  const other = await verifyPurchaseSession("cs_test_zZ9yY8xX7wW6", env, { fetchImpl });

  assert.equal(first.granted, true);
  assert.equal(other.granted, true);
});

test("throws when STRIPE_SECRET_KEY is missing from env", async () => {
  const env = makeEnv({ STRIPE_SECRET_KEY: undefined });
  const fetchImpl = async () => stripeResponse(200, { payment_status: "paid" });

  await assert.rejects(
    () => verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl }),
    PurchaseVerificationError,
  );
});

test("Stripe secret key is read from env and sent as a bearer token, never hardcoded", async () => {
  const secret = "sk_test_env_only_marker_98765";
  let capturedAuth = null;
  const fetchImpl = async (_url, opts) => {
    capturedAuth = opts.headers.Authorization;
    return stripeResponse(200, { payment_status: "paid" });
  };
  const env = makeEnv({ STRIPE_SECRET_KEY: secret });

  await verifyPurchaseSession(VALID_SESSION_ID, env, { fetchImpl });

  assert.equal(capturedAuth, `Bearer ${secret}`);

  const sourcePath = fileURLToPath(
    new URL("../src/entitlements/purchase.js", import.meta.url),
  );
  const source = readFileSync(sourcePath, "utf-8");
  assert.doesNotMatch(
    source,
    /sk_(live|test)_[A-Za-z0-9]/,
    "worker source must never contain a literal Stripe secret key",
  );
});
