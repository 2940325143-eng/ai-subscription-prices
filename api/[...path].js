import { handleApiRequest } from '../shared/api-core.js';

export const config = {
  api: {
    bodyParser: false,
  },
};

async function readNodeBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks);
}

export default async function handler(req, res) {
  const proto = String(req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim();
  const host = req.headers['x-forwarded-host'] || req.headers.host || 'localhost';
  const url = `${proto}://${host}${req.url}`;
  const method = String(req.method || 'GET').toUpperCase();

  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers || {})) {
    if (Array.isArray(v)) {
      v.forEach((item) => headers.append(k, item));
    } else if (v !== undefined) {
      headers.set(k, String(v));
    }
  }

  const init = { method, headers };
  if (method !== 'GET' && method !== 'HEAD') {
    init.body = await readNodeBody(req);
  }

  const webResp = await handleApiRequest(new Request(url, init), process.env);
  res.statusCode = webResp.status;

  if (typeof webResp.headers.getSetCookie === 'function') {
    const cookies = webResp.headers.getSetCookie();
    if (cookies.length) res.setHeader('set-cookie', cookies);
  }
  webResp.headers.forEach((val, key) => {
    if (key.toLowerCase() !== 'set-cookie') {
      res.setHeader(key, val);
    }
  });
  const buf = Buffer.from(await webResp.arrayBuffer());
  res.end(buf);
}
