# SPDX-License-Identifier: GPL-3.0-only
"""Evidence checks for the explicit same-author reply collection flow.

Only a tag-accepted seed authorizes this flow.  A candidate's own hashtags are
never changed, and a metadata field claiming a thread root grants no access.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
MAX_DEFERRED_MEDIA = 256


def _id(value) -> str:
    value = str(value or "")
    return value if len(value) <= 24 and value.isascii() and value.isdigit() and value.strip("0") else ""


def post_context(metadata: dict) -> dict:
    # 只接受貼文本身的作者；timeline 的 user 可能只是頁面擁有者，不能借來認人。
    # gallery-dl's `user` can be the timeline owner even when a tweet's
    # actual author is unavailable. It must never prove reply authorship.
    author = metadata.get("author")
    if not isinstance(author, dict):
        raise ValueError("來源作者資料無效。")
    name = str(author.get("name") or author.get("screen_name") or "").lower()
    post_id = _id(metadata.get("tweet_id"))
    if not post_id or not _HANDLE.fullmatch(name):
        raise ValueError("來源缺少有效貼文編號或作者帳號。")
    return {"post_id": post_id, "author": name,
            "author_id": _id(author.get("id") or author.get("id_str") or author.get("rest_id")),
            "reply_id": _id(metadata.get("reply_id")),
            "conversation_id": _id(metadata.get("conversation_id")),
            "date": str(metadata.get("date") or "")}


def context_date(context: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(context["date"].replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except (KeyError, TypeError, ValueError):
        return None


def same_author(first: dict, second: dict) -> bool:
    # 任一方有數字作者 ID 就必須雙方一致；只有雙方皆缺 ID 才退回帳號比對。
    # If either author has a numeric ID, both must match; use handles only when both lack IDs.
    if first.get("author_id") or second.get("author_id"):
        return bool(first.get("author_id")) and first.get("author_id") == second.get("author_id")
    return bool(first.get("author")) and first.get("author") == second.get("author")


def same_context(first: dict, second: dict) -> bool:
    """A refreshed media row cannot borrow another post's stored membership."""
    # 重試時同時核對作者、回覆對象、討論串與日期，不能只憑同一貼文編號放行。
    # Retry evidence must match author, parent, conversation, and date, not just the post ID.
    return (first.get("post_id") == second.get("post_id") and same_author(first, second)
            and first.get("reply_id") == second.get("reply_id")
            and first.get("conversation_id") == second.get("conversation_id")
            and context_date(first) == context_date(second))


def reply_relation(seed: dict, candidate: dict, lookup) -> bool | None:
    """True is proven related; None needs more ancestry; False is unrelated."""
    # 起點是整串根貼文時可核對 conversation_id；起點本身是回覆時必須往上追鏈。
    # Root seeds can use conversation_id; reply seeds require a proven ancestor chain.
    if (seed.get("post_id") == candidate.get("post_id")
            or not candidate.get("reply_id") or not seed.get("conversation_id")
            or candidate.get("conversation_id") != seed.get("conversation_id")):
        return False
    if seed["conversation_id"] == seed["post_id"]:
        return True
    parent = candidate["reply_id"]
    # 同串的兄弟或祖先不算此起點的留言；迴圈與過長鏈也不授權下載。
    # Siblings and ancestors are excluded; cycles and excessive ancestry never authorize media.
    visited = {candidate["post_id"]}
    # Loop and pathological ancestry are rejected with constant working memory.
    for _ in range(1024):
        if parent == seed["post_id"]:
            return True
        if not parent or parent in visited:
            return False
        visited.add(parent)
        context = lookup(parent)
        if not context:
            return None
        if context.get("conversation_id") != seed["conversation_id"]:
            return False
        parent = context.get("reply_id")
    return False


def eligible_reply(seed: dict, candidate: dict, lookup) -> bool:
    """Only the seed author and proved descendants inherit the seed's tag."""
    # 標籤例外僅適用同作者、時間合理且已證明屬於該起點的後代留言。
    # The tag exception requires the same author, valid chronology, and proven descent from the seed.
    if not same_author(seed, candidate):
        return False
    started, replied = context_date(seed), context_date(candidate)
    if started is None or replied is None or replied < started:
        return False
    return reply_relation(seed, candidate, lookup) is True
