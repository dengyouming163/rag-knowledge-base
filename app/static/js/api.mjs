export class ApiError extends Error {
  constructor(message, { status = 0, body = null, cause } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export function scopeHeaders(scope = {}, { json = false } = {}) {
  const headers = {};
  if (scope.tenant) headers["X-Tenant-Id"] = scope.tenant;
  if (scope.user) headers["X-User-Id"] = scope.user;
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

function responseMessage(body, statusText) {
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    return body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  if (typeof body?.message === "string") return body.message;
  if (typeof body?.error === "string") return body.error;
  if (typeof body === "string" && body.trim()) return body.trim();
  return statusText || "Request failed";
}

export async function apiJson(path, { scope = {}, signal, ...options } = {}) {
  const { body, headers: suppliedHeaders = {}, ...rest } = options;
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const hasJsonBody = body !== undefined && body !== null && !isFormData;
  const headers = new Headers(scopeHeaders(scope, { json: hasJsonBody }));
  new Headers(suppliedHeaders).forEach((value, name) => headers.set(name, value));

  if (isFormData) headers.delete("content-type");

  const response = await fetch(path, {
    ...rest,
    headers,
    signal,
    body: hasJsonBody ? JSON.stringify(body) : body,
  });
  const text = await response.text();
  let data = null;

  if (text) {
    try {
      data = JSON.parse(text);
    } catch (error) {
      if (response.ok) {
        throw new ApiError(`Invalid JSON response (HTTP ${response.status})`, {
          status: response.status,
          body: text,
          cause: error,
        });
      }
      data = text;
    }
  }

  if (!response.ok) {
    throw new ApiError(
      `HTTP ${response.status}: ${responseMessage(data, response.statusText)}`,
      { status: response.status, body: data },
    );
  }

  return data;
}
