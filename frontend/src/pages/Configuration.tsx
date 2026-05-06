import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, AlertCircle, Copy, ExternalLink, Key, Server, Bell, Activity } from 'lucide-react'
import { useTmsStore } from '../store'
import { fetchHealth } from '../lib/api'
import styles from './Configuration.module.css'

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span className={styles.statusDot} style={{ background: ok ? 'var(--risk-low)' : 'var(--risk-none)' }} />
  )
}

function Field({
  label, type = 'text', value, onChange, placeholder, mono = false,
}: {
  label: string; type?: string; value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean
}) {
  return (
    <div className={styles.field}>
      <label className={styles.fieldLabel}>{label}</label>
      <input
        className={`${styles.fieldInput} ${mono ? styles.mono : ''}`}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </div>
  )
}

export default function Configuration() {
  const {
    apiKey, setApiKey,
    apiBaseUrl, setApiBaseUrl,
    webhookUrl, setWebhookUrl,
    emailAlert, setEmailAlert,
    grafanaUrl, setGrafanaUrl,
    network, setNetwork,
  } = useTmsStore()

  const [localKey, setLocalKey] = useState(apiKey)
  const [localUrl, setLocalUrl] = useState(apiBaseUrl)
  const [localWebhook, setLocalWebhook] = useState(webhookUrl)
  const [localEmail, setLocalEmail] = useState(emailAlert)
  const [localGrafana, setLocalGrafana] = useState(grafanaUrl)
  const [keySaved, setKeySaved] = useState(false)
  const [showKey, setShowKey] = useState(false)

  const { data: health, isError: healthError } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  })

  function saveConnection() {
    setApiKey(localKey)
    setApiBaseUrl(localUrl)
    setKeySaved(true)
    setTimeout(() => setKeySaved(false), 2000)
  }

  function saveAlerting() {
    setWebhookUrl(localWebhook)
    setEmailAlert(localEmail)
    setGrafanaUrl(localGrafana)
    setKeySaved(true)
    setTimeout(() => setKeySaved(false), 2000)
  }

  const isOnline = !healthError && health?.pipeline_state === 'running'
  const ogmiosOk = health?.ogmios_connected ?? false

  return (
    <div className={styles.root}>
      {/* System status banner */}
      <div className={styles.statusBanner} data-online={isOnline}>
        <span className={styles.bannerIcon}>
          {isOnline ? <Check size={14} /> : <AlertCircle size={14} />}
        </span>
        <span className={styles.bannerText}>
          {health?.status === 'demo'
            ? 'Running in demo mode — no backend connected'
            : isOnline
              ? `Pipeline online · ${health?.ws_connections ?? 0} WebSocket connection${health?.ws_connections === 1 ? '' : 's'}`
              : 'Backend unreachable — check connection settings'}
        </span>
        {health && health.status !== 'demo' && (
          <span className={styles.bannerMeta}>
            Slot {health.last_processed_slot?.toLocaleString() ?? '—'} ·
            Uptime {Math.floor((health.uptime_seconds ?? 0) / 60)}m
          </span>
        )}
      </div>

      <div className={styles.grid}>
        {/* Connection */}
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <Server size={15} />
            <h2 className={styles.cardTitle}>Connection</h2>
          </div>

          <Field
            label="API Base URL"
            value={localUrl}
            onChange={setLocalUrl}
            placeholder="http://localhost:8000 (empty = same host)"
            mono
          />

          <div className={styles.field}>
            <label className={styles.fieldLabel}>API Key</label>
            <div className={styles.keyRow}>
              <input
                className={`${styles.fieldInput} ${styles.mono} ${styles.keyInput}`}
                type={showKey ? 'text' : 'password'}
                value={localKey}
                onChange={(e) => setLocalKey(e.target.value)}
                placeholder="Enter TMS-API-Key…"
              />
              <button className={styles.iconBtn} onClick={() => setShowKey((v) => !v)}>
                <Key size={13} />
              </button>
              <button className={styles.iconBtn} onClick={() => navigator.clipboard.writeText(localKey)}>
                <Copy size={13} />
              </button>
            </div>
          </div>

          <div className={styles.field}>
            <label className={styles.fieldLabel}>Network</label>
            <div className={styles.networkGroup}>
              {(['mainnet', 'preprod', 'preview'] as const).map((n) => (
                <button
                  key={n}
                  className={`${styles.networkBtn} ${network === n ? styles.networkBtnActive : ''}`}
                  onClick={() => setNetwork(n)}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>

          <div className={styles.statusRow}>
            <StatusDot ok={!healthError} />
            <span className={styles.statusLabel}>API endpoint</span>
            <StatusDot ok={ogmiosOk} />
            <span className={styles.statusLabel}>Ogmios WebSocket</span>
          </div>

          <button
            className={styles.saveBtn}
            onClick={saveConnection}
          >
            {keySaved ? <><Check size={13} /> Saved</> : 'Save Connection'}
          </button>
        </section>

        {/* Alerting */}
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <Bell size={15} />
            <h2 className={styles.cardTitle}>Alerting Preferences</h2>
          </div>

          <Field
            label="Webhook URL"
            value={localWebhook}
            onChange={setLocalWebhook}
            placeholder="https://hooks.example.com/tms-alerts"
            mono
          />
          <p className={styles.helpText}>
            Receives POST requests with alert payload on Critical/High events.
          </p>

          <Field
            label="Email Alert Recipient"
            type="email"
            value={localEmail}
            onChange={setLocalEmail}
            placeholder="ops@yourteam.com"
          />

          <button className={styles.saveBtn} onClick={saveAlerting}>
            Save Alerting
          </button>
        </section>

        {/* Integrations */}
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <Activity size={15} />
            <h2 className={styles.cardTitle}>Monitoring Integrations</h2>
          </div>

          <Field
            label="Grafana Dashboard URL"
            value={localGrafana}
            onChange={setLocalGrafana}
            placeholder="https://grafana.yourteam.com/d/tms"
            mono
          />

          <div className={styles.integrationGrid}>
            <div className={styles.integration}>
              <div className={styles.integrationHeader}>
                <span className={styles.integrationName}>Prometheus</span>
                <span className={styles.integrationTag}>Metrics</span>
              </div>
              <p className={styles.integrationDesc}>
                Expose TMS metrics at <code>/metrics</code> for scraping.
              </p>
              <code className={styles.integrationCode}>
                GET {apiBaseUrl || 'http://localhost:8000'}/metrics
              </code>
            </div>

            <div className={styles.integration}>
              <div className={styles.integrationHeader}>
                <span className={styles.integrationName}>Grafana</span>
                <span className={styles.integrationTag}>Dashboard</span>
              </div>
              <p className={styles.integrationDesc}>
                Import the TMS Grafana dashboard for pre-built panels.
              </p>
              {localGrafana && (
                <a href={localGrafana} target="_blank" rel="noopener noreferrer" className={styles.integrationLink}>
                  <ExternalLink size={11} /> Open Dashboard
                </a>
              )}
            </div>

            <div className={styles.integration}>
              <div className={styles.integrationHeader}>
                <span className={styles.integrationName}>WebSocket API</span>
                <span className={styles.integrationTag}>Real-time</span>
              </div>
              <p className={styles.integrationDesc}>
                Stream lifecycle events directly for automated responses.
              </p>
              <code className={styles.integrationCode}>
                ws://{window.location.host}/ws?api_key=YOUR_KEY
              </code>
            </div>

            <div className={styles.integration}>
              <div className={styles.integrationHeader}>
                <span className={styles.integrationName}>Health Probe</span>
                <span className={styles.integrationTag}>K8s / Docker</span>
              </div>
              <p className={styles.integrationDesc}>
                Liveness and readiness probes for container orchestration.
              </p>
              <code className={styles.integrationCode}>
                GET {apiBaseUrl || 'http://localhost:8000'}/health
              </code>
            </div>
          </div>

          <button className={styles.saveBtn} onClick={() => { setGrafanaUrl(localGrafana) }}>
            Save Integrations
          </button>
        </section>

        {/* API reference */}
        <section className={styles.card}>
          <div className={styles.cardHeader}>
            <Key size={15} />
            <h2 className={styles.cardTitle}>API Reference</h2>
          </div>
          <div className={styles.apiEndpoints}>
            {[
              ['GET', '/api/transactions', 'List transactions'],
              ['GET', '/api/analysis/results', 'List scored alerts'],
              ['GET', '/api/analysis/stats', 'Score distribution stats'],
              ['GET', '/api/lifecycle/stats/summary', 'Lifecycle statistics'],
              ['GET', '/health/detail', 'System health status'],
              ['WS',  '/ws', 'Real-time lifecycle events'],
            ].map(([method, path, desc]) => (
              <div key={path} className={styles.endpoint}>
                <span className={styles.method} data-method={method}>{method}</span>
                <code className={styles.path}>{path}</code>
                <span className={styles.endpointDesc}>{desc}</span>
              </div>
            ))}
          </div>
          <p className={styles.helpText}>
            All endpoints require <code>TMS-API-Key</code> header or <code>?api_key=…</code> query param.
          </p>
        </section>
      </div>
    </div>
  )
}
