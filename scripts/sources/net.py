"""작은 HTTP 도우미 (표준 라이브러리만). 오류 문구에 키가 섞이지 않게 URL 은 남기지 않는다."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

USER_AGENT = "PASSLOG-datapack/1.0 (static travel-data pack builder; GitHub Actions) python-urllib/3"


# 응답 하나 최대 크기 (상류가 비정상적으로 큰 응답을 주면 러너 메모리 · 시간을 쓰지 않고 멈춘다).
# 데이터셋 5MB 검증보다 앞에서 거른다. 페이지 감시(urlwatch)는 더 작은 상한을 넘긴다
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class FetchError(Exception):
    pass


def get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, retries: int = 3,
        method: str = "GET", data: Optional[bytes] = None,
        max_bytes: int = MAX_RESPONSE_BYTES) -> Tuple[int, Dict[str, str], bytes]:
    """(status, headers(소문자 키), body). 304 도 그대로 돌려준다. 네트워크 오류는 재시도 후 FetchError(URL 없음).
    본문이 max_bytes 를 넘으면 받다가 멈추고 FetchError("too large") (재시도하지 않는다)"""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=hdrs, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise FetchError(f"too large ({int(length)} > {max_bytes})")
                body = resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise FetchError(f"too large (> {max_bytes})")
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
            if e.code in (400, 401, 403, 404, 410):
                raise FetchError(f"HTTP {e.code}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = type(e).__name__
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(last or "network error")
