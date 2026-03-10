from typing import Tuple

from flask import jsonify, request

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

HTTP_ERROR_CODE_MAP = {
    400: 40001,
    401: 40101,
    403: 40301,
    404: 40401,
    422: 42201,
    409: 40901,
    410: 41001,
    500: 50001,
}


def is_api_path(path: str) -> bool:
    return (path or "").startswith("/api/")


def api_ok(payload=None, status: int = 200, **extra):
    data = {}
    if isinstance(payload, dict):
        data.update(payload)
    elif payload is not None:
        data["value"] = payload
    if extra:
        data.update(extra)
    body = {"code": 0, "message": "ok", "data": data}
    # Backward compatibility for legacy pages expecting flattened fields.
    body.update(data)
    body["ok"] = True
    return jsonify(body), status


def api_error(message: str, status: int = 400, code: str = None, **extra):
    err_code = code if code is not None else HTTP_ERROR_CODE_MAP.get(int(status), 50001)
    data = dict(extra or {})
    body = {"code": err_code, "message": message, "data": data}
    # Backward compatibility for legacy pages expecting `ok`/`error`.
    body["ok"] = False
    body["error"] = message
    if data:
        body.update(data)
    return jsonify(body), status


def parse_pagination(default_limit: int = DEFAULT_LIMIT, max_limit: int = MAX_LIMIT) -> Tuple[int, int]:
    limit_raw = request.args.get("limit", default=default_limit)
    offset_raw = request.args.get("offset", default=0)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(offset_raw)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    return limit, offset
