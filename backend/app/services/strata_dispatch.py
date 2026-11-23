"""分层派发：把多个分层的候选样本按真实身份去重后，按配额切片送入标注任务。

业务约束（与页面动作、接口参数一一对应）：
- 候选按 `(策略, 层, 用户)` 保留，同一个人的多条层记录在分组时合并来源层；
- 派发/领取以真实身份为单位，整组进出同一个标注任务，不拆散观察关系；
- 配额默认按身份计数，可选配层配额；后补名额只能增加，且不低于已分配数量；
- 同一身份同时只允许一个有效领取，重复提交按 token 幂等；
- 领取失效后释放样本并重算已派发，不留下虚增的计数；
- 每次有效领取生成标注任务（批次、样本范围、采样策略出处）和标注待办。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any

from app.store import store
from app.strata_rules import (
    as_int,
    group_candidates,
    independent_count,
    layer_remaining,
    layer_usage,
    normalize_layer_quota,
    normalize_member_list,
    plan_layer_topup,
    plan_topup,
    split_list,
    split_strategies,
    stratum_attribution,
)

CANDIDATE_TABLE = "_strata_candidates"
CLAIM_TABLE = "_strata_claims"
TODO_MODULE = "label_todo"
CANDIDATE_STATUS = {"待派发", "已领取"}
CLAIM_LIVE = "有效"
CLAIM_DEAD = "已失效"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _next_seq(rows: list[dict[str, Any]], field: str, prefix: str) -> int:
    """从 `PREFIX-0001` 这类编号里取最大序号，保证新建记录不撞号。"""
    best = 0
    for row in rows:
        raw = str(row.get(field) or "")
        if raw.startswith(prefix):
            tail = raw[len(prefix):].lstrip("-")
            best = max(best, as_int(tail))
    return best


def _next_code(rows: list[dict[str, Any]], field: str, prefix: str, width: int = 4) -> str:
    """生成下一个可用编号：前缀 + 最大序号加一，并跳过已被占用的编号。"""
    taken = {str(row.get(field) or "") for row in rows}
    seq = _next_seq(rows, field, prefix) + 1
    while f"{prefix}{seq:0{width}d}" in taken:
        seq += 1
    return f"{prefix}{seq:0{width}d}"


class StrataDispatchService:
    MODULE = "strata_dispatch"
    ENTITY = "分层派发"
    LIST_FIELDS = [
        "派发编号", "采样策略", "分层字段", "候选人数", "配额",
        "已派发", "剩余配额", "来源层", "派发批次", "最后更新",
    ]
    REQUIRED_FIELDS = ["采样策略", "候选样本"]
    STATUS_ORDER = ["待派发", "部分派发", "已派发", "已关闭"]
    ACTION_RULES = {
        "批量派发": "部分派发",
        "补充名额": "待派发",
        "失效领取": "待派发",
        "关闭派发": "已关闭",
    }
    NEGATIVE_ACTIONS: list[str] = []
    DEFAULT_CLAIMANT = "数据工程组"

    # 同一进程内串行化“查余量→选样本→记领取”，两位值班员同时提交时
    # 只有先进入临界区的那一位能拿到同一身份。
    _LOCK = threading.RLock()

    # ------------------------------------------------------------------
    # 候选与批次
    # ------------------------------------------------------------------

    def list_entries(self, *, filters=None, keyword=None, status=None, page=1, size=20):
        rows = list(store.rows(self.MODULE))
        for row in rows:
            self._refresh(row)
        return super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size
        )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, entry_id)
        if entry is not None:
            self._refresh(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        strategies = split_strategies(
            values.get("采样策略") or values.get("采样策略出处") or values.get("strategy")
        )
        raw_members = (
            values.get("候选样本")
            or values.get("候选")
            or values.get("members")
            or values.get("用户ID列表")
        )
        members = normalize_member_list(raw_members)
        missing: list[str] = []
        if not strategies:
            missing.append("采样策略")
        if not members:
            missing.append("候选样本")
        if missing:
            return None, missing

        default_strategy = strategies[0]
        default_layer = str(
            values.get("默认层") or values.get("默认分层") or "未分层"
        ).strip() or "未分层"
        cleaned: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for member in members:
            user_id = str(member.get("用户ID") or "").strip()
            if not user_id:
                continue
            layer = str(member.get("来源层") or default_layer).strip() or default_layer
            key = (user_id, layer)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append({
                "用户ID": user_id,
                "来源层": layer,
                "策略编号": str(member.get("策略编号") or default_strategy),
            })
        if not cleaned:
            return None, ["候选样本"]

        rows = store.rows(self.MODULE)
        entry_id = max((as_int(row.get("id")) for row in rows), default=0) + 1
        dispatch_no = str(values.get("派发编号") or "").strip()
        if not dispatch_no or any(row.get("派发编号") == dispatch_no for row in rows):
            dispatch_no = _next_code(rows, "派发编号", "DSPT-")

        groups = group_candidates(cleaned)
        quota = values.get("配额")
        if quota in (None, ""):
            quota = len(groups)
        quota = max(0, as_int(quota))
        layer_quota = normalize_layer_quota(
            values.get("层配额") or values.get("分层配额")
        )
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "派发编号": dispatch_no,
            "采样策略": ";".join(strategies),
            "分层字段": str(values.get("分层字段") or "").strip(),
            "候选人数": independent_count(member["用户ID"] for member in groups),
            "配额": quota,
            "已派发": 0,
            "剩余配额": quota,
            "来源层": ";".join(
                dict.fromkeys(member["来源层"] for member in cleaned if member["来源层"])
            ),
            "派发批次": "",
            "最后更新": _now(),
            "层配额": layer_quota,
            "层归属": {},
            "独立标注数": 0,
            "有效领取数": 0,
            "补额步长": max(1, as_int(values.get("补额步长"), 1)),
            "默认值班员": str(values.get("默认值班员") or self.DEFAULT_CLAIMANT).strip()
            or self.DEFAULT_CLAIMANT,
        }
        rows.append(entry)

        candidates = store.rows(CANDIDATE_TABLE)
        next_candidate_id = max((as_int(row.get("id")) for row in candidates), default=0) + 1
        for order, member in enumerate(cleaned):
            candidates.append({
                "id": next_candidate_id,
                "派发ID": entry_id,
                "派发编号": dispatch_no,
                "用户ID": member["用户ID"],
                "来源层": member["来源层"],
                "策略编号": member["策略编号"],
                "状态": "待派发",
                "领取编号": "",
                "派发批次": "",
                "顺序": order,
            })
            next_candidate_id += 1
        self._refresh(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def candidates(self, dispatch_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in store.rows(CANDIDATE_TABLE)
            if as_int(row.get("派发ID")) == dispatch_id
        ]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return sorted(rows, key=lambda row: (as_int(row.get("顺序")), as_int(row.get("id"))))

    def claims(self, dispatch_id: int, *, include_void: bool = True) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in store.rows(CLAIM_TABLE)
            if as_int(row.get("派发ID")) == dispatch_id
            and (include_void or row.get("状态") == CLAIM_LIVE)
        ]
        return sorted(rows, key=lambda row: as_int(row.get("id")))

    def todos(self, dispatch_id: int) -> list[dict[str, Any]]:
        dispatch_no = self._dispatch_no(dispatch_id)
        rows = [
            dict(row)
            for row in store.rows(TODO_MODULE)
            if str(row.get("派发编号") or "") == dispatch_no
        ]
        return sorted(rows, key=lambda row: as_int(row.get("id")))

    def summary(self, dispatch_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(dispatch_id)
        if entry is None:
            return None
        live = self.claims(dispatch_id, include_void=False)
        users = self._claimed_users(dispatch_id)
        pairs = self._claimed_pairs(dispatch_id)
        return {
            "派发编号": entry.get("派发编号"),
            "候选人数": entry.get("候选人数"),
            "配额": entry.get("配额"),
            "已派发": entry.get("已派发"),
            "剩余配额": entry.get("剩余配额"),
            "层归属": stratum_attribution(pairs),
            "独立标注数": independent_count(users),
            "有效领取数": len(live),
            "标注任务": sorted({str(claim.get("标注任务")) for claim in live if claim.get("标注任务")}),
            "待办数": len(self.todos(dispatch_id)),
        }

    # ------------------------------------------------------------------
    # 领取 / 派发
    # ------------------------------------------------------------------

    def claim(
        self,
        dispatch_id: int,
        *,
        claimant: str = DEFAULT_CLAIMANT,
        count: int | None = None,
        mode: str = "整组",
        token: str | None = None,
        user_ids: Any = None,
        label_type: str | None = None,
        label_spec: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with self._LOCK:
            entry = store.find(self.MODULE, dispatch_id)
            if entry is None:
                return None, f"{self.ENTITY} {dispatch_id} 不存在或已归档"
            if entry.get("status") == "已关闭":
                return None, f"{self.ENTITY} {entry.get('派发编号')} 已关闭，不能继续领取"

            claimant = str(claimant or entry.get("默认值班员") or self.DEFAULT_CLAIMANT).strip()
            if count is not None and as_int(count) <= 0:
                return None, "本次领取数必须大于 0；不传数量表示按剩余配额整组派发"
            if token:
                existing = self._claim_by_token(dispatch_id, token)
                if existing is not None:
                    self._refresh(entry)
                    return existing, f"领取 {existing.get('领取编号')} 已存在，未重复占用名额"

            self._refresh(entry)
            candidates = self.candidates(dispatch_id)
            valid_claims = self.claims(dispatch_id, include_void=False)
            claimed_users = {
                str(user) for claim in valid_claims for user in claim.get("用户ID列表", [])
            }
            groups = group_candidates(candidates)
            group_by_user = {str(group["用户ID"]): group for group in groups}
            if not group_by_user:
                return None, f"{entry.get('派发编号')} 没有候选样本"

            explicit = [str(user).strip() for user in split_list(user_ids) if str(user).strip()]
            if explicit:
                missing = [user for user in explicit if user not in group_by_user]
                if missing:
                    return None, f"候选样本中没有用户：{'、'.join(missing)}"
                conflicts = [user for user in explicit if user in claimed_users]
                if conflicts:
                    return None, (
                        "以下用户已被其他有效领取持有，同一样本只能有一个领取者："
                        + "、".join(conflicts)
                    )
                selected = [
                    group_by_user[user]
                    for user in dict.fromkeys(explicit)
                ]
                if count is not None and len(selected) > max(0, as_int(count)):
                    return None, (
                        f"指定了 {len(selected)} 个用户，但本次只允许领取 {as_int(count)} 个；"
                        "请减少名单或放开领取数"
                    )
                problem = self._quota_problem(entry, selected, claimed_users)
                if problem:
                    return None, problem
            else:
                selected, problem = self._select_groups(
                    entry,
                    groups,
                    claimed_users,
                    count=count,
                    mode=mode,
                )
                if problem:
                    return None, problem
                if not selected:
                    return None, self._empty_reason(entry, groups, claimed_users)

            batch_no = self._next_batch_no(entry)
            task, todo, claim_row = self._materialize(
                entry,
                selected,
                claimant=claimant,
                batch_no=batch_no,
                token=token,
                label_type=label_type,
                label_spec=label_spec,
            )
            self._refresh(entry)
            return claim_row, (
                f"{claimant} 领取 {len(selected)} 个独立样本，"
                f"已派发到标注任务 {task.get('任务编号')}，"
                f"待办 {todo.get('待办编号')}，剩余配额 {entry.get('剩余配额')}"
            )

    def dispatch(self, dispatch_id: int, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        """批量派发的语义等同一次系统领取，保留独立入口便于接口表达。"""
        kwargs.setdefault("claimant", self.DEFAULT_CLAIMANT)
        return self.claim(dispatch_id, **kwargs)

    def void_claim(
        self, dispatch_id: int, claim_ref: str
    ) -> tuple[dict[str, Any] | None, str]:
        with self._LOCK:
            entry = store.find(self.MODULE, dispatch_id)
            if entry is None:
                return None, f"{self.ENTITY} {dispatch_id} 不存在或已归档"
            ref = str(claim_ref or "").strip()
            claim_row = self._find_claim(dispatch_id, ref)
            if claim_row is None:
                return None, f"领取 {ref} 不存在"
            if claim_row.get("状态") == CLAIM_DEAD:
                self._refresh(entry)
                return claim_row, f"领取 {claim_row.get('领取编号')} 已失效，计数未重复回退"

            claim_no = str(claim_row.get("领取编号"))
            claim_row["状态"] = CLAIM_DEAD
            claim_row["失效时间"] = _now()
            for row in store.rows(CANDIDATE_TABLE):
                if (
                    as_int(row.get("派发ID")) == dispatch_id
                    and str(row.get("领取编号") or "") == claim_no
                ):
                    row["状态"] = "待派发"
                    row["领取编号"] = ""
                    row["派发批次"] = ""
            for todo in store.rows(TODO_MODULE):
                if str(todo.get("领取编号") or "") == claim_no:
                    todo["状态"] = "已失效"
                    todo["pending"] = False
            task = self._find_label_task(str(claim_row.get("标注任务") or ""))
            if task is not None:
                task["status"] = "已回收"
                task["pending"] = False
            entry["最后更新"] = _now()
            self._refresh(entry)
            return claim_row, (
                f"领取 {claim_no} 已失效，已派发回退为 {entry.get('已派发')}，"
                f"剩余配额 {entry.get('剩余配额')}"
            )

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------

    def top_up(
        self,
        dispatch_id: int,
        *,
        total: int | None = None,
        increment: int | None = None,
        layer_totals: Any = None,
        layer_increments: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with self._LOCK:
            entry = store.find(self.MODULE, dispatch_id)
            if entry is None:
                return None, f"{self.ENTITY} {dispatch_id} 不存在或已归档"
            self._refresh(entry)
            dispatched = as_int(entry.get("已派发"))
            plan, problem = plan_topup(
                entry.get("配额"), dispatched, total=total, increment=increment
            )
            if problem:
                return None, problem

            current_layers = normalize_layer_quota(entry.get("层配额"))
            usage = self._layer_usage(dispatch_id)
            layers, problem = plan_layer_topup(
                current_layers,
                usage,
                totals=layer_totals,
                increments=layer_increments,
            )
            if problem:
                return None, problem

            entry["配额"] = plan["配额"]
            entry["层配额"] = layers or {}
            entry["最后更新"] = _now()
            self._refresh(entry)
            return entry, (
                f"配额已补充到 {plan['配额']}（此前已分配 {plan['已分配']}，"
                f"剩余 {plan['剩余配额']}）"
            )

    # ------------------------------------------------------------------
    # 页面动作
    # ------------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        action = str(action or "").strip()

        if action == "批量派发":
            claim_row, message = self.claim(
                entry_id, claimant=str(entry.get("默认值班员") or self.DEFAULT_CLAIMANT)
            )
            self._refresh(entry)
            return (entry if claim_row is not None else None), message
        if action == "补充名额":
            step = max(1, as_int(entry.get("补额步长"), 1))
            updated, message = self.top_up(entry_id, increment=step)
            return updated, message
        if action == "失效领取":
            live = self.claims(entry_id, include_void=False)
            if not live:
                return entry, "当前没有有效领取可失效"
            claim_row, message = self.void_claim(entry_id, str(live[-1].get("领取编号")))
            self._refresh(entry)
            return (entry if claim_row is not None else None), message
        if action == "关闭派发":
            entry["status"] = "已关闭"
            entry["pending"] = False
            entry["最后更新"] = _now()
            return entry, f"{self.ENTITY} {entry.get('派发编号')} 已关闭"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        for row in rows:
            self._refresh(row)
        claimed = self._claimed_users(None)
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": {
                status: sum(1 for row in rows if row.get("status") == status)
                for status in self.STATUS_ORDER
            },
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
            "独立样本数": independent_count(claimed),
        }

    # ------------------------------------------------------------------
    # 内部：选择与落库
    # ------------------------------------------------------------------

    def _select_groups(
        self,
        entry: dict[str, Any],
        groups: list[dict[str, Any]],
        claimed_users: set[str],
        *,
        count: int | None,
        mode: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        total_remaining = max(0, as_int(entry.get("配额")) - as_int(entry.get("已派发")))
        layer_quota = normalize_layer_quota(entry.get("层配额"))
        usage = self._layer_usage(int(entry["id"]))
        layer_left = layer_remaining(layer_quota, usage) if layer_quota else {}
        limit = None if count in (None, "") else max(0, as_int(count))
        selected: list[dict[str, Any]] = []
        skipped: list[str] = []

        for group in groups:
            user = str(group["用户ID"])
            if user in claimed_users:
                continue
            if limit is not None and len(selected) >= limit:
                break
            if total_remaining - len(selected) <= 0:
                skipped.append(user)
                if mode == "严格":
                    break
                continue
            if layer_quota:
                blocked = [
                    layer
                    for layer in group.get("来源层", [])
                    if layer in layer_left and layer_left[layer] <= 0
                ]
                if blocked:
                    # 整组搬走：只要某个来源层没有名额，这个人的所有层记录一起留在原地。
                    skipped.append(user)
                    if mode == "严格":
                        break
                    continue
            selected.append(group)
            for layer in group.get("来源层", []):
                if layer in layer_left:
                    layer_left[layer] -= 1

        if mode in ("全部", "全量") and skipped:
            return [], (
                f"配额不足以整组放下全部候选：剩余可派 {total_remaining} 个身份，"
                f"还有 {len(skipped)} 个身份未排入，已保持原样不派发"
            )
        return selected, None

    def _quota_problem(
        self,
        entry: dict[str, Any],
        selected: list[dict[str, Any]],
        claimed_users: set[str],
    ) -> str | None:
        remaining = max(0, as_int(entry.get("配额")) - as_int(entry.get("已派发")))
        if len(selected) > remaining:
            return (
                f"配额不足：指定 {len(selected)} 个独立样本，"
                f"当前只剩 {remaining} 个名额"
            )
        layer_quota = normalize_layer_quota(entry.get("层配额"))
        if not layer_quota:
            return None
        usage = self._layer_usage(int(entry["id"]))
        needed: dict[str, int] = {}
        for group in selected:
            for layer in group.get("来源层", []):
                if layer in layer_quota:
                    needed[layer] = needed.get(layer, 0) + 1
        for layer, need in needed.items():
            left = max(0, layer_quota[layer] - usage.get(layer, 0))
            if need > left:
                return f"层 {layer} 配额不足：需要 {need} 个，只剩 {left} 个"
        return None

    def _empty_reason(
        self,
        entry: dict[str, Any],
        groups: list[dict[str, Any]],
        claimed_users: set[str],
    ) -> str:
        remaining = max(0, as_int(entry.get("配额")) - as_int(entry.get("已派发")))
        if remaining <= 0:
            return f"配额已用完（配额 {entry.get('配额')}），请先补充名额再领取"
        unclaimed = [group for group in groups if str(group["用户ID"]) not in claimed_users]
        if not unclaimed:
            return "候选样本都已被有效领取，没有可派发的身份"
        layer_quota = normalize_layer_quota(entry.get("层配额"))
        if layer_quota:
            return "各来源层的剩余名额不足，当前的候选组无法整组派发"
        return "没有满足派发条件的候选样本"

    def _materialize(
        self,
        entry: dict[str, Any],
        selected: list[dict[str, Any]],
        *,
        claimant: str,
        batch_no: str,
        token: str | None,
        label_type: str | None,
        label_spec: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        dispatch_id = int(entry["id"])
        users = [str(group["用户ID"]) for group in selected]
        user_set = set(users)
        pairs = [
            (str(row["用户ID"]), str(row["来源层"]))
            for row in self.candidates(dispatch_id)
            if str(row["用户ID"]) in user_set and row.get("来源层")
        ]
        layers = stratum_attribution(pairs)
        detail = self._user_layer_detail(dispatch_id, users)

        claims = store.rows(CLAIM_TABLE)
        claim_id = max((as_int(row.get("id")) for row in claims), default=0) + 1
        claim_no = _next_code(claims, "领取编号", "CLAI-")

        task = self._create_label_task(
            entry,
            users,
            layers,
            batch_no=batch_no,
            claimant=claimant,
            label_type=label_type,
            label_spec=label_spec,
        )
        todo = self._create_todo(
            entry,
            claim_no=claim_no,
            task_no=str(task.get("任务编号")),
            claimant=claimant,
            users=users,
            layers=layers,
        )

        claim_row = {
            "id": claim_id,
            "领取编号": claim_no,
            "派发ID": dispatch_id,
            "派发编号": entry.get("派发编号"),
            "值班员": claimant,
            "token": token or "",
            "用户ID列表": users,
            "样本数": len(users),
            "层归属": layers,
            "样本层明细": detail,
            "标注任务": task.get("任务编号"),
            "待办编号": todo.get("待办编号"),
            "批次编号": batch_no,
            "状态": CLAIM_LIVE,
            "领取时间": _now(),
            "失效时间": "",
        }
        claims.append(claim_row)

        for row in store.rows(CANDIDATE_TABLE):
            if as_int(row.get("派发ID")) != dispatch_id:
                continue
            if str(row.get("用户ID")) in user_set and row.get("状态") == "待派发":
                row["状态"] = "已领取"
                row["领取编号"] = claim_no
                row["派发批次"] = batch_no
        entry["派发批次"] = batch_no
        entry["最后更新"] = _now()
        return task, todo, claim_row

    def _create_label_task(
        self,
        entry: dict[str, Any],
        users: list[str],
        layers: dict[str, int],
        *,
        batch_no: str,
        claimant: str,
        label_type: str | None,
        label_spec: str | None,
    ) -> dict[str, Any]:
        rows = store.rows("label_task")
        task_id = max((as_int(row.get("id")) for row in rows), default=0) + 1
        task_no = _next_code(rows, "任务编号", "LABE-")
        deadline = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
        task = {
            "id": task_id,
            "status": "标注中",
            "pending": True,
            "abnormal": False,
            "任务编号": task_no,
            "批次编号": batch_no,
            "样本范围": ",".join(users),
            "采样策略出处": f"{entry.get('采样策略')}（{entry.get('派发编号')}）",
            "标注类型": str(label_type or "分层标注"),
            "标注规范": str(label_spec or "同一身份整组标注，保留跨层观察关系"),
            "派发人数": str(len(users)),
            "截止日期": deadline,
            "完成进度": "0%",
            "派发时间": _now(),
            "分层派发编号": entry.get("派发编号"),
            "领取人": claimant,
            "层归属": self._format_layers(layers),
            "独立标注数": len(users),
        }
        rows.append(task)
        return task

    def _create_todo(
        self,
        entry: dict[str, Any],
        *,
        claim_no: str,
        task_no: str,
        claimant: str,
        users: list[str],
        layers: dict[str, int],
    ) -> dict[str, Any]:
        rows = store.rows(TODO_MODULE)
        todo_id = max((as_int(row.get("id")) for row in rows), default=0) + 1
        todo_no = _next_code(rows, "待办编号", "TODO-")
        todo = {
            "id": todo_id,
            "status": "待标注",
            "pending": True,
            "abnormal": False,
            "待办编号": todo_no,
            "领取编号": claim_no,
            "派发编号": entry.get("派发编号"),
            "标注任务": task_no,
            "值班员": claimant,
            "样本数": len(users),
            "来源层": self._format_layers(layers),
            "领取时间": _now(),
            "层归属": layers,
            "用户ID列表": ",".join(users),
        }
        rows.append(todo)
        return todo

    # ------------------------------------------------------------------
    # 内部：统计与查找
    # ------------------------------------------------------------------

    def _refresh(self, entry: dict[str, Any]) -> None:
        dispatch_id = as_int(entry.get("id"))
        candidates = self.candidates(dispatch_id)
        users = self._claimed_users(dispatch_id)
        pairs = self._claimed_pairs(dispatch_id)
        dispatched = independent_count(users)
        paired = stratum_attribution(pairs)
        entry["已派发"] = dispatched
        entry["剩余配额"] = max(0, as_int(entry.get("配额")) - dispatched)
        entry["层归属"] = paired
        entry["独立标注数"] = dispatched
        entry["有效领取数"] = len(self.claims(dispatch_id, include_void=False))
        entry["来源层"] = ";".join(
            dict.fromkeys(str(row.get("来源层")) for row in candidates if row.get("来源层"))
        )
        if entry.get("status") != "已关闭":
            available = [
                row
                for row in candidates
                if str(row.get("用户ID")) not in users and row.get("状态") == "待派发"
            ]
            if dispatched == 0:
                entry["status"] = "待派发"
                entry["pending"] = True
            elif not available:
                entry["status"] = "已派发"
                entry["pending"] = False
            else:
                entry["status"] = "部分派发"
                entry["pending"] = True

    def _claimed_users(self, dispatch_id: int | None) -> list[str]:
        users: list[str] = []
        for claim in store.rows(CLAIM_TABLE):
            if claim.get("状态") != CLAIM_LIVE:
                continue
            if dispatch_id is not None and as_int(claim.get("派发ID")) != dispatch_id:
                continue
            for user in claim.get("用户ID列表", []):
                if str(user) not in users:
                    users.append(str(user))
        return users

    def _claimed_pairs(self, dispatch_id: int | None) -> list[tuple[str, str]]:
        users = set(self._claimed_users(dispatch_id))
        if not users:
            return []
        pairs: list[tuple[str, str]] = []
        for row in store.rows(CANDIDATE_TABLE):
            if dispatch_id is not None and as_int(row.get("派发ID")) != dispatch_id:
                continue
            if str(row.get("用户ID")) in users and row.get("来源层"):
                pairs.append((str(row.get("用户ID")), str(row.get("来源层"))))
        return pairs

    def _layer_usage(self, dispatch_id: int) -> dict[str, int]:
        return layer_usage(self._claimed_pairs(dispatch_id))

    def _user_layer_detail(self, dispatch_id: int, users: list[str]) -> dict[str, list[str]]:
        """每个身份的完整来源层，落进领取记录与标注任务，补齐后仍能还原层归属。"""
        detail: dict[str, list[str]] = {user: [] for user in users}
        for row in store.rows(CANDIDATE_TABLE):
            if as_int(row.get("派发ID")) != dispatch_id:
                continue
            user = str(row.get("用户ID"))
            if user not in detail:
                continue
            layer = str(row.get("来源层") or "")
            if layer and layer not in detail[user]:
                detail[user].append(layer)
        return detail

    def _claim_by_token(self, dispatch_id: int, token: str) -> dict[str, Any] | None:
        for row in store.rows(CLAIM_TABLE):
            if (
                as_int(row.get("派发ID")) == dispatch_id
                and str(row.get("token") or "") == token
                and row.get("状态") == CLAIM_LIVE
            ):
                return dict(row)
        return None

    def _find_claim(self, dispatch_id: int, ref: str) -> dict[str, Any] | None:
        if not ref:
            return None
        for row in store.rows(CLAIM_TABLE):
            if as_int(row.get("派发ID")) != dispatch_id:
                continue
            if ref in {str(row.get("领取编号")), str(row.get("token"))}:
                return row
        return None

    def _find_label_task(self, task_no: str) -> dict[str, Any] | None:
        for row in store.rows("label_task"):
            if str(row.get("任务编号")) == task_no:
                return row
        return None

    def _dispatch_no(self, dispatch_id: int) -> str:
        entry = store.find(self.MODULE, dispatch_id)
        return str(entry.get("派发编号") if entry else "")

    def _next_batch_no(self, entry: dict[str, Any]) -> str:
        count = sum(
            1
            for row in store.rows(CLAIM_TABLE)
            if as_int(row.get("派发ID")) == as_int(entry.get("id"))
        )
        return f"{entry.get('派发编号')}-B{count + 1:02d}"

    @staticmethod
    def _format_layers(layers: dict[str, int]) -> str:
        return ";".join(f"{layer}:{count}" for layer, count in layers.items())
