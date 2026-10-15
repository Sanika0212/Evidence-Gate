// Load test for POST /v1/check with OAuth2 client-credentials tokens.
//
//   k6 run -e BASE=http://localhost:8090 -e TOKEN_URL=http://localhost:8081/oauth2/token \
//          -e STAGES=10:1m,25:2m,50:2m loadtest/k6/check.js
//
// Each request checks one of a fixed set of medical answers against its passages
// (the shape a RAG application sends), so the checker does the same work per request.
// The checker caches scores for (passage, claim) pairs it has seen, so by default every
// request tags its passages with a unique reference and nothing is served from cache;
// -e CACHED=1 sends identical requests to measure the cached path instead.
// -e SHIFT=1 simulates a retrieval outage: each answer is checked against another case's
// passages, so support scores collapse and the drift monitor should notice.
import http from "k6/http";
import { check } from "k6";
import { Trend, Rate } from "k6/metrics";

const BASE = __ENV.BASE || "http://localhost:8090";
const TOKEN_URL = __ENV.TOKEN_URL || "http://localhost:8081/oauth2/token";
const STAGES = (__ENV.STAGES || "10:1m,25:2m,50:2m").split(",").map((s) => {
  const [target, duration] = s.split(":");
  return { target: Number(target), duration };
});

export const options = {
  scenarios: {
    ramp: { executor: "ramping-vus", startVUs: 1, stages: STAGES, gracefulRampDown: "10s" },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{endpoint:check}": ["p(95)<1500"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const claims = new Trend("eg_claims_per_answer");
const flagged = new Rate("eg_flagged");

const CASES = [
  {
    question: "What enzyme does aspirin inhibit?",
    answer: "Aspirin irreversibly inhibits cyclooxygenase by acetylating a serine residue. This blocks thromboxane A2 production in platelets. Aspirin also permanently cures migraine after one dose.",
    passages: [
      { id: "a1", text: "Aspirin irreversibly inhibits cyclooxygenase-1 and -2 by acetylating a serine residue in the active site, which blocks thromboxane A2 synthesis in platelets." },
      { id: "a2", text: "Aspirin can relieve acute migraine pain; it does not prevent future attacks." },
    ],
  },
  {
    question: "What is first-line therapy for type 2 diabetes?",
    answer: "Metformin is the usual first-line medication for type 2 diabetes. It lowers hepatic glucose production. It is contraindicated in every patient over 60.",
    passages: [
      { id: "m1", text: "Metformin remains the preferred initial pharmacologic agent for most adults with type 2 diabetes." },
      { id: "m2", text: "Metformin decreases hepatic glucose production and improves insulin sensitivity; dose adjustment is needed in reduced kidney function." },
    ],
  },
  {
    question: "At what age should average-risk adults start colorectal cancer screening?",
    answer: "Screening is recommended for adults aged 45 to 75. Colonoscopy every 10 years is one accepted option.",
    passages: [
      { id: "c1", text: "The USPSTF recommends screening for colorectal cancer in all adults aged 45 to 75 years." },
      { id: "c2", text: "Screening strategies include colonoscopy every 10 years and annual fecal immunochemical testing." },
    ],
  },
];

export function setup() {
  const res = http.post(TOKEN_URL, { grant_type: "client_credentials", client_id: "demo-client", client_secret: "demo-secret" });
  check(res, { "token issued": (r) => r.status === 200 });
  return { token: res.json("access_token") };
}

const CACHED = __ENV.CACHED === "1";
const SHIFT = __ENV.SHIFT === "1";

export default function (data) {
  const i = Math.floor(Math.random() * CASES.length);
  const base = SHIFT ? { ...CASES[i], passages: CASES[(i + 1) % CASES.length].passages } : CASES[i];
  const ref = `${__VU}-${__ITER}-${Date.now()}`;
  const c = CACHED
    ? base
    : { ...base, passages: base.passages.map((p) => ({ id: p.id, text: `${p.text} (ref ${ref})` })) };
  const res = http.post(`${BASE}/v1/check`, JSON.stringify(c), {
    headers: { "content-type": "application/json", authorization: `Bearer ${data.token}` },
    tags: { endpoint: "check" },
  });
  const ok = check(res, { "status 200": (r) => r.status === 200 });
  if (ok) {
    const body = res.json();
    claims.add(body.claims.length);
    flagged.add(body.decision !== "pass");
  }
}
