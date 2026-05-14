import { NavLink, useNavigate } from 'react-router-dom'
import { Archive, LogOut, Moon, Sun, Upload, User } from 'lucide-react'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useAuth } from '@/lib/auth'
import { useTheme } from '@/components/theme-context'
import { systemModules } from '@/mocks/attacks'
import { cn } from '@/lib/utils'

function initials(name: string | undefined) {
  if (!name) return 'U'
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join('')
}

export function TopNav() {
  const navigate = useNavigate()
  const { user, logout } = useAuth()
  const { theme, toggleTheme } = useTheme()
  const allOnline = systemModules.every((m) => m.online)

  return (
    <header className="border-b border-border bg-background">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center justify-between px-6">
        <nav className="flex items-center gap-1">
          <span className="mr-3 text-base font-extrabold tracking-tight text-foreground">
            TMS
          </span>
          <span className="mr-3 text-border">|</span>
          <NavItem to="/dashboard">Attacks</NavItem>
          <NavItem to="/reports">Reports</NavItem>
          {user?.role === 'Admin' && <NavItem to="/users">Users</NavItem>}
        </nav>

        <div className="flex items-center gap-5">
          <DropdownMenu>
            <DropdownMenuTrigger className="flex items-center gap-2 rounded-md px-2 py-1 text-sm font-medium text-foreground outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring">
              <span
                className={cn(
                  'h-2.5 w-2.5 rounded-full',
                  allOnline ? 'bg-status-online' : 'bg-status-warning'
                )}
              />
              System Status
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>Modules</DropdownMenuLabel>
              {systemModules.map((m) => (
                <DropdownMenuItem key={m.name} className="gap-3">
                  <span
                    className={cn(
                      'h-2.5 w-2.5 rounded-full',
                      m.online ? 'bg-status-online' : 'bg-status-offline'
                    )}
                  />
                  {m.name}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          <DropdownMenu>
            <DropdownMenuTrigger className="outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-full">
              <Avatar>
                <AvatarFallback>{initials(user?.fullName)}</AvatarFallback>
              </Avatar>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[14rem]">
              <DropdownMenuItem className="gap-3" disabled>
                <User className="h-4 w-4 text-brand" />
                <span className="font-medium text-foreground">
                  {user?.fullName ?? 'User'}
                </span>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={toggleTheme} className="gap-3">
                {theme === 'dark' ? (
                  <Moon className="h-4 w-4" />
                ) : (
                  <Sun className="h-4 w-4" />
                )}
                {theme === 'dark' ? 'Dark Mode On' : 'Light Mode On'}
              </DropdownMenuItem>
              <DropdownMenuItem className="gap-3">
                <Upload className="h-4 w-4" />
                Import Attack
              </DropdownMenuItem>
              <DropdownMenuItem
                className="gap-3"
                onSelect={() => navigate('/archive')}
              >
                <Archive className="h-4 w-4" />
                Archive
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() => {
                  logout()
                  navigate('/signup', { replace: true })
                }}
                className="gap-3 justify-end"
              >
                Log Out
                <LogOut className="h-4 w-4" />
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </header>
  )
}

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          'rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
          isActive
            ? 'text-foreground'
            : 'text-muted-foreground hover:text-foreground'
        )
      }
    >
      {children}
    </NavLink>
  )
}
