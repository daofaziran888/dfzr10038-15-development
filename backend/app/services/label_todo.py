"""标注待办：由分层领取同步生成，值班员从这里进入标注任务。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class LabelTodoService(BaseModuleService):
    MODULE = "label_todo"
    ENTITY = "标注待办"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["待办编号", "派发编号", "标注任务", "值班员", "样本数", "来源层", "领取时间", "领取编号"]
    REQUIRED_FIELDS = ["待办编号", "标注任务"]
    STATUS_ORDER = ["待标注", "标注中", "已完成", "已失效"]
    ACTION_RULES = {"开始标注": "标注中", "完成标注": "已完成"}
    NEGATIVE_ACTIONS = []
