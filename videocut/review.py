"""Bind manual exclusions to the exact video, profile revision and threshold."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


def review_signature(video, profile, threshold):
    path = Path(video).resolve()
    stat = path.stat()
    payload = [str(path), stat.st_size, stat.st_mtime_ns, profile, float(threshold)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def review_exclusions(spec, video, profile, threshold):
    review = spec.get('review')
    if review is None:
        return []
    if not isinstance(review, dict) or review.get('signature') != review_signature(video, profile, threshold):
        raise ValueError('手动排除记录已失效，录像、配置或阈值发生变化，请重新测试。')
    excluded = review.get('excluded', [])
    if not isinstance(excluded, list) or len(excluded) > 500:
        raise ValueError('手动排除记录无效。')
    excluded_ids = review.get('excluded_ids', [])
    if (not isinstance(excluded_ids, list) or len(excluded_ids) + len(excluded) > 500
            or any(not isinstance(ident, str) or re.fullmatch(r'[0-9a-f]{64}', ident) is None for ident in excluded_ids)):
        raise ValueError('手动排除事件标识无效。')
    result = [{'event_id': ident} for ident in dict.fromkeys(excluded_ids)]
    for span in excluded:
        if not isinstance(span, list) or len(span) != 2:
            raise ValueError('手动排除时间范围无效。')
        try:
            start, end = map(float, span)
        except (ValueError, TypeError):
            raise ValueError('手动排除时间范围无效。')
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end:
            raise ValueError('手动排除时间范围无效。')
        result.append([start, end])
    return result
