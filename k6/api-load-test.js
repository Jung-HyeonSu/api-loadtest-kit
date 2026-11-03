import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';

const baseUrl = (__ENV.BASE_URL || '').replace(/\/+$/, '');
const apiPath = __ENV.API_PATH || '';
const method = (__ENV.HTTP_METHOD || 'GET').toUpperCase();
const bodyFile = __ENV.BODY_FILE || '';
const bodyJson = __ENV.BODY_JSON || '';
const pageMode = __ENV.PAGE_MODE || 'first';
const pageField = __ENV.PAGE_FIELD || '';
const pageCount = Number(__ENV.PAGE_COUNT || 1);
const smoke = __ENV.SMOKE === '1';
const expectedStatus = Number(__ENV.EXPECTED_STATUS || 200);
const expectJson = (__ENV.EXPECT_JSON || 'true').toLowerCase() === 'true';
const jsonType = (__ENV.JSON_TYPE || 'any').toLowerCase();
const minItems = Number(__ENV.MIN_ITEMS || 0);

let initialBody = null;
if (bodyFile) {
  initialBody = JSON.parse(open(bodyFile));
} else if (bodyJson) {
  initialBody = JSON.parse(bodyJson);
}

const headers = JSON.parse(__ENV.HEADERS_JSON || '{}');
if (__ENV.AUTH_HEADER_NAME && __ENV.AUTH_HEADER_VALUE) {
  headers[__ENV.AUTH_HEADER_NAME] = __ENV.AUTH_HEADER_VALUE;
}

if (!baseUrl || !apiPath) {
  throw new Error('BASE_URL과 API_PATH가 필요합니다.');
}
if (!Number.isInteger(expectedStatus) || expectedStatus < 100 || expectedStatus > 599) {
  throw new Error('EXPECTED_STATUS는 100~599 사이여야 합니다.');
}
if (!['first', 'rotate'].includes(pageMode)) {
  throw new Error('PAGE_MODE는 first 또는 rotate여야 합니다.');
}
if (pageMode === 'rotate' && (!pageField || !initialBody || !Number.isInteger(pageCount) || pageCount < 1)) {
  throw new Error('PAGE_MODE=rotate에는 객체형 본문, PAGE_FIELD, 1 이상의 PAGE_COUNT가 필요합니다.');
}
if (!['any', 'array', 'object'].includes(jsonType)) {
  throw new Error('JSON_TYPE은 any, array, object 중 하나여야 합니다.');
}

const thresholds = {
  http_req_failed: ['rate<' + (__ENV.FAILURE_RATE || '0.01')],
  checks: ['rate>' + (__ENV.CHECK_SUCCESS_RATE || '0.99')],
};
if (!smoke) {
  thresholds.dropped_iterations = ['count==0'];
}

export const options = {
  scenarios: smoke
    ? {
        smoke: {
          executor: 'shared-iterations',
          vus: 1,
          iterations: 1,
          maxDuration: '1m',
        },
      }
    : {
        api: {
          executor: 'constant-arrival-rate',
          rate: Number(__ENV.RATE || 1),
          timeUnit: '1s',
          duration: __ENV.DURATION || '1m',
          preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 10),
          maxVUs: Number(__ENV.MAX_VUS || 50),
        },
      },
  thresholds,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export default function () {
  const requestBody = initialBody === null
    ? null
    : JSON.parse(JSON.stringify(initialBody));

  if (pageMode === 'rotate' && !smoke) {
    requestBody[pageField] = (exec.scenario.iterationInTest % pageCount) + 1;
  } else if (requestBody && pageField && pageMode === 'first') {
    requestBody[pageField] = 1;
  }

  const response = http.request(
    method,
    baseUrl + apiPath,
    requestBody === null ? null : JSON.stringify(requestBody),
    {
      headers,
      redirects: 0,
      timeout: __ENV.TIMEOUT || '30s',
      tags: { name: method + ' ' + apiPath },
    }
  );

  let json;
  let validJson = false;
  try {
    json = response.json();
    validJson = true;
  } catch (_) {
    json = null;
  }

  const isArray = Array.isArray(json);
  const isObject = json !== null && typeof json === 'object' && !isArray;
  const shapeMatches = jsonType === 'any'
    || (jsonType === 'array' && isArray)
    || (jsonType === 'object' && isObject);
  const hasMinimumItems = !isArray || json.length >= minItems;

  check(response, {
    'Expected HTTP status': (r) => r.status === expectedStatus,
    'Valid JSON response': () => !expectJson || validJson,
    'JSON response shape': () => !expectJson || shapeMatches,
    'Minimum array items': () => !expectJson || hasMinimumItems,
  });

  if (smoke && isArray) {
    console.log('Response array items: ' + json.length);
  }
}
