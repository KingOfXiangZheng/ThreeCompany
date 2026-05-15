const fs = require('fs');
const path = require('path');

const configDir = path.join(__dirname, '..', 'config');
const cookies = JSON.parse(fs.readFileSync(path.join(configDir, 'cookies.json'), 'utf8'));
const headers = JSON.parse(fs.readFileSync(path.join(configDir, 'headers.json'), 'utf8'));
const configData = JSON.parse(fs.readFileSync(path.join(configDir, 'config.json'), 'utf8'));

function buildQueryParams(rpcId, reqId) {
  const params = new URLSearchParams({
    rpcids: rpcId,
    'source-path': '/app',
    bl: configData.version,
    'f.sid': generateSessionId(),
    hl: config.hl,
    _reqid: reqId || Math.floor(Math.random() * 9000000 + 1000000),
    rt: 'c'
  });
  return params.toString();
}

function generateSessionId() {
  const timestamp = Date.now();
  const random = Math.floor(Math.random() * 9999999999999);
  const combined = BigInt(timestamp) * BigInt(10000000000000) + BigInt(random);
  return combined.toString();
}

function buildRequestBody(rpcId, params, conversationId = null) {
  const bodyParams = {
    'f.req': JSON.stringify([[[[rpcId, JSON.stringify(params), null, 'generic']]]])
  };
  return new URLSearchParams(bodyParams).toString();
}

function buildHeaders(extraExt = null) {
  const allHeaders = { ...headers };

  // 添加 goog-ext header
  if (extraExt) {
    allHeaders['x-goog-ext-525001261-jspb'] = extraExt;
  } else {
    allHeaders['x-goog-ext-525001261-jspb'] = '[1,null,null,null,null,null,null,null,[4],null,null,null,null,null,1,null,"225CDC15-DF99-4545-A619-74C720D374A7"]';
  }
  allHeaders['x-goog-ext-73010989-jspb'] = '[0]';

  // 添加 cookie
  allHeaders['cookie'] = `__Secure-ENID=${cookies['__Secure-ENID']}`;

  return allHeaders;
}

module.exports = {
  buildQueryParams,
  buildRequestBody,
  buildHeaders,
  config: configData
};