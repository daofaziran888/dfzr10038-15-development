"""分层派发的纯规则：候选去重、配额核算与层归属统计。

这里只做与存储无关的计算，seed、建批次和补名额共用同一套口径，
避免“建批次时一套算法、补名额时另一套算法”导致数量对不上。

模型约定：
- 候选记录按 `(策略, 层, 用户)` 保留，一个人的多个来源层就是多条记录；
- 派发和领取以**真实身份**（用户ID）为单位去重，一个人的整组记录要么一起
  进入同一个标注任务，要么都不进，保证观察关系不被拆散；
- 配额默认按身份数计算；如果配置了层配额，则每个来源层再各自受上限约束。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

USER_KEYS = ("用户ID", "用户id", "用户", "user_id", "userId", "uid", "id")
LAYER_KEYS = ("来源层", "层", "分层", "layers", "layer", "strata", "stratum")
STRATEGY_KEYS = ("策略编号", "策略", "采样策略", "strategy", "strategy_no")


def as_int(value: Any, default: int = 0) -> int:
    """把前端传来的字符串/数字统一成整数，非法值回落到默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        value = value[0] if value else ""
    return str(value).strip()


def split_list(value: Any) -> list[str]:
    """接受列表、JSON 数组字符串或 `A;B,C` 文本，保持顺序去重。"""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                parts = [str(item) for item in parsed]
            else:
                parts = [text]
        else:
            cleaned = text.replace("，", ",").replace("、", ",").replace("；", ";")
            for sep in (";", "|", "\n"):
                cleaned = cleaned.replace(sep, ",")
            parts = cleaned.split(",")
    result: list[str] = []
    for part in parts:
        item = str(part).strip()
        if item and item not in result:
            result.append(item)
    return result


def split_strategies(value: Any) -> list[str]:
    return split_list(value)


def normalize_members(value: Any) -> list[dict[str, Any]]:
    """把多种候选样本写法归一成 `[{用户ID, 来源层, 策略编号}]`。

    支持：
    - 字典列表，字段名允许中英文别名；
    - JSON 字符串形式的列表；
    - `"U1:A|B;U2:B"` 这类“用户:层|层”的紧凑写法；
    - 纯用户ID列表（层留空，由批次的分层字段兜底）。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return normalize_members(parsed)
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return normalize_members(parsed)
        members: list[dict[str, Any]] = []
        for chunk in text.replace("；", ";").split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk or "：" in chunk:
                user, layers = chunk.replace("：", ":").split(":", 1)
                for layer in split_list(layers):
                    members.append({"用户ID": user.strip(), "来源层": layer})
            else:
                for user in split_list(chunk):
                    members.append({"用户ID": user})
        return members
    if isinstance(value, dict):
        # 两种形态：{"U1": ["A","B"]} 或 {"用户ID": "...", "来源层": ...}
        if _first(value, USER_KEYS) is not None:
            return [_member_from_dict(value)]
        members = []
        for user, layers in value.items():
            for layer in split_list(layers):
                members.append({"用户ID": str(user), "来源层": layer})
        return members
    if isinstance(value, (list, tuple, set)):
        members = []
        for item in value:
            if isinstance(item, dict):
                members.extend(normalize_members(item))
            elif isinstance(item, str) and (":" in item or "：" in item):
                members.extend(normalize_members(item))
            else:
                for user in split_list(item):
                    members.append({"用户ID": user})
        return members
    return []


def _member_from_dict(item: dict[str, Any]) -> dict[str, Any]:
    """单条候选字典 → 统一结构；来源层保持列表，由调用方决定是否展开。"""
    return {
        "用户ID": _as_text(_first(item, USER_KEYS)),
        "来源层": split_list(_first(item, LAYER_KEYS)),
        "策略编号": _as_text(_first(item, STRATEGY_KEYS)),
    }


def normalize_member_list(value: Any) -> list[dict[str, Any]]:
    """展开后仍是字典列表，便于逐条落候选表。"""
    members = normalize_members(value)
    expanded: list[dict[str, Any]] = []
    for member in members:
        layers = split_list(member.get("来源层"))
        if not layers:
            expanded.append({"用户ID": member["用户ID"], "来源层": "", "策略编号": member.get("策略编号", "")})
        else:
            for layer in layers:
                expanded.append({"用户ID": member["用户ID"], "来源层": layer, "策略编号": member.get("策略编号", "")})
    return expanded


def group_candidates(members: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按真实身份分组，保留所有来源层和策略出处。

    返回顺序按用户首次出现的顺序，层按发生顺序去重；同一层重复出现只记一次，
    但候选记录本身仍保留在调用方的明细表里。
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for member in members:
        user_id = _as_text(member.get("用户ID"))
        if not user_id:
            continue
        group = groups.get(user_id)
        if group is None:
            group = {"用户ID": user_id, "来源层": [], "策略来源": []}
            groups[user_id] = group
            order.append(user_id)
        layer = _as_text(member.get("来源层"))
        if layer and layer not in group["来源层"]:
            group["来源层"].append(layer)
        strategy = _as_text(member.get("策略编号"))
        if strategy and strategy not in group["策略来源"]:
            group["策略来源"].append(strategy)
    return [groups[user_id] for user_id in order]


def remaining_quota(quota: Any, dispatched: Any, claimed: Any = 0) -> int:
    """剩余名额 = 配额 - 已派发 - 有效领取；失效领取不计入。"""
    return max(0, as_int(quota) - as_int(dispatched) - as_int(claimed))


def normalize_layer_quota(value: Any) -> dict[str, int]:
    """层配额支持 JSON 对象或 `A:3;B:2` 文本，值为非负整数。"""
    if not value:
        return {}
    data: Any = value
    if isinstance(value, str):
        text = value.strip()
        data = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                data = None
        if not isinstance(data, dict):
            data = {}
            for part in text.replace("；", ";").replace(",", ";").split(";"):
                if ":" in part or "：" in part:
                    key, raw = part.replace("：", ":").split(":", 1)
                    data[key.strip()] = as_int(raw)
    if not isinstance(data, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in data.items():
        layer = str(key).strip()
        if layer:
            result[layer] = max(0, as_int(raw))
    return result


def layer_usage(user_layer_pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """按层统计已占名额；同一用户的重复层只记一次。"""
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    for user_id, layer in user_layer_pairs:
        key = (str(user_id), str(layer))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        counts[key[1]] = counts.get(key[1], 0) + 1
    return counts


def layer_remaining(layer_quota: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    return {layer: max(0, quota - usage.get(layer, 0)) for layer, quota in layer_quota.items()}


def plan_topup(
    current_quota: Any,
    dispatched: Any,
    *,
    total: Any = None,
    increment: Any = None,
    claimed: Any = 0,
) -> tuple[dict[str, int] | None, str | None]:
    """总配额后补：可给总量也可给增量，不能低于已经分配出去的数量。"""
    allocated = max(0, as_int(dispatched) + as_int(claimed))
    current = max(0, as_int(current_quota))
    if total is None and increment is None:
        total_value = current
    elif total is None:
        total_value = current + as_int(increment)
    else:
        total_value = as_int(total)
    if total_value < allocated:
        return None, (
            f"配额 {total_value} 小于此前已分配 {allocated}，"
            "补名额只能增加、不能回退已派发的数量"
        )
    return {
        "配额": total_value,
        "已分配": allocated,
        "剩余配额": total_value - allocated,
    }, None


def plan_layer_topup(
    current: dict[str, int],
    usage: dict[str, int],
    *,
    totals: Any = None,
    increments: Any = None,
) -> tuple[dict[str, int] | None, str | None]:
    """层配额后补：按该层已占用数量核算，未提及的层保持原值。"""
    totals_map = normalize_layer_quota(totals) if totals else {}
    increments_map = normalize_layer_quota(increments) if increments else {}
    result = dict(current)
    for layer, value in totals_map.items():
        used = usage.get(layer, 0)
        if value < used:
            return None, f"层 {layer} 的配额 {value} 小于此前已分配 {used}"
        result[layer] = value
    for layer, value in increments_map.items():
        if value < 0:
            return None, f"层 {layer} 的追加名额不能为负数"
        result[layer] = max(result.get(layer, 0), usage.get(layer, 0)) + value
    return result, None


def stratum_attribution(user_layer_pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """按来源层统计样本记录数；一个多来源用户在每个层各记一次。"""
    return layer_usage(user_layer_pairs)


def independent_count(user_ids: Iterable[str]) -> int:
    """独立标注数：按真实身份去重，跨层重复入选只计一次。"""
    return len({str(user_id) for user_id in user_ids if str(user_id).strip()})
