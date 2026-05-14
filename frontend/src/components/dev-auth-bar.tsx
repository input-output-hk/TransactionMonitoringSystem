import { LogIn, LogOut } from 'lucide-react'

const AUTH_KEY = 'tms-auth'

export function DevAuthBar() {
  if (!import.meta.env.DEV) return null

  const isAuthed = (() => {
    try {
      const raw = window.localStorage.getItem(AUTH_KEY)
      if (!raw) return false
      const parsed = JSON.parse(raw) as { verified?: boolean } | null
      return Boolean(parsed?.verified)
    } catch {
      return false
    }
  })()

  const skipAuth = () => {
    window.localStorage.setItem(
      AUTH_KEY,
      JSON.stringify({
        user: { fullName: 'Dev User', email: 'dev@example.com' },
        verified: true,
      })
    )
    window.location.href = '/dashboard'
  }

  const devLogout = () => {
    window.localStorage.removeItem(AUTH_KEY)
    window.location.href = '/signup'
  }

  return (
    <div className="fixed bottom-3 right-3 z-[60] flex items-center gap-2 rounded-full border border-border bg-card/90 px-2 py-1 text-xs text-muted-foreground shadow-lg backdrop-blur">
      <span className="rounded-sm bg-brand/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-brand">
        Dev
      </span>
      {isAuthed ? (
        <button
          type="button"
          onClick={devLogout}
          className="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-foreground hover:bg-accent"
        >
          <LogOut className="h-3.5 w-3.5" />
          Logout
        </button>
      ) : (
        <button
          type="button"
          onClick={skipAuth}
          className="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-foreground hover:bg-accent"
        >
          <LogIn className="h-3.5 w-3.5" />
          Skip auth
        </button>
      )}
    </div>
  )
}
