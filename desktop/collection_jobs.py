# SPDX-License-Identifier: GPL-3.0-only
"""Pure source planning for durable collection jobs; no account or network I/O."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from urllib.parse import quote

WINDOW_DAYS = 30
MAX_WINDOWS = 512


def source_spec(kind: str, url: str, name: str, **data) -> dict:
    # 來源鍵只由公開查詢產生，不含 Cookie 或本機登入檔案位置。
    # Source keys derive from public queries, never cookies or local login paths.
    digest = hashlib.sha256(f"{kind}:{url}".encode("utf-8")).hexdigest()[:32]
    return {"key": f"{kind}:{digest}", "kind": kind, "url": url, "name": name, "data": data}


def search_sources(tags: tuple[str, ...], since: date | None, windowed: bool, today: date) -> list[dict]:
    # 只有明確起始日期才分窗；未指定日期時維持一個最新搜尋，不暗設歷史起點。
    # Window only an explicit cutoff; an undated search has no invented historical start.
    windows = []
    if windowed and since is not None:
        end = today + timedelta(days=1)
        while end > since:
            start = max(since, end - timedelta(days=WINDOW_DAYS))
            windows.append((start, end))
            if len(windows) > MAX_WINDOWS:
                raise ValueError("搜尋日期區間過多，請縮短起始日期或關閉分段搜尋。")
            end = start
    else:
        windows = [(since, None)]
    sources = []
    for start, end in windows:
        for tag in tags:
            query = f"#{tag} filter:media"
            if start is not None:
                # X 的粗日期查詢留一天重疊，真正邊界由引擎依台灣日期逐篇判斷。
                # Overlap the coarse X date query; the engine enforces exact Taiwan dates.
                query += " since:" + (start - timedelta(days=1)).isoformat()
            if end is not None:
                query += " until:" + end.isoformat()
                name = f"標籤搜尋 #{tag}（{start.isoformat()}～{(end - timedelta(days=1)).isoformat()}）"
            else:
                name = f"標籤搜尋 #{tag}"
            sources.append(source_spec("search", "https://x.com/search?q=" + quote(query, safe="") + "&f=live",
                name, window_start=start.isoformat() if end else None, window_end=end.isoformat() if end else None))
    return sources


def job_fingerprint(public_options: dict, output_dir: str) -> str:
    # 變更目的目錄或收集條件就開新工作；配額、代理與登入更新可沿用未完成工作。
    # Changed destinations or collection scope start a new job; budget/routing/login changes can resume.
    value = json.dumps({"scope": public_options, "destination": output_dir}, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class JobLedger:
    """A worker-local view of persisted source checkpoints, not pagination cursors."""
    def __init__(self, store, fingerprint: str, options: dict, sources: list[dict], resume: bool):
        # 續跑沿用原工作保存的窗口，不因跨日重算而移動已完成區間。
        # Resume the original stored schedule, so a new day cannot shift completed windows.
        self.store = store
        old = store.find_resumable_job(fingerprint) if resume else None
        self.resumed = old is not None
        self.job = old or store.create_job(fingerprint, options, sources)
        self.job_id = self.job["job_id"]
        self.sources = {row["key"]: row for row in store.job_sources(self.job_id)}
        self.search_seen = store.job_search_seen(self.job_id)
        self.store.set_job_state(self.job_id, "running", "收集工作執行中。")

    def add(self, spec: dict) -> dict:
        if spec["key"] not in self.sources:
            self.store.add_job_sources(self.job_id, [spec])
            self.sources.update({row["key"]: row for row in self.store.job_sources(self.job_id)})
        return self.sources[spec["key"]]

    def state(self, key: str, state: str, message: str = "", progress: dict | None = None):
        self.store.set_source_state(self.job_id, key, state, message, progress)
        self.sources[key]["state"] = state
        self.sources[key]["message"] = message
        if progress is not None:
            self.sources[key]["progress"] = progress

    def remember_search(self, post_id: str):
        self.store.record_job_search_seen(self.job_id, post_id)
        self.search_seen.add(post_id)

    def author(self, author: str):
        self.store.record_job_author(self.job_id, author)

    def finish(self, stopped: bool, has_failures: bool = False) -> list[dict]:
        # 完成的來源不重掃；未完成來源下次從頭列舉並靠媒體紀錄去重，並非分頁續傳。
        # Completed sources stay skipped; unfinished ones rescan with media dedup, not cursor resume.
        unfinished = [row for row in self.sources.values() if row["state"] != "completed"]
        state = "stopped" if stopped else "partial" if unfinished or has_failures else "completed"
        message = {"stopped": "工作已停止；未完成來源可續跑。", "partial": "部分來源尚未完成，可稍後續跑。",
                   "completed": "本次來源列舉已完成；X 可見範圍仍有限。"}[state]
        self.store.set_job_state(self.job_id, state, message)
        return unfinished
