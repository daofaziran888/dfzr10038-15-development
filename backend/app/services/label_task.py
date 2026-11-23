"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class LabelTaskService(BaseModuleService):
    MODULE = "label_task"
    ENTITY = "标注任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "批次编号", "样本范围", "采样策略出处", "标注类型", "标注规范", "派发人数", "截止日期", "完成进度", "派发时间"]
    REQUIRED_FIELDS = ["任务编号", "样本范围", "标注类型"]
    STATUS_ORDER = ["待派发", "标注中", "已完成", "已回收"]
    ACTION_RULES = {"派发任务": "标注中", "回收任务": "已完成", "强制完成": "已回收"}
    NEGATIVE_ACTIONS = []
