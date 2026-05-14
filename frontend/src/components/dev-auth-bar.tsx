import { LogIn, LogOut } from 'lucide-react'
import { useAuth, useAuthStore } from '@/lib/auth'

export function DevAuthBar() {
  if (!import.meta.env.DEV) return null

  const { isAuthenticated } = useAuth()

  const skipAuth = () => {
    useAuthStore.setState({
      user: {
        fullName: 'Dev User',
        email: 'dev@example.com',
        role: 'Admin',
      },
      verified: true,
    })
    window.location.href = '/dashboard'
  }

  const devLogout = () => {
    useAuthStore.getState().logout()
    window.location.href = '/signup'
  }

  return (
    <div className="fixed bottom-3 right-3 z-[60] flex items-center gap-2 rounded-full border border-border bg-card/90 px-2 py-1 text-xs text-muted-foreground shadow-lg backdrop-blur">
      <span className="rounded-sm bg-brand/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-brand">
        Dev
      </span>
      {isAuthenticated ? (
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
