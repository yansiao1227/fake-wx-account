import React from 'react'
import { Download, RefreshCw, X, Loader2, AlertTriangle } from 'lucide-react'
import { t } from '../i18n'
import { useUpdateStore, hasAvailableUpdate } from '../store/updateStore'

// Compact update panel anchored to the NavRail footer. Shown whenever an update
// is available AND the panel is open (auto-opened on detection, re-openable via
// "check for update"). Dismissing (×) just closes it; the menu can re-open it.
const UpdateBanner: React.FC = () => {
  const state = useUpdateStore()
  const open = state.panelOpen

  const available = hasAvailableUpdate(state)
  const status = state.status
  const errored = status?.state === 'error'

  // Full-screen "installing…" overlay: bridges the otherwise blank window
  // between clicking "restart to install" and the app actually quitting to
  // swap the bundle. (The gap AFTER quit, before relaunch, is OS-level and
  // can't be covered.)
  if (state.installing) {
    return (
      <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-3 bg-base/90 backdrop-blur-sm">
        <Loader2 size={28} className="animate-spin text-accent" />
        <p className="text-sm text-content-secondary">{t('update_installing')}</p>
      </div>
    )
  }

  // Show the panel when it's open AND we either know of an update or hit an
  // error. Keeping it up on error is important: a failed download must surface
  // a visible message instead of silently doing nothing.
  if (!open || (!available && !errored)) return null

  const version = state.version
  const preparing = state.preparing
  const downloading = status?.state === 'downloading'
  const downloaded = status?.state === 'downloaded'
  // macOS emits a second progress pass (verify) after hitting 100%; show it as
  // an indeterminate "verifying" state rather than a bar restarting from 0.
  const verifying = downloading && state.progressPeaked
  const busy = preparing || downloading

  return (
    <div className="absolute bottom-2 left-2 right-2 z-40">
      <div className="rounded-lg border border-default bg-elevated shadow-lg p-3 space-y-2.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[13px] font-semibold text-content">
              {errored ? t('update_failed') : t('update_available')}
            </p>
            {!errored && version && (
              <p className="text-xs text-content-tertiary mt-0.5">v{version}</p>
            )}
          </div>
          <button
            onClick={() => state.dismiss()}
            className="text-content-tertiary hover:text-content cursor-pointer flex-shrink-0"
            title={t('update_later')}
          >
            <X size={15} />
          </button>
        </div>

          {errored && (
            <div className="space-y-2.5">
              <div className="flex items-start gap-2 text-xs text-content-secondary">
                <AlertTriangle size={13} className="text-amber-500 flex-shrink-0 mt-0.5" />
                <span className="break-words">
                  {status?.state === 'error' ? status.message : ''}
                </span>
              </div>
              <button
                onClick={() => state.download()}
                className="w-full inline-flex items-center justify-center gap-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover px-3 py-2 text-[13px] font-medium cursor-pointer transition-colors"
              >
                <RefreshCw size={15} />
                {t('update_retry')}
              </button>
            </div>
          )}

          {!errored && preparing && (
            <div className="flex items-center gap-2 text-xs text-content-secondary py-1">
              <Loader2 size={13} className="animate-spin" />
              <span>{t('update_preparing')}</span>
            </div>
          )}

          {!errored && downloading && verifying && (
            <div className="flex items-center gap-2 text-xs text-content-secondary py-1">
              <Loader2 size={13} className="animate-spin" />
              <span>{t('update_verifying')}</span>
            </div>
          )}

          {!errored && downloading && !verifying && (
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-xs text-content-secondary">
                <Loader2 size={13} className="animate-spin" />
                <span>{t('update_downloading')} {state.percent}%</span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-surface-2 overflow-hidden">
                <div className="h-full bg-accent transition-[width] duration-200" style={{ width: `${state.percent}%` }} />
              </div>
            </div>
          )}

          {!errored && !busy && !downloaded && (
            <button
              onClick={() => state.download()}
              className="w-full inline-flex items-center justify-center gap-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover px-3 py-2 text-[13px] font-medium cursor-pointer transition-colors"
            >
              <Download size={15} />
              {t('update_download')}
            </button>
          )}

          {!errored && downloaded && (
            <button
              onClick={() => state.install()}
              className="w-full inline-flex items-center justify-center gap-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover px-3 py-2 text-[13px] font-medium cursor-pointer transition-colors"
            >
              <RefreshCw size={15} />
              {t('update_restart')}
            </button>
          )}
        </div>
    </div>
  )
}

export default UpdateBanner
