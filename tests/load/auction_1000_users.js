import http from "k6/http";
import { check, sleep } from "k6";

const baseUrl = (__ENV.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const lotIds = (__ENV.LOT_IDS || "").split(",").map((value) => value.trim()).filter(Boolean);
const sessionCookies = (__ENV.SESSION_COOKIES || __ENV.SESSION_COOKIE || "")
  .split("|").map((value) => value.trim()).filter(Boolean);
const extraFilterQueries = (__ENV.FILTER_QUERIES || "")
  .split("|").map((value) => value.trim()).filter(Boolean);

export const options = {
  scenarios: {
    auction_browsing: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m", target: 200 },
        { duration: "3m", target: 500 },
        { duration: "5m", target: 1000 },
        { duration: "10m", target: 1000 },
        { duration: "2m", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{endpoint:catalog}": ["p(95)<500"],
    "http_req_duration{endpoint:detail}": ["p(95)<700"],
    "http_req_duration{endpoint:map}": ["p(95)<700"],
    "http_req_duration{endpoint:analytics}": ["p(95)<700"],
  },
};

export function setup() {
  const minimumDatasetLots = Number(__ENV.MIN_DATASET_LOTS || 0);
  const declaredDatasetLots = Number(__ENV.DATASET_LOT_COUNT || 0);
  if (minimumDatasetLots > 0 && declaredDatasetLots < minimumDatasetLots) {
    throw new Error(`Dataset gate failed: ${declaredDatasetLots} < ${minimumDatasetLots} lots`);
  }
  if (sessionCookies.length) {
    return { loginCookie: "" };
  }
  if (!__ENV.PHONE || !__ENV.PASSWORD) {
    throw new Error("Set SESSION_COOKIE or PHONE and PASSWORD for a dedicated load-test account");
  }
  const response = http.post(
    `${baseUrl}/login`,
    { phone: __ENV.PHONE, password: __ENV.PASSWORD },
    { redirects: 0 },
  );
  check(response, { "load-test login accepted": (res) => [302, 303].includes(res.status) });
  const cookies = Object.entries(response.cookies).flatMap(([name, values]) =>
    values.map((item) => `${name}=${item.value}`),
  );
  if (!cookies.length) {
    throw new Error("Login did not return a session cookie");
  }
  return { loginCookie: cookies.join("; ") };
}

function get(path, endpoint, cookie) {
  const response = http.get(`${baseUrl}${path}`, {
    headers: { Cookie: cookie },
    tags: { endpoint },
  });
  check(response, { [`${endpoint} returned 200`]: (res) => res.status === 200 });
}

export default function (data) {
  const cookie = sessionCookies.length
    ? sessionCookies[(__VU - 1) % sessionCookies.length]
    : data.loginCookie;
  const sample = Math.random();
  if (sample < 0.55) {
    const filters = [
      "?lot_scope=active&sort_by=best",
      "?lot_scope=active&right_type=ownership",
      "?lot_scope=active&right_type=lease&lease_duration=short",
      "?lot_scope=active&investment_verdict=needs_check",
      "?lot_scope=all&bid_limit_status=insufficient_data",
      ...extraFilterQueries,
    ];
    get(`/cabinet/auctions-v2${filters[Math.floor(Math.random() * filters.length)]}`, "catalog", cookie);
  } else if (sample < 0.75 && lotIds.length) {
    const lotId = lotIds[Math.floor(Math.random() * lotIds.length)];
    get(`/cabinet/auctions-v2/${encodeURIComponent(lotId)}`, "detail", cookie);
  } else if (sample < 0.9) {
    get("/cabinet/auctions-v2/map?lot_scope=active", "map", cookie);
  } else {
    get("/cabinet/auctions-v2/analytics", "analytics", cookie);
  }
  sleep(1 + Math.random() * 2);
}
