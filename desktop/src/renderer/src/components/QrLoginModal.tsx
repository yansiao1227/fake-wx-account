import React, { useEffect, useRef, useState } from 'react'
import { Loader2, Check, RotateCcw } from 'lucide-react'
import { t } from '../i18n'
import apiClient from '../api/client'
import { Modal } from '../pages/settings/primitives'

type Provider = 'weixin'
type Phase = 'loading' | 'waiting' | 'scanned' | 'success' | 'error'

interface QrLoginModalProps {
  provider: Provider
  onClose: () => void
  // Fired once the channel is connected so the page can refresh.
  onConnected: () => void
}

const POLL_INTERVAL = 2000

// Weixin QR-login modal: fetch a QR, poll status, then connect on success.
const QrLoginModal: React.FC<QrLoginModalProps> = ({ provider, onClose, onConnected }) => {
  const [phase, setPhase] = useState<Phase>('loading')
  const [qr, setQr] = useState('')
  const [errMsg, setErrMsg] = useState('')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const aliveRef = useRef(true)

  const stopPoll = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  const fail = (msg: string) => {
    if (!aliveRef.current) return
    setPhase('error')
    setErrMsg(msg)
  }

  // ---- WeChat: GET qr, POST poll {scaned|confirmed|expired} -----------------
  const pollWeixin = () => {
    timerRef.current = setTimeout(async () => {
      if (!aliveRef.current) return
      try {
        const data = await apiClient.weixinQrAction('poll')
        if (!aliveRef.current) return
        if (data.status !== 'success') return pollWeixin()
        const s = data.qr_status as string
        if (s === 'confirmed') {
          setPhase('success')
          await apiClient.channelAction('connect', 'weixin', {})
          if (aliveRef.current) onConnected()
        } else if (s === 'expired' && (data.qr_image || data.qrcode_url)) {
          setQr((data.qr_image as string) || (data.qrcode_url as string))
          setPhase('waiting')
          pollWeixin()
        } else if (s === 'scaned') {
          setPhase('scanned')
          pollWeixin()
        } else {
          pollWeixin()
        }
      } catch {
        pollWeixin()
      }
    }, POLL_INTERVAL)
  }

  const startWeixin = async () => {
    setPhase('loading')
    try {
      const data = await apiClient.getWeixinQr()
      if (!aliveRef.current) return
      if (data.status !== 'success') return fail(data.message || t('weixin_scan_fail'))
      setQr(data.qr_image || data.qrcode_url || '')
      setPhase('waiting')
      pollWeixin()
    } catch {
      fail(t('weixin_scan_fail'))
    }
  }

  const start = () => {
    stopPoll()
    void startWeixin()
  }

  useEffect(() => {
    aliveRef.current = true
    start()
    return () => {
      aliveRef.current = false
      stopPoll()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider])

  const title = t('weixin_scan_title')
  const desc = t('weixin_scan_desc')
  const tip = t('weixin_qr_tip')

  const statusText = (): string => {
    if (phase === 'scanned') return t('weixin_scan_scanned')
    return t('weixin_scan_waiting')
  }

  return (
    <Modal open title={title} onClose={onClose}>
      <div className="flex flex-col items-center py-2">
        {phase === 'loading' && (
          <div className="flex items-center text-content-tertiary py-10">
            <Loader2 size={18} className="animate-spin mr-2" />
            {t('weixin_scan_loading')}
          </div>
        )}

        {(phase === 'waiting' || phase === 'scanned') && (
          <>
            <p className="text-sm text-content-secondary mb-4 text-center">{desc}</p>
            <div className="bg-white p-3 rounded-card border border-subtle mb-3">
              {qr ? (
                <img src={qr} alt="QR" className="w-48 h-48" style={{ imageRendering: 'pixelated' }} />
              ) : (
                <div className="w-48 h-48 flex items-center justify-center text-content-tertiary text-xs">QR</div>
              )}
            </div>
            <p className={`text-xs mb-1 ${phase === 'scanned' ? 'text-accent' : 'text-warning'}`}>{statusText()}</p>
            <p className="text-xs text-content-tertiary">{tip}</p>
          </>
        )}

        {phase === 'success' && (
          <div className="flex flex-col items-center py-8">
            <div className="w-12 h-12 rounded-full bg-accent-soft flex items-center justify-center mb-3">
              <Check size={22} className="text-accent" />
            </div>
            <p className="text-sm font-medium text-accent">{t('weixin_scan_success')}</p>
          </div>
        )}

        {phase === 'error' && (
          <div className="flex flex-col items-center py-8">
            <p className="text-sm text-danger text-center mb-3">{errMsg}</p>
            <button
              onClick={start}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn border border-strong text-sm text-content-secondary hover:bg-inset cursor-pointer transition-colors"
            >
              <RotateCcw size={13} />
              {t('status_retry')}
            </button>
          </div>
        )}
      </div>
    </Modal>
  )
}

export default QrLoginModal
