import {
  cn,
  haptic,
  host,
  Tip,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'codex-usage'

const compact = value => {
  const number = Number(value || 0)
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: number >= 1000000 ? 1 : 0,
    notation: number >= 10000 ? 'compact' : 'standard'
  }).format(number)
}

const percent = value => `${Math.round(Number(value || 0))}%`

const formatReset = epoch => {
  if (!epoch) return 'reset unknown'
  const date = new Date(Number(epoch) * 1000)
  const delta = date.getTime() - Date.now()
  if (!Number.isFinite(delta)) return 'reset unknown'
  if (delta <= 0) return 'resets now'
  const minutes = Math.ceil(delta / 60000)
  if (minutes < 60) return `resets in ${minutes}m`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  if (hours < 24) return `resets in ${hours}h ${remainingMinutes}m`
  const days = Math.floor(hours / 24)
  return `resets in ${days}d ${hours % 24}h`
}

const modelKey = value => String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '')

const currentQuotaLimit = (quota, model) => {
  const limits = quota?.limits || []
  const wanted = modelKey(model)
  const exact = limits.find(limit => {
    if (limit.kind !== 'model') return false
    const group = modelKey(limit.group)
    return group && wanted && (group.includes(wanted) || wanted.includes(group))
  })
  return exact || limits.find(limit => limit.kind === 'account') || limits[0] || null
}

function useLiveUsage() {
  const sessionId = useValue(host.state.activeSessionId)
  return useQuery({
    queryKey: [ID, 'live', sessionId],
    queryFn: () => host.request('session.usage', { session_id: sessionId }),
    enabled: Boolean(sessionId),
    refetchInterval: 2500,
    staleTime: 1000,
    retry: 1
  })
}

function useQuota(ctx) {
  return useQuery({
    queryKey: [ID, 'quota'],
    queryFn: () => ctx.rest('/quota', { timeoutMs: 20000 }),
    refetchInterval: 60000,
    staleTime: 30000,
    retry: 1
  })
}

function useModels(ctx) {
  return useQuery({
    queryKey: [ID, 'models'],
    queryFn: () => ctx.rest('/models', { timeoutMs: 10000 }),
    refetchInterval: 5000,
    staleTime: 2500,
    retry: 1
  })
}

function Progress({ used, danger = false }) {
  const safe = Math.max(0, Math.min(100, Number(used || 0)))
  const color = danger || safe >= 90 ? 'var(--ui-red)' : safe >= 70 ? 'var(--ui-orange)' : 'var(--ui-accent)'
  return jsx('div', {
    className: 'h-1.5 w-full overflow-hidden rounded-full',
    style: { background: 'var(--ui-stroke-secondary)' },
    children: jsx('div', {
      className: 'h-full rounded-full transition-[width] duration-300',
      style: { background: color, width: `${safe}%` }
    })
  })
}

function SectionTitle({ children, detail }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-2',
    children: [
      jsx('div', { className: 'text-xs font-semibold uppercase tracking-wide text-(--ui-text-secondary)', children }),
      detail ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: detail }) : null
    ]
  })
}

function QuotaRow({ limit }) {
  const danger = Boolean(limit.limit_reached) || Number(limit.used_percent || 0) >= 90
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-xs font-medium', children: limit.group }),
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: limit.label })
            ]
          }),
          jsx('div', {
            className: 'shrink-0 text-xs font-semibold tabular-nums',
            style: { color: danger ? 'var(--ui-red)' : 'var(--ui-text-primary)' },
            children: `${percent(limit.remaining_percent)} left`
          })
        ]
      }),
      jsx(Progress, { used: limit.used_percent, danger }),
      jsxs('div', {
        className: 'flex justify-between text-[0.625rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { children: `${percent(limit.used_percent)} used` }),
          jsx('span', { children: formatReset(limit.reset_at) })
        ]
      })
    ]
  })
}

function Metric({ label, value }) {
  return jsxs('div', {
    className: 'min-w-0 rounded-md border border-(--ui-stroke-secondary) px-2 py-1.5',
    children: [
      jsx('div', { className: 'truncate text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: label }),
      jsx('div', { className: 'truncate text-sm font-semibold tabular-nums', children: value })
    ]
  })
}

function ModelRow({ item }) {
  const provider = item.providers?.length ? item.providers.join(', ') : 'provider unknown'
  const tasks = (item.tasks || []).filter(task => task.task !== 'main').slice(0, 2)
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 border-b border-(--ui-stroke-secondary) py-2.5 last:border-b-0',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-xs font-medium', children: item.model }),
              jsx('div', { className: 'truncate text-[0.625rem] text-(--ui-text-quaternary)', children: provider })
            ]
          }),
          jsx('div', { className: 'shrink-0 text-xs font-semibold tabular-nums', children: compact(item.total_tokens) })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-x-3 gap-y-0.5 text-[0.625rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: `in ${compact(item.input_tokens)}` }),
          jsx('span', { children: `out ${compact(item.output_tokens)}` }),
          jsx('span', { children: `cache ${compact(item.cache_tokens)}` }),
          jsx('span', { children: `${compact(item.api_calls)} calls` })
        ]
      }),
      tasks.length
        ? jsx('div', {
            className: 'text-[0.625rem] text-(--ui-text-quaternary)',
            children: tasks.map(task => `${task.task} ${compact(task.tokens)}`).join(' · ')
          })
        : null
    ]
  })
}

function UsagePane({ ctx }) {
  const model = useValue(host.state.model)
  const gateway = useValue(host.state.gateway)
  const live = useLiveUsage()
  const quota = useQuota(ctx)
  const models = useModels(ctx)
  const liveData = live.data || {}
  const quotaData = quota.data || {}
  const modelData = models.data || {}
  const limits = quotaData.limits || []
  const accountLimits = limits.filter(limit => limit.kind === 'account')
  const modelLimits = limits.filter(limit => limit.kind === 'model')
  const contextKnown = Number(liveData.context_max || 0) > 0

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2.5',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-sm font-semibold', children: 'Codex Usage' }),
              jsx('div', {
                className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)',
                children: `${model || liveData.model || 'No active model'} · ${quotaData.plan || 'plan unavailable'}`
              })
            ]
          }),
          jsx('div', {
            className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
            children: gateway === 'open' ? 'live' : gateway
          })
        ]
      }),
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto p-3',
        children: jsxs('div', {
          className: 'flex flex-col gap-4',
          children: [
            jsxs('section', {
              className: 'flex flex-col gap-2',
              children: [
                jsx(SectionTitle, {
                  detail: quotaData.reset_credits ? `${quotaData.reset_credits} reset banked` : null,
                  children: 'Account quota'
                }),
                quota.isLoading
                  ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Checking Codex quota…' })
                  : accountLimits.length
                    ? accountLimits.map(limit => jsx(QuotaRow, { key: limit.id, limit }))
                    : jsx('div', {
                        className: 'rounded-md border border-(--ui-stroke-secondary) p-2.5 text-xs text-(--ui-text-tertiary)',
                        children: quotaData.error || 'No account quota window was returned.'
                      })
              ]
            }),
            modelLimits.length
              ? jsxs('section', {
                  className: 'flex flex-col gap-2',
                  children: [
                    jsx(SectionTitle, { children: 'Model limits' }),
                    modelLimits.map(limit => jsx(QuotaRow, { key: limit.id, limit }))
                  ]
                })
              : null,
            jsxs('section', {
              className: 'flex flex-col gap-2',
              children: [
                jsx(SectionTitle, { detail: liveData.calls ? `${compact(liveData.calls)} API calls` : null, children: 'Current session' }),
                contextKnown
                  ? jsxs('div', {
                      className: 'flex flex-col gap-1.5',
                      children: [
                        jsxs('div', {
                          className: 'flex justify-between text-xs',
                          children: [
                            jsx('span', { children: 'Context occupancy' }),
                            jsx('span', {
                              className: 'font-semibold tabular-nums',
                              children: `${percent(liveData.context_percent)} · ${compact(liveData.context_used)}/${compact(liveData.context_max)}`
                            })
                          ]
                        }),
                        jsx(Progress, { used: liveData.context_percent })
                      ]
                    })
                  : jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'Context occupancy appears after the first model call.' }),
                jsx('div', {
                  className: 'grid grid-cols-2 gap-1.5',
                  children: [
                    jsx(Metric, { key: 'total', label: 'Session spend', value: compact(liveData.total) }),
                    jsx(Metric, { key: 'calls', label: 'API calls', value: compact(liveData.calls) }),
                    jsx(Metric, { key: 'input', label: 'Input', value: compact(liveData.input) }),
                    jsx(Metric, { key: 'output', label: 'Output', value: compact(liveData.output) })
                  ]
                })
              ]
            }),
            jsxs('section', {
              className: 'flex flex-col gap-1',
              children: [
                jsx(SectionTitle, {
                  detail: modelData.overall ? `${compact(modelData.overall.total_tokens)} tracked` : null,
                  children: 'Models · all tracked'
                }),
                models.isLoading
                  ? jsx('div', { className: 'py-2 text-xs text-(--ui-text-tertiary)', children: 'Loading model telemetry…' })
                  : modelData.models?.length
                    ? modelData.models.slice(0, 8).map(item => jsx(ModelRow, { key: item.model, item }))
                    : jsx('div', { className: 'py-2 text-xs text-(--ui-text-tertiary)', children: modelData.error || 'No token telemetry yet.' })
              ]
            }),
            jsx('div', {
              className: 'border-t border-(--ui-stroke-secondary) pt-2 text-[0.625rem] leading-relaxed text-(--ui-text-quaternary)',
              children: 'Quota % is read from ChatGPT. Session spend is cumulative API accounting; context occupancy is the live prompt window. Cached tokens are included only in the all-tracked model totals.'
            })
          ]
        })
      })
    ]
  })
}

function UsageChip({ ctx }) {
  const model = useValue(host.state.model)
  const live = useLiveUsage()
  const quota = useQuota(ctx)
  const limit = currentQuotaLimit(quota.data, model)
  const context = live.data?.context_percent
  const parts = []
  if (limit) parts.push(`${percent(limit.remaining_percent)} left`)
  if (Number.isFinite(Number(context))) parts.push(`ctx ${percent(context)}`)
  if (!parts.length && live.data?.total) parts.push(`${compact(live.data.total)} tok`)
  const label = parts.length ? `Codex ${parts.join(' · ')}` : 'Codex usage'

  return jsx(Tip, {
    label: 'Live Codex quota and token usage',
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      type: 'button',
      onClick: () => {
        haptic('tap')
        host.notify({ kind: 'info', message: 'Live details are in the Codex Usage pane.' })
      },
      children: label
    })
  })
}

export default {
  id: ID,
  name: 'Codex Usage',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'codex usage',
      data: { placement: 'right', width: '330px' },
      render: () => jsx(UsagePane, { ctx })
    })

    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 115,
      render: () => jsx(UsageChip, { ctx })
    })
  }
}
