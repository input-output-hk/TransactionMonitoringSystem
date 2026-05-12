import { useEffect, useState } from 'react'
import { ThemeContext, type Theme, type ThemeContextValue } from './theme-context'

const STORAGE_KEY = 'tms-theme'

function getSystemTheme(): Theme {
  if (typeof window === 'undefined' || !window.matchMedia) return 'dark'
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

function getStoredTheme(): Theme | null {
  if (typeof window === 'undefined') return null
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : null
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [override, setOverride] = useState<Theme | null>(() => getStoredTheme())
  const [systemTheme, setSystemTheme] = useState<Theme>(() => getSystemTheme())

  useEffect(() => {
    if (!window.matchMedia) return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) =>
      setSystemTheme(e.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const theme: Theme = override ?? systemTheme

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])

  useEffect(() => {
    if (override) window.localStorage.setItem(STORAGE_KEY, override)
    else window.localStorage.removeItem(STORAGE_KEY)
  }, [override])

  const value: ThemeContextValue = {
    theme,
    setTheme: (t) => setOverride(t),
    toggleTheme: () => setOverride(theme === 'dark' ? 'light' : 'dark'),
    isUserOverride: override !== null,
    resetToSystem: () => setOverride(null),
  }

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
