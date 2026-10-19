#!/usr/bin/env python3
"""status.json 의 알림 → GitHub 이슈 (datapack.yml 에서만 실행, gh CLI · GH_TOKEN 필요).

  python3 -I scripts/report_issues.py --status public/packs/v2/status.json --run-url <url>
  python3 -I scripts/report_issues.py --workflow-failed --run-url <url>
  python3 -I scripts/report_issues.py --status … --dry-run          # gh 를 부르지 않고 할 일만 출력

- 알림마다 제목이 고정된 이슈 하나: "데이터 팩: <데이터셋> 실패" · "데이터 팩: <데이터셋> 거부" · "데이터 팩: 공식 페이지 변경 감지"
  이미 열려 있으면 20시간에 한 번만 댓글을 단다(실행마다 메일이 쏟아지지 않게).
- 실패 · 거부 이슈는 그 데이터셋이 다시 성공(연속 실패 0)하면 댓글을 달고 닫는다.
- 리포 소유자에게 GitHub 알림 메일이 간다. gh 가 없거나 실패해도 종료 코드 0 (데이터 배포를 막지 않음).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PREFIX = "데이터 팩: "
COMMENT_EVERY_HOURS = 20


def plan(status: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """(열거나 댓글 달 (제목, 본문) 목록, 닫을 제목 목록)"""
    opens: Dict[str, List[str]] = {}
    for a in status.get("alerts") or []:
        ds = a.get("dataset", "?")
        kind = a.get("kind", "failed")
        if kind == "changed":
            title = PREFIX + "공식 페이지 변경 감지"
        else:
            title = PREFIX + f"{ds} {'거부' if kind == 'rejected' else '실패'}"
        opens.setdefault(title, []).append(str(a.get("message", "")))
    closes = []
    for ds, row in (status.get("datasets") or {}).items():
        if isinstance(row, dict) and row.get("state") in ("updated", "unchanged") and not row.get("consecutiveFailures"):
            closes += [PREFIX + f"{ds} 실패", PREFIX + f"{ds} 거부"]
    items = [(t, "\n".join(f"- {m}" for m in msgs)) for t, msgs in opens.items()]
    return items, [c for c in closes if c not in opens]


def gh(args: List[str]) -> Optional[str]:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"::warning::gh 실행 실패: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"::warning::gh {' '.join(args[:2])} 실패: {r.stderr.strip()[:300]}", file=sys.stderr)
        return None
    return r.stdout


def open_issues() -> Dict[str, Dict[str, Any]]:
    out = gh(["issue", "list", "--state", "open", "--limit", "100", "--search", f"in:title \"{PREFIX.strip()}\"",
              "--json", "number,title,updatedAt"])
    if not out:
        return {}
    try:
        return {i["title"]: i for i in json.loads(out)}
    except (ValueError, KeyError, TypeError):
        return {}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="status.json 알림 → GitHub 이슈")
    ap.add_argument("--status", type=Path)
    ap.add_argument("--workflow-failed", action="store_true")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    items: List[Tuple[str, str]] = []
    closes: List[str] = []
    if args.workflow_failed:
        items.append((PREFIX + "워크플로 실패", "datapack 워크플로가 실패했어요. 로그를 확인하세요."))
    if args.status:
        try:
            status = json.loads(args.status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        a, c = plan(status)
        items += a
        closes += c
    if not items and not closes:
        print("알림 없음", file=sys.stderr)
        return 0
    if args.dry_run or not shutil.which("gh"):
        for t, b in items:
            print(f"[열기/댓글] {t}\n{b}")
        for t in closes:
            print(f"[닫기(열려 있으면)] {t}")
        return 0
    existing = open_issues()
    now = dt.datetime.now(dt.timezone.utc)
    for title, body in items:
        text = body + (f"\n\n실행: {args.run_url}" if args.run_url else "")
        issue = existing.get(title)
        if issue:
            try:
                updated = dt.datetime.fromisoformat(str(issue.get("updatedAt", "")).replace("Z", "+00:00"))
            except ValueError:
                updated = None
            if updated and (now - updated).total_seconds() < COMMENT_EVERY_HOURS * 3600:
                continue
            gh(["issue", "comment", str(issue["number"]), "--body", text])
        else:
            gh(["issue", "create", "--title", title, "--body", text])
    for title in closes:
        issue = existing.get(title)
        if issue:
            gh(["issue", "close", str(issue["number"]), "--comment", "다시 정상으로 돌아와 닫아요." +
                (f" ({args.run_url})" if args.run_url else "")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
