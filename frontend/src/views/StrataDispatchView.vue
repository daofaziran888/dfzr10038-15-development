<template>
  <section class="page dispatch-page" data-testid="strata-dispatch">
    <header class="page-head">
      <div>
        <h2>分层派发</h2>
        <p class="page-desc">
          同批多选出样后按真实身份去重切片：一个用户的多个来源层整组进入同一个标注任务，
          领取结果同步到标注任务与标注待办。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="loading" @click="reload">刷新</button>
      </div>
    </header>

    <div v-if="errorMessage" class="error-text" data-testid="dispatch-error">{{ errorMessage }}</div>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">配额</span>
        <strong class="stat-value" data-testid="stat-quota">{{ dispatch?.配额 ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已派发</span>
        <strong class="stat-value" data-testid="stat-dispatched">{{ dispatch?.已派发 ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">剩余配额</span>
        <strong class="stat-value" data-testid="stat-remaining">{{ dispatch?.剩余配额 ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">独立标注数</span>
        <strong class="stat-value" data-testid="stat-independent">{{ dispatch?.独立标注数 ?? '—' }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>派发批次</th>
          <th>采样策略</th>
          <th>分层字段</th>
          <th>候选人数</th>
          <th>配额</th>
          <th>已派发</th>
          <th>剩余配额</th>
          <th>层归属</th>
          <th>状态</th>
          <th>选择</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`dispatch-row-${row.派发编号}`"
          :class="{ selected: row.id === selectedId }"
        >
          <td>{{ row.派发编号 }}</td>
          <td>{{ row.采样策略 }}</td>
          <td>{{ row.分层字段 || '—' }}</td>
          <td>{{ row.候选人数 }}</td>
          <td>{{ row.配额 }}</td>
          <td>{{ row.已派发 }}</td>
          <td>{{ row.剩余配额 }}</td>
          <td>{{ formatLayers(row.层归属) }}</td>
          <td>{{ row.status }}</td>
          <td>
            <button class="link" type="button" @click="select(row)">查看</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="10" class="empty-state">暂无分层派发批次</td>
        </tr>
      </tbody>
    </table>

    <section v-if="dispatch" class="dispatch-detail">
      <h3>{{ dispatch.派发编号 }} · 领取与派发</h3>

      <div class="analysis-form">
        <label>
          值班员
          <input v-model="claimant" data-testid="claimant" placeholder="领取人" />
        </label>
        <label>
          本次领取数
          <input v-model.number="claimCount" data-testid="claim-count" type="number" min="0" />
        </label>
        <button class="btn primary" data-testid="claim" type="button" :disabled="busy" @click="claim(null)">
          批量派发
        </button>
        <button class="btn" data-testid="claim-all" type="button" :disabled="busy" @click="claim('全部')">
          全量整组派发
        </button>
        <button class="btn" data-testid="top-up" type="button" :disabled="busy" @click="topUp">
          补充名额
        </button>
      </div>

      <p class="dispatch-hint">
        同一样本只允许一个有效领取；两位值班员同时提交时，后到的那位会被拒绝。失效领取会释放样本并回退已派发计数。
      </p>

      <h4>候选样本（按真实身份去重，保留全部来源层）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>用户</th>
            <th>来源层</th>
            <th>策略出处</th>
            <th>状态</th>
            <th>领取编号</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in candidates"
            :key="item.id"
            :data-testid="`candidate-${item.用户ID}-${item.来源层}`"
            :data-status="item.状态"
          >
            <td>{{ item.用户ID }}</td>
            <td>{{ item.来源层 || '—' }}</td>
            <td>{{ item.策略编号 }}</td>
            <td>{{ item.状态 }}</td>
            <td>{{ item.领取编号 || '—' }}</td>
            <td>
              <button
                v-if="item.状态 === '待派发'"
                class="link"
                type="button"
                @click="claim(item.用户ID)"
              >
                指定领取
              </button>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h4>有效领取</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>领取编号</th>
            <th>值班员</th>
            <th>样本数</th>
            <th>层归属</th>
            <th>标注任务</th>
            <th>待办</th>
            <th>批次</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="claim in claims" :key="claim.id" :data-testid="`claim-${claim.领取编号}`">
            <td>{{ claim.领取编号 }}</td>
            <td>{{ claim.值班员 }}</td>
            <td>{{ claim.样本数 }}</td>
            <td>{{ formatLayers(claim.层归属) }}</td>
            <td>{{ claim.标注任务 }}</td>
            <td>{{ claim.待办编号 }}</td>
            <td>{{ claim.批次编号 }}</td>
            <td>{{ claim.状态 }}</td>
            <td>
              <button
                v-if="claim.状态 === '有效'"
                class="link"
                type="button"
                @click="voidClaimRow(claim.领取编号)"
              >
                标记失效
              </button>
            </td>
          </tr>
          <tr v-if="!claims.length">
            <td colspan="9" class="empty-state">还没有领取记录</td>
          </tr>
        </tbody>
      </table>

      <h4>同步到标注待办</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>待办编号</th>
            <th>标注任务</th>
            <th>值班员</th>
            <th>样本数</th>
            <th>来源层</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="todo.id" :data-testid="`todo-${todo.待办编号}`">
            <td>{{ todo.待办编号 }}</td>
            <td>{{ todo.标注任务 }}</td>
            <td>{{ todo.值班员 }}</td>
            <td>{{ todo.样本数 }}</td>
            <td>{{ todo.来源层 }}</td>
            <td>{{ todo.status }}</td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="6" class="empty-state">领取后会自动生成标注待办</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ rows.length }} 个派发批次</span>
      <span v-if="lastMessage" data-testid="dispatch-message">{{ lastMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  claimSamples,
  listCandidates,
  listEntries,
  topUpQuota,
  voidClaim,
} from '@/api/backend-service'
import type {
  StratumCandidate,
  StratumClaim,
  StratumTodo,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type DispatchRow = EntryRow & {
  派发编号: string
  采样策略: string
  分层字段: string
  候选人数: number
  配额: number
  已派发: number
  剩余配额: number
  层归属: Record<string, number>
}

const rows = ref<DispatchRow[]>([])
const dispatch = ref<DispatchRow | null>(null)
const candidates = ref<StratumCandidate[]>([])
const claims = ref<StratumClaim[]>([])
const todos = ref<StratumTodo[]>([])
const selectedId = ref<number | null>(null)
const claimant = ref('值班员甲')
const claimCount = ref<number | null>(null)
const loading = ref(false)
const busy = ref(false)
const errorMessage = ref('')
const lastMessage = ref('')

function formatLayers(layers: Record<string, number> | undefined): string {
  if (!layers) return '—'
  const entries = Object.entries(layers)
  return entries.length ? entries.map(([layer, count]) => `${layer}:${count}`).join('；') : '—'
}

function token(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

async function reload() {
  loading.value = true
  errorMessage.value = ''
  try {
    const payload = await listEntries('strata_dispatch')
    rows.value = payload.items as DispatchRow[]
    const keep = rows.value.find((row) => row.id === selectedId.value)
    const next = keep ?? rows.value[0] ?? null
    if (next) {
      await select(next)
    } else {
      dispatch.value = null
      candidates.value = []
      claims.value = []
      todos.value = []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分层派发读取失败'
  } finally {
    loading.value = false
  }
}

async function select(row: DispatchRow) {
  selectedId.value = row.id
  errorMessage.value = ''
  try {
    const payload = await listCandidates(row.id)
    dispatch.value = payload.dispatch as DispatchRow
    candidates.value = payload.items
    claims.value = payload.claims
    todos.value = payload.todos
    const index = rows.value.findIndex((item) => item.id === row.id)
    if (index >= 0) rows.value[index] = payload.dispatch as DispatchRow
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '候选明细读取失败'
  }
}

async function claim(userId: string | null) {
  if (!dispatch.value) return
  busy.value = true
  errorMessage.value = ''
  lastMessage.value = ''
  try {
    const mode = userId === '全部' ? '全部' : '整组'
    const result = await claimSamples(dispatch.value.id, {
      claimant: claimant.value || '值班员',
      count: userId ? null : claimCount.value,
      token: token('ui'),
      user_ids: userId && userId !== '全部' ? [userId] : undefined,
      mode,
    })
    if (!result.ok) throw new Error(result.message)
    lastMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
    await refreshSelected()
  } finally {
    busy.value = false
  }
}

async function voidClaimRow(claimNo: string) {
  if (!dispatch.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await voidClaim(dispatch.value.id, claimNo)
    if (!result.ok) throw new Error(result.message)
    lastMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失效领取失败'
  } finally {
    busy.value = false
  }
}

async function topUp() {
  if (!dispatch.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await topUpQuota(dispatch.value.id, { increment: 1 })
    if (!result.ok) throw new Error(result.message)
    lastMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补充名额失败'
  } finally {
    busy.value = false
  }
}

async function refreshSelected() {
  if (dispatch.value) await select(dispatch.value)
}

onMounted(reload)
</script>

<style scoped>
.dispatch-detail {
  margin-top: 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
}
.dispatch-detail h3 {
  margin: 0 0 12px;
  font-size: 15px;
}
.dispatch-detail h4 {
  margin: 18px 0 8px;
  font-size: 13px;
  color: var(--muted);
}
.dispatch-hint {
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 0;
}
.data-table tr.selected {
  background: #eef4ff;
}
</style>
