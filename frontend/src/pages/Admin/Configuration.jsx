// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { configApi } from '../../services/api'
import {
  Settings, Save, Loader, CheckCircle, AlertCircle, Eye, EyeOff,
  Wifi, WifiOff, Code2, GitBranch, Mail, Server, Bot, Video, Route,
} from 'lucide-react'

const CATEGORY_META = {
  ai:      { label: 'AI & Embeddings',  icon: Bot,       desc: 'LLM provider and embedding settings' },
  routing: { label: 'LLM Routing',      icon: Route,     desc: 'Per-purpose model overrides (used when USE_LLM_ROUTING is enabled)' },
  video:   { label: 'Video Generation', icon: Video,     desc: 'Promo / explainer video provider, models, and keys' },
  gitlab:  { label: 'GitLab',           icon: GitBranch, desc: 'GitLab server credentials and default repository' },
  email:   { label: 'Email / SMTP',     icon: Mail,      desc: 'SMTP settings for email notifications' },
  jenkins: { label: 'Jenkins CI',       icon: Server,    desc: 'Jenkins build server configuration' },
  uat:     { label: 'UAT Server',       icon: Server,    desc: 'UAT deployment and health check settings' },
}

export default function Configuration() {
  const qc = useQueryClient()
  const [edits, setEdits] = useState({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [revealed, setRevealed] = useState({})
  const [testResults, setTestResults] = useState({})
  const [testing, setTesting] = useState({})

  const { data: configData, isLoading } = useQuery({
    queryKey: ['app-config'],
    queryFn: () => configApi.getAll().then(r => r.data),
  })

  // Reset edits when data loads
  useEffect(() => {
    if (configData) {
      const initial = {}
      Object.values(configData).flat().forEach(c => {
        initial[c.key] = c.value || ''
      })
      setEdits(initial)
    }
  }, [configData])

  const hasChanges = () => {
    if (!configData) return false
    const original = {}
    Object.values(configData).flat().forEach(c => { original[c.key] = c.value || '' })
    return Object.keys(edits).some(k => edits[k] !== original[k])
  }

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      await configApi.update({ configs: edits })
      setSaved(true)
      qc.invalidateQueries({ queryKey: ['app-config'] })
      setTimeout(() => setSaved(false), 3000)
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (type) => {
    setTesting(prev => ({ ...prev, [type]: true }))
    setTestResults(prev => ({ ...prev, [type]: null }))
    try {
      const fn = type === 'gitlab' ? configApi.testGitlab : configApi.testOllama
      const res = await fn()
      setTestResults(prev => ({ ...prev, [type]: res.data }))
    } catch (e) {
      setTestResults(prev => ({ ...prev, [type]: { status: 'error', message: e.message } }))
    } finally {
      setTesting(prev => ({ ...prev, [type]: false }))
    }
  }

  if (isLoading) return (
    <div style={{ padding: '32px', fontSize: '13px', color: 'var(--text-muted)' }}>Loading configuration...</div>
  )

  return (
    <div style={{ padding: '32px 40px', maxWidth: 1100, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '28px' }}>
        <div>
          <h1 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: '600', color: 'var(--text-primary)' }}>
            Platform Configuration
          </h1>
          <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)' }}>
            Manage API keys, credentials, and integration settings
          </p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving || !hasChanges()}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px',
            padding: '8px 18px', background: hasChanges() ? 'var(--accent)' : 'var(--bg-elevated)',
            color: hasChanges() ? 'white' : 'var(--text-muted)',
            border: hasChanges() ? 'none' : '1px solid var(--border)',
            borderRadius: '6px', fontSize: '13px', fontWeight: '600',
            cursor: (saving || !hasChanges()) ? 'not-allowed' : 'pointer',
            opacity: (saving || !hasChanges()) ? 0.6 : 1,
          }}
        >
          {saving ? <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <Save size={14} />}
          Save Changes
        </button>
      </div>

      {/* Status messages */}
      {saved && (
        <div style={{
          padding: '10px 16px', borderRadius: '8px', marginBottom: '20px',
          background: 'rgba(76,175,125,0.08)', border: '1px solid rgba(76,175,125,0.25)',
          display: 'flex', alignItems: 'center', gap: '8px',
        }}>
          <CheckCircle size={14} style={{ color: 'var(--success)' }} />
          <span style={{ fontSize: '13px', color: 'var(--success)', fontWeight: '500' }}>Configuration saved successfully</span>
        </div>
      )}
      {error && (
        <div style={{
          padding: '10px 16px', borderRadius: '8px', marginBottom: '20px',
          background: 'rgba(224,108,108,0.08)', border: '1px solid rgba(224,108,108,0.25)',
          display: 'flex', alignItems: 'center', gap: '8px',
        }}>
          <AlertCircle size={14} style={{ color: 'var(--danger)' }} />
          <span style={{ fontSize: '13px', color: 'var(--danger)' }}>{error}</span>
        </div>
      )}

      {/* Config sections */}
      {configData && Object.entries(configData).map(([category, fields]) => {
        const meta = CATEGORY_META[category] || { label: category, icon: Settings, desc: '' }
        const Icon = meta.icon

        return (
          <div key={category} style={{
            marginBottom: '24px', borderRadius: '8px',
            background: 'var(--bg-elevated)', border: '1px solid var(--border)', overflow: 'hidden',
          }}>
            {/* Section header */}
            <div style={{
              padding: '14px 20px', borderBottom: '1px solid var(--border-subtle)',
              display: 'flex', alignItems: 'center', gap: '10px',
            }}>
              <Icon size={16} style={{ color: 'var(--accent)' }} />
              <div style={{ flex: 1 }}>
                <p style={{ margin: '0 0 1px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                  {meta.label}
                </p>
                <p style={{ margin: 0, fontSize: '11px', color: 'var(--text-muted)' }}>{meta.desc}</p>
              </div>
              {/* Test connection buttons */}
              {category === 'gitlab' && (
                <button onClick={() => handleTest('gitlab')} disabled={testing.gitlab} style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '5px 12px', background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: '5px', fontSize: '11px', color: 'var(--text-muted)', cursor: testing.gitlab ? 'not-allowed' : 'pointer',
                }}>
                  {testing.gitlab ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Wifi size={11} />}
                  Test Connection
                </button>
              )}
              {category === 'ai' && (
                <button onClick={() => handleTest('ollama')} disabled={testing.ollama} style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '5px 12px', background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: '5px', fontSize: '11px', color: 'var(--text-muted)', cursor: testing.ollama ? 'not-allowed' : 'pointer',
                }}>
                  {testing.ollama ? <Loader size={11} style={{ animation: 'spin 1s linear infinite' }} /> : <Wifi size={11} />}
                  Test Ollama
                </button>
              )}
            </div>

            {/* Test result */}
            {(testResults.gitlab && category === 'gitlab') && (
              <div style={{
                padding: '8px 20px', borderBottom: '1px solid var(--border-subtle)',
                background: testResults.gitlab.status === 'ok' ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
                display: 'flex', alignItems: 'center', gap: '8px',
              }}>
                {testResults.gitlab.status === 'ok'
                  ? <CheckCircle size={13} style={{ color: 'var(--success)' }} />
                  : <AlertCircle size={13} style={{ color: 'var(--danger)' }} />}
                <span style={{ fontSize: '12px', color: testResults.gitlab.status === 'ok' ? 'var(--success)' : 'var(--danger)' }}>
                  {testResults.gitlab.message}
                </span>
              </div>
            )}
            {(testResults.ollama && category === 'ai') && (
              <div style={{
                padding: '8px 20px', borderBottom: '1px solid var(--border-subtle)',
                background: testResults.ollama.status === 'ok' ? 'rgba(76,175,125,0.06)' : 'rgba(224,108,108,0.06)',
                display: 'flex', alignItems: 'center', gap: '8px',
              }}>
                {testResults.ollama.status === 'ok'
                  ? <CheckCircle size={13} style={{ color: 'var(--success)' }} />
                  : <AlertCircle size={13} style={{ color: 'var(--danger)' }} />}
                <span style={{ fontSize: '12px', color: testResults.ollama.status === 'ok' ? 'var(--success)' : 'var(--danger)' }}>
                  {testResults.ollama.message}
                </span>
              </div>
            )}

            {/* Fields */}
            <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '14px' }}>
              {fields.map(field => (
                <div key={field.key}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                    <label style={{ fontSize: '12px', fontWeight: '500', color: 'var(--text-secondary)' }}>
                      {field.label}
                    </label>
                    {field.has_value && (
                      <span style={{
                        fontSize: '9px', padding: '1px 6px', borderRadius: '3px',
                        background: 'rgba(76,175,125,0.1)', color: 'var(--success)',
                        border: '1px solid rgba(76,175,125,0.2)', fontWeight: '600',
                      }}>SET</span>
                    )}
                    {field.source === 'database' && (
                      <span style={{
                        fontSize: '9px', padding: '1px 6px', borderRadius: '3px',
                        background: 'rgba(218,119,86,0.1)', color: 'var(--accent)',
                        border: '1px solid rgba(218,119,86,0.2)', fontWeight: '600',
                      }}>DB</span>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <input
                      type={field.is_secret && !revealed[field.key] ? 'password' : 'text'}
                      value={edits[field.key] || ''}
                      onChange={e => setEdits(prev => ({ ...prev, [field.key]: e.target.value }))}
                      placeholder={field.placeholder}
                      style={{
                        flex: 1, padding: '8px 12px', borderRadius: '6px',
                        border: '1px solid var(--border)', background: 'var(--bg-input)',
                        color: 'var(--text-primary)', fontSize: '13px', fontFamily: field.is_secret ? 'inherit' : 'monospace',
                      }}
                    />
                    {field.is_secret && (
                      <button
                        onClick={() => setRevealed(prev => ({ ...prev, [field.key]: !prev[field.key] }))}
                        style={{
                          padding: '8px 10px', background: 'transparent', border: '1px solid var(--border)',
                          borderRadius: '6px', color: 'var(--text-muted)', cursor: 'pointer',
                          display: 'flex', alignItems: 'center',
                        }}
                      >
                        {revealed[field.key] ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    )}
                  </div>
                  {field.description && (
                    <p style={{ margin: '3px 0 0', fontSize: '11px', color: 'var(--text-muted)' }}>
                      {field.description}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
