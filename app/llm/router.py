"""按任务（看视频 / 写回复 / 敏感检查）选择模型，失败时切换到备用模型，并记账。"""
from __future__ import annotations

import logging

from ..db import Database
from .client import LLMError, LLMResult, chat, model_can_see

log = logging.getLogger(__name__)

TASK_LABELS = {"vision": "看视频", "write": "写评论和回复", "safety": "敏感内容检查", "fallback": "备用"}


class BudgetExceeded(Exception):
    pass


class Router:
    def __init__(self, db: Database):
        self.db = db

    def _resolve(self, task: str) -> tuple[dict, str]:
        tasks = self.db.tasks()
        cfg = tasks.get(task) or {}
        pid, model = cfg.get("provider"), cfg.get("model")
        if not pid or not model:
            # 没单独配置的任务，借用“写回复”的模型
            cfg = tasks.get("write") or {}
            pid, model = cfg.get("provider"), cfg.get("model")
        if not pid or not model:
            raise LLMError("还没有在“模型与 API Key”里给任务分配模型")
        p = self.db.provider(pid)
        if not p:
            raise LLMError(f"找不到服务商 {pid}")
        return p, model

    def _check_budget(self) -> None:
        limit = float(self.db.rules()["limits"].get("cost") or 0)
        if limit > 0 and float(self.db.usage()["cost"]) >= limit:
            raise BudgetExceeded("今日模型花费已到上限")

    def _account(self, r: LLMResult) -> None:
        self.db.add_usage("tokens", r.tokens)
        self.db.add_usage("cost", r.cost)

    def task_can_see(self, task: str) -> bool:
        """这个任务分配到的模型能不能看图。没配置时返回 False。"""
        try:
            p, model = self._resolve(task)
        except LLMError:
            return False
        return model_can_see(p, model)

    def call(self, task: str, system: str, user_text: str, images: list[bytes] | None = None,
             max_tokens: int = 600, temperature: float = 0.8) -> LLMResult:
        self._check_budget()
        p, model = self._resolve(task)
        if images and not model_can_see(p, model):
            images = None  # 分配的模型不能识图：只用文字判断
        try:
            r = chat(p, model, system, user_text, images, max_tokens, temperature)
            self._account(r)
            return r
        except LLMError as first:
            fb = self.db.tasks().get("fallback") or {}
            if not fb.get("provider") or not fb.get("model") or (fb["provider"], fb["model"]) == (p["id"], model):
                raise
            fp = self.db.provider(fb["provider"])
            if not fp:
                raise
            fb_images = images if images and model_can_see(fp, fb["model"]) else None
            log.warning("主模型失败，切换到备用模型：%s", first)
            r = chat(fp, fb["model"], system, user_text, fb_images, max_tokens, temperature)
            self._account(r)
            return r
