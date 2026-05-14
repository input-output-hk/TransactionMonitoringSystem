import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/lib/auth'

export function SignUpPage() {
  const navigate = useNavigate()
  const { signUp } = useAuth()
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!fullName.trim() || !email.trim()) return
    setSubmitting(true)
    try {
      await signUp({ fullName: fullName.trim(), email: email.trim() })
      navigate('/verify-email')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-[460px] rounded-2xl border border-border bg-card p-10 shadow-sm">
        <h1 className="mb-8 text-center text-4xl font-extrabold tracking-tight text-foreground">
          TMS
        </h1>

        <form onSubmit={onSubmit} className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <Label htmlFor="fullName">Full Name</Label>
            <Input
              id="fullName"
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Abcdefg Cdehedk"
              required
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user1234@email.com"
              required
            />
          </div>

          <div className="mt-6 flex justify-center">
            <Button
              type="submit"
              variant="outline"
              disabled={submitting}
              className="min-w-[140px]"
            >
              {submitting ? 'Signing Up…' : 'Sign Up'}
            </Button>
          </div>
        </form>
      </div>
    </div>
  )
}
