import { request, fetchJson } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type StratumCandidate = {
  id: number
  用户ID: string
  来源层: string
  策略编号: string
  状态: string
  领取编号: string
  派发批次: string
}

export type StratumClaim = {
  id: number
  领取编号: string
  值班员: string
  用户ID列表: string[]
  样本数: number
  层归属: Record<string, number>
  标注任务: string
  待办编号: string
  批次编号: string
  状态: string
  领取时间: string
}

export type StratumTodo = {
  id: number
  待办编号: string
  领取编号: string
  标注任务: string
  值班员: string
  样本数: number
  来源层: string
  status: string
  领取时间: string
}

export type StratumSummary = {
  派发编号: string
  候选人数: number
  配额: number
  已派发: number
  剩余配额: number
  层归属: Record<string, number>
  独立标注数: number
  有效领取数: number
  标注任务: string[]
  待办数: number
}

export type StratumCandidateResult = {
  dispatch: EntryRow
  items: StratumCandidate[]
  claims: StratumClaim[]
  todos: StratumTodo[]
}

export async function listCandidates(dispatchId: number): Promise<StratumCandidateResult> {
  return await fetchJson<StratumCandidateResult>(`/api/strata_dispatch/${dispatchId}/candidates`)
}

export async function getDispatchSummary(dispatchId: number): Promise<StratumSummary> {
  return await fetchJson<StratumSummary>(`/api/strata_dispatch/${dispatchId}/summary`)
}

export async function claimSamples(
  dispatchId: number,
  payload: {
    claimant: string
    count?: number | null
    token: string
    user_ids?: string[]
    mode?: string
  },
): Promise<ActionResult> {
  const response = await request(`/api/strata_dispatch/${dispatchId}/claims`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}

export async function voidClaim(dispatchId: number, claimNo: string): Promise<ActionResult> {
  const response = await request(`/api/strata_dispatch/${dispatchId}/void-claim`, {
    method: 'POST',
    body: JSON.stringify({ claim: claimNo }),
  })
  return await response.json()
}

export async function topUpQuota(
  dispatchId: number,
  payload: { increment?: number; total?: number },
): Promise<ActionResult> {
  const response = await request(`/api/strata_dispatch/${dispatchId}/top-up`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}
