import { handleApiRequest } from '../../shared/api-core.js';

export async function onRequest(context) {
  return handleApiRequest(context.request, context.env || {});
}
