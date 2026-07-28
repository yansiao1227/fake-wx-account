import React, { useMemo, useRef, useCallback } from 'react'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import { t } from '../i18n'

/**
 * Markdown renderer aligned 1:1 with the web console (markdown-it + highlight.js
 * + GitHub themes). Using the same engine guarantees identical line-break,
 * linkify and code-highlight behavior across web and desktop.
 */

const md: MarkdownIt = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(str, { language: lang }).value
      } catch {
        /* fall through */
      }
    }
    try {
      return hljs.highlightAuto(str).value
    } catch {
      return ''
    }
  },
})

// Fix greedy linkify: markdown-it's linkify swallows markdown emphasis (`*`)
// and CJK full-width punctuation glued to a URL (common in LLM output like
// `**https://x**，中文`), turning the whole tail into one broken link. Cut the
// URL at the first such char and spill the remainder back as plain text.
const _GREEDY_LINK_CUT = /[*\u3000-\u303F\uFF00-\uFFEF]/
md.core.ruler.after('linkify', 'fix_greedy_linkify', (state) => {
  for (const blk of state.tokens) {
    if (blk.type !== 'inline' || !blk.children) continue
    const ch = blk.children
    for (let i = 0; i < ch.length; i++) {
      const open = ch[i]
      if (open.type !== 'link_open' || open.markup !== 'linkify') continue
      const textTok = ch[i + 1]
      const close = ch[i + 2]
      if (!textTok || textTok.type !== 'text' || !close || close.type !== 'link_close') continue
      const idx = textTok.content.search(_GREEDY_LINK_CUT)
      if (idx < 0) continue
      const keep = textTok.content.slice(0, idx)
      const spill = textTok.content.slice(idx)
      textTok.content = keep
      open.attrSet('href', keep)
      const spillTok = new state.Token('text', '', 0)
      spillTok.content = spill
      ch.splice(i + 3, 0, spillTok)
    }
  }
})

// Open links in a new tab safely.
const defaultLinkOpen =
  md.renderer.rules.link_open ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
  tokens[idx].attrPush(['target', '_blank'])
  tokens[idx].attrPush(['rel', 'noopener noreferrer'])
  return defaultLinkOpen(tokens, idx, options, env, self)
}

// Wrap fenced code blocks so we can render a header (lang + copy button).
const defaultFence =
  md.renderer.rules.fence ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options)
  }
md.renderer.rules.fence = function (tokens, idx, options, env, self) {
  const token = tokens[idx]
  const info = token.info ? token.info.trim().split(/\s+/)[0] : ''
  // Ensure the `hljs` class is present so the GitHub theme background/base
  // color applies (markdown-it only adds language-* by default).
  let rendered = defaultFence(tokens, idx, options, env, self)
  if (rendered.includes('<code class="')) {
    rendered = rendered.replace('<code class="', '<code class="hljs ')
  } else {
    rendered = rendered.replace('<code>', '<code class="hljs">')
  }
  return (
    `<div class="code-block-wrapper">` +
    `<div class="code-block-header">` +
    `<span class="code-block-lang">${info || 'text'}</span>` +
    `<button type="button" class="code-copy-btn" data-code-id="cb-${idx}" aria-label="Copy code">${t('msg_copy')}</button>` +
    `</div>` +
    rendered +
    `</div>`
  )
}

interface MarkdownProps {
  content: string
  /**
   * Intercept clicks on internal document links (relative `.md` hrefs). When
   * provided, such links open in-app instead of being handed to the OS. Used by
   * the knowledge viewer so index links open the target doc rather than firing
   * an "application cannot be opened (-120)" error in Electron.
   */
  onInternalLink?: (href: string) => void
}

const Markdown: React.FC<MarkdownProps> = ({ content, onInternalLink }) => {
  const rootRef = useRef<HTMLDivElement>(null)

  const html = useMemo(() => md.render(content || ''), [content])

  // Delegate clicks: copy buttons on code blocks, and internal doc links.
  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement

      // Internal knowledge links (relative *.md), when a handler is provided.
      if (onInternalLink) {
        const a = target.closest('a') as HTMLAnchorElement | null
        if (a) {
          const href = a.getAttribute('href') || ''
          if (href.endsWith('.md') && !/^https?:\/\//i.test(href)) {
            e.preventDefault()
            onInternalLink(href)
            return
          }
        }
      }

      const btn = target.closest('.code-copy-btn') as HTMLElement | null
      if (!btn) return
      const pre = btn.closest('.code-block-wrapper')?.querySelector('pre')
      if (!pre) return
      navigator.clipboard.writeText(pre.textContent || '')
      const original = btn.textContent
      btn.textContent = t('msg_copied')
      btn.classList.add('copied')
      setTimeout(() => {
        btn.textContent = original
        btn.classList.remove('copied')
      }, 1600)
    },
    [onInternalLink]
  )

  return (
    <div
      ref={rootRef}
      className="msg-content text-sm text-content leading-relaxed break-words"
      onClick={handleClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

export default Markdown
